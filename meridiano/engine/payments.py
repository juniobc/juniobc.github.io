"""Pagamentos com Mercado Pago. Regras de ouro:
 - só um pagamento que o PRÓPRIO Mercado Pago confirma por API (status approved, valor, moeda e pedido conferem) vira venda;
 - o aviso (webhook) é só uma dica: nunca confiamos no conteúdo dele, só no que a API devolve com o nosso token;
 - tudo é idempotente por payment_id (o mesmo aviso mil vezes = uma venda);
 - pagamentos de teste nunca entram no caixa; reembolso e chargeback revertem a venda;
 - nenhuma tool de agente chega aqui: o caixa só muda por pagamento real."""
import hashlib
import hmac
import json
import re
import secrets
import time

import httpx

from . import config, delivery, secrets_store

MP_API = "https://api.mercadopago.com"
TERMINAL_REFUND = ("refunded", "charged_back")


class PaymentFetchError(Exception):
    """Falha de rede ou resposta inesperada do Mercado Pago: o webhook responde erro e o Mercado Pago tenta de novo."""


def mp_request(method, path, token, json_body=None, idem=None):
    headers = {"Authorization": "Bearer " + token}
    if idem:
        headers["X-Idempotency-Key"] = idem
    try:
        r = httpx.request(method, MP_API + path, headers=headers, json=json_body, timeout=20)
        data = r.json() if r.content else {}
    except Exception as e:   # a mensagem de httpx não contém o token
        raise PaymentFetchError("sem resposta do Mercado Pago") from e
    return r.status_code, data


def brl_to_cents(v):
    return int(round(float(v) * 100))


# ---------- assinatura do webhook ----------

def parse_signature(header):
    parts = dict(p.strip().split("=", 1) for p in (header or "").split(",") if "=" in p)
    return parts.get("ts"), parts.get("v1")


def expected_signature(secret, data_id, request_id, ts):
    """Manifesto do Mercado Pago: id:<data.id>;request-id:<x-request-id>;ts:<ts>; (partes ausentes são omitidas)."""
    manifest = ""
    if data_id:
        manifest += f"id:{str(data_id).lower()};"
    if request_id:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"
    return hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()


def signature_ok(secret, data_id, request_id, header):
    ts, v1 = parse_signature(header)
    if not ts or not v1:
        return False
    return hmac.compare_digest(expected_signature(secret, data_id, request_id, ts), v1)


# ---------- pedido e preferência de pagamento ----------

def create_order(conn, product):
    """Cria o pedido com o preço de agora. None se há pedidos abertos demais (proteção do banco)."""
    abertos = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN ('criado','aguardando') AND created_at > now() - interval '10 minutes'").fetchone()["n"]
    if abertos >= config.MAX_OPEN_ORDERS:
        return None
    return conn.execute("INSERT INTO orders(token,product_id,amount_cents) VALUES (%s,%s,%s) RETURNING *",
                        (secrets.token_urlsafe(24), product["id"], product["price_cents"])).fetchone()


def create_preference(conn, order, product, request=None):
    """Pede ao Mercado Pago a página de pagamento. Devolve a URL para onde mandar o cliente, ou None."""
    request = request or mp_request   # resolvido na hora do uso (os testes trocam por respostas simuladas)
    try:
        token = secrets_store.read_token()
    except (OSError, ValueError):
        return None
    base = config.PUBLIC_BASE_URL
    body = {"items": [{"id": product["slug"], "title": product["name"][:250], "quantity": 1, "currency_id": "BRL", "unit_price": order["amount_cents"] / 100}],
            "external_reference": str(order["id"]), "notification_url": f"{base}/api/mp/webhook",
            "back_urls": {k: f"{base}/pub/loja/pedido/{order['token']}" for k in ("success", "failure", "pending")}, "auto_return": "approved"}
    try:
        code, data = request("POST", "/checkout/preferences", token, body, idem=order["token"])
    except PaymentFetchError:
        return None
    if code not in (200, 201):
        return None
    url = data.get("sandbox_init_point") if token.startswith("TEST-") else data.get("init_point")
    if not url or not str(url).startswith("https://"):
        return None
    conn.execute("UPDATE orders SET preference_id=%s, status='aguardando', test=%s, updated_at=now() WHERE id=%s", (str(data.get("id", ""))[:80], token.startswith("TEST-"), order["id"]))
    return url


# ---------- confirmação do pagamento ----------

def fetch_payment(payment_id, request=None):
    request = request or mp_request
    if not re.fullmatch(r"\d{3,20}", str(payment_id)):
        raise PaymentFetchError("id de pagamento inválido")
    try:
        token = secrets_store.read_token()
    except (OSError, ValueError):
        raise PaymentFetchError("token do Mercado Pago não configurado")
    code, data = request("GET", f"/v1/payments/{payment_id}", token)
    if code != 200 or not isinstance(data, dict) or str(data.get("id")) != str(payment_id):
        raise PaymentFetchError(f"resposta inesperada do Mercado Pago ({code})")
    return data


def _event(conn, pid, order_id, status, outcome, note=""):
    conn.execute("INSERT INTO payment_events(payment_id,order_id,mp_status,outcome,note) VALUES (%s,%s,%s,%s,%s)", (pid, order_id, status[:40], outcome, note[:300]))


def _ledger(conn, kind, cents, description, ref):
    """Lança no caixa uma única vez (índice único em external_ref). Devolve True se lançou agora."""
    if cents <= 0:
        return False
    cur = conn.execute("INSERT INTO ledger(kind,amount_cents,description,source,external_ref) VALUES (%s,%s,%s,'pagamento',%s) ON CONFLICT DO NOTHING", (kind, cents, description[:300], ref))
    return cur.rowcount == 1


def _fee_cents(p):
    fees = [d for d in (p.get("fee_details") or []) if isinstance(d, dict)]
    if not fees:
        return None
    return sum(brl_to_cents(d.get("amount", 0)) for d in fees if d.get("fee_payer", "collector") == "collector")


def process_payment(conn, payment_id, fetch=None):
    """Confirma o pagamento no Mercado Pago e aplica ao pedido. Seguro para repetir, para rodar em paralelo e para avisos fora de ordem.
    Devolve {"outcome": ..., "order_id": ...}. PaymentFetchError sobe para o webhook responder erro (e o MP tentar de novo)."""
    pid = str(payment_id)
    p = (fetch or fetch_payment)(pid)
    status = str(p.get("status", ""))
    try:
        order_id = int(str(p.get("external_reference", "")))
    except ValueError:
        order_id = None
    with conn.transaction():
        order = conn.execute("SELECT o.*, pr.name AS pname FROM orders o JOIN products pr ON pr.id=o.product_id WHERE o.id=%s FOR UPDATE OF o", (order_id,)).fetchone() if order_id else None
        if not order:
            _event(conn, pid, None, status, "sem_pedido", "external_reference não corresponde a nenhum pedido nosso")
            return {"outcome": "sem_pedido", "order_id": None}
        gross = brl_to_cents(p.get("transaction_amount", 0))
        refunded = brl_to_cents(p.get("transaction_amount_refunded", 0) or 0)
        test = p.get("live_mode") is False or bool(order["test"])
        desc = f"Venda #{order['id']} · {order['pname']} (pagamento {pid})"
        oid = order["id"]

        if status in ("approved", "refunded", "charged_back"):
            if gross != order["amount_cents"] or str(p.get("currency_id", "BRL")) != "BRL":
                _event(conn, pid, oid, status, "valor_divergente", f"pago {gross} centavos {p.get('currency_id')}; pedido {order['amount_cents']}")
                return {"outcome": "valor_divergente", "order_id": oid}
            paid_states = ("pago", "reembolsado", "chargeback")
            outcome = "ja_processado"
            # 1) a venda (se este pagamento ainda não foi aplicado; vale também quando o aviso de reembolso chega antes do de aprovação)
            if not (order["payment_id"] == pid and order["status"] in paid_states):
                duplicate = order["payment_id"] not in (None, pid) and order["status"] in paid_states
                fee = _fee_cents(p)
                net = gross - fee if fee is not None else gross
                if not duplicate:
                    conn.execute("UPDATE orders SET status='pago', payment_id=%s, test=%s, gross_cents=%s, fee_cents=%s, net_cents=%s, paid_at=now(), updated_at=now() WHERE id=%s",
                                 (pid, test, gross, fee, net, oid))
                if not test:
                    _ledger(conn, "receita", gross, desc, f"mp:{pid}")
                    if fee:
                        _ledger(conn, "despesa", fee, f"Taxa do Mercado Pago · {desc}", f"mp:{pid}:taxa")
                if not duplicate:
                    delivery.issue(conn, oid)    # o link de download nasce junto com a venda
                outcome = "pagamento_duplicado" if duplicate else ("venda_teste" if test else "venda")
                _event(conn, pid, oid, status, outcome, "segundo pagamento aprovado para o mesmo pedido: precisa de reembolso manual" if duplicate else "")
            # 2) o reembolso, lançado pela DIFERENÇA do que já foi devolvido (reembolsos parciais sucessivos não contam em dobro)
            if status in TERMINAL_REFUND or refunded > 0:
                cur = conn.execute("SELECT status,payment_id,refunded_cents FROM orders WHERE id=%s", (oid,)).fetchone()
                main = cur["payment_id"] == pid
                full = status in TERMINAL_REFUND or refunded >= gross
                target = gross if full else refunded
                kind_ref = "chargeback" if status == "charged_back" else ("reembolso" if full else f"reembolso-parcial-{refunded}")
                label = "Chargeback" if status == "charged_back" else ("Reembolso" if full else "Reembolso parcial")
                already = conn.execute("SELECT 1 FROM ledger WHERE external_ref=%s", (f"mp:{pid}:{kind_ref}",)).fetchone() is not None
                if main:
                    new_status = "chargeback" if status == "charged_back" else ("reembolsado" if full else cur["status"])
                    if full:
                        delivery.revoke(conn, oid)    # reembolso total ou chargeback encerra o acesso
                    if cur["status"] == new_status and cur["refunded_cents"] >= target:
                        return {"outcome": outcome, "order_id": oid}          # este reembolso já foi aplicado
                    delta = target - cur["refunded_cents"]
                    conn.execute("UPDATE orders SET status=%s, refunded_cents=%s, updated_at=now() WHERE id=%s", (new_status, max(target, cur["refunded_cents"]), oid))
                else:
                    delta = target
                    if already:
                        return {"outcome": outcome, "order_id": oid}
                if not test:
                    _ledger(conn, "despesa", delta, f"{label} · {desc}", f"mp:{pid}:{kind_ref}")
                outcome = "chargeback" if status == "charged_back" else ("reembolso" if full else "reembolso_parcial")
                _event(conn, pid, oid, status, outcome, "")
            return {"outcome": outcome, "order_id": oid}

        if status in ("pending", "in_process", "authorized", "in_mediation"):
            if order["status"] in ("criado", "recusado"):
                conn.execute("UPDATE orders SET status='aguardando', updated_at=now() WHERE id=%s", (oid,))
            _event(conn, pid, oid, status, "pendente", "")
            return {"outcome": "pendente", "order_id": oid}

        if status in ("rejected", "cancelled"):
            if order["status"] in ("criado", "aguardando"):   # nunca derruba um pedido que já foi pago
                conn.execute("UPDATE orders SET status='recusado', updated_at=now() WHERE id=%s", (oid,))
            _event(conn, pid, oid, status, "recusado", "")
            return {"outcome": "recusado", "order_id": oid}

        _event(conn, pid, oid, status, "ignorado", "status que não muda o pedido")
        return {"outcome": "ignorado", "order_id": oid}
