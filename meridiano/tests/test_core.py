import os
import subprocess
import time
import pytest
from fastapi.testclient import TestClient

PGBIN = "/usr/lib/postgresql/16/bin"


@pytest.fixture(scope="module")
def url(tmp_path_factory):
    d = tmp_path_factory.mktemp("pg")
    if not os.path.isdir(PGBIN) or os.geteuid() == 0:
        if not os.environ.get("TEST_DATABASE_URL"):
            pytest.skip("sem Postgres de teste")
        yield os.environ["TEST_DATABASE_URL"]
        return
    subprocess.run([f"{PGBIN}/initdb", "-D", str(d), "-A", "trust"], check=True, capture_output=True)
    subprocess.run([f"{PGBIN}/pg_ctl", "-D", str(d), "-o", f"-p 54399 -k {d}", "-w", "start"], check=True, capture_output=True)
    subprocess.run([f"{PGBIN}/createdb", "-h", str(d), "-p", "54399", "t"], check=True)
    yield "postgresql://localhost:54399/t"
    subprocess.run([f"{PGBIN}/pg_ctl", "-D", str(d), "-m", "immediate", "stop"], capture_output=True)


@pytest.fixture()
def conn(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    from meridiano.engine import db
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    db.init_schema(c)
    return c


def test_cash_starts_zero_and_agents_cannot_touch_it(conn):
    from meridiano.engine import db, seed, tools
    seed.seed(conn)
    assert db.cash_cents(conn) == 0
    names = {s["name"] for s in tools.SPECS}
    assert not any("cash" in n or "ledger" in n or "sale" in n or "money" in n for n in names)
    me = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    assert tools.run_tool(conn, me, "register_sale", {"amount": 99}).startswith("erro")
    assert db.cash_cents(conn) == 0


def test_hire_rules_and_model_whitelist(conn):
    from meridiano.engine import config, seed, tools
    seed.seed(conn)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    out = tools.run_tool(conn, ceo, "hire_employee", {"name": "Luiza", "role": "Gerente de Conteúdo", "area": "Conteúdo", "instructions": "x", "reason": "falta conteúdo"})
    assert out.startswith("contratado")
    luiza = conn.execute("SELECT * FROM employees WHERE slug='luiza'").fetchone()
    assert luiza["model"] in config.ALLOWED_MODELS and luiza["hire_reason"] and luiza["hired_by"] == ceo["id"]
    out = tools.run_tool(conn, luiza, "hire_employee", {"name": "Z", "role": "Estagiário", "area": "A", "instructions": "x", "reason": "r"})
    assert out.startswith("contratado")  # gerente pode
    z = conn.execute("SELECT * FROM employees WHERE slug='z'").fetchone()
    assert tools.run_tool(conn, z, "hire_employee", {"name": "Y", "role": "R", "area": "A", "instructions": "x", "reason": "r"}).startswith("erro")


def test_llm_rejects_unlisted_model_and_budget(conn, monkeypatch):
    from meridiano.engine import llm
    with pytest.raises(ValueError):
        llm.chat(conn, None, "openai/gpt-4o", [])
    monkeypatch.setattr("meridiano.engine.config.DAILY_CALL_LIMIT", 0)
    with pytest.raises(llm.BudgetExceeded):
        llm.chat(conn, None, "poolside/laguna-s-2.1:free", [])


def test_agent_turn_with_fake_llm(conn, tmp_path, monkeypatch):
    from meridiano.engine import agent, config, seed
    monkeypatch.setattr(config, "PRODUCTS_DIR", str(tmp_path))
    seed.seed(conn)
    me = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    steps = iter([
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "create_task", "arguments": '{"assignee":"presidente","title":"Plano"}'}}]},
        {"role": "assistant", "content": "Feito."},
    ])
    agent.run_turn(conn, me, chat=lambda *a, **k: next(steps))
    assert conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1
    assert conn.execute("SELECT status FROM employees WHERE slug='ceo'").fetchone()["status"] == "idle"


def test_api_auth_and_ledger(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura")
    monkeypatch.setenv("MERIDIANO_ADMIN_TOKEN", "admin")
    from meridiano.api import main
    main._conn = conn
    c = TestClient(main.app)
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"X-Token": "leitura"}).json()["caixa"] == 0
    body = {"kind": "receita", "amount_cents": 4990, "description": "Venda real Hotmart", "external_ref": "HP1"}
    assert c.post("/api/ledger", json=body, headers={"X-Token": "leitura"}).status_code == 401
    r = c.post("/api/ledger", json=body, headers={"X-Admin-Token": "admin"})
    assert r.json()["caixa"] == 49.9
    assert c.post("/api/ledger", json=body, headers={"X-Admin-Token": "admin"}).status_code == 409


def test_deploy_log_route(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura")
    log = tmp_path / "deploy.log"
    log.write_text("\n".join(f"linha {i}" for i in range(80)))
    monkeypatch.setenv("DEPLOY_LOG", str(log))
    from meridiano.api import main
    main._conn = conn
    c = TestClient(main.app)
    assert c.get("/api/deploy-log").status_code == 401
    r = c.get("/api/deploy-log", headers={"X-Token": "leitura"}).json()
    assert len(r["linhas"]) == 50 and r["linhas"][-1] == "linha 79"


def test_empty_response_is_error_and_does_not_use_budget(conn, monkeypatch):
    from meridiano.engine import llm
    monkeypatch.setenv("NOUS_API_KEY", "x")

    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"role": "assistant", "content": None, "reasoning": "..."}}], "usage": {"prompt_tokens": 100, "completion_tokens": 3500}}
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: R())
    with pytest.raises(llm.EmptyResponse):
        llm.chat(conn, None, "poolside/laguna-s-2.1:free", [])
    assert llm.usage_today(conn) == (0, 0)
    n, err = llm.failures_today(conn)
    assert n == 1 and "vazia" in err


def test_migrations_idempotent_and_clean_old_empty_usage(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    from meridiano.engine import db, migrate
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    # estado "antigo": só a migração 001, com 2 chamadas vazias e 1 útil de hoje
    real = migrate.DIR
    only1 = sorted(real.glob("001_*.sql"))[0]
    c.execute(only1.read_text())
    c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    c.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (only1.name,))
    c.execute("INSERT INTO employees(slug,name,role,area,instructions,model) VALUES ('a','A','CEO','D','x','m'),('b','B','CEO','D','x','m')")
    c.execute("INSERT INTO usage_log(employee_id,model,prompt_tokens,completion_tokens) SELECT id,'m',100,3500 FROM employees, generate_series(1,2)")
    c.execute("INSERT INTO messages(from_id,body) SELECT id,'oi' FROM employees WHERE slug='b'")
    assert migrate.migrate(c) == ["002_zera_uso_vazio.sql"]
    assert migrate.migrate(c) == []
    ok = c.execute("SELECT e.slug FROM usage_log u JOIN employees e ON e.id=u.employee_id WHERE u.ok GROUP BY 1").fetchall()
    assert [r["slug"] for r in ok] == ["b"]  # b tem mensagem gravada: chamadas dele eram úteis
