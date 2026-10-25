"""Loja pública: gera páginas estáticas (sem JavaScript) a partir do banco. Todo texto de agente é escapado."""
import html
import json
import re
import secrets



MIN_PRICE_CENTS = 900
MAX_PRICE_CENTS = 100000
CONTACT_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[^@\s]{2,20}$")
# Promessas que não podem aparecer em texto de venda (honestidade e CDC).
BANNED_CLAIMS = ("garantido", "garantida", "garantidos", "garantidas", "sem risco", "enriqueça", "enriquecer", "fique rico",
                 "renda passiva fácil", "milhões", "milhão", "dinheiro fácil", "resultado imediato", "100%")
REFUND_TEXT = ("Garantia de 7 dias: se você não gostar, peça o reembolso integral em até 7 dias após a compra (direito de arrependimento, "
               "art. 49 do Código de Defesa do Consumidor), pelo contato do vendedor abaixo.")
GENERIC_LEGAL = "Material educativo e informativo. Os resultados dependem de cada pessoa e não há promessa de ganho."
CSS = ("body{margin:0;font:17px/1.55 system-ui,sans-serif;background:#fafaf7;color:#1c2321}main{max-width:680px;margin:0 auto;padding:20px 18px 48px}"
       "h1{font-size:30px;line-height:1.2;margin:.2em 0}h2{font-size:21px;margin-top:1.6em}.sub{font-size:19px;color:#4a5a55}"
       ".price{font-size:34px;font-weight:700;margin:.4em 0}.btn{display:inline-block;background:#1e7a5f;color:#fff;border:0;border-radius:10px;"
       "padding:14px 26px;font-size:18px}.soon{display:inline-block;background:#e4e8e5;color:#4a5a55;border-radius:10px;padding:14px 26px}"
       "ul{padding-left:1.2em}li{margin:.35em 0}.legal{font-size:14px;color:#4a5a55;border-top:1px solid #d8ddd9;margin-top:2em;padding-top:1em}"
       "details{margin:.6em 0}summary{cursor:pointer;font-weight:600}a{color:#1e7a5f}")


def brl(cents):
    return "R$ " + f"{cents / 100:.2f}".replace(".", ",")


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM store_settings").fetchall()}


def store_ready(conn):
    """A loja só abre com a identificação do vendedor (exigência legal)."""
    s = get_settings(conn)
    return bool(s.get("seller_name") and s.get("contact_email"))


def check_copy(f):
    """Lista de problemas do texto de venda. Vazia = pode salvar."""
    p = []
    lim = (("headline", 90), ("subtitle", 180), ("pitch", 1500), ("audience", 300))
    for k, n in lim:
        v = f.get(k, "")
        if not isinstance(v, str) or len(v) > n:
            p.append(f"{k}: texto obrigatório com no máximo {n} caracteres")
    if not f.get("headline", "").strip():
        p.append("headline não pode ficar vazia")
    b = f.get("benefits")
    if not isinstance(b, list) or not 3 <= len(b) <= 7 or any(not isinstance(x, str) or not x.strip() or len(x) > 160 for x in b):
        p.append("benefits: lista de 3 a 7 frases de até 160 caracteres")
    q = f.get("faq")
    if not isinstance(q, list) or not 2 <= len(q) <= 6 or any(not isinstance(x, dict) or not str(x.get("q", "")).strip() or not str(x.get("a", "")).strip()
                                                           or len(str(x.get("q"))) > 160 or len(str(x.get("a"))) > 400 for x in q):
        p.append("faq: lista de 2 a 6 itens {q, a} (pergunta até 160 e resposta até 400 caracteres)")
    blob = " ".join([f.get(k, "") if isinstance(f.get(k, ""), str) else "" for k, _ in lim] + [str(x) for x in (b or [])] + [str(x) for x in (q or [])]).lower()
    hits = sorted({w for w in BANNED_CLAIMS if w in blob})
    if hits:
        p.append("promessas proibidas no texto de venda (seja honesto, sem garantia de ganho): " + ", ".join(hits))
    if "<" in blob or ">" in blob:
        p.append("não use HTML nem os sinais < e >")
    return p


def published(conn):
    """Produtos que podem aparecer: prontos, com texto de venda e preço, e loja identificada."""
    if not store_ready(conn):
        return []
    rows = conn.execute(
        "SELECT p.id,p.name,p.description,p.price_cents,p.legal_note,s.slug,s.headline,s.subtitle,s.pitch,s.benefits,s.audience,s.faq "
        "FROM products p JOIN product_store s ON s.product_id=p.id WHERE p.status='pronto' AND p.price_cents>=%s ORDER BY p.id", (MIN_PRICE_CENTS,)).fetchall()
    for r in rows:   # listas guardadas como texto JSON
        r["benefits"] = json.loads(r["benefits"])
        r["faq"] = json.loads(r["faq"])
    return rows


# O botão de compra só liga quando o cliente consegue RECEBER o produto depois de pagar. A entrega por link (próxima etapa)
# vira esta chave para True; até lá ninguém paga e fica sem receber.
DELIVERY_READY = False


def checkout_available(conn):
    from . import secrets_store
    return DELIVERY_READY and secrets_store.configured()


def _page(title, body, nonce, desc="", refresh=0):
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{html.escape(title)}</title><meta name="description" content="{html.escape(desc[:160], quote=True)}">'
            f'<style nonce="{nonce}">{CSS}</style></head><body><main>{body}</main></body></html>')


def new_nonce():
    return secrets.token_urlsafe(16)


def csp(nonce):
    return ("default-src 'none'; style-src 'nonce-" + nonce + "'; img-src 'self' data:; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; script-src 'none'")


def _legal(conn, p):
    s = get_settings(conn)
    return (f'<div class="legal"><p>{html.escape(p["legal_note"] or GENERIC_LEGAL)}</p>'
            f'<p>Vendedor: {html.escape(s.get("seller_name", ""))}. Contato: <a href="mailto:{html.escape(s.get("contact_email", ""), quote=True)}">'
            f'{html.escape(s.get("contact_email", ""))}</a>.</p></div>')


def render_product(conn, slug, nonce):
    p = next((x for x in published(conn) if x["slug"] == slug), None)
    if not p:
        return None
    secs = conn.execute("SELECT position,title,content FROM product_sections WHERE product_id=%s ORDER BY position", (p["id"],)).fetchall()
    words = sum(len(x["content"].split()) for x in secs)
    e = html.escape
    sumario = "".join(f"<li>{e(x['title'])}</li>" for x in secs)
    benef = "".join(f"<li>{e(b)}</li>" for b in p["benefits"])
    faq = "".join(f"<details><summary>{e(str(i['q']))}</summary><p>{e(str(i['a']))}</p></details>" for i in p["faq"])
    if checkout_available(conn):
        buy = f'<form method="post" action="/pub/loja/{e(p["slug"], quote=True)}/comprar"><button class="btn" type="submit">Comprar agora</button></form>'
    else:
        buy = '<span class="soon">Pagamento em breve</span>'
    body = (f'<p><a href="/pub/loja/">Todos os produtos</a></p><h1>{e(p["headline"])}</h1><p class="sub">{e(p["subtitle"])}</p>'
            f'<p class="price">{brl(p["price_cents"])}</p>{buy}<p>{e(p["pitch"])}</p><h2>O que você leva</h2><ul>{benef}</ul>'
            f'<h2>Para quem é</h2><p>{e(p["audience"])}</p><h2>O que tem dentro</h2>'
            f'<p>{len(secs)} seções e cerca de {words} palavras:</p><ol>{sumario}</ol><h2>Perguntas frequentes</h2>{faq}'
            f'<h2>Garantia</h2><p>{e(REFUND_TEXT)}</p>{buy}{_legal(conn, p)}')
    return _page(p["headline"], body, nonce, p["subtitle"])


def render_index(conn, nonce):
    items = published(conn)
    if not items:
        return _page("Loja", "<h1>Loja em preparação</h1><p>Em breve, materiais educativos prontos para você.</p>", nonce)
    e = html.escape
    lis = "".join(f'<li><a href="/pub/loja/{e(i["slug"], quote=True)}/">{e(i["headline"])}</a> <span>({brl(i["price_cents"])})</span></li>' for i in items)
    return _page("Loja", f"<h1>Loja</h1><ul>{lis}</ul>", nonce)


def render_soon(nonce):
    return _page("Pagamento em breve", "<h1>Pagamento em breve</h1><p>O pagamento online ainda não está disponível. Volte em breve.</p>"
                 '<p><a href="/pub/loja/">Voltar à loja</a></p>', nonce)


ORDER_TEXT = {
    "criado": ("Pedido criado", "Estamos abrindo o pagamento. Se a página do Mercado Pago não abriu, volte à loja e tente de novo."),
    "aguardando": ("Aguardando o pagamento", "Assim que o Mercado Pago confirmar, esta página mostra o seu acesso. Ela se atualiza sozinha."),
    "pago": ("Pagamento confirmado", "Obrigado pela compra!"),
    "recusado": ("O pagamento não foi aprovado", "Nada foi cobrado. Você pode tentar de novo pela loja, com outro meio de pagamento."),
    "cancelado": ("Pedido cancelado", "Nada foi cobrado."),
    "reembolsado": ("Pagamento reembolsado", "O valor foi devolvido e o acesso ao material foi encerrado."),
    "chargeback": ("Pagamento contestado", "O pagamento foi contestado junto ao banco ou à operadora e o acesso ao material foi encerrado."),
}


def render_order(conn, token, nonce, delivery_html=""):
    """Página do pedido: só o estado, sem nenhum dado pessoal. O token do pedido é o segredo de quem comprou."""
    o = conn.execute("SELECT o.status,o.amount_cents,p.name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.token=%s", (token,)).fetchone()
    if not o:
        return None
    titulo, texto = ORDER_TEXT[o["status"]]
    e = html.escape
    body = (f'<p><a href="/pub/loja/">Loja</a></p><h1>{e(titulo)}</h1><p>{e(o["name"])} · {brl(o["amount_cents"])}</p><p>{e(texto)}</p>'
            + (delivery_html if o["status"] == "pago" else ""))
    return _page(titulo, body, nonce, refresh=15 if o["status"] in ("criado", "aguardando") else 0)
