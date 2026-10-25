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


def test_llm_rejects_unlisted_model(conn):
    from meridiano.engine import llm
    with pytest.raises(ValueError):
        llm.chat(conn, None, "openai/gpt-4o", [])


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
        status_code = 200
        headers = {}
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
    assert migrate.migrate(c) == ["002_zera_uso_vazio.sql", "003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql"]
    assert migrate.migrate(c) == []
    ok = c.execute("SELECT e.slug FROM usage_log u JOIN employees e ON e.id=u.employee_id WHERE u.ok GROUP BY 1").fetchall()
    assert [r["slug"] for r in ok] == ["b"]  # b tem mensagem gravada: chamadas dele eram úteis


def _ceo(conn):
    from meridiano.engine import seed
    seed.seed(conn)
    return conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()


def test_tasks_and_products_are_idempotent(conn, tmp_path, monkeypatch):
    from meridiano.engine import config, tools
    monkeypatch.setattr(config, "PRODUCTS_DIR", str(tmp_path))
    ceo = _ceo(conn)
    assert "criada" in tools.run_tool(conn, ceo, "create_task", {"assignee": "presidente", "title": "Pesquisar copy de vendas"})
    r = tools.run_tool(conn, ceo, "create_task", {"assignee": "ceo", "title": "pesquisar  copy de vendas!"})
    assert "não criei" in r and conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 1
    tools.run_tool(conn, ceo, "write_product", {"product_name": "Toolkit do Vendedor", "content": "parte 1", "price_brl": 29.9})
    tools.run_tool(conn, ceo, "write_product", {"product_name": "toolkit do vendedor", "content": "parte 2", "mode": "append"})
    tools.run_tool(conn, ceo, "write_product", {"product_name": "Toolkit do Vendedor", "content": "x", "mode": "append", "status": "pronto"})
    rows = conn.execute("SELECT * FROM products").fetchall()
    assert len(rows) == 1 and rows[0]["content"] == "parte 1\n\nparte 2\n\nx"
    assert rows[0]["price_cents"] == 2990 and rows[0]["status"] == "pronto"
    assert "parte 1" in tools.run_tool(conn, ceo, "read_product", {"product_name": "Toolkit do Vendedor"})
    assert "já existe" in (tools.run_tool(conn, ceo, "ask_owner", {"body": "Abrir conta na Hotmart com CPF"}) and
                           tools.run_tool(conn, ceo, "ask_owner", {"body": "abrir conta na hotmart com cpf"}))


def test_agent_echo_is_clean(conn):
    from meridiano.engine import agent
    ceo = _ceo(conn)
    helena = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    seen = {}

    def fake(c, eid, model, messages, tools):
        seen["n"] = len(messages)
        if len(messages) == 2:
            return {"role": "assistant", "content": None, "reasoning": "pensando...", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "list_products", "arguments": "{}"}}]}
        seen["echo"] = messages[2]
        return {"role": "assistant", "content": "ok"}
    agent.run_turn(conn, ceo, chat=fake)
    assert set(seen["echo"]) == {"role", "content", "tool_calls"} and seen["echo"]["content"] == ""


def test_http_error_body_is_logged(conn, monkeypatch):
    from meridiano.engine import llm
    import httpx
    monkeypatch.setenv("NOUS_API_KEY", "segredo123")

    def boom(*a, **k):
        req = httpx.Request("POST", "http://x")
        raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, text='{"error":"missing tags"}', request=req))
    monkeypatch.setattr(llm.httpx, "post", boom)
    with pytest.raises(httpx.HTTPStatusError):
        llm.chat(conn, None, "poolside/laguna-s-2.1:free", [])
    err = llm.failures_today(conn)[1]
    assert "missing tags" in err and "400" in err and "segredo123" not in err


def test_migration_003_cleans_duplicates(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    from meridiano.engine import db, migrate
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    for f in sorted(migrate.DIR.glob("00[12]_*.sql")):
        c.execute(f.read_text())
    c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    c.execute("INSERT INTO schema_migrations(name) VALUES ('001_schema.sql'),('002_zera_uso_vazio.sql')")
    for _ in range(3):
        c.execute("INSERT INTO products(name,file_path) VALUES ('Toolkit X','a.txt')")
        c.execute("INSERT INTO tasks(title) VALUES ('Pesquisar copy')")
    c.execute("INSERT INTO tasks(title,details) VALUES ('Receber fontes','mandar os arquivos .txt fonte')")
    assert migrate.migrate(c) == ["003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql"]
    assert c.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"] == 1
    assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"] == 1


def test_fire_rules_and_activity(conn, tmp_path, monkeypatch):
    from meridiano.engine import config, tools
    monkeypatch.setattr(config, "PRODUCTS_DIR", str(tmp_path))
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    hire = lambda n, r: tools.run_tool(conn, ceo, "hire_employee", {"name": n, "role": r, "area": "A", "instructions": "x", "reason": "falta função"})
    hire("Luiza", "Gerente de Conteúdo"); hire("Davi", "Redator")
    luiza = conn.execute("SELECT * FROM employees WHERE slug='luiza'").fetchone()
    davi = conn.execute("SELECT * FROM employees WHERE slug='davi'").fetchone()
    # regras
    assert tools.run_tool(conn, davi, "fire_employee", {"slug": "luiza", "reason": "teste teste"}).startswith("erro")   # redator não demite
    assert tools.run_tool(conn, ceo, "fire_employee", {"slug": "presidente", "reason": "teste teste"}).startswith("erro")  # fundador
    assert tools.run_tool(conn, luiza, "fire_employee", {"slug": "luiza", "reason": "teste teste"}).startswith("erro")     # a si mesma
    assert tools.run_tool(conn, ceo, "fire_employee", {"slug": "davi", "reason": "x"}).startswith("erro")                  # sem motivo
    # demissão com tarefa aberta passando para o chefe (quem contratou = CEO)
    tools.run_tool(conn, ceo, "create_task", {"assignee": "davi", "title": "Escrever guia de planilhas"})
    out = tools.run_tool(conn, luiza, "fire_employee", {"slug": "davi", "reason": "função não é mais necessária"})
    assert "demitido" in out and "1 tarefa" in out
    d = conn.execute("SELECT * FROM employees WHERE slug='davi'").fetchone()
    assert not d["active"] and d["fired_at"] and d["fire_reason"] and d["fired_by"] == luiza["id"]
    assert conn.execute("SELECT assignee_id FROM tasks").fetchone()["assignee_id"] == ceo["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM messages WHERE body LIKE 'Demitiu Davi%'").fetchone()["n"] == 1
    # atividade só do que aconteceu
    tools.run_tool(conn, ceo, "post_message", {"to": "presidente", "body": "Plano pronto"})
    tools.run_tool(conn, ceo, "write_product", {"product_name": "Guia", "content": "texto"})
    tools.run_tool(conn, ceo, "post_message", {"to": "ninguem", "body": "x"})  # erro: não registra
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM activity WHERE employee_id=%s ORDER BY id", (ceo["id"],)).fetchall()]
    assert kinds.count("conversando") == 1 and "escrevendo_produto" in kinds and "contratando" in kinds
    tgt = conn.execute("SELECT target_id FROM activity WHERE kind='conversando'").fetchone()["target_id"]
    assert tgt == pres["id"]
    tools.run_tool(conn, ceo, "start_meeting", {"participants": ["presidente", "luiza"], "topic": "Preço do guia"})
    assert conn.execute("SELECT COUNT(*) AS n FROM activity WHERE kind='reunião'").fetchone()["n"] == 3


def test_api_activity_history_card(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    from meridiano.engine import tools
    from meridiano.api import main
    ceo = _ceo(conn)
    tools.run_tool(conn, ceo, "hire_employee", {"name": "Luiza", "role": "Gerente", "area": "A", "instructions": "x", "reason": "r"})
    tools.run_tool(conn, ceo, "post_message", {"to": "luiza", "body": "Bem-vinda"})
    main._conn = conn
    c = TestClient(main.app); h = {"X-Token": "t"}
    emps = {e["slug"]: e for e in c.get("/api/employees", headers=h).json()}
    assert emps["ceo"]["atividade_tipo"] == "conversando" and emps["ceo"]["atividade_alvo"] == "luiza"
    assert c.get("/api/activity", headers=h).json()[0]["slug"] == "ceo"
    assert {x["tipo"] for x in c.get("/api/history", headers=h).json()} == {"contratado"}
    card = c.get("/api/employees/ceo", headers=h).json()
    assert card["mensagens"][0]["body"] == "Bem-vinda" and card["ultima_atividade"]["kind"] == "conversando"
    assert c.get("/api/employees/nao-existe", headers=h).status_code == 404


def test_429_backoff_not_counted_as_failure(conn, monkeypatch):
    from meridiano.engine import llm
    monkeypatch.setenv("NOUS_API_KEY", "x")
    calls = {"n": 0}

    class R:
        def __init__(self, code): self.status_code = code; self.headers = {}
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}

    def post(*a, **k):
        calls["n"] += 1
        return R(429 if calls["n"] <= 2 else 200)
    monkeypatch.setattr(llm.httpx, "post", post)
    waits = []
    msg = llm.chat_fallback(conn, None, "poolside/laguna-s-2.1:free", [], None, sleep=waits.append)
    assert msg["content"] == "ok" and waits == [2, 4]          # recuo exponencial
    assert llm.failures_today(conn)[0] == 0                     # 429 não é falha
    assert llm.usage_today(conn) == (1, 10)


def test_budget_fraction_and_state_fields(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    from meridiano.engine import llm, config
    from meridiano.api import main
    monkeypatch.setattr(config, "DAILY_TOKEN_LIMIT", 1000)
    conn.execute("INSERT INTO usage_log(model,prompt_tokens,completion_tokens) VALUES ('m',400,100)")
    assert llm.budget_fraction(conn) == 0.5
    main._conn = conn
    st = TestClient(main.app).get("/api/state", headers={"X-Token": "t"}).json()
    assert st["orcamento_pct"] == 50.0 and "T" in st["orcamento_zera_em"]


def test_limits_old_deploy_values_are_replaced(monkeypatch):
    import importlib
    from meridiano.engine import config
    monkeypatch.setenv("DAILY_CALL_LIMIT", "300"); monkeypatch.setenv("DAILY_TOKEN_LIMIT", "400000")
    c = importlib.reload(config)
    assert (c.DAILY_CALL_LIMIT, c.DAILY_TOKEN_LIMIT) == (600, 1500000)
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "3000000")
    assert importlib.reload(config).DAILY_TOKEN_LIMIT == 3000000      # valor maior escolhido é respeitado
    monkeypatch.delenv("DAILY_CALL_LIMIT"); monkeypatch.delenv("DAILY_TOKEN_LIMIT")
    importlib.reload(config)


def test_pacing_never_stops_only_slows(conn, monkeypatch):
    from meridiano.engine import config, pacing
    assert pacing.next_delay(conn) == (config.TICK_SECONDS, "nenhum")
    assert pacing.next_delay(conn, errored=True) == (config.TICK_SECONDS, "erro")
    assert pacing.next_delay(conn, rate_backoff=120) == (120, "429")
    conn.execute("INSERT INTO usage_log(model,prompt_tokens,completion_tokens) VALUES ('m',%s,0)", (config.DAILY_TOKEN_LIMIT,))
    assert pacing.next_delay(conn) == (config.SLOW_TICK_SECONDS, "limite")   # passou do limite duro: lento, não parado
    assert config.SLOW_TICK_SECONDS < 3600


def test_api_engine_status(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    from meridiano.engine import pacing
    from meridiano.api import main
    ceo = _ceo(conn)
    main._conn = conn
    c = TestClient(main.app); h = {"X-Token": "t"}
    assert c.get("/api/engine", headers=h).json()["vivo"] is False          # nunca iniciou
    pacing.beat(conn, current=ceo["id"], delay=60)
    r = c.get("/api/engine", headers=h).json()
    assert r["vivo"] and r["funcionario_da_vez"] == "Rafael" and 0 <= r["proximo_turno_em_s"] <= 60 and r["motivo_espera"] == "nenhum"
    pacing.beat(conn, last_turn=ceo["id"], delay=600, reason="limite")
    r = c.get("/api/engine", headers=h).json()
    assert r["motivo_espera"] == "limite" and r["ultimo_turno_por"] == "Rafael" and r["funcionario_da_vez"] is None
    conn.execute("UPDATE engine_status SET heartbeat_at = now() - interval '2 hours'")
    assert c.get("/api/engine", headers=h).json()["vivo"] is False           # sem batimento: processo morto
