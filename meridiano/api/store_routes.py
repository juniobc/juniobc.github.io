"""Rotas públicas da loja (sem token) e as de configuração (com token). Páginas sem JavaScript e com CSP restritiva."""
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..engine import store

router = APIRouter()
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def deps():
    from . import main   # import tardio: main inclui este router
    return main


def _html(markup, nonce, status=200):
    return HTMLResponse(markup, status_code=status, headers={
        "Content-Security-Policy": store.csp(nonce), "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY", "Cache-Control": "no-store"})


@router.get("/pub/loja/", response_class=HTMLResponse)
def loja_index():
    n = store.new_nonce()
    return _html(store.render_index(deps().conn(), n), n)


@router.get("/pub/loja/{slug}/", response_class=HTMLResponse)
def loja_produto(slug: str):
    n = store.new_nonce()
    page = store.render_product(deps().conn(), slug, n) if SLUG_RE.match(slug) else None
    if page is None:
        return _html(store._page("Não encontrado", "<h1>Produto não encontrado</h1><p><a href=\"/pub/loja/\">Voltar à loja</a></p>", n), n, 404)
    return _html(page, n)


class Settings(BaseModel):
    seller_name: str = Field(min_length=3, max_length=120)
    contact_email: str = Field(min_length=6, max_length=190)


def register(app, read_auth, admin_auth, conn_fn):
    @app.post("/api/store/settings", dependencies=[Depends(admin_auth)])
    def set_store_settings(body: Settings):
        email = body.contact_email.strip()
        if not store.CONTACT_RE.match(email):
            raise HTTPException(400, "e-mail de contato inválido")
        c = conn_fn()
        for k, v in (("seller_name", body.seller_name.strip()), ("contact_email", email)):
            c.execute("INSERT INTO store_settings(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (k, v))
        from ..engine import secrets_store
        secrets_store.sync_owner_request(c)
        return {"ok": True}

    @app.get("/api/store/settings", dependencies=[Depends(read_auth)])
    def get_store_settings():
        s = store.get_settings(conn_fn())
        return {"seller_name": s.get("seller_name"), "contact_email": s.get("contact_email"), "pronta": store.store_ready(conn_fn())}

    @app.get("/api/store/products", dependencies=[Depends(read_auth)])
    def store_products():
        """Para o painel: o que falta para cada produto aparecer na loja."""
        c = conn_fn()
        rows = c.execute("SELECT p.id,p.name,p.status,p.price_cents,s.slug,(s.product_id IS NOT NULL) AS tem_texto FROM products p "
                         "LEFT JOIN product_store s ON s.product_id=p.id ORDER BY p.id").fetchall()
        pub = {p["slug"] for p in store.published(c)}
        return [dict(r, publicado=bool(r["slug"] in pub), url=f"/pub/loja/{r['slug']}/" if r["slug"] in pub else None) for r in rows]
