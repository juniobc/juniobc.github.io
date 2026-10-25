"""Entrega do produto: link único e temporário (7 dias, 5 downloads) ligado ao pedido pago. Reembolso e chargeback revogam."""
import datetime
import re
import secrets

from . import config, pdf

FORMATS = {"pdf": ("application/pdf", "pdf"), "md": ("text/markdown; charset=utf-8", "md")}
DL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,60}$")


def issue(conn, order_id):
    """Cria o link da compra paga (uma vez por pedido; repetir não gera outro)."""
    conn.execute("INSERT INTO downloads(order_id,token,expires_at,max_count) VALUES (%s,%s,now() + make_interval(days => %s),%s) ON CONFLICT (order_id) DO NOTHING",
                 (order_id, secrets.token_urlsafe(24), config.DOWNLOAD_DAYS, config.DOWNLOAD_MAX))


def revoke(conn, order_id):
    conn.execute("UPDATE downloads SET revoked=TRUE WHERE order_id=%s", (order_id,))


def link_for_order(conn, order_id):
    """O link do pedido com o que falta: válido, expirado, esgotado ou revogado."""
    d = conn.execute("SELECT *, expires_at <= now() AS expired FROM downloads WHERE order_id=%s", (order_id,)).fetchone()
    if not d:
        return None
    d["state"] = "revogado" if d["revoked"] else "expirado" if d["expired"] else "esgotado" if d["count"] >= d["max_count"] else "valido"
    d["left"] = max(0, d["max_count"] - d["count"])
    return d


def claim(conn, dl_token, fmt):
    """Consome UM download, de forma atômica (8 pedidos ao mesmo tempo com 1 download sobrando: só um passa).
    Devolve (produto, pedido) ou (None, motivo)."""
    if fmt not in FORMATS or not DL_TOKEN_RE.match(dl_token or ""):
        return None, "invalido"
    row = conn.execute(
        "UPDATE downloads d SET count=d.count+1 FROM orders o WHERE d.token=%s AND d.order_id=o.id AND NOT d.revoked AND d.expires_at > now() "
        "AND d.count < d.max_count AND o.status='pago' RETURNING d.order_id", (dl_token,)).fetchone()
    if not row:
        d = conn.execute("SELECT *, expires_at <= now() AS expired FROM downloads WHERE token=%s", (dl_token,)).fetchone()
        if not d:
            return None, "invalido"
        return None, "revogado" if d["revoked"] else "expirado" if d["expired"] else "esgotado"
    o = conn.execute("SELECT o.id,o.status,p.name,p.content,p.legal_note FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=%s", (row["order_id"],)).fetchone()
    return o, "ok"


def build(order, fmt):
    """Bytes e nome do arquivo. O PDF leva o número do pedido no rodapé (uso pessoal)."""
    from .tools import slugify
    nome = slugify(order["name"])
    if fmt == "pdf":
        corpo = (order["legal_note"] + "\n\n" if order["legal_note"] else "") + order["content"]
        return pdf.render_pdf(order["name"], corpo, footer=f"Pedido #{order['id']} · uso pessoal"), f"{nome}.pdf"
    texto = f"# {order['name']}\n\n" + (f"_{order['legal_note']}_\n\n" if order["legal_note"] else "") + order["content"] + f"\n\n---\nPedido #{order['id']} · uso pessoal\n"
    return texto.encode("utf-8"), f"{nome}.md"
