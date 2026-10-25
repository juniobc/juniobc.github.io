"""Tools dos agentes. Propositalmente NÃO existe tool de dinheiro nem de envio de mensagem externa."""
import json
import os
import re
import html
import httpx
from . import config

HIRER_RE = re.compile(r"presidente|ceo|diretor|diretora|gerente", re.I)

SPECS = [
    {"name": "web_search", "description": "Pesquisa na web e devolve títulos, links e trechos.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "create_task", "description": "Cria uma tarefa para um funcionário (slug).",
     "parameters": {"type": "object", "properties": {"assignee": {"type": "string"}, "title": {"type": "string"}, "details": {"type": "string"}}, "required": ["assignee", "title"]}},
    {"name": "complete_task", "description": "Conclui uma tarefa sua com o resultado.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "result": {"type": "string"}}, "required": ["task_id", "result"]}},
    {"name": "write_product_file", "description": "Escreve um arquivo de produto (texto/markdown/csv) e registra o produto.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "filename": {"type": "string"}, "content": {"type": "string"}, "description": {"type": "string"}}, "required": ["product_name", "filename", "content"]}},
    {"name": "post_message", "description": "Envia mensagem a um colega (slug) ou ao mural (to vazio). Só colegas internos.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["body"]}},
    {"name": "hire_employee", "description": "Contrata um agente novo. Só cargos de chefia. Exige motivo.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "area": {"type": "string"}, "instructions": {"type": "string"}, "reason": {"type": "string"}}, "required": ["name", "role", "area", "instructions", "reason"]}},
    {"name": "list_team", "description": "Lista os funcionários ativos.", "parameters": {"type": "object", "properties": {}}},
]
TOOL_DEFS = [{"type": "function", "function": s} for s in SPECS]


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().encode("ascii", "ignore").decode()).strip("-")
    return s or "agente"


def _emp_by_slug(conn, slug):
    return conn.execute("SELECT * FROM employees WHERE slug=%s AND active", (slug,)).fetchone()


def web_search(query):
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=30,
                   headers={"User-Agent": "Mozilla/5.0 (Meridiano)"})
    items = re.findall(r'class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    out = []
    for href, title, snip in items[:5]:
        clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
        out.append(f"{clean(title)} | {href} | {clean(snip)}")
    return "\n".join(out) or "sem resultados"


def run_tool(conn, me, name, args):
    """Executa uma tool para o funcionário `me` (dict). Devolve texto."""
    try:
        if name == "web_search":
            return web_search(str(args["query"])[:200])
        if name == "list_team":
            rows = conn.execute("SELECT slug,name,role,area FROM employees WHERE active ORDER BY id").fetchall()
            return "\n".join(f"{r['slug']}: {r['name']} - {r['role']} ({r['area']})" for r in rows)
        if name == "create_task":
            to = _emp_by_slug(conn, args["assignee"])
            if not to:
                return "erro: funcionário não existe"
            r = conn.execute("INSERT INTO tasks(title,details,assignee_id,created_by) VALUES (%s,%s,%s,%s) RETURNING id",
                             (str(args["title"])[:200], str(args.get("details", ""))[:2000], to["id"], me["id"])).fetchone()
            return f"tarefa {r['id']} criada para {to['name']}"
        if name == "complete_task":
            r = conn.execute("UPDATE tasks SET status='done', result=%s, done_at=now() WHERE id=%s AND assignee_id=%s AND status='open' RETURNING id",
                             (str(args["result"])[:4000], int(args["task_id"]), me["id"])).fetchone()
            return "tarefa concluída" if r else "erro: tarefa não é sua ou não está aberta"
        if name == "write_product_file":
            fn = os.path.basename(str(args["filename"]))
            if not re.fullmatch(r"[\w.\-]{1,80}", fn) or fn.startswith("."):
                return "erro: nome de arquivo inválido"
            os.makedirs(config.PRODUCTS_DIR, exist_ok=True)
            path = os.path.join(config.PRODUCTS_DIR, fn)
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(args["content"])[:200000])
            conn.execute("INSERT INTO products(name,description,file_path,created_by) VALUES (%s,%s,%s,%s)",
                         (str(args["product_name"])[:200], str(args.get("description", ""))[:1000], fn, me["id"]))
            return f"arquivo {fn} salvo"
        if name == "post_message":
            to_slug = (args.get("to") or "").strip()
            to_id = None
            if to_slug:
                to = _emp_by_slug(conn, to_slug)
                if not to:
                    return "erro: destinatário não existe (só colegas internos)"
                to_id = to["id"]
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,%s,%s)", (me["id"], to_id, str(args["body"])[:1000]))
            return "mensagem registrada"
        if name == "hire_employee":
            if not HIRER_RE.search(me["role"]):
                return "erro: seu cargo não pode contratar"
            n = conn.execute("SELECT COUNT(*) AS n FROM employees WHERE active").fetchone()["n"]
            if n >= config.MAX_EMPLOYEES:
                return "erro: limite de funcionários atingido"
            slug = slugify(args["name"])
            if _emp_by_slug(conn, slug):
                slug = f"{slug}-{n + 1}"
            model = config.DEFAULT_ROTATION[n % len(config.DEFAULT_ROTATION)]
            conn.execute(
                "INSERT INTO employees(slug,name,role,area,instructions,model,hired_by,hire_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (slug, str(args["name"])[:60], str(args["role"])[:80], str(args["area"])[:80],
                 str(args["instructions"])[:3000], model, me["id"], str(args["reason"])[:1000]))
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)",
                         (me["id"], f"Contratei {args['name']} ({args['role']}). Motivo: {str(args['reason'])[:300]}"))
            return f"contratado: {slug}"
    except KeyError as e:
        return f"erro: falta o argumento {e}"
    except Exception as e:
        return f"erro: {e}"
    return "erro: tool desconhecida"


def parse_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}
