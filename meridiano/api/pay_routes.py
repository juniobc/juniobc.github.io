"""Rotas de pagamento. Cada requisição usa a SUA conexão com o banco (transações de dinheiro não podem se misturar)."""
import re

from fastapi import APIRouter, Body, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

import html as _html_mod

from fastapi import Response

from ..engine import db, delivery, payments, secrets_store, store

router = APIRouter()
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,60}$")


def _html(markup, nonce, status=200):
    return HTMLResponse(markup, status_code=status, headers={
        "Content-Security-Policy": store.csp(nonce), "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY", "Cache-Control": "no-store"})


def _msg(titulo, texto, status):
    n = store.new_nonce()
    return _html(store._page(titulo, f'<h1>{titulo}</h1><p>{texto}</p><p><a href="/pub/loja/">Voltar à loja</a></p>', n), n, status)


@router.post("/pub/loja/{slug}/comprar", response_class=HTMLResponse)
def comprar(slug: str):
    """Cria o pedido e manda o cliente para o pagamento do Mercado Pago. Desligado até a entrega existir."""
    c = db.connect()
    try:
        product = next((p for p in store.published(c) if p["slug"] == slug), None) if SLUG_RE.match(slug) else None
        if not product:
            return _msg("Produto não encontrado", "Este produto não está à venda.", 404)
        aberto, _motivo = store.sale_state(c, product)   # o motivo é para o painel, não para o cliente
        if not aberto:
            return _msg("Pagamento em breve", "O pagamento online ainda não está disponível. Nada foi cobrado. Volte em breve.", 503)
        order = payments.create_order(c, product)
        if not order:
            return _msg("Muitos pedidos agora", "Tente de novo em alguns minutos.", 429)
        url = payments.create_preference(c, order, product)
        if not url:
            c.execute("UPDATE orders SET status='cancelado', updated_at=now() WHERE id=%s", (order["id"],))
            return _msg("Não foi possível abrir o pagamento", "Nada foi cobrado. Tente de novo em instantes.", 502)
        return RedirectResponse(url, status_code=303)
    finally:
        c.close()


def _delivery_html(c, order_token):
    """Botões de download (POST, para que pré-visualizadores de links não gastem downloads) ou o motivo de não haver."""
    o = c.execute("SELECT id,status FROM orders WHERE token=%s", (order_token,)).fetchone()
    if not o or o["status"] != "pago":
        return ""
    d = delivery.link_for_order(c, o["id"])
    s = store.get_settings(c)
    contato = f'Fale com o vendedor: <a href="mailto:{_html_mod.escape(s.get("contact_email", ""), quote=True)}">{_html_mod.escape(s.get("contact_email", ""))}</a>.'
    if not d:
        return f"<p>Seu acesso está sendo preparado. Esta página se atualiza sozinha.</p><p>{contato}</p>"
    if d["state"] != "valido":
        motivo = {"revogado": "O acesso foi encerrado.", "expirado": "O link expirou.", "esgotado": "Os downloads deste link acabaram."}[d["state"]]
        return f"<h2>Download</h2><p>{motivo} {contato}</p>"
    forms = "".join(f'<form method="post" action="/pub/loja/baixar/{d["token"]}/{f}" style="display:inline-block;margin:0 8px 8px 0"><button class="btn" type="submit">Baixar {label}</button></form>'
                    for f, label in (("pdf", "PDF"), ("md", "Markdown")))
    return (f"<h2>Seu material</h2><p>{forms}</p><p>Válido até {d['expires_at'].strftime('%d/%m/%Y')} · {d['left']} download(s) restante(s). "
            f"Guarde o arquivo: o link é só seu e não pode ser compartilhado.</p><p>{contato}</p>")


@router.post("/pub/loja/baixar/{dl_token}/{fmt}")
def baixar(dl_token: str, fmt: str):
    """Entrega o arquivo. Cada clique consome um dos 5 downloads, de forma atômica; reembolso e chargeback revogam."""
    c = db.connect()
    try:
        order, motivo = delivery.claim(c, dl_token, fmt)
        if order is None:
            texto = {"expirado": "Este link expirou.", "esgotado": "Os downloads deste link acabaram.", "revogado": "O acesso a este material foi encerrado."}.get(motivo, "Link inválido.")
            return _msg("Download indisponível", texto + " Fale com o vendedor pela página da loja.", 410 if motivo != "invalido" else 404)
        body, nome = delivery.build(order, fmt)
        return Response(content=body, media_type=delivery.FORMATS[fmt][0], headers={
            "Content-Disposition": f'attachment; filename="{nome}"', "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox"})
    finally:
        c.close()


@router.get("/pub/loja/pedido/{token}", response_class=HTMLResponse)
def pedido(token: str, payment_id: str = Query(None)):
    """Estado do pedido. Se o Mercado Pago mandou o cliente de volta com payment_id, confirmamos NA API dele (nunca na URL)."""
    if not TOKEN_RE.match(token):
        return _msg("Pedido não encontrado", "Confira o endereço.", 404)
    c = db.connect()
    try:
        if payment_id and re.fullmatch(r"\d{3,20}", payment_id):
            try:
                payments.process_payment(c, payment_id)
            except (payments.PaymentFetchError, OSError, ValueError):
                pass   # o webhook confirma depois
        n = store.new_nonce()
        page = store.render_order(c, token, n, delivery_html=_delivery_html(c, token))
        if page is None:
            return _msg("Pedido não encontrado", "Confira o endereço.", 404)
        return _html(page, n)
    finally:
        c.close()


@router.post("/api/mp/webhook")
def webhook(payload: dict = Body(default=None), data_id: str = Query(None, alias="data.id"), type: str = Query(None),
            x_signature: str = Header(None), x_request_id: str = Header(None)):
    """Aviso do Mercado Pago. O aviso é só uma dica: a venda só existe se a API do Mercado Pago confirmar."""
    payload = payload if isinstance(payload, dict) else {}
    kind = type or payload.get("type")
    pid = data_id or str((payload.get("data") or {}).get("id", "") or "")
    if kind and kind != "payment":
        return {"ok": True, "ignorado": kind}
    if not pid:
        raise HTTPException(400, "sem id de pagamento")
    secret = secrets_store.webhook_secret()
    if secret and not payments.signature_ok(secret, data_id or pid, x_request_id, x_signature):
        raise HTTPException(401, "assinatura inválida")
    c = db.connect()
    try:
        try:
            r = payments.process_payment(c, pid)
        except payments.PaymentFetchError:
            raise HTTPException(502, "não foi possível confirmar o pagamento agora")
        return {"ok": True, "resultado": r["outcome"]}
    finally:
        c.close()
