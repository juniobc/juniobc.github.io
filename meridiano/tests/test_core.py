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
    assert migrate.migrate(c) == ["002_zera_uso_vazio.sql", "003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql", "006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql"]
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
    assert migrate.migrate(c) == ["003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql", "006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql"]
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
    for old in (("300", "400000"), ("600", "1500000"), ("2000", "8000000")):
        monkeypatch.setenv("DAILY_CALL_LIMIT", old[0]); monkeypatch.setenv("DAILY_TOKEN_LIMIT", old[1])
        c = importlib.reload(config)
        assert (c.DAILY_CALL_LIMIT, c.DAILY_TOKEN_LIMIT) == (8000, 8000000)
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "12000000")
    assert importlib.reload(config).DAILY_TOKEN_LIMIT == 12000000     # valor maior escolhido é respeitado
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


WORDS = lambda n: " ".join(["palavra"] * n)


def test_plan_sections_review_and_ready_flow(conn, tmp_path, monkeypatch):
    from meridiano.engine import agent, plan, tools, config
    monkeypatch.setattr(config, "PRODUCTS_DIR", str(tmp_path))
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    conn.execute("INSERT INTO products(name,file_path) VALUES ('Copy de Vendas','copy.txt')")   # produto antigo, sem conteúdo
    plan.ensure(conn); plan.ensure(conn)                                                        # idempotente
    assert conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"] == 3              # reaproveitou o existente
    assert conn.execute("SELECT COUNT(*) AS n FROM product_sections").fetchone()["n"] == 28
    # produto com plano recusa write_product
    assert tools.run_tool(conn, ceo, "write_product", {"product_name": "Copy de Vendas", "content": "x"}).startswith("erro")
    # seção curta é recusada; completa é aceita
    assert "mínimo" in tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 1, "content": WORDS(50)})
    out = tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 1, "content": WORDS(450)})
    assert "1 de 8" in out and "Próxima: seção 2" in out
    assert tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 99, "content": WORDS(450)}).startswith("erro")
    prompt = agent.build_prompt(conn, ceo)
    assert "PRODUÇÃO" in prompt and "seção 2" in prompt
    # revisor (Presidente) vê instrução de revisão e pede reescrita
    pprompt = agent.build_prompt(conn, pres)
    assert "REVISOR" in pprompt and "section_number=1" in pprompt
    assert "reescrita pedida" in tools.run_tool(conn, pres, "request_rewrite", {"product_name": "Copy de Vendas", "section_number": 1, "reason": "genérica demais"})
    assert "REESCRITA" in agent.build_prompt(conn, ceo) and "genérica demais" in agent.build_prompt(conn, ceo)
    # termina todas as seções: vira 'pronto' e pede ao dono uma única vez
    for n in range(1, 9):
        tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": n, "content": WORDS(420)})
    p = conn.execute("SELECT * FROM products WHERE name='Copy de Vendas'").fetchone()
    assert p["status"] == "pronto" and "## 8." in p["content"]
    tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 3, "content": WORDS(430)})   # reescrever depois de pronto
    reqs = conn.execute("SELECT body FROM owner_requests").fetchall()
    assert [r["body"] for r in reqs] == ["Produto Copy de Vendas pronto, preciso que o Junio publique no marketplace"]


def test_archived_messages_stay_out_of_context(conn):
    from meridiano.engine import agent
    ceo = _ceo(conn)
    conn.execute("INSERT INTO messages(from_id,body,archived) VALUES (%s,'Confirme onde estão os arquivos fonte .txt',true)", (ceo["id"],))
    conn.execute("INSERT INTO messages(from_id,body) VALUES (%s,'mensagem nova')", (ceo["id"],))
    p = agent.build_prompt(conn, ceo)
    assert "mensagem nova" in p and ".txt" not in p


def test_migration_006_archives_existing_messages(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    from meridiano.engine import db, migrate
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    files = sorted(migrate.DIR.glob("*.sql"))
    c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    for f in files[:5]:
        c.execute(f.read_text()); c.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
    c.execute("INSERT INTO messages(body) VALUES ('velha 1'),('velha 2')")
    assert migrate.migrate(c) == ["006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql"]
    c.execute("INSERT INTO messages(body) VALUES ('nova')")
    assert [r["body"] for r in c.execute("SELECT body FROM messages WHERE NOT archived").fetchall()] == ["nova"]


def test_api_products_show_sections_progress(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    from meridiano.engine import plan, tools
    from meridiano.api import main
    ceo = _ceo(conn)
    plan.ensure(conn)
    tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 1, "content": WORDS(450)})
    main._conn = conn
    ps = {p["name"]: p for p in TestClient(main.app).get("/api/products", headers={"X-Token": "t"}).json()}
    cv = ps["Copy de Vendas"]
    assert (cv["secoes_prontas"], cv["secoes_total"]) == (1, 8) and cv["caracteres"] > 2000


# ---------- fábrica de ferramentas: validação e isolamento ----------

@pytest.fixture()
def gate_on():
    """Liga o portão com o autoteste REAL; sem isolamento neste ambiente, pula. Sempre volta a fechar no fim."""
    from meridiano.engine import sandbox
    g = sandbox.refresh_gate()
    if not g["enabled"]:
        pytest.skip("isolamento indisponível neste ambiente: " + g["reason"])
    yield g
    sandbox._GATE.update(enabled=False, backend="", reason="fechado após o teste")


GOOD = "import json\ndef run(args):\n    return {'soma': sum(args.get('n', [])), 'txt': json.dumps([1])}\n"
GOOD_TESTS = "def test_soma():\n    assert run({'n': [1, 2]})['soma'] == 3\n\ndef test_vazio():\n    assert run({})['soma'] == 0\n"


@pytest.mark.parametrize("code,fragment", [
    ("import os\ndef run(a):\n    return os.environ\n", "import proibido: os"),
    ("import subprocess\ndef run(a):\n    return 1\n", "import proibido: subprocess"),
    ("import socket\ndef run(a):\n    return 1\n", "import proibido: socket"),
    ("def run(a):\n    return __import__('os')\n", "nome proibido: __import__"),
    ("def run(a):\n    return ().__class__.__bases__\n", "atributo proibido: __class__"),
    ("def run(a):\n    return open('x').read()\n", "nome proibido: open"),
    ("def run(a):\n    return eval('1')\n", "nome proibido: eval"),
    ("def run(a):\n    return getattr(a, 'x')\n", "nome proibido: getattr"),
    ("def run(a):\n    return exec('1')\n", "nome proibido: exec"),
    ("from collections import _sys\ndef run(a):\n    return 1\n", "import proibido: _sys"),
    ("import collections\ndef run(a):\n    return collections._sys\n", "atributo proibido: _sys"),
    ("def run(a):\n    return '/proc/self/environ'\n", "texto proibido"),
    ("def run(a):\n    return '../segredo'\n", "texto proibido: '..'"),
    ("def run(a):\n    g = (i for i in [1])\n    return g.gi_frame\n", "atributo proibido: gi_frame"),
    ("def nada():\n    return 1\n", "def run(args)"),
    ("import json\ndef run(a):\n    return '{0.__globals__}'.format(json.dumps)\n", "atributo proibido: format"),
    ("def run(a):\n    return '{x.__class__}'.format_map({})\n", "atributo proibido: format_map"),
])
def test_validation_blocks_dangerous_code(code, fragment):
    from meridiano.engine import sandbox
    ok, problems, _ = sandbox.validate(code)
    assert not ok and any(fragment in p for p in problems), problems


def test_good_tool_runs_isolated_and_tests_pass(gate_on):
    from meridiano.engine import sandbox
    assert sandbox.validate(GOOD)[0]
    assert sandbox.execute(GOOD, {"n": [4, 5]})["result"]["soma"] == 9
    r = sandbox.execute(GOOD, tests=GOOD_TESTS)
    assert r["ok"] and r["result"]["testes"] == 2
    bad = sandbox.execute(GOOD, tests="def test_x():\n    assert run({})['soma'] == 99\n")
    assert not bad["ok"] and "AssertionError" in bad["error"]


def test_runtime_blocks_module_escape_routes(gate_on):
    """Passam pela AST (nomes comuns), mas o runtime restrito impede o acesso."""
    from meridiano.engine import sandbox
    via_urllib = "import urllib.parse as u\ndef run(a):\n    return str(u.sys)\n"
    via_formatter = "import string\ndef run(a):\n    return string.Formatter()\n"
    for code in (via_urllib, via_formatter):
        assert sandbox.validate(code)[0]
        r = sandbox.execute(code)
        assert not r["ok"] and "AttributeError" in r["error"], r
    assert sandbox.execute("def run(a):\n    x = 3\n    return f'valor {x + 1}'\n")["result"] == "valor 4"   # f-string segue permitida
    ok = sandbox.execute("import urllib.parse as u\ndef run(a):\n    return u.quote('a b')\n")
    assert ok["ok"] and ok["result"] == "a%20b"


def test_environment_is_empty_for_the_child(gate_on, monkeypatch):
    from meridiano.engine import sandbox
    monkeypatch.setenv("NOUS_API_KEY", "segredo-nao-pode-vazar")
    seen = {}
    real = sandbox.subprocess.run

    def spy(cmd, **kw):
        seen.update(kw)
        return real(cmd, **kw)
    monkeypatch.setattr(sandbox.subprocess, "run", spy)
    sandbox.execute(GOOD)
    assert seen["env"] == {} and seen["cwd"] and "meridiano_tool_" in seen["cwd"]


def test_timeout_and_memory_and_output_limits(gate_on, monkeypatch):
    from meridiano.engine import sandbox, config
    monkeypatch.setattr(config, "TOOL_TIMEOUT_SECONDS", 2)
    r = sandbox.execute("def run(a):\n    while True:\n        pass\n")
    assert not r["ok"] and ("tempo" in r["error"] or "sem resposta" in r["error"]) and r["ms"] < 6000
    r = sandbox.execute("def run(a):\n    x = 'a' * (2 * 10 ** 9)\n    return len(x)\n")
    assert not r["ok"] and ("memória" in r["error"] or "MemoryError" in r["error"] or "sem resposta" in r["error"]), r
    r = sandbox.execute("def run(a):\n    return 'x' * 50000\n")
    assert not r["ok"] and "maior que" in r["error"]
    r = sandbox.execute("def run(a):\n    return {1, 2}\n")
    assert not r["ok"] and "TypeError" in r["error"]          # resultado não serializável


def _mk(conn, name, role):
    from meridiano.engine import tools
    ceo = _ceo(conn)
    tools.run_tool(conn, ceo, "hire_employee", {"name": name, "role": role, "area": "A", "instructions": "x", "reason": "r"})
    return conn.execute("SELECT * FROM employees WHERE name=%s", (name,)).fetchone()


def test_tool_factory_cross_approval_flow(gate_on, conn, tmp_path, monkeypatch):
    from meridiano.engine import tools, config
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    dev = _mk(conn, "Davi", "Analista")
    prop = {"name": "somador", "description": "soma números", "python_code": GOOD, "tests_code": GOOD_TESTS, "reason": "contas rápidas"}
    out = tools.run_tool(conn, dev, "propose_tool", prop)
    assert "aguarda revisão" in out and "2 teste" in out
    assert (tmp_path / "tools" / "somador" / "v1.py").exists()
    assert "revisao" in tools.run_tool(conn, dev, "list_tools", {})
    assert tools.run_tool(conn, dev, "run_custom_tool", {"name": "somador", "args": {"n": [1]}}).startswith("erro")      # ainda não ativa
    assert "GOOD" not in tools.run_tool(conn, pres, "read_tool", {"name": "somador"}) and "def run" in tools.run_tool(conn, pres, "read_tool", {"name": "somador"})
    assert tools.run_tool(conn, dev, "approve_tool", {"name": "somador", "approve": True, "note": "ok ok ok"}).startswith("erro: só Presidente")
    # o autor (CEO) não aprova a própria; outro revisor aprova
    prop2 = dict(prop, name="somador_ceo")
    tools.run_tool(conn, ceo, "propose_tool", prop2)
    assert "autor não pode" in tools.run_tool(conn, ceo, "approve_tool", {"name": "somador_ceo", "approve": True, "note": "eu mesmo"})
    assert "ATIVA" in tools.run_tool(conn, pres, "approve_tool", {"name": "somador_ceo", "approve": True, "note": "li o código, ok"})
    assert "ATIVA" in tools.run_tool(conn, ceo, "approve_tool", {"name": "somador", "approve": True, "note": "li o código, ok"})
    # uso: executa, audita, conta
    assert '"soma": 12' in tools.run_tool(conn, dev, "run_custom_tool", {"name": "somador", "args": {"n": [5, 7]}})
    t = conn.execute("SELECT uses,status,reviewed_by FROM custom_tools WHERE name='somador'").fetchone()
    assert t["uses"] == 1 and t["status"] == "ativa" and t["reviewed_by"] == ceo["id"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM tool_runs WHERE tool_name='somador' ORDER BY id").fetchall()]
    assert kinds == ["teste", "uso"]
    # só o autor repropõe uma ferramenta ativa; versão sobe e volta para revisão
    assert tools.run_tool(conn, pres, "propose_tool", prop).startswith("erro: só o autor")
    assert "v2" in tools.run_tool(conn, dev, "propose_tool", dict(prop, reason="melhoria"))
    assert conn.execute("SELECT status,version FROM custom_tools WHERE name='somador'").fetchone() == {"status": "revisao", "version": 2}
    assert conn.execute("SELECT COUNT(*) AS n FROM custom_tool_versions").fetchone()["n"] == 3


def test_tool_factory_rejections_and_level_c(gate_on, conn, tmp_path, monkeypatch):
    from meridiano.engine import tools, config
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    dev = _mk(conn, "Davi", "Analista")
    base = {"description": "d", "tests_code": GOOD_TESTS, "reason": "porque sim"}
    # código perigoso é recusado e nunca executa
    bad = tools.run_tool(conn, dev, "propose_tool", dict(base, name="malvada", python_code="import os\ndef run(a):\n    return os.getcwd()\n"))
    assert "recusada" in bad and "import proibido: os" in bad
    assert conn.execute("SELECT COUNT(*) AS n FROM tool_runs").fetchone()["n"] == 0
    # testes que falham recusam
    out = tools.run_tool(conn, dev, "propose_tool", dict(base, name="quebrada", python_code="def run(a):\n    return {'soma': 0}\n", tests_code="def test_a():\n    assert run({})['soma'] == 1\n"))
    assert "testes falharam" in out
    assert conn.execute("SELECT status FROM custom_tools WHERE name='quebrada'").fetchone()["status"] == "recusada"
    # nome reservado e nome inválido
    assert tools.run_tool(conn, dev, "propose_tool", dict(base, name="write_product", python_code=GOOD)).startswith("erro: nome inválido")
    assert tools.run_tool(conn, dev, "propose_tool", dict(base, name="Nome Ruim", python_code=GOOD)).startswith("erro: nome inválido")
    # nível C (precisa de rede): fica pendente, UM pedido ao dono, e ninguém ativa
    net = "def run(a):\n    return http_get('https://exemplo.com')\n"
    out = tools.run_tool(conn, dev, "propose_tool", dict(base, name="busca_web", python_code=net, tests_code="def test_a():\n    assert True\n"))
    assert "nível C" in out
    tools.run_tool(conn, dev, "propose_tool", dict(base, name="busca_web", python_code=net, tests_code="def test_a():\n    assert True\n"))
    assert conn.execute("SELECT COUNT(*) AS n FROM owner_requests WHERE body LIKE '%busca_web%'").fetchone()["n"] == 1
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    assert "nível C só o dono" in tools.run_tool(conn, pres, "approve_tool", {"name": "busca_web", "approve": True, "note": "tentando aprovar"})
    assert tools.run_tool(conn, dev, "run_custom_tool", {"name": "busca_web"}).startswith("erro")


def test_engine_reports_isolation(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    from meridiano.engine import pacing, sandbox
    from meridiano.api import main
    ceo = _ceo(conn)
    pacing.beat(conn, current=ceo["id"], delay=60)
    monkeypatch.setattr(sandbox, "refresh_gate", lambda: {"enabled": False, "reason": "unshare negado: Operation not permitted", "help": "liberar user namespaces", "backend": "", "detail": "", "checked_at": None})
    pacing.record_isolation(conn)
    main._conn = conn
    r = TestClient(main.app).get("/api/engine", headers={"X-Token": "t"}).json()["isolamento_ferramentas"]
    assert r["ligadas"] is False and "Operation not permitted" in r["motivo"] and "user namespaces" in r["o_que_falta"]


def test_plan_renames_old_product_without_duplicating(conn):
    from meridiano.engine import plan
    pid = conn.execute("INSERT INTO products(name,file_path,content) VALUES ('Plano Mestre Financeiro Pessoal 2025','p.md','texto antigo') RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO product_sections(product_id,position,title,content) VALUES (%s,7,'Metas e planejamento anual de 2025','texto da seção 7')", (pid,))
    plan.ensure(conn); plan.ensure(conn)
    rows = conn.execute("SELECT id,name,content FROM products WHERE name ILIKE 'Plano Mestre%'").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == pid and rows[0]["name"] == "Plano Mestre Financeiro Pessoal" and rows[0]["content"] == "texto antigo"
    sec = conn.execute("SELECT title,content FROM product_sections WHERE product_id=%s AND position=7", (pid,)).fetchone()
    assert sec["title"] == "Metas e planejamento anual" and sec["content"] == "texto da seção 7"   # título novo, texto preservado
    assert not conn.execute("SELECT 1 FROM products WHERE name ILIKE '%2025%'").fetchone()


# ---------- portão que falha fechado ----------

CLOSED = dict(enabled=False, backend="", reason="fechado para o teste", help="", detail="")


def test_gate_is_closed_by_default_and_everything_refuses(conn, tmp_path, monkeypatch):
    from meridiano.engine import sandbox, tools, config
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setitem(sandbox._GATE, "enabled", False)
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    dev = _mk(conn, "Davi", "Analista")
    r = sandbox.execute(GOOD)
    assert r["ok"] is False and r["error"] == "ferramentas desligadas: isolamento indisponível"
    out = tools.run_tool(conn, dev, "propose_tool", {"name": "somador", "description": "d", "python_code": GOOD, "tests_code": GOOD_TESTS, "reason": "contas"})
    assert "RASCUNHO" in out and "desligadas" in out
    assert conn.execute("SELECT status FROM custom_tools WHERE name='somador'").fetchone()["status"] == "rascunho"
    assert conn.execute("SELECT COUNT(*) AS n FROM tool_runs").fetchone()["n"] == 0            # nada foi executado
    assert tools.run_tool(conn, pres, "approve_tool", {"name": "somador", "approve": True, "note": "li o código"}) == "ferramentas desligadas: isolamento indisponível"
    assert tools.run_tool(conn, dev, "run_custom_tool", {"name": "somador"}) == "ferramentas desligadas: isolamento indisponível"
    # código perigoso continua sendo recusado na validação mesmo com o portão fechado
    bad = tools.run_tool(conn, dev, "propose_tool", {"name": "ruim", "description": "d", "python_code": "import os\ndef run(a):\n    return 1\n", "tests_code": GOOD_TESTS, "reason": "x"})
    assert "recusada" in bad
    # ferramenta que já estava ATIVA também para quando o portão fecha
    conn.execute("UPDATE custom_tools SET status='ativa' WHERE name='somador'")
    assert tools.run_tool(conn, dev, "run_custom_tool", {"name": "somador"}).startswith("ferramentas desligadas")


def test_drafts_are_processed_when_gate_opens(gate_on, conn, tmp_path, monkeypatch):
    from meridiano.engine import sandbox, tools, config, pacing
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    dev = _mk(conn, "Davi", "Analista")
    sandbox._GATE.update(CLOSED)
    tools.run_tool(conn, dev, "propose_tool", {"name": "somador", "description": "d", "python_code": GOOD, "tests_code": GOOD_TESTS, "reason": "contas"})
    tools.run_tool(conn, dev, "propose_tool", {"name": "quebrada", "description": "d", "python_code": "def run(a):\n    return {'soma': 0}\n",
                                                "tests_code": "def test_a():\n    assert run({})['soma'] == 1\n", "reason": "x"})
    assert conn.execute("SELECT COUNT(*) AS n FROM custom_tools WHERE status='rascunho'").fetchone()["n"] == 2
    ceo = _ceo(conn)
    pacing.beat(conn, current=ceo["id"], delay=60)
    pacing.record_isolation(conn)                                                    # autoteste real: abre e processa os rascunhos
    st = {r["name"]: r["status"] for r in conn.execute("SELECT name,status FROM custom_tools").fetchall()}
    assert st == {"somador": "revisao", "quebrada": "recusada"}
    assert conn.execute("SELECT tools_enabled FROM engine_status").fetchone()["tools_enabled"] is True


@pytest.mark.parametrize("stderr,code,reason_part,help_part", [
    ("unshare: unshare failed: Operation not permitted", 1, "Operation not permitted", "apparmor_restrict_unprivileged_userns"),
    ("", 97, "mount negado", "bubblewrap"),
    ("unshare: write failed /proc/self/uid_map: Operation not permitted", 1, "uid_map", "usuário Linux separado"),
])
def test_gate_reports_exact_reason_and_what_admin_must_do(monkeypatch, stderr, code, reason_part, help_part):
    from meridiano.engine import sandbox
    monkeypatch.setattr(sandbox, "_backends", lambda: ["unshare"])
    monkeypatch.setattr(sandbox, "_run_probe", lambda *a, **k: (None, sandbox._help_for(stderr, code)))
    g = sandbox.refresh_gate()
    assert g["enabled"] is False and reason_part in g["reason"] and help_part in g["help"], g


def test_gate_without_any_backend(monkeypatch):
    from meridiano.engine import sandbox
    monkeypatch.setattr(sandbox, "_backends", lambda: [])
    g = sandbox.refresh_gate()
    assert not g["enabled"] and "nem unshare nem bubblewrap" in g["reason"] and "instalar" in g["help"]


CLEAN = {"net_conectou": [False, False], "canario_legivel": False, "env_file_legivel": False, "ocultos_nao_vazios": {},
         "pids_visiveis": 1, "environ_alheio_legivel": 0, "env_chaves": ["PWD"], "escreveu_tmp": True}


@pytest.mark.parametrize("patch,fragment", [
    ({"net_conectou": [False, True]}, "rede NÃO bloqueada"),
    ({"canario_legivel": True}, "legíveis"),
    ({"env_file_legivel": True}, "legíveis"),
    ({"ocultos_nao_vazios": {"/home/empresa": 3}}, "têm conteúdo"),
    ({"environ_alheio_legivel": 2}, "/proc"),
    ({"env_chaves": ["PWD", "NOUS_API_KEY"]}, "NOUS_API_KEY"),
    ({"escreveu_tmp": False}, "escrever"),
])
def test_gate_closes_if_any_probe_fails(monkeypatch, patch, fragment):
    from meridiano.engine import sandbox
    monkeypatch.setattr(sandbox, "_backends", lambda: ["unshare"])
    monkeypatch.setattr(sandbox, "_run_probe", lambda *a, **k: (dict(CLEAN, **patch), None))
    g = sandbox.refresh_gate()
    assert not g["enabled"] and fragment in g["reason"], g


def test_gate_opens_only_when_every_probe_is_clean(monkeypatch):
    from meridiano.engine import sandbox
    monkeypatch.setattr(sandbox, "_backends", lambda: ["unshare"])
    monkeypatch.setattr(sandbox, "_run_probe", lambda *a, **k: (dict(CLEAN), None))
    g = sandbox.refresh_gate()
    assert g["enabled"] and g["backend"] == "unshare"
    sandbox._GATE.update(CLOSED)


def test_probe_really_detects_a_non_isolated_process(tmp_path):
    """A sonda rodando SEM envoltório (como num servidor sem isolamento) precisa ser reprovada."""
    import subprocess as sp, sys, json
    from meridiano.engine import sandbox
    canary = tmp_path / "canario"
    canary.write_text("segredo")
    out = sp.run([sys.executable, "-I", sandbox.PROBE], input=json.dumps({"hide": [str(tmp_path)], "canary": str(canary), "env_file": None}).encode(),
                 capture_output=True, cwd=str(tmp_path), env={}, timeout=30)
    fails = sandbox._judge(json.loads(out.stdout))
    assert any("legíveis" in f for f in fails) and any("têm conteúdo" in f for f in fails), fails


def test_real_selftest_passes_here_or_skips(gate_on):
    from meridiano.engine import sandbox
    g = sandbox.gate()
    assert g["enabled"] and g["backend"] in ("unshare", "bwrap")
    assert not os.path.exists(os.path.join(os.path.expanduser("~"), ".meridiano_isolation_canary"))   # canário removido
