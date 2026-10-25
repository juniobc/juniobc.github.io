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
    assert migrate.migrate(c) == ["002_zera_uso_vazio.sql", "003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql", "006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql", "009_vendas_no_servidor.sql", "010_loja.sql", "011_oportunidades.sql"]
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
    assert migrate.migrate(c) == ["003_produtos_pedidos_pacing.sql", "004_demissao_atividade.sql", "005_engine_status.sql", "006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql", "009_vendas_no_servidor.sql", "010_loja.sql", "011_oportunidades.sql"]
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
    assert conn.execute("SELECT COUNT(*) AS n FROM owner_requests").fetchone()["n"] == 0        # ninguém pede nada ao dono: a loja vende sozinha
    avisos = conn.execute("SELECT body FROM messages WHERE body LIKE '%pronto para a loja%'").fetchall()
    assert [a["body"] for a in avisos] == ["Produto 'Copy de Vendas' pronto para a loja."]       # aviso interno uma única vez


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
    assert migrate.migrate(c) == ["006_secoes_arquivo.sql", "007_ferramentas.sql", "008_portao_isolamento.sql", "009_vendas_no_servidor.sql", "010_loja.sql", "011_oportunidades.sql"]
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


# ---------- vendas no servidor: etapa 1 (pedido ao dono e token seguro) ----------

def test_no_prompt_asks_for_marketplaces_or_publishing(conn):
    from meridiano.engine import agent, plan, seed
    import re
    ceo = _ceo(conn)
    plan.ensure(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    seed.refresh_instructions(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    texts = [agent.RULES, agent.build_prompt(conn, ceo), agent.build_prompt(conn, pres), pres["instructions"], ceo["instructions"]]
    for t in texts:
        assert not re.search(r"hotmart|kiwify|eduzz|mercado livre|marketplace|publique|cnpj", t, re.I), t[:200]
    assert "loja do próprio servidor" in agent.RULES


def test_migration_009_supersedes_old_owner_requests(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    from meridiano.engine import db, migrate
    c = db.connect()
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    files = sorted(migrate.DIR.glob("*.sql"))
    c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    for f in files[:8]:
        c.execute(f.read_text()); c.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
    c.execute("INSERT INTO owner_requests(body) VALUES ('Produto Toolkit pronto, preciso que o Junio publique no marketplace'),('Abrir conta na Hotmart com CPF')")
    c.execute("INSERT INTO owner_requests(body,done) VALUES ('antigo já feito',true)")
    assert migrate.migrate(c) == ["009_vendas_no_servidor.sql", "010_loja.sql", "011_oportunidades.sql"]
    rows = c.execute("SELECT done,resolution FROM owner_requests ORDER BY id").fetchall()
    assert all(r["done"] for r in rows)
    assert [r["resolution"] for r in rows[:2]] == ["substituído: as vendas passam a ser feitas no próprio servidor, com Mercado Pago"] * 2


def test_token_request_is_the_only_owner_request_and_closes_when_configured(conn, tmp_path, monkeypatch):
    from meridiano.engine import config, secrets_store as ss
    monkeypatch.setattr(config, "SECRETS_DIR", str(tmp_path / "segredos"))
    ss.sync_owner_request(conn); ss.sync_owner_request(conn)                       # idempotente
    rows = conn.execute("SELECT body,done,from_id FROM owner_requests").fetchall()
    assert len(rows) == 1 and rows[0]["body"] == ss.TOKEN_REQUEST and not rows[0]["done"] and rows[0]["from_id"] is None
    monkeypatch.setattr(ss, "verify", lambda t: ("ok", "token aceito pelo Mercado Pago"))
    assert ss.save_token("APP_USR-" + "a1b2c3d4" * 6)[0]
    ss.sync_owner_request(conn)
    assert conn.execute("SELECT done FROM owner_requests").fetchone()["done"] is False          # falta a identificação do vendedor
    conn.execute("INSERT INTO store_settings(key,value) VALUES ('seller_name','Junio Teste'),('contact_email','junio@example.com')")
    ss.sync_owner_request(conn)
    assert conn.execute("SELECT done,resolution FROM owner_requests").fetchone() == {"done": True, "resolution": "token e identificação recebidos"}
    ss.sync_owner_request(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM owner_requests").fetchone()["n"] == 1   # não reabre


TOKEN = "APP_USR-1234567890123456-012345-" + "abcdef0123456789" * 2 + "-987654321"


def test_api_token_is_stored_private_and_never_shown(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura"); monkeypatch.setenv("MERIDIANO_ADMIN_TOKEN", "admin")
    from meridiano.engine import config, secrets_store as ss
    from meridiano.api import main
    sec = tmp_path / "segredos"
    monkeypatch.setattr(config, "SECRETS_DIR", str(sec))
    monkeypatch.setattr(ss, "verify", lambda t: ("ok", "token aceito pelo Mercado Pago"))
    main._conn = conn
    c = TestClient(main.app)
    ss.sync_owner_request(conn)
    body = {"access_token": TOKEN}
    assert c.post("/api/secrets/mp-token", json=body).status_code == 401                                  # sem admin
    assert c.post("/api/secrets/mp-token", json=body, headers={"X-Token": "leitura"}).status_code == 401   # leitura não basta
    assert c.post("/api/secrets/mp-token", json={"access_token": "qualquer coisa"}, headers={"X-Admin-Token": "admin"}).status_code == 400
    r = c.post("/api/secrets/mp-token", json=body, headers={"X-Admin-Token": "admin"})
    assert r.status_code == 200 and r.json()["configurado"] and r.json()["modo"] == "producao" and r.json()["verificado"]
    assert TOKEN not in r.text and "abcdef0123456789" not in r.text                                       # nunca devolvido
    # arquivo privado: pasta 700, arquivo 600
    assert oct(os.stat(sec).st_mode & 0o777) == "0o700" and oct(os.stat(sec / "mp-token").st_mode & 0o777) == "0o600"
    assert (sec / "mp-token").read_text() == TOKEN and not (sec / "mp-token.tmp").exists()
    # status não mostra o token
    st = c.get("/api/secrets/mp-token", headers={"X-Token": "leitura"})
    assert st.json()["configurado"] is True and TOKEN not in st.text
    assert c.get("/api/secrets/mp-token").status_code == 401
    # falta a identificação: o pedido continua aberto até ela chegar
    assert conn.execute("SELECT done FROM owner_requests").fetchone()["done"] is False
    assert c.post("/api/store/settings", json={"seller_name": "Junio Teste", "contact_email": "junio@example.com"}, headers={"X-Admin-Token": "admin"}).status_code == 200
    assert conn.execute("SELECT done FROM owner_requests").fetchone()["done"] is True
    # o token não está em nenhuma tabela do banco
    tabelas = [t["table_name"] for t in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()]
    for t in tabelas:
        dump = str(conn.execute(f'SELECT * FROM "{t}"').fetchall())
        assert TOKEN not in dump and "abcdef0123456789abcdef0123456789" not in dump, t


def test_invalid_token_is_rejected_and_not_saved(tmp_path, monkeypatch):
    from meridiano.engine import config, secrets_store as ss
    monkeypatch.setattr(config, "SECRETS_DIR", str(tmp_path / "segredos"))
    monkeypatch.setattr(ss, "verify", lambda t: ("invalido", "o Mercado Pago recusou este token"))
    ok, msg, meta = ss.save_token(TOKEN)
    assert not ok and "recusou" in msg and not ss.configured()
    monkeypatch.setattr(ss, "verify", lambda t: ("nao_verificado", "não foi possível falar com o Mercado Pago agora"))
    ok, msg, meta = ss.save_token(TOKEN)
    assert ok and meta["verificado"] is False and ss.configured()                                         # rede fora: salva e avisa
    assert ss.status()["verificado"] is False and "TEST" not in str(ss.status())
    ss.save_token("TEST-" + "x1y2z3w4" * 5)
    assert ss.status()["modo"] == "teste"


def test_token_never_reaches_tool_sandbox_or_agent_surface(tmp_path, monkeypatch):
    """O arquivo mora sob a home, que o isolamento esconde; e nenhuma tool de agente lê esse caminho."""
    from meridiano.engine import config, sandbox, tools
    names = {sp["name"] for sp in tools.SPECS}
    assert not any("secret" in n or "token" in n or "mp_" in n for n in names)
    assert os.path.expanduser("~") in config.SECRETS_DIR or config.SECRETS_DIR.startswith(os.path.expanduser("~"))
    assert os.path.realpath(os.path.expanduser("~")) in sandbox.hide_dirs() or any(config.SECRETS_DIR.startswith(h) for h in sandbox.hide_dirs())


# ---------- loja pública (etapa 2) ----------

GOOD_COPY = {"headline": "Aprenda a escrever textos de venda claros", "subtitle": "Um guia prático, direto ao ponto",
             "pitch": "Material em português para quem quer vender melhor sem enrolar o cliente.",
             "benefits": ["Estrutura de página de vendas passo a passo", "Como pesquisar o seu público", "Modelos de títulos para testar"],
             "audience": "Pequenos vendedores online que escrevem os próprios textos.",
             "faq": [{"q": "Tem reembolso?", "a": "Sim, em até 7 dias."}, {"q": "Em que formato vem?", "a": "Texto para ler no celular."}]}


def _ready_product(conn, name="Copy de Vendas"):
    from meridiano.engine import plan, tools
    ceo = _ceo(conn)
    plan.ensure(conn)
    n = conn.execute("SELECT COUNT(*) AS n FROM product_sections s JOIN products p ON p.id=s.product_id WHERE p.name=%s", (name,)).fetchone()["n"]
    for i in range(1, n + 1):
        tools.run_tool(conn, ceo, "write_section", {"product_name": name, "section_number": i, "content": WORDS(420)})
    return ceo


def _open_store(conn, name="Copy de Vendas", price=29.9, copy=None):
    from meridiano.engine import tools
    ceo = _ready_product(conn, name)
    out = tools.run_tool(conn, ceo, "write_store_copy", dict(copy or GOOD_COPY, product_name=name))
    assert "salvo" in out, out
    assert "definido" in tools.run_tool(conn, ceo, "set_price", {"product_name": name, "price_brl": price, "reason": "preço de entrada para guias deste tamanho"})
    conn.execute("INSERT INTO store_settings(key,value) VALUES ('seller_name','Junio Teste'),('contact_email','junio@example.com') ON CONFLICT DO NOTHING")
    return ceo


def _client(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura"); monkeypatch.setenv("MERIDIANO_ADMIN_TOKEN", "admin")
    from meridiano.api import main
    main._conn = conn
    return TestClient(main.app)


@pytest.mark.parametrize("patch,fragment", [
    ({"headline": ""}, "headline"),
    ({"headline": "Ganhe dinheiro garantido"}, "promessas proibidas"),
    ({"pitch": "Resultado 100% certo para você"}, "promessas proibidas"),
    ({"benefits": ["só um", "dois"]}, "benefits"),
    ({"faq": [{"q": "Só uma?", "a": "sim"}]}, "faq"),
    ({"audience": "<b>qualquer um</b>"}, "HTML"),
    ({"subtitle": "x" * 300}, "subtitle"),
])
def test_sales_copy_is_validated(conn, patch, fragment):
    from meridiano.engine import tools
    ceo = _ready_product(conn)
    out = tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name="Copy de Vendas", **patch))
    assert out.startswith("erro: texto de venda recusado") and fragment in out, out
    assert conn.execute("SELECT COUNT(*) AS n FROM product_store").fetchone()["n"] == 0


def test_copy_and_price_rules(conn):
    from meridiano.engine import plan, tools
    ceo = _ceo(conn); plan.ensure(conn)
    assert "ainda não está pronto" in tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name="Copy de Vendas"))
    _ready_product(conn)
    dev = _mk(conn, "Davi", "Analista")
    assert tools.run_tool(conn, dev, "set_price", {"product_name": "Copy de Vendas", "price_brl": 29, "reason": "porque sim"}).startswith("erro: só Presidente ou CEO")
    for bad in (8.99, 0, -5, 1000.01):
        assert tools.run_tool(conn, ceo, "set_price", {"product_name": "Copy de Vendas", "price_brl": bad, "reason": "teste de faixa"}).startswith("erro"), bad
    assert tools.run_tool(conn, ceo, "set_price", {"product_name": "Copy de Vendas", "price_brl": 9, "reason": "preço mínimo permitido"}).startswith("preço")
    assert conn.execute("SELECT price_cents FROM products WHERE name='Copy de Vendas'").fetchone()["price_cents"] == 900


def test_store_page_is_static_escaped_and_locked_down(conn, monkeypatch):
    c = _client(conn, monkeypatch)
    xss = dict(GOOD_COPY, headline="Guia <script>alert(1)</script>", pitch='Aprenda "rápido" & bem <img src=x onerror=alert(1)>')
    _open_store(conn, copy=dict(xss, headline="Guia de copy", pitch="Texto normal sobre vendas"))      # base válida
    from meridiano.engine import store
    conn.execute("UPDATE product_store SET headline=%s, pitch=%s", (xss["headline"], xss["pitch"]))     # força conteúdo hostil direto no banco
    r = c.get("/pub/loja/copy-de-vendas/")
    assert r.status_code == 200
    html = r.text
    assert "<script" not in html.lower() and "<img" not in html.lower() and "onerror" not in html.split("<main>")[1].replace("&lt;img src=x onerror", "")    # tudo escapado
    assert "&lt;script&gt;" in html and "R$ 29,90" in html
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'none'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<style nonce="{nonce}">' in html
    assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/pub/loja/copy-de-vendas/").headers["content-security-policy"] != csp                 # nonce novo a cada resposta
    # conteúdo real: sumário vem das seções, não do agente
    assert "8 seções" in html and "cerca de 3360 palavras" in html and "Princípios de copy que converte" in html
    # legal: reembolso 7 dias (CDC), aviso educativo, vendedor identificado
    assert "7 dias" in html and "art. 49" in html and "Material educativo" in html
    assert "Junio Teste" in html and "mailto:junio@example.com" in html
    # sem checkout ainda: nada de botão de compra
    assert "Pagamento em breve" in html and "<form" not in html and "Comprar" not in html
    assert "script" not in html.split("</style>")[1].lower().replace("&lt;script&gt;alert(1)&lt;/script&gt;", "")
    assert c.post("/pub/loja/copy-de-vendas/comprar").status_code == 503                                  # ninguém é cobrado


def test_store_publishes_only_when_everything_is_ready(conn, monkeypatch):
    from meridiano.engine import tools
    c = _client(conn, monkeypatch)
    assert c.get("/pub/loja/").status_code == 200 and "Loja em preparação" in c.get("/pub/loja/").text
    ceo = _ready_product(conn)
    assert c.get("/pub/loja/copy-de-vendas/").status_code == 404                                           # pronto, mas sem texto/preço
    tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name="Copy de Vendas"))
    assert c.get("/pub/loja/copy-de-vendas/").status_code == 404                                           # falta preço
    tools.run_tool(conn, ceo, "set_price", {"product_name": "Copy de Vendas", "price_brl": 19.9, "reason": "preço de entrada"})
    assert c.get("/pub/loja/copy-de-vendas/").status_code == 404                                           # falta identificar o vendedor
    conn.execute("INSERT INTO store_settings(key,value) VALUES ('seller_name','Junio Teste'),('contact_email','junio@example.com')")
    assert c.get("/pub/loja/copy-de-vendas/").status_code == 200
    assert "Aprenda a escrever textos de venda claros" in c.get("/pub/loja/").text
    tools.run_tool(conn, ceo, "request_rewrite", {"product_name": "Copy de Vendas", "section_number": 2, "reason": "seção fraca demais"})   # volta a rascunho
    assert c.get("/pub/loja/copy-de-vendas/").status_code == 404
    for bad in ("../etc/passwd", "COPY", "x" * 200, "%00"):
        assert c.get(f"/pub/loja/{bad}/").status_code in (404, 307, 308)


def test_store_settings_endpoint_and_panel_listing(conn, monkeypatch):
    c = _client(conn, monkeypatch)
    body = {"seller_name": "Junio Teste", "contact_email": "junio@example.com"}
    assert c.post("/api/store/settings", json=body).status_code == 401
    assert c.post("/api/store/settings", json=body, headers={"X-Token": "leitura"}).status_code == 401
    assert c.post("/api/store/settings", json=dict(body, contact_email="sem-arroba"), headers={"X-Admin-Token": "admin"}).status_code == 400
    assert c.post("/api/store/settings", json=body, headers={"X-Admin-Token": "admin"}).status_code == 200
    g = c.get("/api/store/settings", headers={"X-Token": "leitura"}).json()
    assert g == {"seller_name": "Junio Teste", "contact_email": "junio@example.com", "pronta": True}
    _open_store(conn)
    lst = {p["name"]: p for p in c.get("/api/store/products", headers={"X-Token": "leitura"}).json()}
    assert lst["Copy de Vendas"]["publicado"] and lst["Copy de Vendas"]["url"] == "/pub/loja/copy-de-vendas/"
    assert not lst["Toolkit do Vendedor E-commerce"]["publicado"]


def test_prompt_directs_copy_then_price_once_products_are_ready(conn):
    from meridiano.engine import agent, tools
    ceo = _ceo(conn)
    for n in ("Copy de Vendas", "Toolkit do Vendedor E-commerce", "Plano Mestre Financeiro Pessoal"):
        _ready_product(conn, n)
    assert "write_store_copy" in agent.build_prompt(conn, ceo) and "texto de venda" in agent.build_prompt(conn, ceo)
    tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name="Copy de Vendas"))
    assert "Toolkit do Vendedor" in agent.build_prompt(conn, ceo)                                           # próximo produto sem texto
    for n in ("Toolkit do Vendedor E-commerce", "Plano Mestre Financeiro Pessoal"):
        tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name=n))
    assert "set_price" in agent.build_prompt(conn, ceo) and "R$ 9,00" in agent.build_prompt(conn, ceo)
    for n in ("Copy de Vendas", "Toolkit do Vendedor E-commerce", "Plano Mestre Financeiro Pessoal"):
        tools.run_tool(conn, ceo, "set_price", {"product_name": n, "price_brl": 24.9, "reason": "preço de entrada"})
    assert "todos os produtos estão na loja" in agent.build_prompt(conn, ceo)


def test_financial_product_page_shows_investment_disclaimer(conn, monkeypatch):
    c = _client(conn, monkeypatch)
    _open_store(conn, name="Plano Mestre Financeiro Pessoal", price=39.9)
    html = c.get("/pub/loja/plano-mestre-financeiro-pessoal/").text
    assert "Não constitui recomendação de investimento" in html and "7 dias" in html


# ---------- missão ampla e mesa de oportunidades ----------

IDEA = dict(idea="Planilha personalizada de orçamento para pequenos comércios, vendida sob encomenda", audience="donos de pequenos comércios",
            how_it_earns="cobrança única no checkout próprio por planilha entregue", effort="baixo", risk="baixo")


def test_mission_is_broad_but_bounded(conn):
    from meridiano.engine import agent, seed
    import re
    r = agent.RULES
    assert "meios legítimos de gerar dinheiro real" in r and "testá-los rápido" in r
    for exemplo in ("serviços sob encomenda", "micro-ferramentas e calculadoras", "newsletters e assinaturas", "dados públicos organizados", "automações para pequenos negócios", "não é lista fechada"):
        assert exemplo in r, exemplo
    for limite in ("LGPD", "sem golpe", "sem falsa promessa de ganho", "sem criar contas nem assumir identidade de terceiros", "sem dado pessoal", "ask_owner"):
        assert limite in r, limite
    assert not re.search(r"hotmart|kiwify|eduzz|mercado livre|marketplace|publique|cnpj", r, re.I)


def test_opportunity_rules_and_single_owner_request(conn):
    from meridiano.engine import tools
    ceo = _ceo(conn)
    out = tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, needs_owner="aceitar os termos do serviço de envio de arquivos"))
    assert "registrada" in out and "mínimo 5" in out
    assert "já existe" in tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea="planilha personalizada de orçamento para pequenos comércios vendida sob encomenda"))
    assert tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea="x")).startswith("erro: descreva")
    for ruim in ("Disparo em massa de mensagens para uma lista de contatos comprada", "Criar perfil falso para vender cursos", "Esquema de pirâmide com indicações"):
        assert tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea=ruim)).startswith("erro: ideia recusada"), ruim
    assert conn.execute("SELECT COUNT(*) AS n FROM opportunities").fetchone()["n"] == 1
    # descartar exige aprendizado; teste limitado a 2 ao mesmo tempo
    ids = [1]
    for i, idea in enumerate(["Calculadora web de reajuste de aluguel pelos índices oficiais", "Newsletter semanal com resumo de editais públicos abertos", "Relatório de pesquisa de mercado sob encomenda para microempresas"]):
        tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea=idea, needs_owner=""))
        ids.append(conn.execute("SELECT MAX(id) AS i FROM opportunities").fetchone()["i"])
    assert tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[0], "status": "descartada", "learning": "curto"}).startswith("erro: para descartar")
    assert "agora está 'teste'" in tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[0], "status": "teste", "learning": "vai ao ar na loja"})
    assert "agora está 'teste'" in tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[1], "status": "teste", "learning": "página de calculadora"})
    assert tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[2], "status": "teste", "learning": "terceiro"}).startswith("erro: já há 2 testes")
    # só UM pedido ao dono, quando a ideia que depende dele avança
    reqs = [r["body"] for r in conn.execute("SELECT body FROM owner_requests").fetchall()]
    assert len(reqs) == 1 and "aceitar os termos" in reqs[0]
    tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[0], "status": "ativa", "learning": "primeiras visitas"})
    assert conn.execute("SELECT COUNT(*) AS n FROM owner_requests").fetchone()["n"] == 1
    assert "agora está 'descartada'" in tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[1], "status": "descartada", "learning": "sem nenhuma visita medida em 3 dias; descartada"})
    assert conn.execute("SELECT decided_at IS NOT NULL AS d FROM opportunities WHERE id=%s", (ids[1],)).fetchone()["d"]
    assert "agora está 'teste'" in tools.run_tool(conn, ceo, "update_opportunity", {"id": ids[2], "status": "teste", "learning": "agora cabe"})
    assert tools.run_tool(conn, ceo, "update_opportunity", {"id": 9999, "status": "teste", "learning": "x"}).startswith("erro: oportunidade não existe")
    assert "metrics" not in tools.SPECS[[s["name"] for s in tools.SPECS].index("update_opportunity")]["parameters"]["properties"]   # agente não escreve medições


def test_prompt_keeps_five_ideas_and_limits_tests(conn):
    from meridiano.engine import agent, tools
    ceo = _ceo(conn)
    p = agent.build_prompt(conn, ceo)
    assert "MESA DE OPORTUNIDADES" in p and "(vazia)" in p and "mantenha no mínimo 5" in p
    for i, idea in enumerate(["Calculadora web de reajuste de aluguel pelos índices oficiais", "Newsletter semanal com resumo de editais públicos abertos",
                              "Relatório de pesquisa de mercado sob encomenda para microempresas", "Tradução técnica de manuais curtos sob encomenda",
                              "Análise de currículo com checklist objetivo sob encomenda"]):
        tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea=idea))
    p = agent.build_prompt(conn, ceo)
    assert "mantenha no mínimo" not in p and "escolha a ideia de menor esforço" in p
    conn.execute("UPDATE opportunities SET status='teste' WHERE id IN (1,2)")
    p = agent.build_prompt(conn, ceo)
    assert "escolha a ideia de menor esforço" not in p and "Descarte rápido" in p and "dado real" in p


def test_api_lists_opportunities_with_status_order(conn, monkeypatch):
    from meridiano.engine import tools
    c = _client(conn, monkeypatch)
    ceo = _ceo(conn)
    for idea in ("Calculadora web de reajuste de aluguel pelos índices oficiais", "Newsletter semanal com resumo de editais públicos abertos"):
        tools.run_tool(conn, ceo, "propose_opportunity", dict(IDEA, idea=idea))
    tools.run_tool(conn, ceo, "update_opportunity", {"id": 2, "status": "teste", "learning": "no ar"})
    r = c.get("/api/opportunities", headers={"X-Token": "leitura"}).json()
    assert [o["status"] for o in r] == ["teste", "ideia"] and r[0]["autor"] == "Rafael" and r[0]["metrics"] == "{}"
    assert c.get("/api/opportunities").status_code == 401


# ---------- token por arquivo no servidor (caminho b) ----------

@pytest.fixture()
def secdir(tmp_path, monkeypatch):
    from meridiano.engine import config, secrets_store as ss
    d = tmp_path / "secrets"
    d.mkdir(mode=0o700)
    monkeypatch.setattr(config, "SECRETS_DIR", str(d))
    monkeypatch.setattr(ss, "_state", {"fp": None, "last": 0.0})
    return d


def _verify_counter(monkeypatch, result=("ok", "token aceito pelo Mercado Pago")):
    from meridiano.engine import secrets_store as ss
    calls = []
    monkeypatch.setattr(ss, "verify", lambda t: calls.append(t) or result)
    return calls


@pytest.mark.parametrize("conteudo", [TOKEN + "\n", "MP_ACCESS_TOKEN=" + TOKEN + "\n", "export X='" + TOKEN + "'", '"' + TOKEN + '"', "# comentario\n" + TOKEN])
def test_admin_created_token_file_is_picked_up_without_restart(secdir, monkeypatch, conteudo):
    from meridiano.engine import secrets_store as ss
    calls = _verify_counter(monkeypatch)
    assert ss.configured() is False and ss.status()["configurado"] is False
    (secdir / "mp-token").write_text(conteudo)
    os.chmod(secdir / "mp-token", 0o600)
    assert ss.configured() is True and ss.read_token() == TOKEN
    ss.refresh_meta()
    st = ss.status()
    assert st["configurado"] and st["verificado"] and st["modo"] == "producao" and st["problema"] is None and calls == [TOKEN]
    assert TOKEN not in str(st) and "abcdef0123456789" not in str(st)


def test_token_file_is_reverified_every_60_seconds_and_when_it_changes(secdir, monkeypatch):
    from meridiano.engine import secrets_store as ss
    calls = _verify_counter(monkeypatch)
    (secdir / "mp-token").write_text(TOKEN)
    ss.refresh_meta(); ss.refresh_meta(); ss.refresh_meta()
    assert len(calls) == 1                                            # dentro de 60 s não repete
    ss._state["last"] -= 61
    ss.refresh_meta()
    assert len(calls) == 2                                            # passou de 60 s
    outro = "TEST-" + "x1y2z3w4" * 5
    (secdir / "mp-token").write_text(outro)
    ss.refresh_meta()
    assert len(calls) == 3 and calls[-1] == outro and ss.status()["modo"] == "teste"      # arquivo mudou: reverifica na hora


def test_token_file_problems_are_reported_not_hidden(secdir, monkeypatch):
    from meridiano.engine import secrets_store as ss
    calls = _verify_counter(monkeypatch, ("invalido", "o Mercado Pago recusou este token"))
    (secdir / "mp-token").write_text("isso não é um token")
    st = ss.status()
    assert st["configurado"] is False and "não parece um Access Token" in st["problema"] and calls == []
    (secdir / "mp-token").write_text(TOKEN)
    ss.refresh_meta()
    st = ss.status()
    assert st["configurado"] and st["verificado"] is False and "recusou" in st["detalhe"]                # token presente, mas recusado: aparece
    assert (secdir / "mp-token").exists()                                                               # nunca apaga o arquivo do administrador


def test_loose_permissions_are_fixed_and_reported(secdir):
    from meridiano.engine import secrets_store as ss
    (secdir / "mp-token").write_text(TOKEN)
    os.chmod(secdir / "mp-token", 0o644); os.chmod(secdir, 0o755)
    assert ss.ensure_private() is True
    assert oct(os.stat(secdir / "mp-token").st_mode & 0o777) == "0o600" and oct(os.stat(secdir).st_mode & 0o777) == "0o700"


def test_legacy_file_name_still_works(secdir, monkeypatch):
    from meridiano.engine import secrets_store as ss
    _verify_counter(monkeypatch)
    (secdir / "mp_access_token").write_text(TOKEN)
    assert ss.configured() and ss.read_token() == TOKEN
    (secdir / "mp-token").write_text("TEST-" + "k9l8m7n6" * 5)                                          # o nome novo tem prioridade
    assert ss.read_token().startswith("TEST-")


def test_server_files_close_the_owner_request_without_the_panel(conn, secdir, monkeypatch):
    from meridiano.engine import secrets_store as ss, store
    _verify_counter(monkeypatch)
    ss.sync_owner_request(conn)
    assert conn.execute("SELECT done FROM owner_requests").fetchone()["done"] is False
    (secdir / "mp-token").write_text(TOKEN)
    ss.refresh(conn)
    assert conn.execute("SELECT done FROM owner_requests").fetchone()["done"] is False                    # falta a identificação
    (secdir / "store.json").write_text('{"seller_name": "Nome Completo", "contact_email": "contato@exemplo.com.br"}')
    ss.refresh(conn)
    assert store.get_settings(conn) == {"seller_name": "Nome Completo", "contact_email": "contato@exemplo.com.br"}
    assert conn.execute("SELECT done,resolution FROM owner_requests").fetchone() == {"done": True, "resolution": "token e identificação recebidos"}
    # arquivo de identificação inválido não grava nada
    (secdir / "store.json").write_text('{"seller_name": "N", "contact_email": "sem-arroba"}')
    assert ss.import_store_file(conn) is False
    assert store.get_settings(conn)["seller_name"] == "Nome Completo"


def test_status_route_reflects_file_and_verifies_in_background(conn, secdir, monkeypatch):
    from meridiano.engine import secrets_store as ss
    c = _client(conn, monkeypatch)
    _verify_counter(monkeypatch)
    (secdir / "mp-token").write_text(TOKEN)
    r = c.get("/api/secrets/mp-token", headers={"X-Token": "leitura"})
    assert r.json()["configurado"] is True and TOKEN not in r.text
    ss._thread.join(5)                                                                                    # reverificação em segundo plano
    r = c.get("/api/secrets/mp-token", headers={"X-Token": "leitura"}).json()
    assert r["verificado"] is True and r["modo"] == "producao" and r["permissoes_ok"] is True and r["problema"] is None
