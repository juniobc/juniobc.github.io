"""API local (127.0.0.1) da Meridiano. Leitura exige MERIDIANO_API_TOKEN; lançar receita exige MERIDIANO_ADMIN_TOKEN."""
import hmac
import os
import pathlib
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from ..engine import config, db, llm, secrets_store

app = FastAPI(title="Meridiano Digital")
_conn = None


def conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
        db.init_schema(_conn)
    return _conn


def _check(given, env_name):
    want = config.env(env_name)
    if not want or not given or not hmac.compare_digest(given, want):
        raise HTTPException(401, "token inválido")


def read_auth(x_token: str = Header(None), token: str = Query(None)):
    _check(x_token or token, "MERIDIANO_API_TOKEN")


def admin_auth(x_admin_token: str = Header(None)):
    _check(x_admin_token, "MERIDIANO_ADMIN_TOKEN")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(read_auth)])
def state():
    c = conn()
    calls, tokens = llm.usage_today(c)
    sales = c.execute("SELECT COUNT(*) AS n FROM ledger WHERE kind='receita'").fetchone()["n"]
    n = c.execute("SELECT COUNT(*) AS n FROM employees WHERE active").fetchone()["n"]
    falhas, ultimo_erro = llm.failures_today(c)
    reset = c.execute("SELECT date_trunc('day', now()) + interval '1 day' AS r").fetchone()["r"]
    return {"orcamento_pct": round(100 * llm.budget_fraction(c), 1), "orcamento_zera_em": reset.isoformat(),
            "falhas_hoje": falhas, "ultimo_erro": ultimo_erro, "caixa": db.cash_cents(c) / 100, "vendas": sales, "agentes": n, "modo": "real",
            "uso_hoje": {"chamadas": calls, "tokens": tokens,
                         "limite_chamadas": config.DAILY_CALL_LIMIT, "limite_tokens": config.DAILY_TOKEN_LIMIT}}


@app.get("/api/engine", dependencies=[Depends(read_auth)])
def engine_status():
    """O motor está vivo? Quem trabalha, quando foi o último turno, quando é o próximo e por que espera."""
    r = conn().execute(
        "SELECT es.wait_reason, es.sleep_s, "
        "extract(epoch FROM now() - es.heartbeat_at) AS since_beat, extract(epoch FROM es.next_turn_at - now()) AS next_in, "
        "es.last_turn_at, es.isolation, es.tools_enabled, es.tools_reason, es.tools_help, es.tools_backend, l.name AS ultimo_por, c.name AS da_vez "
        "FROM engine_status es LEFT JOIN employees l ON l.id=es.last_turn_employee_id LEFT JOIN employees c ON c.id=es.current_employee_id WHERE es.id=1").fetchone()
    if not r or r["since_beat"] is None:
        return {"vivo": False, "motivo_espera": "motor nunca iniciou", "proximo_turno_em_s": None, "ultimo_turno_em": None, "ultimo_turno_por": None, "funcionario_da_vez": None}
    return {"vivo": float(r["since_beat"]) < r["sleep_s"] + 180, "motivo_espera": r["wait_reason"],
            "proximo_turno_em_s": max(0, round(float(r["next_in"]))), "ultimo_turno_em": r["last_turn_at"].isoformat() if r["last_turn_at"] else None,
            "ultimo_turno_por": r["ultimo_por"], "funcionario_da_vez": r["da_vez"],
            "limites": {"chamadas": config.DAILY_CALL_LIMIT, "tokens": config.DAILY_TOKEN_LIMIT}, "isolamento_ferramentas": {"ligadas": r["tools_enabled"], "motivo": r["tools_reason"], "o_que_falta": r["tools_help"], "backend": r["tools_backend"], "detalhe": r["isolation"]}}


@app.get("/api/employees", dependencies=[Depends(read_auth)])
def employees():
    return conn().execute(
        "SELECT e.id,e.slug,e.name,e.role,e.area,e.model,e.status,e.activity,e.hire_reason,h.name AS hired_by, "
        "(SELECT COALESCE(SUM(u.prompt_tokens+u.completion_tokens),0) FROM usage_log u WHERE u.employee_id=e.id "
        "AND u.created_at >= date_trunc('day', now())) AS tokens_hoje, "
        "(SELECT COUNT(*) FROM usage_log u WHERE u.employee_id=e.id AND u.created_at >= date_trunc('day', now())) AS chamadas_hoje, "
        "e.created_at AS contratado_em, "
        "(SELECT a.kind FROM activity a WHERE a.employee_id=e.id ORDER BY a.id DESC LIMIT 1) AS atividade_tipo, "
        "(SELECT a.text FROM activity a WHERE a.employee_id=e.id ORDER BY a.id DESC LIMIT 1) AS atividade_texto, "
        "(SELECT a.started_at FROM activity a WHERE a.employee_id=e.id ORDER BY a.id DESC LIMIT 1) AS atividade_em, "
        "(SELECT t.slug FROM activity a LEFT JOIN employees t ON t.id=a.target_id WHERE a.employee_id=e.id ORDER BY a.id DESC LIMIT 1) AS atividade_alvo "
        "FROM employees e LEFT JOIN employees h ON h.id=e.hired_by WHERE e.active ORDER BY e.id").fetchall()


@app.get("/api/activity", dependencies=[Depends(read_auth)])
def activity(limit: int = Query(40, le=200)):
    return conn().execute(
        "SELECT a.id,a.kind,a.text,a.started_at,e.slug,e.name,t.slug AS target_slug,t.name AS target_name "
        "FROM activity a JOIN employees e ON e.id=a.employee_id LEFT JOIN employees t ON t.id=a.target_id "
        "ORDER BY a.id DESC LIMIT %s", (limit,)).fetchall()


@app.get("/api/history", dependencies=[Depends(read_auth)])
def history():
    """Contratações e demissões, com motivo."""
    return conn().execute(
        "SELECT * FROM ("
        " SELECT e.created_at AS at, 'contratado' AS tipo, e.name, e.role, e.hire_reason AS motivo, h.name AS por"
        "  FROM employees e LEFT JOIN employees h ON h.id=e.hired_by"
        " UNION ALL"
        " SELECT e.fired_at, 'demitido', e.name, e.role, e.fire_reason, f.name"
        "  FROM employees e LEFT JOIN employees f ON f.id=e.fired_by WHERE e.fired_at IS NOT NULL"
        ") x ORDER BY at DESC LIMIT 50").fetchall()


@app.get("/api/employees/{slug}", dependencies=[Depends(read_auth)])
def employee_card(slug: str):
    c = conn()
    e = c.execute("SELECT id,slug,name,role,area,model,status,activity,hire_reason,active FROM employees WHERE slug=%s", (slug,)).fetchone()
    if not e:
        raise HTTPException(404, "não existe")
    e["mensagens"] = c.execute(
        "SELECT m.id,m.body,m.created_at,t.name AS to_name FROM messages m LEFT JOIN employees t ON t.id=m.to_id "
        "WHERE m.from_id=%s ORDER BY m.id DESC LIMIT 5", (e["id"],)).fetchall()
    e["tarefas_abertas"] = c.execute("SELECT id,title FROM tasks WHERE assignee_id=%s AND status='open' ORDER BY id", (e["id"],)).fetchall()
    e["ultima_atividade"] = c.execute("SELECT kind,text,started_at FROM activity WHERE employee_id=%s ORDER BY id DESC LIMIT 1", (e["id"],)).fetchone()
    return e


@app.get("/api/messages", dependencies=[Depends(read_auth)])
def messages(limit: int = Query(40, le=200)):
    return conn().execute(
        "SELECT m.id,m.body,m.created_at,f.slug AS from_slug,f.name AS from_name,t.slug AS to_slug,t.name AS to_name "
        "FROM messages m LEFT JOIN employees f ON f.id=m.from_id LEFT JOIN employees t ON t.id=m.to_id "
        "ORDER BY m.id DESC LIMIT %s", (limit,)).fetchall()


@app.get("/api/tasks", dependencies=[Depends(read_auth)])
def tasks(limit: int = Query(50, le=200)):
    return conn().execute(
        "SELECT k.id,k.title,k.status,k.result,k.created_at,a.name AS assignee FROM tasks k "
        "LEFT JOIN employees a ON a.id=k.assignee_id ORDER BY k.id DESC LIMIT %s", (limit,)).fetchall()


@app.get("/api/products", dependencies=[Depends(read_auth)])
def products():
    return conn().execute(
        "SELECT p.id,p.name,p.description,p.file_path,p.content,p.status,p.price_cents,p.updated_at, "
        "(SELECT COUNT(*) FROM product_sections s WHERE s.product_id=p.id) AS secoes_total, "
        "(SELECT COUNT(*) FROM product_sections s WHERE s.product_id=p.id AND s.content<>'' AND NOT s.needs_rewrite) AS secoes_prontas, "
        "length(p.content) AS caracteres "
        "FROM products p ORDER BY p.id DESC").fetchall()


@app.get("/api/ledger", dependencies=[Depends(read_auth)])
def ledger():
    return conn().execute("SELECT id,kind,amount_cents,description,source,created_at FROM ledger ORDER BY id DESC LIMIT 100").fetchall()


@app.get("/api/deploy-log", dependencies=[Depends(read_auth)])
def deploy_log(lines: int = Query(50, ge=1, le=50)):
    """Últimas linhas do log do deploy automático (para o Claude ver o resultado de cada push)."""
    path = config.env("DEPLOY_LOG", os.path.join(os.path.expanduser("~"), "meridiano", "deploy.log"))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.read().splitlines()[-lines:]
    except FileNotFoundError:
        tail = []
    return {"path": path, "linhas": tail}


@app.get("/api/owner-requests", dependencies=[Depends(read_auth)])
def owner_requests():
    return conn().execute(
        "SELECT r.id,r.body,r.done,r.created_at,e.name AS from_name FROM owner_requests r "
        "LEFT JOIN employees e ON e.id=r.from_id ORDER BY r.done, r.id DESC LIMIT 30").fetchall()


@app.post("/api/owner-requests/{rid}/done", dependencies=[Depends(read_auth)])  # não mexe em dinheiro
def owner_request_done(rid: int):
    conn().execute("UPDATE owner_requests SET done=true WHERE id=%s", (rid,))
    return {"ok": True}


class MPToken(BaseModel):
    access_token: str = Field(min_length=10, max_length=400)


@app.post("/api/secrets/mp-token", dependencies=[Depends(admin_auth)])
def set_mp_token(body: MPToken):
    """Grava o token do Mercado Pago (arquivo 600). Nunca devolve nem registra o token."""
    ok, msg, meta = secrets_store.save_token(body.access_token)
    if not ok:
        raise HTTPException(400, msg)
    secrets_store.sync_owner_request(conn())
    return {"configurado": True, "modo": meta["modo"], "verificado": meta["verificado"], "mensagem": msg}


@app.get("/api/secrets/mp-token", dependencies=[Depends(read_auth)])
def mp_token_status():
    return secrets_store.status()


class Entry(BaseModel):
    kind: str = Field(pattern="^(receita|despesa)$")
    amount_cents: int = Field(gt=0)
    description: str = Field(min_length=3, max_length=300)
    external_ref: str | None = Field(None, max_length=120)


@app.post("/api/ledger", dependencies=[Depends(admin_auth)])
def add_entry(e: Entry):
    """Só o dono (ou integração de pagamento com o token de admin) lança valores recebidos."""
    c = conn()
    r = c.execute("INSERT INTO ledger(kind,amount_cents,description,source,external_ref) VALUES (%s,%s,%s,'dono',%s) "
                  "ON CONFLICT DO NOTHING RETURNING id", (e.kind, e.amount_cents, e.description, e.external_ref)).fetchone()
    if not r:
        raise HTTPException(409, "lançamento duplicado (external_ref já existe)")
    return {"id": r["id"], "caixa": db.cash_cents(c) / 100}


_web = pathlib.Path(__file__).parent.parent / "web" / "dist"
if _web.is_dir():
    app.mount("/", StaticFiles(directory=_web, html=True), name="web")
