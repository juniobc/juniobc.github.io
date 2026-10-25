"""Tools dos agentes. Propositalmente NÃO existe tool de dinheiro nem de envio de mensagem externa."""
import difflib
import json
import os
import re
import html
import httpx
from . import config, llm

HIRER_RE = re.compile(r"presidente|ceo|diretor|diretora|gerente", re.I)
FOUNDER_RE = re.compile(r"presidente|ceo", re.I)
DECISION_RE = re.compile(r"presidente|ceo|jur[ií]dic", re.I)   # cargos de decisão: sugerem o melhor perfil de LLM liberado

SPECS = [
    {"name": "web_search", "description": "Pesquisa na web e devolve títulos, links e trechos.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "create_task", "description": "Cria uma tarefa para um funcionário (slug).",
     "parameters": {"type": "object", "properties": {"assignee": {"type": "string"}, "title": {"type": "string"}, "details": {"type": "string"}}, "required": ["assignee", "title"]}},
    {"name": "complete_task", "description": "Conclui uma tarefa sua com o resultado.",
     "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "result": {"type": "string"}}, "required": ["task_id", "result"]}},
    {"name": "write_product", "description": "Escreve (ou continua) o CONTEÚDO de um produto digital. Não existem arquivos prontos: você mesmo escreve o texto. Se o produto já existe, atualiza em vez de criar outro. Para textos longos use mode=append em várias chamadas.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string", "enum": ["replace", "append"]}, "description": {"type": "string"}, "price_brl": {"type": "number"}, "status": {"type": "string", "enum": ["rascunho", "revisao", "pronto"]}}, "required": ["product_name", "content"]}},
    {"name": "read_product", "description": "Lê o conteúdo já escrito de um produto (ou só uma seção, com section_number).",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "section_number": {"type": "integer"}}, "required": ["product_name"]}},
    {"name": "write_section", "description": "Escreve UMA seção COMPLETA de um produto do plano (mínimo 400 palavras, em português, conteúdo útil e original). Substitui o texto da seção. Quando todas as seções existem, o produto vira 'pronto'.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "section_number": {"type": "integer"}, "content": {"type": "string"}}, "required": ["product_name", "section_number", "content"]}},
    {"name": "request_rewrite", "description": "Revisor: marca uma seção escrita como fraca e pede reescrita, com o motivo.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "section_number": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["product_name", "section_number", "reason"]}},
    {"name": "list_products", "description": "Lista produtos com status, preço e tamanho do conteúdo.", "parameters": {"type": "object", "properties": {}}},
    {"name": "ask_owner", "description": "Registra UM pedido objetivo ao dono (Junio) para algo que só ele pode fazer (conta de vendedor, CPF, aprovação). Uma linha.",
     "parameters": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}},
    {"name": "reply_owner", "description": "Responde ao dono (Junio) no chat da empresa. Curto, em português, honesto: o que você vai fazer, o que já fez ou por que não pode. Use para responder às MENSAGENS DO DONO.",
     "parameters": {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}},
    {"name": "post_message", "description": "Envia mensagem a um colega (slug) ou ao mural (to vazio). Só colegas internos.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["body"]}},
    {"name": "hire_employee", "description": "Contrata um agente novo. Só cargos de chefia. Exige motivo.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "area": {"type": "string"}, "instructions": {"type": "string", "description": "completas: função, como trabalha, o que entrega e como se mede"}, "reason": {"type": "string"}, "deliverable": {"type": "string", "description": "entrega mensurável por dia, ex.: 2 artigos"}, "llm_profile": {"type": "string", "description": "opcional: rótulo de um perfil de list_llm_profiles que esteja liberado agora. Padrão: grátis"}}, "required": ["name", "role", "area", "instructions", "reason", "deliverable"]}},
    {"name": "list_llm_profiles", "description": "Lista os perfis de LLM (modelo, preço por milhão de tokens, se é grátis) e se cada um está liberado agora para contratação. Só leitura; quem liga ou desliga é o dono.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "team_review", "description": "Presidente ou CEO registram a revisão de equipe (a cada 30 turnos): quem entrega, quem não, e o que muda. Só vale depois de contratar até a meta mínima.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
    {"name": "fire_employee", "description": "Demite um funcionário (não pode ser Presidente, CEO nem você). Só chefia. Exige motivo. Cada contratação divide o orçamento de tokens do dia: demita quem não é mais necessário.",
     "parameters": {"type": "object", "properties": {"slug": {"type": "string"}, "reason": {"type": "string"}}, "required": ["slug", "reason"]}},
    {"name": "start_meeting", "description": "Convoca uma reunião curta com colegas (slugs) sobre um assunto. Registra no mural.",
     "parameters": {"type": "object", "properties": {"participants": {"type": "array", "items": {"type": "string"}}, "topic": {"type": "string"}}, "required": ["participants", "topic"]}},
    {"name": "propose_tool", "description": "Propõe uma ferramenta nova em Python PURO (sem rede, sem arquivos). O código precisa definir `def run(args):` e devolver algo serializável em JSON; imports permitidos: json, re, math, datetime, statistics, csv, textwrap, itertools, collections, decimal, hashlib, base64, unicodedata, string, html, urllib.parse. Os testes (funções test_* com assert que chamam run) rodam antes de ativar. Depois outro chefe revisa e aprova.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "python_code": {"type": "string"}, "tests_code": {"type": "string"}, "reason": {"type": "string"}}, "required": ["name", "description", "python_code", "tests_code", "reason"]}},
    {"name": "list_tools", "description": "Lista as ferramentas criadas por agentes, com status e nível.", "parameters": {"type": "object", "properties": {}}},
    {"name": "read_tool", "description": "Lê o código e os testes de uma ferramenta (para revisar).",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "approve_tool", "description": "Revisor (Presidente ou CEO, nunca o autor): aprova ou recusa uma ferramenta em revisão depois de ler o código.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "approve": {"type": "boolean"}, "note": {"type": "string"}}, "required": ["name", "approve", "note"]}},
    {"name": "run_custom_tool", "description": "Executa uma ferramenta ATIVA, isolada, com os argumentos dados.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "object"}}, "required": ["name"]}},
    {"name": "write_store_copy", "description": "Escreve o TEXTO DE VENDA de um produto pronto, em campos (nunca HTML). Honesto: sem promessa de ganho, sem 'garantido', sem '100%'. A loja monta a página sozinha.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "headline": {"type": "string"}, "subtitle": {"type": "string"}, "pitch": {"type": "string"},
                    "benefits": {"type": "array", "items": {"type": "string"}}, "audience": {"type": "string"},
                    "faq": {"type": "array", "items": {"type": "object", "properties": {"q": {"type": "string"}, "a": {"type": "string"}}}}},
                    "required": ["product_name", "headline", "subtitle", "pitch", "benefits", "audience", "faq"]}},
    {"name": "read_store_page", "description": "Lê o texto de venda e o preço de um produto, como o cliente vai ver, para revisar antes da compra abrir.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}}, "required": ["product_name"]}},
    {"name": "approve_store_page", "description": "Presidente (ou o CEO, se a Presidente escreveu o texto): aprova a página de venda e o preço para a compra abrir. Nunca o autor do texto. Se o texto ou o preço mudar depois, é preciso aprovar de novo.",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "note": {"type": "string"}}, "required": ["product_name", "note"]}},
    {"name": "request_store_rewrite", "description": "Revisor: devolve o texto de venda para reescrita, com o motivo (promessa exagerada, informação que o produto não entrega, texto confuso).",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "reason": {"type": "string"}}, "required": ["product_name", "reason"]}},
    {"name": "set_price", "description": "Presidente ou CEO definem o preço de venda de um produto (mínimo R$ 9,00, máximo R$ 1.000,00).",
     "parameters": {"type": "object", "properties": {"product_name": {"type": "string"}, "price_brl": {"type": "number"}, "reason": {"type": "string"}}, "required": ["product_name", "price_brl", "reason"]}},
    {"name": "propose_opportunity", "description": "Registra uma ideia de como ganhar dinheiro de forma legítima na mesa de oportunidades. Sem spam, golpe, falsa promessa, dado pessoal ou contas de terceiros.",
     "parameters": {"type": "object", "properties": {"idea": {"type": "string"}, "audience": {"type": "string"}, "how_it_earns": {"type": "string"}, "effort": {"type": "string", "enum": ["baixo", "médio", "alto"]},
                    "risk": {"type": "string", "enum": ["baixo", "médio", "alto"]}, "needs_owner": {"type": "string", "description": "o que só o dono pode fazer (identidade, conta, aceite de termos), ou vazio"}},
                    "required": ["idea", "audience", "how_it_earns", "effort", "risk"]}},
    {"name": "update_opportunity", "description": "Muda o status de uma oportunidade (ideia, teste, ativa, descartada) e registra o aprendizado. Máximo 2 testes ao mesmo tempo. Descartar exige o aprendizado.",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"}, "status": {"type": "string", "enum": ["ideia", "teste", "ativa", "descartada"]}, "learning": {"type": "string"}}, "required": ["id", "status", "learning"]}},
    {"name": "list_opportunities", "description": "Lista a mesa de oportunidades.", "parameters": {"type": "object", "properties": {}}},
    {"name": "list_team", "description": "Lista os funcionários ativos.", "parameters": {"type": "object", "properties": {}}},
]
TOOL_DEFS = [{"type": "function", "function": s} for s in SPECS]


def norm(t):
    return re.sub(r"\W+", " ", str(t).lower()).strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.7


def cap(text, n=1500):
    text = str(text)
    return text if len(text) <= n else text[:n] + " …(cortado)"


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


def _find_section(conn, product_name, number):
    """(produto, seção) ou (mensagem de erro, None)."""
    prod = next((p for p in conn.execute("SELECT id,name FROM products").fetchall() if similar(p["name"], product_name)), None)
    if not prod:
        return "erro: produto não existe (use list_products)", None
    sec = conn.execute("SELECT * FROM product_sections WHERE product_id=%s AND position=%s", (prod["id"], int(number))).fetchone()
    if not sec:
        return "erro: este produto não tem essa seção no plano", None
    return prod, sec


def _after_section_saved(conn, prod, me, sec, words):
    """Remonta o texto do produto, atualiza o status e avisa o dono uma única vez quando tudo está escrito."""
    before = conn.execute("SELECT status FROM products WHERE id=%s", (prod["id"],)).fetchone()["status"]
    secs = conn.execute("SELECT position,title,content,needs_rewrite FROM product_sections WHERE product_id=%s ORDER BY position", (prod["id"],)).fetchall()
    text = "\n\n".join(f"## {s['position']}. {s['title']}\n\n{s['content']}" for s in secs if s["content"])
    done = [s for s in secs if s["content"] and not s["needs_rewrite"]]
    ready = len(done) == len(secs)
    conn.execute("UPDATE products SET content=%s, status=%s, updated_at=now() WHERE id=%s",
                 (text, "pronto" if ready else "rascunho", prod["id"]))
    msg = f"seção {sec['position']} salva ({words} palavras). Produto: {len(done)} de {len(secs)} seções prontas."
    if ready:
        if before != "pronto":   # avisa o time uma vez; ninguém pede nada ao dono: a venda é automática na loja do servidor
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], f"Produto '{prod['name']}' pronto para a loja."))
        msg += " Todas as seções existem: produto marcado como pronto para a loja."
    else:
        nxt = next((s for s in secs if not s["content"] or s["needs_rewrite"]), None)
        if nxt:
            msg += f" Próxima: seção {nxt['position']} ({nxt['title']})."
    return msg


TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
REVIEWER_RE = re.compile(r"presidente|ceo", re.I)
MAX_TOOLS = 30
MAX_PROPOSALS_PER_DAY = 10


def _save_tool_file(name, version, code):
    try:
        d = os.path.join(config.WORKSPACE_DIR, "tools", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"v{version}.py"), "w", encoding="utf-8") as f:
            f.write(code)
    except OSError:
        pass


def _log_run(conn, tool, me, kind, args, res):
    out = json.dumps(res.get("result"), ensure_ascii=False)[:20000] if res.get("ok") else ""
    conn.execute("INSERT INTO tool_runs(tool_id,tool_name,employee_id,kind,args,ok,output,error,ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (tool["id"] if tool else None, tool["name"] if tool else "?", me["id"], kind, json.dumps(args, ensure_ascii=False)[:2000],
                  bool(res.get("ok")), out, str(res.get("error", ""))[:1000], res.get("ms", 0)))


def _propose_tool(conn, me, args):
    from . import sandbox
    name = str(args["name"]).strip()
    if not TOOL_NAME_RE.match(name) or name in {s["name"] for s in SPECS}:
        return "erro: nome inválido (use letras minúsculas, números e _, 3 a 40 caracteres, e não repita o nome de uma tool existente)"
    code, tests = str(args["python_code"]), str(args["tests_code"])
    if conn.execute("SELECT COUNT(*) AS n FROM custom_tools").fetchone()["n"] >= MAX_TOOLS and not conn.execute("SELECT 1 FROM custom_tools WHERE name=%s", (name,)).fetchone():
        return "erro: limite de ferramentas atingido"
    if conn.execute("SELECT COUNT(*) AS n FROM custom_tool_versions WHERE created_at >= date_trunc('day', now())").fetchone()["n"] >= MAX_PROPOSALS_PER_DAY:
        return "erro: limite diário de propostas atingido"
    ok_c, prob_c, helpers = sandbox.validate(code, need_run=True)
    ok_t, prob_t, helpers_t = sandbox.validate(tests, need_run=False)
    level, status, note = "A", "revisao", ""
    if not (ok_c and ok_t):
        status, note = "recusada", "validação: " + "; ".join(prob_c + prob_t)
    elif helpers or helpers_t:
        level, status = "C", "pendente_dono"
    existing = conn.execute("SELECT * FROM custom_tools WHERE name=%s", (name,)).fetchone()
    if existing:
        if existing["status"] == "ativa" and existing["author_id"] != me["id"]:
            return "erro: só o autor pode propor nova versão de uma ferramenta ativa"
        version = existing["version"] + 1
        conn.execute("UPDATE custom_tools SET version=%s, description=%s, code=%s, tests_code=%s, level=%s, status=%s, reason=%s, review_note=%s, "
                     "reviewed_by=NULL, last_error='', updated_at=now() WHERE id=%s",
                     (version, str(args["description"])[:300], code, tests, level, status, str(args["reason"])[:500], note, existing["id"]))
        tid = existing["id"]
    else:
        version = 1
        tid = conn.execute("INSERT INTO custom_tools(name,description,code,tests_code,level,status,author_id,reason,review_note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                           (name, str(args["description"])[:300], code, tests, level, status, me["id"], str(args["reason"])[:500], note)).fetchone()["id"]
    conn.execute("INSERT INTO custom_tool_versions(tool_id,version,code,tests_code,author_id) VALUES (%s,%s,%s,%s,%s)", (tid, version, code, tests, me["id"]))
    _save_tool_file(name, version, code)
    tool = {"id": tid, "name": name}
    if status == "recusada":
        return f"ferramenta recusada na validação: {note}"
    if level == "C":
        body = f"Ferramenta '{name}' precisa de rede/arquivo (nível C): {str(args['reason'])[:150]}. Aprovar ou recusar no painel."
        if not conn.execute("SELECT 1 FROM owner_requests WHERE body=%s", (body,)).fetchone():
            conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (%s,%s)", (me["id"], body))
        return "ferramenta de nível C: ficou pendente e o dono foi avisado; ela só ativa se ele aprovar"
    if not sandbox.enabled():   # portão fechado: grava como rascunho, não executa nada
        conn.execute("UPDATE custom_tools SET status='rascunho', review_note=%s WHERE id=%s", (sandbox.OFF_MSG + "; os testes rodam quando as ferramentas forem ligadas", tid))
        return f"ferramenta '{name}' v{version} salva como RASCUNHO: {sandbox.OFF_MSG}. Os testes rodam quando o isolamento for liberado."
    res = sandbox.execute(code, tests=tests)
    _log_run(conn, tool, me, "teste", {}, res)
    if not res.get("ok"):
        conn.execute("UPDATE custom_tools SET status='recusada', review_note=%s, last_error=%s WHERE id=%s", ("testes falharam: " + res.get("error", ""), res.get("error", "")[:500], tid))
        return f"os testes falharam, ferramenta recusada: {res.get('error')}"
    return f"ferramenta '{name}' v{version} passou na validação e nos {res['result'].get('testes', '?')} teste(s); aguarda revisão de outro chefe (read_tool + approve_tool)"


BANNED_IDEAS = ("spam", "mensagem em massa", "disparo em massa", "golpe", "esquema de pirâmide", "pirâmide", "falsificar", "clonar", "dados pessoais de terceiros",
                "comprar lista de e-mails", "lista de contatos", "fingir ser", "se passar por", "perfil falso", "contas falsas", "scraping de dados pessoais")


def _propose_opportunity(conn, me, args):
    idea = str(args["idea"]).strip()
    if len(idea) < 15:
        return "erro: descreva a ideia com pelo menos 15 caracteres"
    low = " ".join(str(args.get(k, "")) for k in ("idea", "audience", "how_it_earns")).lower()
    hit = next((w for w in BANNED_IDEAS if w in low), None)
    if hit:
        return f"erro: ideia recusada ('{hit}'): só meios legais e honestos, sem spam, golpe, falsa identidade ou dados pessoais"
    for r in conn.execute("SELECT id,idea,status FROM opportunities").fetchall():
        if similar(r["idea"], idea):
            return f"não registrei: já existe a oportunidade {r['id']} ({r['status']}) parecida: '{r['idea'][:70]}'"
    effort = args.get("effort") if args.get("effort") in ("baixo", "médio", "alto") else "médio"
    risk = args.get("risk") if args.get("risk") in ("baixo", "médio", "alto") else "médio"
    r = conn.execute("INSERT INTO opportunities(idea,audience,how_it_earns,effort,risk,needs_owner,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (idea[:300], str(args.get("audience", ""))[:300], str(args.get("how_it_earns", ""))[:400], effort, risk, str(args.get("needs_owner", ""))[:300], me["id"], me["id"])).fetchone()
    n = conn.execute("SELECT COUNT(*) AS n FROM opportunities WHERE status<>'descartada'").fetchone()["n"]
    return f"oportunidade {r['id']} registrada. A mesa tem {n} ideia(s) (mínimo {config.MIN_OPPORTUNITIES})."


def _update_opportunity(conn, me, args):
    o = conn.execute("SELECT * FROM opportunities WHERE id=%s", (int(args["id"]),)).fetchone()
    if not o:
        return "erro: oportunidade não existe (use list_opportunities)"
    status = args.get("status")
    if status not in ("ideia", "teste", "ativa", "descartada"):
        return "erro: status inválido"
    learning = str(args.get("learning", "")).strip()[:600]
    if status == "descartada" and len(learning) < 20:
        return "erro: para descartar, registre o aprendizado (o que se viu e por que não vale a pena), com pelo menos 20 caracteres"
    if status == "teste" and o["status"] != "teste":
        if conn.execute("SELECT COUNT(*) AS n FROM opportunities WHERE status='teste'").fetchone()["n"] >= config.MAX_TESTS:
            return f"erro: já há {config.MAX_TESTS} testes em andamento; conclua ou descarte um antes"
    conn.execute("UPDATE opportunities SET status=%s, learning=CASE WHEN %s<>'' THEN %s ELSE learning END, updated_by=%s, updated_at=now(), "
                 "decided_at=CASE WHEN %s='descartada' THEN now() ELSE decided_at END WHERE id=%s", (status, learning, learning, me["id"], status, o["id"]))
    if status in ("teste", "ativa") and o["needs_owner"].strip():   # um pedido único ao dono, só quando a ideia realmente avança
        body = f"Para testar '{o['idea'][:60]}': {o['needs_owner'][:200]}"
        if not any(similar(r["body"], body) for r in conn.execute("SELECT body FROM owner_requests").fetchall()):
            conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (%s,%s)", (me["id"], body))
    return f"oportunidade {o['id']} agora está '{status}'"


def _find_product(conn, name):
    return next((p for p in conn.execute("SELECT id,name,status,price_cents FROM products").fetchall() if similar(p["name"], name)), None)


def _write_store_copy(conn, me, args):
    from . import store
    prod = _find_product(conn, args["product_name"])
    if not prod:
        return "erro: produto não existe (use list_products)"
    if prod["status"] != "pronto":
        return "erro: o produto ainda não está pronto; termine todas as seções antes do texto de venda"
    fields = {k: args.get(k) for k in ("headline", "subtitle", "pitch", "benefits", "audience", "faq")}
    fields = {k: (v if v is not None else ("" if k in ("headline", "subtitle", "pitch", "audience") else [])) for k, v in fields.items()}
    problems = store.check_copy(fields)
    if problems:
        return "erro: texto de venda recusado: " + "; ".join(problems)
    slug = slugify(prod["name"])
    conn.execute(
        "INSERT INTO product_store(product_id,slug,headline,subtitle,pitch,benefits,audience,faq,written_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (product_id) DO UPDATE SET headline=EXCLUDED.headline, subtitle=EXCLUDED.subtitle, pitch=EXCLUDED.pitch, benefits=EXCLUDED.benefits, "
        "audience=EXCLUDED.audience, faq=EXCLUDED.faq, written_by=EXCLUDED.written_by, updated_at=now(), review_status='pendente', review_note='', approved_by=NULL, approved_hash=NULL",
        (prod["id"], slug, fields["headline"].strip(), fields["subtitle"].strip(), fields["pitch"].strip(), json.dumps([b.strip() for b in fields["benefits"]], ensure_ascii=False),
         fields["audience"].strip(), json.dumps([{"q": str(i["q"]).strip(), "a": str(i["a"]).strip()} for i in fields["faq"]], ensure_ascii=False), me["id"]))
    falta = "" if prod["price_cents"] else " Falta definir o preço com set_price."
    return f"texto de venda de '{prod['name']}' salvo; página em /pub/loja/{slug}/ (aparece quando houver preço e a loja estiver identificada).{falta}"


def _review_store(conn, me, name, args):
    from . import store
    prod = _find_product(conn, args["product_name"])
    row = conn.execute("SELECT p.id,p.name,p.status,p.price_cents,s.slug,s.headline,s.subtitle,s.pitch,s.benefits,s.audience,s.faq,s.review_status,s.review_note,s.written_by "
                       "FROM products p JOIN product_store s ON s.product_id=p.id WHERE p.id=%s", (prod["id"],)).fetchone() if prod else None
    if not row:
        return "erro: esse produto ainda não tem texto de venda"
    store._parse_lists(row)
    if name == "read_store_page":
        secs = conn.execute("SELECT title FROM product_sections WHERE product_id=%s ORDER BY position", (row["id"],)).fetchall()
        return cap(f"[{row['review_status']}] {row['name']} · {store.brl(row['price_cents']) if row['price_cents'] else 'SEM PREÇO'}\nTÍTULO: {row['headline']}\nSUBTÍTULO: {row['subtitle']}\n"
                   f"APRESENTAÇÃO: {row['pitch']}\nBENEFÍCIOS: " + " | ".join(row["benefits"]) + f"\nPARA QUEM: {row['audience']}\nFAQ: " + " | ".join(f"{i['q']} -> {i['a']}" for i in row["faq"])
                   + "\nO PRODUTO TEM: " + "; ".join(x["title"] for x in secs) + "\nConfira se cada promessa do texto está nessas seções.", 3000)
    is_pres = re.search(r"presidente", me["role"], re.I)
    is_ceo_ok = re.search(r"\bceo\b", me["role"], re.I) and row["written_by"] is not None and conn.execute("SELECT 1 FROM employees WHERE id=%s AND role ILIKE '%%presidente%%'", (row["written_by"],)).fetchone()
    if not (is_pres or is_ceo_ok):
        return "erro: só a Presidente revisa a página de venda (o CEO só quando a Presidente escreveu o texto)"
    if row["written_by"] == me["id"]:
        return "erro: quem escreveu o texto não pode revisá-lo: peça a outra pessoa"
    if row["status"] != "pronto" or not row["price_cents"]:
        return "erro: o produto precisa estar pronto e ter preço antes da revisão"
    if name == "request_store_rewrite":
        reason = str(args["reason"]).strip()
        if len(reason) < 10:
            return "erro: explique o que reescrever, com pelo menos 10 caracteres"
        conn.execute("UPDATE product_store SET review_status='reescrever', review_note=%s, approved_by=NULL, approved_hash=NULL WHERE product_id=%s", (reason[:400], row["id"]))
        return f"reescrita pedida para a página de '{row['name']}'"
    note = str(args["note"]).strip()
    if len(note) < 20:
        return "erro: registre o que você conferiu (promessas x conteúdo do produto), com pelo menos 20 caracteres"
    conn.execute("UPDATE product_store SET review_status='aprovada', review_note=%s, approved_by=%s, approved_hash=%s WHERE product_id=%s", (note[:400], me["id"], store.page_hash(row), row["id"]))
    conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], f"Página de venda de '{row['name']}' aprovada ({store.brl(row['price_cents'])}). A compra abre quando o token do Mercado Pago estiver verificado."))
    return f"página de '{row['name']}' aprovada; a compra abre quando o resto estiver pronto"


def _set_price(conn, me, args):
    from . import store
    if not REVIEWER_RE.search(me["role"]):
        return "erro: só Presidente ou CEO definem preço"
    prod = _find_product(conn, args["product_name"])
    if not prod:
        return "erro: produto não existe (use list_products)"
    try:
        cents = int(round(float(args["price_brl"]) * 100))
    except (TypeError, ValueError):
        return "erro: preço inválido"
    if cents < store.MIN_PRICE_CENTS or cents > store.MAX_PRICE_CENTS:
        return f"erro: o preço precisa ficar entre {store.brl(store.MIN_PRICE_CENTS)} e {store.brl(store.MAX_PRICE_CENTS)}"
    if len(str(args.get("reason", "")).strip()) < 5:
        return "erro: informe o motivo do preço"
    conn.execute("UPDATE products SET price_cents=%s, updated_at=now() WHERE id=%s", (cents, prod["id"]))
    conn.execute("UPDATE product_store SET review_status='pendente', approved_by=NULL, approved_hash=NULL WHERE product_id=%s", (prod["id"],))    # preço novo: nova revisão
    conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], f"Preço de '{prod['name']}': {store.brl(cents)}. Motivo: {str(args['reason'])[:200]}"))
    return f"preço de '{prod['name']}' definido em {store.brl(cents)}"


def process_drafts(conn):
    """Quando o portão abre, roda os testes dos rascunhos e os manda para revisão (ou recusa). Devolve quantos processou."""
    from . import sandbox
    if not sandbox.enabled():
        return 0
    n = 0
    for t in conn.execute("SELECT * FROM custom_tools WHERE status='rascunho'").fetchall():
        res = sandbox.execute(t["code"], tests=t["tests_code"])
        _log_run(conn, t, {"id": t["author_id"]}, "teste", {}, res)
        if res.get("ok"):
            conn.execute("UPDATE custom_tools SET status='revisao', review_note='', updated_at=now() WHERE id=%s", (t["id"],))
        else:
            conn.execute("UPDATE custom_tools SET status='recusada', review_note=%s, last_error=%s, updated_at=now() WHERE id=%s",
                         ("testes falharam: " + str(res.get("error")), str(res.get("error"))[:500], t["id"]))
        n += 1
    return n


def _approve_tool(conn, me, args):
    from . import sandbox
    if not sandbox.enabled():
        return sandbox.OFF_MSG
    if not REVIEWER_RE.search(me["role"]):
        return "erro: só Presidente ou CEO revisam ferramentas"
    t = conn.execute("SELECT * FROM custom_tools WHERE name=%s", (str(args["name"]).strip(),)).fetchone()
    if not t:
        return "erro: ferramenta não existe"
    if t["author_id"] == me["id"]:
        return "erro: o autor não pode aprovar a própria ferramenta"
    if t["level"] == "C":
        return "erro: ferramenta de nível C só o dono aprova"
    if t["status"] != "revisao":
        return f"erro: a ferramenta está '{t['status']}', não em revisão"
    note = str(args.get("note", ""))[:400]
    if len(note) < 5:
        return "erro: escreva a nota da revisão"
    status = "ativa" if args.get("approve") else "recusada"
    conn.execute("UPDATE custom_tools SET status=%s, reviewed_by=%s, review_note=%s, updated_at=now() WHERE id=%s", (status, me["id"], note, t["id"]))
    return f"ferramenta '{t['name']}' {'aprovada e ATIVA' if status == 'ativa' else 'recusada'}"


def _run_custom_tool(conn, me, args):
    from . import sandbox
    if not sandbox.enabled():
        return sandbox.OFF_MSG
    t = conn.execute("SELECT * FROM custom_tools WHERE name=%s", (str(args["name"]).strip(),)).fetchone()
    if not t:
        return "erro: ferramenta não existe (use list_tools)"
    if t["status"] != "ativa":
        return f"erro: a ferramenta está '{t['status']}'; só ferramentas ativas rodam"
    call_args = args.get("args") if isinstance(args.get("args"), dict) else {}
    res = sandbox.execute(t["code"], args=call_args)
    _log_run(conn, t, me, "uso", call_args, res)
    conn.execute("UPDATE custom_tools SET uses=uses+1, last_error=%s WHERE id=%s", ("" if res.get("ok") else str(res.get("error"))[:500], t["id"]))
    if not res.get("ok"):
        return f"erro na ferramenta: {res.get('error')}"
    return json.dumps(res["result"], ensure_ascii=False) + (f"\n(stdout: {res['stdout'][:500]})" if res.get("stdout") else "")


def run_tool(conn, me, name, args):
    """Executa uma tool para o funcionário `me` (dict). Devolve texto (limitado)."""
    out = cap(_run_tool(conn, me, name, args))
    try:
        _record_activity(conn, me, name, args, out)
    except Exception:  # registrar atividade nunca derruba a tool
        pass
    return out


def activity(conn, emp_id, kind, text="", target_id=None):
    conn.execute("INSERT INTO activity(employee_id,kind,text,target_id) VALUES (%s,%s,%s,%s)", (emp_id, kind, text[:200], target_id))


def _record_activity(conn, me, name, args, out):
    """Grava só o que realmente aconteceu: tool que deu erro ou foi recusada não vira atividade."""
    if out.startswith(("erro", "não criei", "já existe")):
        return
    if name == "web_search":
        activity(conn, me["id"], "pesquisando", f"Pesquisa: {str(args.get('query', ''))[:80]}")
    elif name in ("propose_tool", "approve_tool", "run_custom_tool"):
        activity(conn, me["id"], "planejando", {"propose_tool": "Propôs ferramenta", "approve_tool": "Revisou ferramenta", "run_custom_tool": "Usou ferramenta"}[name] + f" {str(args.get('name', ''))[:40]}")
    elif name in ("propose_opportunity", "update_opportunity"):
        activity(conn, me["id"], "planejando", ("Propôs oportunidade: " + str(args.get("idea", ""))[:70]) if name == "propose_opportunity" else f"Atualizou oportunidade {args.get('id')}")
    elif name in ("read_store_page", "approve_store_page", "request_store_rewrite"):
        activity(conn, me["id"], "planejando", {"read_store_page": "Leu a página de venda de ", "approve_store_page": "Revisou a página de venda de ", "request_store_rewrite": "Pediu reescrita da página de "}[name] + str(args.get("product_name", ""))[:60])
    elif name in ("write_store_copy", "set_price"):
        activity(conn, me["id"], "escrevendo_produto", ("Escreve texto de venda de " if name == "write_store_copy" else "Define preço de ") + str(args.get("product_name", ""))[:60])
    elif name == "write_section":
        activity(conn, me["id"], "escrevendo_produto", f"Escreve {str(args.get('product_name', ''))[:60]}, seção {args.get('section_number')}")
    elif name == "request_rewrite":
        activity(conn, me["id"], "planejando", f"Pediu reescrita da seção {args.get('section_number')}")
    elif name in ("write_product", "read_product"):
        activity(conn, me["id"], "escrevendo_produto", f"{'Escreve' if name == 'write_product' else 'Lê'} {str(args.get('product_name', ''))[:80]}")
    elif name == "post_message":
        to = _emp_by_slug(conn, (args.get("to") or "").strip()) if args.get("to") else None
        activity(conn, me["id"], "conversando", str(args.get("body", ""))[:120], to["id"] if to else None)
    elif name == "start_meeting":
        for sl in args.get("participants") or []:
            e = _emp_by_slug(conn, str(sl))
            if e:
                activity(conn, e["id"], "reunião", str(args.get("topic", ""))[:120])
        activity(conn, me["id"], "reunião", str(args.get("topic", ""))[:120])
    elif name == "team_review":
        activity(conn, me["id"], "planejando", "Registrou a revisão de equipe")
    elif name == "hire_employee":
        activity(conn, me["id"], "contratando", f"Contratou {str(args.get('name', ''))[:60]}")
    elif name == "fire_employee":
        activity(conn, me["id"], "demitindo", f"Demitiu {str(args.get('slug', ''))[:60]}")
    elif name == "reply_owner":
        activity(conn, me["id"], "conversando", "Respondeu ao dono")
    elif name in ("create_task", "complete_task", "ask_owner"):
        activity(conn, me["id"], "planejando", {"create_task": "Criou tarefa", "complete_task": "Concluiu tarefa", "ask_owner": "Pediu algo ao dono"}[name])


def _run_tool(conn, me, name, args):
    try:
        if name == "web_search":
            return web_search(str(args["query"])[:200])
        if name == "list_llm_profiles":
            st = llm.settings(conn)
            rows = conn.execute("SELECT * FROM llm_profiles ORDER BY is_free DESC, id").fetchall()
            head = "MODO CONTINGÊNCIA LIGADO: só perfis grátis são chamados." if st["contingency"] else "Modo contingência desligado: pagos liberados dentro dos tetos diários."
            lines = []
            for r in rows:
                ok, why = llm.usable(conn, r, st)
                price = "grátis" if r["is_free"] else f"US$ {float(r['price_in_per_m']):.2f}/{float(r['price_out_per_m']):.2f} por milhão (entrada/saída)"
                lines.append(f"- {r['label']} ({r['model']}): {price}; " + ("liberado" if ok else f"indisponível: {why}"))
            return head + "\n" + "\n".join(lines)
        if name == "list_team":
            rows = conn.execute("SELECT slug,name,role,area FROM employees WHERE active ORDER BY id").fetchall()
            return "\n".join(f"{r['slug']}: {r['name']} - {r['role']} ({r['area']})" for r in rows)
        if name == "create_task":
            to = _emp_by_slug(conn, args["assignee"])
            if not to:
                return "erro: funcionário não existe"
            for t in conn.execute("SELECT t.id,t.title,e.name FROM tasks t LEFT JOIN employees e ON e.id=t.assignee_id WHERE t.status='open'").fetchall():
                if similar(t["title"], args["title"]):
                    return f"não criei: já existe a tarefa {t['id']} aberta ('{t['title']}', com {t['name']}). Trabalhe nela em vez de repetir."
            r = conn.execute("INSERT INTO tasks(title,details,assignee_id,created_by) VALUES (%s,%s,%s,%s) RETURNING id",
                             (str(args["title"])[:200], str(args.get("details", ""))[:2000], to["id"], me["id"])).fetchone()
            return f"tarefa {r['id']} criada para {to['name']}"
        if name == "complete_task":
            r = conn.execute("UPDATE tasks SET status='done', result=%s, done_at=now() WHERE id=%s AND assignee_id=%s AND status='open' RETURNING id",
                             (str(args["result"])[:4000], int(args["task_id"]), me["id"])).fetchone()
            return "tarefa concluída" if r else "erro: tarefa não é sua ou não está aberta"
        if name == "write_product":
            pname = str(args["product_name"]).strip()[:200]
            content = str(args["content"])[:20000]
            mode = args.get("mode") or "replace"
            status = args.get("status") if args.get("status") in ("rascunho", "revisao", "pronto") else None
            price = int(round(float(args["price_brl"]) * 100)) if args.get("price_brl") not in (None, "") else None
            row = None
            for p in conn.execute("SELECT id,name,content FROM products").fetchall():
                if similar(p["name"], pname):
                    row = p
                    break
            if row and conn.execute("SELECT 1 FROM product_sections WHERE product_id=%s LIMIT 1", (row["id"],)).fetchone():
                return "erro: este produto tem plano de seções; use write_section (uma seção completa por vez)"
            if row:
                new_content = (row["content"] + "\n\n" + content) if mode == "append" and row["content"] else content
                new_content = new_content[:60000]
                conn.execute("UPDATE products SET content=%s, description=COALESCE(NULLIF(%s,''),description), status=COALESCE(%s,status), "
                             "price_cents=COALESCE(%s,price_cents), updated_at=now() WHERE id=%s",
                             (new_content, str(args.get("description", ""))[:1000], status, price, row["id"]))
                pid, shown, total = row["id"], row["name"], len(new_content)
            else:
                fn = slugify(pname) + ".md"
                r = conn.execute("INSERT INTO products(name,description,file_path,created_by,content,status,price_cents) "
                                 "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                                 (pname, str(args.get("description", ""))[:1000], fn, me["id"], content, status or "rascunho", price)).fetchone()
                pid, shown, total = r["id"], pname, len(content)
            try:  # cópia em arquivo para download; o conteúdo oficial está no banco
                os.makedirs(config.PRODUCTS_DIR, exist_ok=True)
                body = conn.execute("SELECT content,file_path FROM products WHERE id=%s", (pid,)).fetchone()
                with open(os.path.join(config.PRODUCTS_DIR, os.path.basename(body["file_path"])), "w", encoding="utf-8") as f:
                    f.write(body["content"])
            except OSError:
                pass
            return f"produto '{shown}' ({'atualizado' if row else 'criado'}): {total} caracteres guardados"
        if name == "read_product":
            for p in conn.execute("SELECT id,name,content,status FROM products").fetchall():
                if similar(p["name"], args["product_name"]):
                    if args.get("section_number"):
                        sec = conn.execute("SELECT title,content,needs_rewrite FROM product_sections WHERE product_id=%s AND position=%s", (p["id"], int(args["section_number"]))).fetchone()
                        if not sec:
                            return "erro: seção não existe"
                        return f"Seção {args['section_number']}: {sec['title']} ({len(sec['content'].split())} palavras{', reescrita pedida' if sec['needs_rewrite'] else ''})\n" + cap(sec["content"] or "(ainda sem texto)", 3000)
                    return f"[{p['status']}] {p['name']} ({len(p['content'])} caracteres)\n" + cap(p["content"], 3000)
            return "erro: produto não existe (use list_products)"
        if name == "list_products":
            rows = conn.execute("SELECT name,status,price_cents,length(content) AS n FROM products ORDER BY id").fetchall()
            return "\n".join(f"{r['name']} | {r['status']} | {'R$ %.2f' % (r['price_cents'] / 100) if r['price_cents'] else 'sem preço'} | {r['n']} caracteres" for r in rows) or "nenhum produto ainda"
        if name == "reply_owner":
            from . import owner_chat
            return owner_chat.reply(conn, me, args["body"])
        if name == "ask_owner":
            body = str(args["body"]).strip()[:300]
            for r in conn.execute("SELECT id,body FROM owner_requests WHERE NOT done").fetchall():
                if similar(r["body"], body):
                    return f"já existe o pedido {r['id']} ao dono; não repita."
            conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (%s,%s)", (me["id"], body))
            return "pedido registrado para o dono"
        if name == "propose_tool":
            return _propose_tool(conn, me, args)
        if name == "list_tools":
            rows = conn.execute("SELECT t.name,t.version,t.level,t.status,t.description,t.uses,a.name AS autor FROM custom_tools t LEFT JOIN employees a ON a.id=t.author_id ORDER BY t.id").fetchall()
            return "\n".join(f"{r['name']} v{r['version']} | nível {r['level']} | {r['status']} | {r['autor']} | {r['uses']} usos | {r['description'][:80]}" for r in rows) or "nenhuma ferramenta ainda"
        if name == "read_tool":
            t = conn.execute("SELECT * FROM custom_tools WHERE name=%s", (str(args["name"]).strip(),)).fetchone()
            if not t:
                return "erro: ferramenta não existe"
            return f"{t['name']} v{t['version']} [{t['status']}, nível {t['level']}] {t['reason']}\n--- código ---\n{t['code']}\n--- testes ---\n{t['tests_code']}"
        if name == "approve_tool":
            return _approve_tool(conn, me, args)
        if name == "run_custom_tool":
            return _run_custom_tool(conn, me, args)
        if name == "propose_opportunity":
            return _propose_opportunity(conn, me, args)
        if name == "update_opportunity":
            return _update_opportunity(conn, me, args)
        if name == "list_opportunities":
            rows = conn.execute("SELECT id,idea,status,effort,risk,needs_owner FROM opportunities ORDER BY id").fetchall()
            return "\n".join(f"[{r['id']}] ({r['status']}) {r['idea'][:80]} | esforço {r['effort']} | risco {r['risk']}" + (f" | precisa do dono: {r['needs_owner'][:60]}" if r["needs_owner"] else "") for r in rows) or "a mesa está vazia"
        if name in ("read_store_page", "approve_store_page", "request_store_rewrite"):
            return _review_store(conn, me, name, args)
        if name == "write_store_copy":
            return _write_store_copy(conn, me, args)
        if name == "set_price":
            return _set_price(conn, me, args)
        if name == "write_section":
            prod, sec = _find_section(conn, args["product_name"], args["section_number"])
            if not sec:
                return prod or "erro: seção não existe"
            content = str(args["content"]).strip()[:40000]
            words = len(content.split())
            if words < config.MIN_SECTION_WORDS:
                return f"erro: a seção tem {words} palavras; o mínimo é {config.MIN_SECTION_WORDS}. Escreva a seção COMPLETA, mais longa e detalhada, e envie de novo."
            conn.execute("UPDATE product_sections SET content=%s, needs_rewrite=false, rewrite_reason='', written_by=%s, written_at=now() WHERE id=%s",
                         (content, me["id"], sec["id"]))
            return _after_section_saved(conn, prod, me, sec, words)
        if name == "request_rewrite":
            prod, sec = _find_section(conn, args["product_name"], args["section_number"])
            if not sec:
                return prod or "erro: seção não existe"
            if not sec["content"]:
                return "erro: a seção ainda não foi escrita"
            reason = str(args["reason"]).strip()[:400]
            if len(reason) < 5:
                return "erro: informe o motivo"
            conn.execute("UPDATE product_sections SET needs_rewrite=true, rewrite_reason=%s WHERE id=%s", (reason, sec["id"]))
            conn.execute("UPDATE products SET status='rascunho', updated_at=now() WHERE id=%s", (prod["id"],))
            return f"reescrita pedida para a seção {sec['position']}"
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
        if name == "team_review":
            from . import team
            if not FOUNDER_RE.search(me["role"]):
                return "erro: só Presidente ou CEO registram a revisão de equipe"
            st = team.status(conn)
            if st["below_target"]:
                return f"erro: a equipe tem {st['headcount']} pessoa(s) e a meta mínima é {st['target']}: contrate antes (hire_employee, um por turno) e registre a revisão depois"
            if not st["review_due"]:
                return f"erro: a próxima revisão só vale em {st['next_review_in']} turno(s)"
            summary = str(args["summary"]).strip()
            if len(summary) < 40:
                return "erro: a revisão precisa de um resumo (quem entrega, quem não, o que muda), com pelo menos 40 caracteres"
            if st["overloaded"]:   # estado real: tarefas demais por pessoa. Só destrava contratando ou aliviando a fila
                return (f"erro: há {st['open_tasks']} tarefas abertas para {st['headcount']} pessoas (mais de {config.MAX_TASKS_PER_PERSON} por pessoa): contrate um especialista "
                        "ou conclua/cancele tarefas antes de registrar a revisão")
            conn.execute("INSERT INTO team_reviews(turn_no,by_id,headcount,open_tasks,summary) VALUES (%s,%s,%s,%s,%s)", (st["turns"], me["id"], st["headcount"], st["open_tasks"], summary[:1000]))
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], f"Revisão de equipe ({st['headcount']} pessoas, {st['open_tasks']} tarefas abertas): {summary[:300]}"))
            return "revisão de equipe registrada"
        if name == "fire_employee":
            if not HIRER_RE.search(me["role"]):
                return "erro: seu cargo não pode demitir"
            target = _emp_by_slug(conn, str(args["slug"]).strip())
            if not target:
                return "erro: funcionário não existe ou já foi demitido"
            if target["id"] == me["id"]:
                return "erro: você não pode demitir a si mesmo"
            if FOUNDER_RE.search(target["role"]):
                return "erro: Presidente e CEO não podem ser demitidos por agentes"
            reason = str(args["reason"]).strip()[:500]
            if len(reason) < 5:
                return "erro: informe o motivo"
            boss = conn.execute("SELECT id FROM employees WHERE id=%s AND active", (target["hired_by"],)).fetchone() if target["hired_by"] else None
            boss_id = boss["id"] if boss and boss["id"] != target["id"] else me["id"]
            conn.execute("UPDATE employees SET active=false, status='idle', activity='', fired_at=now(), fire_reason=%s, fired_by=%s WHERE id=%s",
                         (reason, me["id"], target["id"]))
            moved = conn.execute("UPDATE tasks SET assignee_id=%s WHERE assignee_id=%s AND status='open' RETURNING id", (boss_id, target["id"])).fetchall()
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)",
                         (me["id"], f"Demitiu {target['name']} ({target['role']}). Motivo: {reason[:300]}"))
            return f"{target['name']} demitido; {len(moved)} tarefa(s) abertas passaram para o chefe"
        if name == "start_meeting":
            names = []
            for sl in (args.get("participants") or [])[:6]:
                e = _emp_by_slug(conn, str(sl))
                if e and e["id"] != me["id"]:
                    names.append(e["name"])
            if not names:
                return "erro: nenhum participante válido"
            conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)",
                         (me["id"], f"Reunião com {', '.join(names)}: {str(args['topic'])[:200]}"))
            return "reunião registrada"
        if name == "hire_employee":
            if not HIRER_RE.search(me["role"]):
                return "erro: seu cargo não pode contratar"
            n = conn.execute("SELECT COUNT(*) AS n FROM employees WHERE active").fetchone()["n"]
            if n >= config.MAX_EMPLOYEES:
                return "erro: limite de funcionários atingido"
            if len(str(args.get("instructions", "")).strip()) < 60:
                return "erro: instruções incompletas (mínimo 60 caracteres): descreva a função, como a pessoa trabalha, o que entrega e como se mede"
            if len(str(args.get("reason", "")).strip()) < 10:
                return "erro: informe o motivo da contratação com clareza"
            if len(str(args.get("deliverable", "")).strip()) < 10:
                return "erro: toda contratação precisa de uma entrega mensurável (ex.: '2 artigos por dia')"
            slug = slugify(args["name"])
            if _emp_by_slug(conn, slug):
                slug = f"{slug}-{n + 1}"
            model = config.DEFAULT_ROTATION[n % len(config.DEFAULT_ROTATION)]
            prof = None
            if str(args.get("llm_profile") or "").strip():
                prof, err = llm.allowed_for_hire(conn, args["llm_profile"])
                if err:
                    return "erro: " + err
            elif DECISION_RE.search(str(args["role"])):   # cargos de decisão pegam o melhor perfil liberado dentro do teto
                prof = llm.best_profile(conn)
            else:
                prof = conn.execute("SELECT * FROM llm_profiles WHERE is_free AND enabled AND model=%s ORDER BY id LIMIT 1", (model,)).fetchone()
            if prof:
                model = prof["model"]
            conn.execute(
                "INSERT INTO employees(slug,name,role,area,instructions,model,llm_profile_id,hired_by,hire_reason,deliverable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (slug, str(args["name"])[:60], str(args["role"])[:80], str(args["area"])[:80],
                 str(args["instructions"])[:3000], model, prof["id"] if prof else None, me["id"], str(args["reason"])[:1000], str(args["deliverable"])[:300]))
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
