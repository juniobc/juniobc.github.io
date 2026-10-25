"""Rotas de pagamento. Cada requisição usa a SUA conexão com o banco (transações de dinheiro não podem se misturar)."""
import re

from fastapi import APIRouter, Body, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from ..engine import db, payments, secrets_store, store

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
        if not store.checkout_available(c):
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
        page = store.render_order(c, token, n)
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
