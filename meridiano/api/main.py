"""API local (127.0.0.1) da Meridiano. Leitura exige MERIDIANO_API_TOKEN; lançar receita exige MERIDIANO_ADMIN_TOKEN."""
import hmac
import os
import pathlib
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from ..engine import config, db, llm

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
    return {"falhas_hoje": falhas, "ultimo_erro": ultimo_erro, "caixa": db.cash_cents(c) / 100, "vendas": sales, "agentes": n, "modo": "real",
            "uso_hoje": {"chamadas": calls, "tokens": tokens,
                         "limite_chamadas": config.DAILY_CALL_LIMIT, "limite_tokens": config.DAILY_TOKEN_LIMIT}}


@app.get("/api/employees", dependencies=[Depends(read_auth)])
def employees():
    return conn().execute(
        "SELECT e.id,e.slug,e.name,e.role,e.area,e.model,e.status,e.activity,e.hire_reason,h.name AS hired_by "
        "FROM employees e LEFT JOIN employees h ON h.id=e.hired_by WHERE e.active ORDER BY e.id").fetchall()


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
    return conn().execute("SELECT id,name,description,file_path,created_at FROM products ORDER BY id DESC").fetchall()


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
