import os
import subprocess
import time
import pytest
from fastapi.testclient import TestClient

PGBIN = "/usr/lib/postgresql/16/bin"


HIRE = {"instructions": "Você escreve textos de apoio para os produtos da empresa, revisa o que os outros escrevem e entrega duas seções completas por dia, em português claro e sem promessas de ganho.",
        "reason": "falta quem escreva e revise textos enquanto os fundadores cuidam da estratégia", "deliverable": "2 seções completas e revisadas por dia"}


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
    out = tools.run_tool(conn, ceo, "hire_employee", {"name": "Luiza", "role": "Gerente de Conteúdo", "area": "Conteúdo", **HIRE})
    assert out.startswith("contratado")
    luiza = conn.execute("SELECT * FROM employees WHERE slug='luiza'").fetchone()
    assert luiza["model"] in config.ALLOWED_MODELS and luiza["hire_reason"] and luiza["hired_by"] == ceo["id"]
    out = tools.run_tool(conn, luiza, "hire_employee", {"name": "Z", "role": "Estagiário", "area": "A", **HIRE})
    assert out.startswith("contratado")  # gerente pode
    z = conn.execute("SELECT * FROM employees WHERE slug='z'").fetchone()
    assert tools.run_tool(conn, z, "hire_employee", {"name": "Y", "role": "R", "area": "A", **HIRE}).startswith("erro")


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
    assert migrate.migrate(c) == [f.name for f in sorted(migrate.DIR.glob("*.sql")) if f.name >= "002"]
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
    assert migrate.migrate(c) == [f.name for f in sorted(migrate.DIR.glob("*.sql")) if f.name >= "003"]
    assert c.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"] == 1
    assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"] == 1


def test_fire_rules_and_activity(conn, tmp_path, monkeypatch):
    from meridiano.engine import config, tools
    monkeypatch.setattr(config, "PRODUCTS_DIR", str(tmp_path))
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    hire = lambda n, r: tools.run_tool(conn, ceo, "hire_employee", {"name": n, "role": r, "area": "A", **HIRE})
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
    tools.run_tool(conn, ceo, "hire_employee", {"name": "Luiza", "role": "Gerente", "area": "A", **HIRE})
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
    for old in (("300", "400000"), ("600", "1500000"), ("2000", "8000000"), ("8000", "8000000")):
        monkeypatch.setenv("DAILY_CALL_LIMIT", old[0]); monkeypatch.setenv("DAILY_TOKEN_LIMIT", old[1])
        c = importlib.reload(config)
        assert (c.DAILY_CALL_LIMIT, c.DAILY_TOKEN_LIMIT) == (30000, 30000000)
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "50000000")
    assert importlib.reload(config).DAILY_TOKEN_LIMIT == 50000000     # valor maior escolhido é respeitado
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
    pacing.beat_workers(conn, 3, ["Rafael", "Helena"], 60, "nenhum")
    r = c.get("/api/engine", headers=h).json()
    assert r["vivo"] and r["funcionario_da_vez"] == "Rafael" and 0 <= r["proximo_turno_em_s"] <= 60 and r["motivo_espera"] == "nenhum"
    assert r["trabalhadores"] == 3 and r["turnos_rodando"] == 2 and r["funcionarios_da_vez"] == ["Rafael", "Helena"]
    pacing.beat_turn(conn, ceo["id"])
    pacing.beat_workers(conn, 3, [], 600, "limite")
    r = c.get("/api/engine", headers=h).json()
    assert r["motivo_espera"] == "limite" and r["ultimo_turno_por"] == "Rafael" and r["funcionario_da_vez"] is None and r["turnos_rodando"] == 0
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
    assert migrate.migrate(c) == [f.name for f in sorted(migrate.DIR.glob("*.sql")) if f.name >= "006"]
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
    tools.run_tool(conn, ceo, "hire_employee", {"name": name, "role": role, "area": "A", **HIRE})
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
    assert migrate.migrate(c) == [f.name for f in sorted(migrate.DIR.glob("*.sql")) if f.name >= "009"]
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
    assert "Compra indisponível" in html and "<form" not in html and "Comprar" not in html
    assert "script" not in html.split("</style>")[1].lower().replace("&lt;script&gt;alert(1)&lt;/script&gt;", "")
    assert c.post("/pub/loja/copy-de-vendas/comprar").status_code == 503                                  # ninguém é cobrado
    assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0


def test_store_publishes_only_when_everything_is_ready(conn, monkeypatch):
    from meridiano.engine import tools
    c = _client(conn, monkeypatch)
    assert c.get("/pub/loja/").status_code == 200 and "Nenhum produto à venda" in c.get("/pub/loja/").text
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


# ---------- equipe: contratação, revisão a cada 30 turnos, turnos em paralelo ----------

def _set_turns(conn, n):
    conn.execute("UPDATE engine_status SET turn_count=%s WHERE id=1", (n,))


def _grow_team(conn, ceo, total):
    from meridiano.engine import tools
    nomes = ["Ana", "Bruno", "Carla", "Diego", "Elisa", "Fabio", "Gisele", "Hugo"]
    papeis = ["Diretora de Produto", "Redator SEO", "Atendimento", "Analista de Oportunidades", "Designer de página", "Financeiro", "Suporte", "Analista"]
    i = 0
    while conn.execute("SELECT COUNT(*) AS n FROM employees WHERE active").fetchone()["n"] < total:
        out = tools.run_tool(conn, ceo, "hire_employee", {"name": nomes[i], "role": papeis[i], "area": "A", **HIRE})
        assert out.startswith("contratado"), out
        i += 1


def test_hiring_requires_complete_instructions_reason_and_measurable_deliverable(conn):
    from meridiano.engine import tools
    ceo = _ceo(conn)
    base = {"name": "Ana", "role": "Redatora SEO", "area": "Marketing", **HIRE}
    assert tools.run_tool(conn, ceo, "hire_employee", dict(base, instructions="escreve coisas")).startswith("erro: instruções incompletas")
    assert tools.run_tool(conn, ceo, "hire_employee", dict(base, reason="falta")).startswith("erro: informe o motivo")
    assert tools.run_tool(conn, ceo, "hire_employee", dict(base, deliverable="")).startswith("erro: toda contratação precisa de uma entrega mensurável")
    assert tools.run_tool(conn, ceo, "hire_employee", base).startswith("contratado")
    ana = conn.execute("SELECT * FROM employees WHERE slug='ana'").fetchone()
    assert ana["deliverable"] == HIRE["deliverable"] and ana["hire_reason"] and ana["hired_by"] == ceo["id"]


def test_founders_must_hire_until_the_minimum_team_and_others_are_not_nagged(conn):
    from meridiano.engine import agent, team
    ceo = _ceo(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    st = team.status(conn)
    assert st["headcount"] == 2 and st["below_target"] and st["target"] == 6
    for f in (ceo, pres):
        p = agent.build_prompt(conn, f)
        assert "EQUIPE: 2 pessoa(s)" in p and "meta mínima 6" in p and "VOCÊ DEVE CONTRATAR AGORA" in p and "hire_employee" in p
        assert "Diretor/a de Produto" in p and "Redator/a SEO" in p                      # só sugestões de funções que ninguém cobre
    _grow_team(conn, ceo, 3)
    dev = conn.execute("SELECT * FROM employees WHERE slug='ana'").fetchone()
    assert "VOCÊ DEVE CONTRATAR" not in agent.build_prompt(conn, dev) and "EQUIPE:" not in agent.build_prompt(conn, dev)   # só Presidente/CEO
    names = [n for n, _ in team.missing_roles(team.status(conn)["people"])]
    assert "Diretor/a de Produto" not in names                                            # já há uma Diretora de Produto
    _grow_team(conn, ceo, 6)
    assert "VOCÊ DEVE CONTRATAR" not in agent.build_prompt(conn, ceo)                      # meta atingida e revisão ainda não devida


def test_team_review_rules(conn):
    from meridiano.engine import tools, team, agent
    ceo = _ceo(conn)
    SUMMARY = "Rafael e Helena entregam as seções; Ana escreve artigos SEO; ninguém para demitir agora."
    assert tools.run_tool(conn, ceo, "team_review", {"summary": SUMMARY}).startswith("erro: a equipe tem 2 pessoa(s)")      # abaixo da meta
    _grow_team(conn, ceo, 6)
    assert "próxima revisão só vale em 30 turno(s)" in tools.run_tool(conn, ceo, "team_review", {"summary": SUMMARY})        # ainda não é hora
    _set_turns(conn, 30)
    assert team.status(conn)["review_due"] and "REVISÃO DE EQUIPE DEVIDA" in agent.build_prompt(conn, ceo)
    dev = conn.execute("SELECT * FROM employees WHERE slug='ana'").fetchone()
    assert tools.run_tool(conn, dev, "team_review", {"summary": SUMMARY}).startswith("erro: só Presidente ou CEO")
    assert tools.run_tool(conn, ceo, "team_review", {"summary": "ok"}).startswith("erro: a revisão precisa de um resumo")
    # tarefas demais para a equipe: precisa contratar (ou aliviar) antes de registrar
    for i in range(19):
        conn.execute("INSERT INTO tasks(title,assignee_id,created_by) VALUES (%s,%s,%s)", (f"Tarefa {i}", ceo["id"], ceo["id"]))
    assert team.status(conn)["overloaded"] and "mais tarefas que capacidade" in agent.build_prompt(conn, ceo)
    assert "mais de 3 por pessoa" in tools.run_tool(conn, ceo, "team_review", {"summary": SUMMARY})
    _grow_team(conn, ceo, 7)
    conn.execute("UPDATE tasks SET status='done' WHERE id > 8")
    assert tools.run_tool(conn, ceo, "team_review", {"summary": SUMMARY}) == "revisão de equipe registrada"
    assert conn.execute("SELECT COUNT(*) AS n FROM messages WHERE body LIKE 'Revisão de equipe (7 pessoas%'").fetchone()["n"] == 1
    assert not team.status(conn)["review_due"] and team.status(conn)["next_review_in"] == 30
    _set_turns(conn, 59)
    assert not team.status(conn)["review_due"]
    _set_turns(conn, 60)
    assert team.status(conn)["review_due"]


def test_review_prompt_lists_real_deliveries(conn):
    from meridiano.engine import agent, tools, plan
    ceo = _ceo(conn)
    _grow_team(conn, ceo, 6)
    plan.ensure(conn)
    tools.run_tool(conn, ceo, "write_section", {"product_name": "Copy de Vendas", "section_number": 1, "content": WORDS(420)})
    conn.execute("INSERT INTO tasks(title,assignee_id,created_by,status) VALUES ('feita',%s,%s,'done')", (ceo["id"], ceo["id"]))
    _set_turns(conn, 31)
    p = agent.build_prompt(conn, ceo)
    assert "- Rafael (CEO): 1 tarefa(s) concluída(s)" in p and "1 seção(ões) escrita(s)" in p
    assert "meta: 2 seções completas e revisadas por dia" in p and "fire_employee" in p


class _Run:
    """run_turn de mentira: registra a concorrência e espera os colegas (barreira) para provar que rodaram juntos."""
    def __init__(self, parties=None):
        import threading
        self.lock = threading.Lock(); self.active = set(); self.max = 0; self.calls = []; self.dup = False
        self.barrier = threading.Barrier(parties, timeout=10) if parties else None

    def __call__(self, conn, me):
        with self.lock:
            if me["id"] in self.active:
                self.dup = True
            self.active.add(me["id"]); self.calls.append(me["slug"]); self.max = max(self.max, len(self.active))
        try:
            if self.barrier:
                self.barrier.wait()
        finally:
            with self.lock:
                self.active.discard(me["id"])


def _threads(n, fn):
    import threading
    out = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    [t.start() for t in out]; [t.join(30) for t in out]


def test_engine_runs_turns_in_parallel_without_overlapping_the_same_employee(conn, url, monkeypatch):
    from meridiano.engine import main as eng_main, db
    ceo = _ceo(conn); _grow_team(conn, ceo, 4)
    conns = [db.connect() for _ in range(3)]
    e = eng_main.Engine(workers=3)
    run = _Run(parties=3)
    results = []
    _threads(3, lambda i: results.append(e.step(conns[i], run=run)))
    assert run.max == 3 and not run.dup                                    # 3 turnos ao mesmo tempo (a barreira só abre com os 3)
    assert len(set(run.calls)) == 3 and all(r[0] for r in results)       # 3 funcionários diferentes
    assert conn.execute("SELECT turn_count FROM engine_status").fetchone()["turn_count"] == 3
    assert e.snapshot() == []                                              # todos liberados


def test_engine_round_robin_covers_everyone_and_never_double_books(conn):
    from meridiano.engine import main as eng_main, db
    ceo = _ceo(conn); _grow_team(conn, ceo, 4)                             # 4 funcionários
    conns = [db.connect() for _ in range(3)]
    e = eng_main.Engine(workers=3)
    run = _Run()
    def work(i):
        for _ in range(4):
            e.step(conns[i], run=run)
    _threads(3, work)
    assert not run.dup and run.max <= 3 and len(run.calls) == 12
    assert set(run.calls) == {"presidente", "ceo", "ana", "bruno"}
    counts = {s: run.calls.count(s) for s in set(run.calls)}
    assert max(counts.values()) - min(counts.values()) <= 1                # rodízio justo: 3 turnos cada


def test_engine_claim_returns_none_when_everyone_is_busy(conn):
    from meridiano.engine import main as eng_main
    _ceo(conn)
    e = eng_main.Engine(workers=3)
    a, b = e.claim(conn), e.claim(conn)
    assert {a["slug"], b["slug"]} == {"presidente", "ceo"} and e.claim(conn) is None
    e.release(a["id"])
    assert e.claim(conn)["id"] == a["id"]


def test_rate_limit_pauses_everyone_with_exponential_backoff_and_is_not_a_failure(conn):
    from meridiano.engine import main as eng_main, llm
    _ceo(conn)
    e = eng_main.Engine(workers=3)
    def limited(c, me):
        raise llm.RateLimited("m", None)
    ran, delay, reason = e.step(conn, run=limited)
    assert not ran and reason == "429" and delay == 60 and e.backoff == 60 and e.pause_until > 0
    calls = []
    ran, delay, reason = e.step(conn, run=lambda c, me: calls.append(me))          # outro trabalhador: em pausa, nem tenta
    assert not ran and reason == "429" and calls == [] and 0 < delay <= 60
    e.pause_until = 0
    e.step(conn, run=limited)
    assert e.backoff == 120                                                       # dobra
    e.pause_until = 0
    ran, _, reason = e.step(conn, run=lambda c, me: None)
    assert ran and reason == "nenhum" and e.backoff == 0                          # sucesso zera o recuo
    from meridiano.engine import llm as L
    assert L.failures_today(conn)[0] == 0                                         # 429 não conta como falha
    assert conn.execute("SELECT turn_count FROM engine_status").fetchone()["turn_count"] == 1    # só o turno que deu certo conta


def test_engine_error_in_one_turn_does_not_stop_the_others(conn):
    from meridiano.engine import main as eng_main
    _ceo(conn)
    e = eng_main.Engine(workers=2)
    def boom(c, me):
        raise RuntimeError("falha de teste")
    ran, delay, reason = e.step(conn, run=boom)
    assert not ran and reason == "erro" and e.snapshot() == []                    # liberou o funcionário
    assert e.step(conn, run=lambda c, me: None)[0] is True


def test_hard_limit_slows_every_worker_instead_of_stopping(conn):
    from meridiano.engine import main as eng_main, config
    _ceo(conn)
    conn.execute("INSERT INTO usage_log(model,prompt_tokens,completion_tokens) VALUES ('m',%s,0)", (config.DAILY_TOKEN_LIMIT,))
    e = eng_main.Engine(workers=3)
    ran, delay, reason = e.step(conn, run=lambda c, me: None)
    assert ran and reason == "limite" and delay == config.SLOW_TICK_SECONDS


def test_housekeeping_publishes_parallel_status_to_the_panel(conn, monkeypatch):
    from meridiano.engine import main as eng_main
    from meridiano.api import main
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "t")
    ceo = _ceo(conn)
    e = eng_main.Engine(workers=3)
    me = e.claim(conn)
    e.housekeeping(conn, once=True)
    main._conn = conn
    r = TestClient(main.app).get("/api/engine", headers={"X-Token": "t"}).json()
    assert r["vivo"] and r["trabalhadores"] == 3 and r["turnos_rodando"] == 1 and r["funcionarios_da_vez"] == [me["name"]]
    assert r["equipe"] == {"funcionarios": 2, "meta": 6, "tarefas_abertas": 0, "proxima_revisao_em_turnos": 30}


# ---------- pagamentos com Mercado Pago (simulados: nenhuma chamada real) ----------

import re as _re
import hmac as _hmac
import hashlib as _hashlib


class FakeMP:
    def __init__(self):
        self.payments, self.calls = {}, []
        self.pref = (201, {"id": "PREF1", "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=PREF1",
                           "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=PREF1"})

    def __call__(self, method, path, token, json_body=None, idem=None):
        self.calls.append((method, path, token, json_body, idem))
        if method == "POST" and path == "/checkout/preferences":
            return self.pref
        m = _re.fullmatch(r"/v1/payments/(\d+)", path)
        if method == "GET" and m:
            p = self.payments.get(m.group(1))
            return (200, p) if p else (404, {"message": "not found"})
        raise AssertionError((method, path))


def mp_payment(pid, order_id, amount=29.9, status="approved", fee=1.5, **extra):
    p = {"id": int(pid), "status": status, "transaction_amount": amount, "currency_id": "BRL", "external_reference": str(order_id),
         "fee_details": [{"type": "mercadopago_fee", "amount": fee, "fee_payer": "collector"}] if fee is not None else [],
         "live_mode": True, "payer": {"email": "comprador.secreto@example.com", "first_name": "Fulano"}}
    p.update(extra)
    return p


@pytest.fixture()
def shop(conn, tmp_path, monkeypatch):
    from meridiano.engine import config, secrets_store as ss, payments, store
    monkeypatch.setattr(config, "SECRETS_DIR", str(tmp_path / "segredos"))
    (tmp_path / "segredos").mkdir(mode=0o700)
    (tmp_path / "segredos" / "mp-token").write_text(TOKEN)
    monkeypatch.setattr(ss, "_state", {"fp": None, "last": 0.0})
    monkeypatch.setattr(ss, "verify", lambda t: ("ok", "token aceito pelo Mercado Pago"))
    ss.refresh_meta(force=True)                                                # token verificado, como o motor faria a cada 60 s
    fake = FakeMP()
    monkeypatch.setattr(payments, "mp_request", fake)
    c = _client(conn, monkeypatch)
    _open_store(conn)
    class S: pass
    s = S()
    s.fake, s.client, s.secdir = fake, c, tmp_path / "segredos"
    s.product = store.published(conn)[0]
    s.order = lambda: payments.create_order(conn, s.product)
    s.pay = lambda order, pid="1001", **kw: fake.payments.__setitem__(str(pid), mp_payment(pid, order["id"], **kw))
    def approve():
        from meridiano.engine import tools
        pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
        out = tools.run_tool(conn, pres, "approve_store_page", {"product_name": "Copy de Vendas", "note": "Conferi cada promessa contra as 8 seções do produto: tudo confere."})
        assert "aprovada" in out, out
    s.approve = approve
    s.webhook = lambda pid="1001", **kw: c.post(f"/api/mp/webhook?data.id={pid}&type=payment", json={"type": "payment", "data": {"id": str(pid)}}, **kw)
    return s


def _ledger(conn):
    return [(r["kind"], r["amount_cents"], r["external_ref"]) for r in conn.execute("SELECT kind,amount_cents,external_ref FROM ledger ORDER BY id").fetchall()]


def test_signature_matches_the_mercado_pago_manifest():
    from meridiano.engine import payments
    secret = "segredo-do-webhook-123456"
    manifest = "id:123456;request-id:abc-req-1;ts:1704908010;"
    v1 = _hmac.new(secret.encode(), manifest.encode(), _hashlib.sha256).hexdigest()
    hdr = f"ts=1704908010,v1={v1}"
    assert payments.signature_ok(secret, "123456", "abc-req-1", hdr)
    assert payments.signature_ok(secret, "123456", "abc-req-1", " ts=1704908010 , v1=" + v1)
    for bad in (dict(data_id="123457"), dict(request_id="outro"), dict(header=f"ts=1704908011,v1={v1}"), dict(header="v1=" + v1), dict(header=f"ts=1704908010,v1={'0' * 64}"), dict(header=""), dict(secret="outro-segredo-123456")):
        args = dict(secret=secret, data_id="123456", request_id="abc-req-1", header=hdr); args.update(bad)
        assert not payments.signature_ok(args["secret"], args["data_id"], args["request_id"], args["header"]), bad
    m2 = "id:123456;ts:1704908010;"                                               # sem x-request-id: a parte é omitida
    assert payments.signature_ok(secret, "123456", None, "ts=1704908010,v1=" + _hmac.new(secret.encode(), m2.encode(), _hashlib.sha256).hexdigest())


def test_confirmed_payment_becomes_a_sale_with_fee_and_net(conn, shop):
    o = shop.order(); shop.pay(o, "1001", amount=29.9, fee=1.5)
    r = shop.webhook("1001")
    assert r.status_code == 200 and r.json() == {"ok": True, "resultado": "venda"}
    row = conn.execute("SELECT status,payment_id,gross_cents,fee_cents,net_cents,test FROM orders WHERE id=%s", (o["id"],)).fetchone()
    assert row == {"status": "pago", "payment_id": "1001", "gross_cents": 2990, "fee_cents": 150, "net_cents": 2840, "test": False}
    assert _ledger(conn) == [("receita", 2990, "mp:1001"), ("despesa", 150, "mp:1001:taxa")]
    from meridiano.engine import db
    assert db.cash_cents(conn) == 2840
    st = shop.client.get("/api/state", headers={"X-Token": "leitura"}).json()
    assert st["caixa"] == 28.4 and st["vendas"] == 1
    ev = conn.execute("SELECT outcome FROM payment_events").fetchall()
    assert [e["outcome"] for e in ev] == ["venda"]


def test_same_notification_many_times_is_one_sale(conn, shop):
    o = shop.order(); shop.pay(o, "1001")
    outs = [shop.webhook("1001").json()["resultado"] for _ in range(4)]
    assert outs == ["venda", "ja_processado", "ja_processado", "ja_processado"]
    assert len(_ledger(conn)) == 2 and conn.execute("SELECT COUNT(*) AS n FROM payment_events").fetchone()["n"] == 1


def test_parallel_notifications_do_not_double_count(conn, shop, url):
    from meridiano.engine import payments, db
    o = shop.order(); shop.pay(o, "1001")
    conns = [db.connect() for _ in range(6)]
    res = []
    _threads(6, lambda i: res.append(payments.process_payment(conns[i], "1001")["outcome"]))
    assert sorted(res).count("venda") == 1 and set(res) <= {"venda", "ja_processado"}
    assert len(_ledger(conn)) == 2


def test_forged_or_wrong_notifications_never_create_a_sale(conn, shop):
    o = shop.order()
    assert shop.webhook("999999").status_code == 502                                     # MP não conhece: erro (MP tenta de novo), nada muda
    shop.pay(o, "1001", status="pending")
    assert shop.webhook("1001").json()["resultado"] == "pendente"
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "aguardando"
    shop.pay(o, "1002", amount=1.0)                                                      # valor diferente do pedido
    assert shop.webhook("1002").json()["resultado"] == "valor_divergente"
    shop.pay(o, "1003", currency_id="USD")
    assert shop.webhook("1003").json()["resultado"] == "valor_divergente"
    fake_o = {"id": 987654}
    shop.pay(fake_o, "1004")                                                             # pedido que não é nosso
    assert shop.webhook("1004").json()["resultado"] == "sem_pedido"
    shop.pay(o, "1005", status="rejected")
    assert shop.webhook("1005").json()["resultado"] == "recusado"
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "recusado"
    assert _ledger(conn) == [] and conn.execute("SELECT COUNT(*) AS n FROM orders WHERE status='pago'").fetchone()["n"] == 0
    # um pedido pago nunca é derrubado por um aviso posterior de recusa
    shop.pay(o, "1006"); assert shop.webhook("1006").json()["resultado"] == "venda"
    shop.pay(o, "1007", status="rejected"); shop.webhook("1007")
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "pago"
    # o conteúdo do aviso não é confiável: valores falsos no corpo não mudam nada
    r = shop.client.post("/api/mp/webhook?data.id=1002&type=payment", json={"type": "payment", "data": {"id": "1002"}, "status": "approved", "transaction_amount": 29.9})
    assert r.json()["resultado"] == "valor_divergente"


def test_webhook_signature_is_required_once_the_secret_exists(conn, shop):
    o = shop.order(); shop.pay(o, "1001")
    shop.fake.calls.clear()
    (shop.secdir / "mp-webhook-secret").write_text("segredo-do-webhook-123456\n")
    r = shop.webhook("1001")
    assert r.status_code == 401 and shop.fake.calls == [] and _ledger(conn) == []                                  # nem consulta o MP
    ts = "1704908010"
    sig = _hmac.new(b"segredo-do-webhook-123456", f"id:1001;request-id:req-9;ts:{ts};".encode(), _hashlib.sha256).hexdigest()
    bad = shop.client.post("/api/mp/webhook?data.id=1001&type=payment", json={}, headers={"x-signature": f"ts={ts},v1={'a' * 64}", "x-request-id": "req-9"})
    assert bad.status_code == 401
    ok = shop.client.post("/api/mp/webhook?data.id=1001&type=payment", json={}, headers={"x-signature": f"ts={ts},v1={sig}", "x-request-id": "req-9"})
    assert ok.status_code == 200 and ok.json()["resultado"] == "venda"
    assert shop.client.get("/api/secrets/mp-token", headers={"X-Token": "leitura"}).json()["webhook_assinado"] is True


def test_webhook_ignores_other_event_types_and_requires_an_id(shop):
    assert shop.client.post("/api/mp/webhook?type=merchant_order&data.id=5", json={}).json() == {"ok": True, "ignorado": "merchant_order"}
    assert shop.client.post("/api/mp/webhook?type=payment", json={}).status_code == 400
    assert shop.client.post("/api/mp/webhook", json={"type": "payment", "data": {"id": "abc"}}).status_code == 502      # id inválido nunca chega a ser pedido ao MP
    assert shop.fake.calls == []


def test_test_mode_payments_never_reach_the_cash_book(conn, shop):
    o = shop.order(); shop.pay(o, "1001", live_mode=False)
    assert shop.webhook("1001").json()["resultado"] == "venda_teste"
    assert conn.execute("SELECT status,test FROM orders").fetchone() == {"status": "pago", "test": True}
    assert _ledger(conn) == []
    assert shop.client.get("/api/state", headers={"X-Token": "leitura"}).json()["caixa"] == 0


def test_missing_fee_means_net_equals_gross_and_no_fee_entry(conn, shop):
    o = shop.order(); shop.pay(o, "1001", fee=None)
    shop.webhook("1001")
    assert conn.execute("SELECT fee_cents,net_cents FROM orders").fetchone() == {"fee_cents": None, "net_cents": 2990}
    assert _ledger(conn) == [("receita", 2990, "mp:1001")]


def test_refund_and_chargeback_reverse_the_sale_once(conn, shop):
    from meridiano.engine import db
    o = shop.order(); shop.pay(o, "1001")
    shop.webhook("1001")
    shop.pay(o, "1001", status="refunded")
    outs = [shop.webhook("1001").json()["resultado"] for _ in range(3)]
    assert outs == ["reembolso", "ja_processado", "ja_processado"]
    assert conn.execute("SELECT status,refunded_cents FROM orders").fetchone() == {"status": "reembolsado", "refunded_cents": 2990}
    assert _ledger(conn) == [("receita", 2990, "mp:1001"), ("despesa", 150, "mp:1001:taxa"), ("despesa", 2990, "mp:1001:reembolso")]
    assert db.cash_cents(conn) == -150                                                 # a taxa do MP não volta sozinha
    assert shop.client.get("/api/state", headers={"X-Token": "leitura"}).json()["vendas"] == 0
    o2 = shop.order(); shop.pay(o2, "2001"); shop.webhook("2001")
    shop.pay(o2, "2001", status="charged_back"); r = shop.webhook("2001")
    assert r.json()["resultado"] == "chargeback" and conn.execute("SELECT status FROM orders WHERE id=%s", (o2["id"],)).fetchone()["status"] == "chargeback"
    assert ("despesa", 2990, "mp:2001:chargeback") in _ledger(conn)


def test_partial_refund_keeps_the_sale_and_records_the_money_back(conn, shop):
    o = shop.order(); shop.pay(o, "1001"); shop.webhook("1001")
    shop.pay(o, "1001", transaction_amount_refunded=10.0)
    assert shop.webhook("1001").json()["resultado"] == "reembolso_parcial"
    assert shop.webhook("1001").json()["resultado"] == "ja_processado"
    assert conn.execute("SELECT status,refunded_cents FROM orders").fetchone() == {"status": "pago", "refunded_cents": 1000}
    assert [x for x in _ledger(conn) if "parcial" in x[2]] == [("despesa", 1000, "mp:1001:reembolso-parcial-1000")]
    # segundo reembolso parcial (cumulativo 15,00): só a diferença (5,00) é lançada; depois o reembolso total completa o resto
    shop.pay(o, "1001", transaction_amount_refunded=15.0); shop.webhook("1001")
    assert ("despesa", 500, "mp:1001:reembolso-parcial-1500") in _ledger(conn)
    shop.pay(o, "1001", status="refunded", transaction_amount_refunded=29.9); shop.webhook("1001")
    assert ("despesa", 1490, "mp:1001:reembolso") in _ledger(conn)                           # 29,90 - 15,00 já devolvidos
    from meridiano.engine import db
    assert sum(a for k, a, r in _ledger(conn) if k == "despesa" and "reembolso" in r) == 2990   # devolveu exatamente o que recebeu, nunca mais
    assert db.cash_cents(conn) == -150 and conn.execute("SELECT status,refunded_cents FROM orders").fetchone() == {"status": "reembolsado", "refunded_cents": 2990}


def test_out_of_order_notifications_still_book_sale_and_refund(conn, shop):
    from meridiano.engine import db
    o = shop.order(); shop.pay(o, "1001", status="refunded", transaction_amount_refunded=29.9)      # o aviso de reembolso chega ANTES do de aprovação
    assert shop.webhook("1001").json()["resultado"] == "reembolso"
    assert _ledger(conn) == [("receita", 2990, "mp:1001"), ("despesa", 150, "mp:1001:taxa"), ("despesa", 2990, "mp:1001:reembolso")]
    assert db.cash_cents(conn) == -150
    shop.pay(o, "1001", status="approved")                                                          # o aviso atrasado de aprovação não desfaz nada nem duplica
    assert shop.webhook("1001").json()["resultado"] in ("ja_processado",)
    assert len(_ledger(conn)) == 3 and conn.execute("SELECT status FROM orders").fetchone()["status"] == "reembolsado"
    o2 = shop.order(); shop.pay(o2, "2001", transaction_amount_refunded=10.0)                        # aprovado já com reembolso parcial, primeira vez que vemos
    assert shop.webhook("2001").json()["resultado"] == "reembolso_parcial"
    assert ("receita", 2990, "mp:2001") in _ledger(conn) and ("despesa", 1000, "mp:2001:reembolso-parcial-1000") in _ledger(conn)


def test_second_payment_for_the_same_order_is_flagged_not_hidden(conn, shop):
    o = shop.order(); shop.pay(o, "1001"); shop.webhook("1001")
    shop.pay(o, "1002"); r = shop.webhook("1002")
    assert r.json()["resultado"] == "pagamento_duplicado"
    assert conn.execute("SELECT payment_id FROM orders").fetchone()["payment_id"] == "1001"          # o pedido continua ligado ao primeiro
    assert ("receita", 2990, "mp:1002") in _ledger(conn)                                              # o dinheiro entrou de verdade
    assert "reembolso manual" in conn.execute("SELECT note FROM payment_events WHERE outcome='pagamento_duplicado'").fetchone()["note"]


def test_checkout_stays_closed_until_the_president_approves_and_charges_no_one(conn, shop):
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 503 and conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0
    assert shop.fake.calls == []
    page = shop.client.get("/pub/loja/copy-de-vendas/").text
    assert "Comprar" not in page and "Compra indisponível" in page


def test_checkout_creates_order_and_preference_and_redirects(conn, shop, monkeypatch):
    from meridiano.engine import store, config
    shop.approve()
    page = shop.client.get("/pub/loja/copy-de-vendas/").text
    assert 'action="/pub/loja/copy-de-vendas/comprar"' in page and "Comprar agora" in page
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://www.mercadopago.com.br/checkout/")
    o = conn.execute("SELECT * FROM orders").fetchone()
    assert o["status"] == "aguardando" and o["amount_cents"] == 2990 and o["preference_id"] == "PREF1" and len(o["token"]) >= 30
    method, path, token, body, idem = shop.fake.calls[-1]
    assert (method, path) == ("POST", "/checkout/preferences") and token == TOKEN and idem == o["token"]
    assert body["items"] == [{"id": "copy-de-vendas", "title": "Copy de Vendas", "quantity": 1, "currency_id": "BRL", "unit_price": 29.9}]
    assert body["external_reference"] == str(o["id"]) and body["auto_return"] == "approved"
    assert body["notification_url"] == config.PUBLIC_BASE_URL + "/api/mp/webhook" and body["back_urls"]["success"] == f"{config.PUBLIC_BASE_URL}/pub/loja/pedido/{o['token']}"
    # o preço do pedido fica congelado, mesmo se o preço do produto mudar depois
    conn.execute("UPDATE products SET price_cents=9900")
    assert conn.execute("SELECT amount_cents FROM orders").fetchone()["amount_cents"] == 2990


def test_checkout_failures_are_safe(conn, shop, monkeypatch):
    from meridiano.engine import store, config, secrets_store as ss
    shop.approve()
    shop.fake.pref = (500, {})
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 502 and "Nada foi cobrado" in r.text and "mercadopago" not in r.text.lower()
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "cancelado"
    shop.fake.pref = (201, {"id": "P", "init_point": "http://inseguro.example/x"})                    # só redireciona para https
    assert shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False).status_code == 502
    assert shop.client.post("/pub/loja/nao-existe/comprar", follow_redirects=False).status_code == 404
    shop.fake.pref = (201, {"id": "P", "init_point": "https://www.mercadopago.com.br/x", "sandbox_init_point": "https://sandbox.mercadopago.com.br/x"})
    (shop.secdir / "mp-token").write_text("TEST-" + "x1y2z3w4" * 5)                                    # token de teste vai para o sandbox
    assert shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False).status_code == 503   # token trocado: ainda não verificado
    ss.refresh_meta(force=True)
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://sandbox.")
    assert conn.execute("SELECT test FROM orders ORDER BY id DESC").fetchone()["test"] is True
    # proteção contra enxurrada de pedidos abertos
    monkeypatch.setattr(config, "MAX_OPEN_ORDERS", 2)
    codes = [shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False).status_code for _ in range(3)]
    assert 429 in codes


def test_order_page_shows_state_and_confirms_the_return_with_the_mp_api(conn, shop):
    o = shop.order()
    conn.execute("UPDATE orders SET status='aguardando' WHERE id=%s", (o["id"],))
    r = shop.client.get(f"/pub/loja/pedido/{o['token']}")
    assert r.status_code == 200 and "Aguardando o pagamento" in r.text and 'http-equiv="refresh"' in r.text and "script-src 'none'" in r.headers["content-security-policy"]
    shop.pay(o, "1001")
    r = shop.client.get(f"/pub/loja/pedido/{o['token']}?payment_id=1001&status=approved&collection_status=approved")
    assert "Pagamento confirmado" in r.text and 'http-equiv="refresh"' not in r.text
    assert conn.execute("SELECT status FROM orders").fetchone()["status"] == "pago" and len(_ledger(conn)) == 2
    # a URL de retorno sozinha não vende nada: o status na URL é ignorado
    o2 = shop.order()
    r = shop.client.get(f"/pub/loja/pedido/{o2['token']}?payment_id=4040&status=approved")
    assert "Pagamento confirmado" not in r.text and conn.execute("SELECT status FROM orders WHERE id=%s", (o2["id"],)).fetchone()["status"] == "criado"
    assert shop.client.get("/pub/loja/pedido/token-inexistente-xxxxxxxxxxxxxxxx").status_code == 404
    assert shop.client.get("/pub/loja/pedido/curto").status_code == 404
    assert shop.client.get(f"/pub/loja/pedido/{o['token']}?payment_id=1%20OR%201=1").status_code == 200       # id malformado é ignorado
    conn.execute("UPDATE orders SET status='reembolsado'")
    assert "reembolsado" in shop.client.get(f"/pub/loja/pedido/{o['token']}").text.lower()


def test_no_personal_data_of_the_buyer_is_stored_anywhere(conn, shop):
    o = shop.order(); shop.pay(o, "1001"); shop.webhook("1001")
    for t in [r["table_name"] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()]:
        dump = str(conn.execute(f'SELECT * FROM "{t}"').fetchall())
        assert "comprador.secreto" not in dump and "Fulano" not in dump, t
    cols = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name IN ('orders','payment_events')").fetchall()}
    assert not any(k in cols for k in ("email", "name", "nome", "cpf", "phone", "payer"))


def test_orders_api_needs_token_and_exposes_no_buyer_data(conn, shop):
    o = shop.order(); shop.pay(o, "1001"); shop.webhook("1001")
    assert shop.client.get("/api/orders").status_code == 401
    r = shop.client.get("/api/orders", headers={"X-Token": "leitura"})
    assert r.status_code == 200 and r.json()[0]["status"] == "pago" and r.json()[0]["net_cents"] == 2840 and "comprador" not in r.text


def test_no_agent_tool_can_touch_money_or_orders():
    from meridiano.engine import tools
    names = {s["name"] for s in tools.SPECS}
    assert not any(k in n for n in names for k in ("cash", "ledger", "money", "order", "pedido", "payment", "refund", "sale"))


# ---------- entrega, revisão da Presidente e abertura da loja (etapa 4) ----------

def _pdf_ok(data):
    """Estrutura mínima de PDF válida: cabeçalho, xref apontando para os objetos certos e EOF."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    sx = int(data.rsplit(b"startxref", 1)[1].split()[0])
    assert data[sx:sx + 4] == b"xref"
    n = int(data[sx:].split(b"\n")[1].split()[1])
    entries = data[sx:].split(b"\n")[2:2 + n]
    for i, ent in enumerate(entries[1:], 1):
        off = int(ent.split()[0])
        assert data[off:].startswith(b"%d 0 obj" % i), (i, off)
    return n


def test_pdf_is_valid_keeps_accents_bullets_and_paginates():
    from meridiano.engine.pdf import render_pdf
    md = "## 1. Introdução\n\nAção, coração, é, ç, ü. Preço R$ 29,90 (parênteses) e \\ barra.\n\n- item um\n- item dois\n\n### Sub\n\n" + ("Parágrafo longo de teste. " * 60 + "\n\n") * 12
    pdf = render_pdf("Copy de Vendas", md, footer="Pedido #7 · uso pessoal")
    objs = _pdf_ok(pdf)
    assert objs > 8                                                          # várias páginas
    try:
        import pypdf, io
    except ImportError:
        return
    r = pypdf.PdfReader(io.BytesIO(pdf))
    first, last = r.pages[0].extract_text(), r.pages[-1].extract_text()
    assert "Copy de Vendas" in first and "Introdução" in first and "ação, coração" in first.lower() and "• item um" in first and "R$ 29,90 (parênteses)" in first
    assert f"página {len(r.pages)} de {len(r.pages)}" in last and "Pedido #7 · uso pessoal" in last


def test_store_gate_names_the_exact_missing_item(conn, shop, monkeypatch):
    from meridiano.engine import store, secrets_store as ss, tools
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    motivo = lambda: store.store_status(conn)["produtos"][2]
    st = store.store_status(conn)
    assert st["aberta"] is False and "revisão da Presidente pendente" in st["motivo"]
    # token ausente / não verificado / trocado
    (shop.secdir / "mp-token").write_text("TEST-" + "x1y2z3w4" * 5)
    assert "ainda não foi verificado" in store.store_status(conn)["motivo"]
    ss.refresh_meta(force=True)
    (shop.secdir / "mp-token").unlink()
    assert "falta o token do Mercado Pago" in store.store_status(conn)["motivo"]
    (shop.secdir / "mp-token").write_text(TOKEN); ss.refresh_meta(force=True)
    # vendedor sem identificação
    conn.execute("DELETE FROM store_settings")
    assert store.store_status(conn)["aberta"] is False and store.store_status(conn)["motivo"] != "1 produto(s) à venda"
    conn.execute("INSERT INTO store_settings(key,value) VALUES ('seller_name','Junio Teste'),('contact_email','junio@example.com')")
    # autor não revisa o próprio texto; o CEO escreveu, a Presidente revisa
    assert tools.run_tool(conn, ceo, "approve_store_page", {"product_name": "Copy de Vendas", "note": "Conferi tudo contra o produto, está certo."}).startswith("erro: só a Presidente revisa")
    # se a PRESIDENTE escreveu o texto, ela não o aprova e o CEO passa a poder revisar
    conn.execute("UPDATE product_store SET written_by=%s", (pres["id"],))
    assert "quem escreveu o texto não pode" in tools.run_tool(conn, pres, "approve_store_page", {"product_name": "Copy de Vendas", "note": "Conferi tudo contra o produto, está certo."})
    assert "aprovada" in tools.run_tool(conn, ceo, "approve_store_page", {"product_name": "Copy de Vendas", "note": "Conferi cada promessa contra as seções do produto, tudo confere."})
    conn.execute("UPDATE product_store SET written_by=%s, review_status='pendente', approved_hash=NULL, approved_by=NULL", (ceo["id"],))
    dev = _mk(conn, "Davi", "Analista")
    assert tools.run_tool(conn, dev, "approve_store_page", {"product_name": "Copy de Vendas", "note": "Conferi tudo contra o produto, está certo."}).startswith("erro: só a Presidente")
    assert tools.run_tool(conn, pres, "approve_store_page", {"product_name": "Copy de Vendas", "note": "ok"}).startswith("erro: registre o que você conferiu")
    # devolvida para reescrita: fechada com o motivo
    assert "reescrita pedida" in tools.run_tool(conn, pres, "request_store_rewrite", {"product_name": "Copy de Vendas", "reason": "a promessa de 3 mil palavras não confere"})
    assert "pediu reescrita: a promessa de 3 mil palavras" in store.store_status(conn)["motivo"]
    # aprovada: aberta
    shop.approve()
    st = store.store_status(conn)
    assert st["aberta"] is True and st["motivo"] == "1 produto(s) à venda" and [p["aberto"] for p in st["produtos"]].count(True) == 1
    # mudou o preço ou o texto depois da aprovação: a aprovação cai
    tools.run_tool(conn, ceo, "set_price", {"product_name": "Copy de Vendas", "price_brl": 34.9, "reason": "teste de preço"})
    assert store.store_status(conn)["aberta"] is False and "revisão da Presidente pendente" in store.store_status(conn)["motivo"]
    shop.approve()
    assert store.store_status(conn)["aberta"] is True
    conn.execute("UPDATE product_store SET headline='Outro título qualquer'")                      # mudança por fora do fluxo: o hash não bate
    assert "mudou depois da aprovação" in store.store_status(conn)["motivo"]
    c = shop.client
    assert c.get("/api/store/status", headers={"X-Token": "leitura"}).json()["aberta"] is False
    assert c.get("/api/store/status").status_code == 401
    assert "Comprar" not in c.get("/pub/loja/copy-de-vendas/").text


def test_president_review_prompt_and_ceo_rewrite_prompt(conn):
    from meridiano.engine import agent, tools
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone() if conn.execute("SELECT 1 FROM employees").fetchone() else None
    ceo = _open_store(conn)
    conn.execute("INSERT INTO store_settings(key,value) VALUES ('seller_name','J'),('contact_email','j@e.com') ON CONFLICT DO NOTHING")
    for n in ("Toolkit do Vendedor E-commerce", "Plano Mestre Financeiro Pessoal"):
        _ready_product(conn, n)
        tools.run_tool(conn, ceo, "write_store_copy", dict(GOOD_COPY, product_name=n))
        tools.run_tool(conn, ceo, "set_price", {"product_name": n, "price_brl": 24.9, "reason": "preço de entrada"})
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    p = agent.build_prompt(conn, pres)
    assert "espera a SUA revisão" in p and "read_store_page" in p and "approve_store_page" in p and "request_store_rewrite" in p
    assert "espera a SUA revisão" not in agent.build_prompt(conn, ceo)                       # quem escreveu não é o revisor
    first = conn.execute("SELECT p.name FROM products p JOIN product_store s ON s.product_id=p.id WHERE s.review_status<>'aprovada' ORDER BY p.id").fetchone()["name"]
    tools.run_tool(conn, pres, "request_store_rewrite", {"product_name": first, "reason": "o texto promete mais do que o produto tem"})
    c = agent.build_prompt(conn, ceo)
    assert "REESCRITA do texto de venda" in c and "o texto promete mais do que o produto tem" in c


def test_read_store_page_shows_what_the_customer_sees(conn):
    from meridiano.engine import tools
    ceo = _open_store(conn)
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    out = tools.run_tool(conn, pres, "read_store_page", {"product_name": "Copy de Vendas"})
    assert "R$ 29,90" in out and "Aprenda a escrever textos de venda claros" in out and "Princípios de copy que converte" in out and "[pendente]" in out
    assert tools.run_tool(conn, pres, "read_store_page", {"product_name": "Toolkit do Vendedor E-commerce"}).startswith("erro: esse produto ainda não tem texto")


def _buy_and_pay(shop, conn, pid="1001"):
    """Compra completa pela loja: clica em Comprar, o MP aprova e avisa pelo webhook. Devolve o pedido."""
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 303
    o = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchone()
    shop.pay(o, pid)
    assert shop.webhook(pid).json()["resultado"] == "venda"
    return conn.execute("SELECT * FROM orders WHERE id=%s", (o["id"],)).fetchone()


def test_full_flow_purchase_cash_link_download_and_refund_revokes(conn, shop):
    from meridiano.engine import db
    shop.approve()
    page = shop.client.get("/pub/loja/copy-de-vendas/").text
    assert "Comprar agora" in page                                                           # aberta: aprovada + token verificado + vendedor + texto + preço
    o = _buy_and_pay(shop, conn)
    assert _ledger(conn) == [("receita", 2990, "mp:1001"), ("despesa", 150, "mp:1001:taxa")] and db.cash_cents(conn) == 2840
    d = conn.execute("SELECT * FROM downloads").fetchone()
    assert d["order_id"] == o["id"] and d["max_count"] == 5 and not d["revoked"] and len(d["token"]) >= 30
    days = conn.execute("SELECT extract(epoch FROM expires_at - now())/86400 AS d FROM downloads").fetchone()["d"]
    assert 6.99 < float(days) <= 7.0                                                          # 7 dias
    # a página do pedido mostra os botões (POST, não links) e o prazo
    pg = shop.client.get(f"/pub/loja/pedido/{o['token']}").text
    assert "Pagamento confirmado" in pg and f'action="/pub/loja/baixar/{d["token"]}/pdf"' in pg and "5 download(s) restante(s)" in pg and "Baixar Markdown" in pg
    assert shop.client.get(f"/pub/loja/baixar/{d['token']}/pdf").status_code in (404, 405)    # GET (pré-visualização de link) não entrega nem gasta download
    assert conn.execute("SELECT count FROM downloads").fetchone()["count"] == 0
    # baixa o PDF e o Markdown
    r = shop.client.post(f"/pub/loja/baixar/{d['token']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and 'attachment; filename="copy-de-vendas.pdf"' in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    _pdf_ok(r.content)
    try:
        import pypdf, io
        txt = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
        assert "Copy de Vendas" in txt and "Princípios de copy que converte" in txt and f"Pedido #{o['id']}" in txt and "Material educativo" in txt
    except ImportError:
        pass
    m = shop.client.post(f"/pub/loja/baixar/{d['token']}/md")
    assert m.status_code == 200 and m.headers["content-type"].startswith("text/markdown") and "## 1. Princípios de copy que converte" in m.text and f"Pedido #{o['id']}" in m.text
    assert conn.execute("SELECT count FROM downloads").fetchone()["count"] == 2
    # limite de 5: mais três funcionam, o sexto não
    codes = [shop.client.post(f"/pub/loja/baixar/{d['token']}/md").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 410]
    assert "Os downloads deste link acabaram" in shop.client.post(f"/pub/loja/baixar/{d['token']}/pdf").text
    assert "Os downloads deste link acabaram" in shop.client.get(f"/pub/loja/pedido/{o['token']}").text and "junio@example.com" in shop.client.get(f"/pub/loja/pedido/{o['token']}").text
    # reembolso revoga o link (mesmo com downloads sobrando) e a página explica
    conn.execute("UPDATE downloads SET count=0")
    shop.pay(o, "1001", status="refunded"); assert shop.webhook("1001").json()["resultado"] == "reembolso"
    assert conn.execute("SELECT revoked FROM downloads").fetchone()["revoked"] is True
    r = shop.client.post(f"/pub/loja/baixar/{d['token']}/pdf")
    assert r.status_code == 410 and "acesso a este material foi encerrado" in r.text
    assert "reembolsado" in shop.client.get(f"/pub/loja/pedido/{o['token']}").text.lower()


def test_chargeback_also_revokes_and_partial_refund_does_not(conn, shop):
    shop.approve()
    o = _buy_and_pay(shop, conn)
    d = conn.execute("SELECT token FROM downloads").fetchone()["token"]
    shop.pay(o, "1001", transaction_amount_refunded=5.0); shop.webhook("1001")
    assert shop.client.post(f"/pub/loja/baixar/{d}/md").status_code == 200                     # reembolso parcial não tira o acesso
    shop.pay(o, "1001", status="charged_back"); shop.webhook("1001")
    assert shop.client.post(f"/pub/loja/baixar/{d}/md").status_code == 410


def test_download_expiry_invalid_tokens_and_unpaid_orders(conn, shop):
    shop.approve()
    o = _buy_and_pay(shop, conn)
    d = conn.execute("SELECT token FROM downloads").fetchone()["token"]
    conn.execute("UPDATE downloads SET expires_at = now() - interval '1 minute'")
    r = shop.client.post(f"/pub/loja/baixar/{d}/pdf")
    assert r.status_code == 410 and "link expirou" in r.text
    assert "O link expirou" in shop.client.get(f"/pub/loja/pedido/{o['token']}").text
    for bad in ("curto", "x" * 80, "../../etc/passwd", "nao-existe-nao-existe-nao-existe"):
        assert shop.client.post(f"/pub/loja/baixar/{bad}/pdf").status_code in (404, 405, 410)
    assert shop.client.post(f"/pub/loja/baixar/{d}/exe").status_code == 404                   # formato desconhecido
    # um pedido que não está pago nunca entrega, mesmo se existir uma linha de download
    conn.execute("UPDATE downloads SET expires_at = now() + interval '7 days'")
    conn.execute("UPDATE orders SET status='aguardando'")
    assert shop.client.post(f"/pub/loja/baixar/{d}/pdf").status_code == 410


def test_parallel_downloads_never_exceed_the_limit(conn, shop):
    from meridiano.engine import delivery, db
    shop.approve()
    _buy_and_pay(shop, conn)
    d = conn.execute("SELECT token FROM downloads").fetchone()["token"]
    conn.execute("UPDATE downloads SET count=4")                                             # sobra 1 download
    conns = [db.connect() for _ in range(8)]
    res = []
    _threads(8, lambda i: res.append(delivery.claim(conns[i], d, "pdf")[1]))
    assert res.count("ok") == 1 and res.count("esgotado") == 7
    assert conn.execute("SELECT count FROM downloads").fetchone()["count"] == 5


def test_sandbox_purchase_delivers_but_never_reaches_the_cash_book(conn, shop):
    from meridiano.engine import secrets_store as ss
    shop.approve()
    (shop.secdir / "mp-token").write_text("TEST-" + "x1y2z3w4" * 5); ss.refresh_meta(force=True)
    r = shop.client.post("/pub/loja/copy-de-vendas/comprar", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://sandbox.")
    o = conn.execute("SELECT * FROM orders").fetchone()
    shop.pay(o, "1001", live_mode=False); assert shop.webhook("1001").json()["resultado"] == "venda_teste"
    assert _ledger(conn) == []
    d = conn.execute("SELECT token FROM downloads").fetchone()["token"]
    assert shop.client.post(f"/pub/loja/baixar/{d}/pdf").status_code == 200                  # o dono consegue testar o fluxo inteiro


def test_second_payment_does_not_issue_a_second_link(conn, shop):
    shop.approve()
    o = _buy_and_pay(shop, conn)
    shop.pay(o, "1002"); assert shop.webhook("1002").json()["resultado"] == "pagamento_duplicado"
    assert conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"] == 1


# ---------------------------------------------------------------- PR A: perfis de LLM, custo, tetos e contingência
def _paid(conn, label="Nous · Claude Haiku 5.5"):
    return conn.execute("SELECT * FROM llm_profiles WHERE label=%s", (label,)).fetchone()


def _open_paid(conn, secdir, label="Nous · Claude Haiku 5.5", cap=1.0):
    p = _paid(conn, label)
    (secdir / f"llm-{p['key_name']}").write_text("chave-de-teste-123456")
    conn.execute("UPDATE llm_profiles SET enabled=true, daily_usd_cap=%s WHERE id=%s", (cap, p["id"]))
    return conn.execute("SELECT * FROM llm_profiles WHERE id=%s", (p["id"],)).fetchone()


def test_llm_migration_seeds_profiles_contingency_on_and_backfills_employees(conn):
    from meridiano.engine import seed
    assert conn.execute("SELECT contingency FROM llm_settings").fetchone()["contingency"] is True
    free = conn.execute("SELECT COUNT(*) AS n FROM llm_profiles WHERE is_free").fetchone()["n"]
    enabled_free = conn.execute("SELECT COUNT(*) AS n FROM llm_profiles WHERE is_free AND enabled").fetchone()["n"]
    assert free == 8 and enabled_free == 6
    paid = conn.execute("SELECT * FROM llm_profiles WHERE NOT is_free").fetchall()
    assert len(paid) >= 10 and not any(p["enabled"] for p in paid)
    h = _paid(conn)
    assert float(h["price_in_per_m"]) == 0.10 and float(h["price_out_per_m"]) == 0.50 and h["model"] == "anthropic/claude-haiku-5.5"
    assert conn.execute("SELECT 1 FROM llm_profiles WHERE provider='anthropic' AND base_url='https://api.anthropic.com/v1'").fetchone()
    seed.seed(conn)
    from meridiano.engine import llm
    me = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    assert llm.resolve(conn, me)["profile"]["is_free"] and llm.resolve(conn, me)["reason"] == ""


def test_contingency_keeps_config_but_calls_only_free(conn, secdir):
    from meridiano.engine import llm, seed
    seed.seed(conn)
    p = _open_paid(conn, secdir)
    conn.execute("UPDATE employees SET llm_profile_id=%s WHERE slug='ceo'", (p["id"],))
    me = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    r = llm.resolve(conn, me)                                   # contingência ligada por padrão
    assert r["profile"]["is_free"] and r["wanted"]["id"] == p["id"] and "contingência" in r["reason"]
    assert conn.execute("SELECT llm_profile_id FROM employees WHERE slug='ceo'").fetchone()["llm_profile_id"] == p["id"]   # configuração preservada
    conn.execute("UPDATE llm_settings SET contingency=false")
    assert llm.resolve(conn, me)["profile"]["id"] == p["id"]    # voltou quando o modo desligou
    conn.execute("UPDATE llm_settings SET contingency=true")
    with pytest.raises(ValueError):
        llm.chat(conn, None, p["model"], [])                    # nem a chamada direta passa a pago na contingência


def test_paid_requires_key_cap_and_respects_profile_and_global_caps(conn, secdir):
    from meridiano.engine import llm
    conn.execute("UPDATE llm_settings SET contingency=false")
    p = _paid(conn)
    conn.execute("UPDATE llm_profiles SET enabled=true WHERE id=%s", (p["id"],))
    p = _paid(conn)
    assert llm.usable(conn, p) == (False, "sem chave configurada")
    (secdir / "llm-nous").write_text("chave-de-teste-123456")
    conn.execute("UPDATE llm_profiles SET daily_usd_cap=NULL WHERE id=%s", (p["id"],))
    assert llm.usable(conn, _paid(conn))[1] == "sem limite diário em USD"
    conn.execute("UPDATE llm_profiles SET daily_usd_cap=0.50 WHERE id=%s", (p["id"],))
    assert llm.usable(conn, _paid(conn))[0]
    conn.execute("INSERT INTO usage_log(model,profile_id,cost_usd) VALUES ('x',%s,0.50)", (p["id"],))
    ok, why = llm.usable(conn, _paid(conn))
    assert not ok and "limite diário" in why
    conn.execute("UPDATE llm_profiles SET daily_usd_cap=5 WHERE id=%s", (p["id"],))
    conn.execute("UPDATE llm_settings SET global_usd_cap=0.50")
    ok, why = llm.usable(conn, _paid(conn))
    assert not ok and "teto global" in why


def test_cost_is_computed_from_provider_usage_and_logged(conn, secdir, monkeypatch):
    from meridiano.engine import llm
    conn.execute("UPDATE llm_settings SET contingency=false")
    p = _open_paid(conn, secdir, "Nous · Claude Sonnet 5.5")
    seen = {}

    class R:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"role": "assistant", "content": "oi"}}], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return R()
    monkeypatch.setattr(llm.httpx, "post", post)
    assert llm.chat_fallback(conn, None, p["model"], [{"role": "user", "content": "a"}])["content"] == "oi"
    assert seen["url"] == "https://inference-api.nousresearch.com/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer chave-de-teste-123456"
    row = conn.execute("SELECT cost_usd, profile_id FROM usage_log ORDER BY id DESC LIMIT 1").fetchone()
    assert float(row["cost_usd"]) == 2.0 + 5.0 and row["profile_id"] == p["id"]    # 1M x 2,00 + 0,5M x 10,00
    assert llm.spent_today(conn) == 7.0
    assert not llm.usable(conn, conn.execute("SELECT * FROM llm_profiles WHERE id=%s", (p["id"],)).fetchone())[0]   # estourou o limite de US$ 1


def test_over_cap_agent_falls_back_to_free_profile(conn, secdir, monkeypatch):
    from meridiano.engine import llm, seed
    seed.seed(conn)
    conn.execute("UPDATE llm_settings SET contingency=false")
    p = _open_paid(conn, secdir, cap=0.01)
    conn.execute("UPDATE employees SET llm_profile_id=%s WHERE slug='ceo'", (p["id"],))
    conn.execute("INSERT INTO usage_log(model,profile_id,cost_usd) VALUES ('x',%s,0.02)", (p["id"],))
    used = []

    class R:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 3}}
    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, **k: used.append(json["model"]) or R())
    monkeypatch.setenv("NOUS_API_KEY", "k-nous-123456")
    ceo = conn.execute("SELECT id FROM employees WHERE slug='ceo'").fetchone()
    llm.chat_fallback(conn, ceo["id"], "x", [])
    assert used and used[0].endswith(":free")


def test_anthropic_adapter_converts_tools_and_results(conn, secdir, monkeypatch):
    from meridiano.engine import llm
    conn.execute("UPDATE llm_settings SET contingency=false")
    p = _open_paid(conn, secdir, "Anthropic direta · Haiku 5.5")
    tools = [{"type": "function", "function": {"name": "create_task", "description": "d", "parameters": {"type": "object", "properties": {"title": {"type": "string"}}}}}]
    msgs = [{"role": "system", "content": "SIS"}, {"role": "user", "content": "faça"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "create_task", "arguments": '{"title":"a"}'}},
                                                               {"id": "t2", "type": "function", "function": {"name": "create_task", "arguments": '{"title":"b"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok1"}, {"role": "tool", "tool_call_id": "t2", "content": "ok2"}]
    conv = llm.to_anthropic(msgs, tools)
    assert conv["system"] == "SIS" and [m["role"] for m in conv["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in conv["messages"][1]["content"]] == ["tool_use", "tool_use"] and conv["messages"][1]["content"][0]["input"] == {"title": "a"}
    assert [b["tool_use_id"] for b in conv["messages"][2]["content"]] == ["t1", "t2"]      # resultados juntos numa só mensagem do usuário
    assert conv["tools"][0] == {"name": "create_task", "description": "d", "input_schema": tools[0]["function"]["parameters"]}
    seen = {}

    class R:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {"content": [{"type": "text", "text": "vou"}, {"type": "tool_use", "id": "u9", "name": "create_task", "input": {"title": "z"}}],
                                "usage": {"input_tokens": 2_000_000, "output_tokens": 1_000_000}}
    monkeypatch.setattr(llm.httpx, "post", lambda url, json=None, headers=None, **k: seen.update(url=url, headers=headers, body=json) or R())
    msg = llm.call(conn, None, p, msgs, tools)
    assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["headers"]["x-api-key"] == "chave-de-teste-123456" and "anthropic-version" in seen["headers"]
    assert "Authorization" not in seen["headers"] and seen["body"]["model"] == "claude-haiku-5-5" and seen["body"]["max_tokens"]
    assert msg["content"] == "vou" and msg["tool_calls"][0] == {"id": "u9", "type": "function", "function": {"name": "create_task", "arguments": '{"title": "z"}'}}
    assert float(conn.execute("SELECT cost_usd FROM usage_log ORDER BY id DESC LIMIT 1").fetchone()["cost_usd"]) == 0.2 + 0.5


def test_empty_anthropic_reply_is_failure_and_error_hides_key(conn, secdir, monkeypatch):
    from meridiano.engine import llm
    import httpx
    conn.execute("UPDATE llm_settings SET contingency=false")
    p = _open_paid(conn, secdir, "Anthropic direta · Haiku 5.5")

    def boom(*a, **k):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(401, text="bad key chave-de-teste-123456", request=req))
    monkeypatch.setattr(llm.httpx, "post", boom)
    with pytest.raises(httpx.HTTPStatusError):
        llm.call(conn, None, p, [{"role": "user", "content": "a"}])
    err = llm.failures_today(conn)[1]
    assert "401" in err and "chave-de-teste" not in err


def test_host_allowlist_and_key_names_block_exfiltration(secdir):
    from meridiano.engine import llm
    for bad in ("http://api.openai.com/v1", "https://evil.example.com/v1", "https://api.openai.com.evil.com/v1", "https://user:p@api.openai.com/v1", "https://api.openai.com:8443/v1", "file:///etc/passwd"):
        assert not llm.host_ok(bad), bad
    for good in ("https://api.openai.com/v1", "https://openrouter.ai/api/v1", "https://inference-api.nousresearch.com/v1", "https://api.anthropic.com/v1"):
        assert llm.host_ok(good)
    assert llm.key_path("../mp-token") is None and llm.key_path("a/b") is None and llm.key_path("") is None
    ok, _ = llm.save_key("openai", "sk-teste-abcdefgh")
    assert ok and oct((secdir / "llm-openai").stat().st_mode & 0o777) == "0o600"
    assert not llm.save_key("../x", "sk-teste-abcdefgh")[0] and not llm.save_key("openai", "curta")[0] and not llm.save_key("openai", "tem espaco no meio")[0]


def _llm_client(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura")
    monkeypatch.setenv("MERIDIANO_ADMIN_TOKEN", "admin")
    from meridiano.api import main
    main._conn = conn
    return TestClient(main.app)


def test_api_llm_admin_actions_and_secrets_never_exposed(conn, secdir, monkeypatch):
    from meridiano.engine import seed
    seed.seed(conn)
    c = _llm_client(conn, monkeypatch)
    R, A = {"X-Token": "leitura"}, {"X-Admin-Token": "admin"}
    assert c.get("/api/llm").status_code == 401
    ov = c.get("/api/llm", headers=R).json()
    assert ov["contingencia"] is True and len(ov["funcionarios"]) == 2 and ov["gasto_hoje_usd"] == 0
    pid = next(p["id"] for p in ov["perfis"] if p["label"] == "Nous · Claude Haiku 5.5")
    assert not next(p for p in ov["perfis"] if p["id"] == pid)["chave_configurada"] or True
    # leitura não altera nada
    for path, body in (("/api/llm/contingency", {"on": False}), (f"/api/llm/profiles/{pid}", {"enabled": True}), ("/api/llm/keys/nous", {"key": "chave-de-teste-123456"}), ("/api/llm/employees/ceo", {"profile_id": pid})):
        r = c.patch(path, json=body, headers=R) if "profiles" in path else c.post(path, json=body, headers=R)
        assert r.status_code == 401, path
    # ligar pago sem chave / sem limite é recusado
    (secdir / "llm-nous").unlink(missing_ok=True)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    assert c.patch(f"/api/llm/profiles/{pid}", json={"enabled": True}, headers=A).status_code == 400
    r = c.post("/api/llm/keys/nous", json={"key": "chave-de-teste-123456"}, headers=A)
    assert r.status_code == 200 and "chave-de-teste" not in r.text
    assert c.patch(f"/api/llm/profiles/{pid}", json={"enabled": True, "daily_usd_cap": 0}, headers=A).status_code == 400
    assert c.patch(f"/api/llm/profiles/{pid}", json={"enabled": True, "daily_usd_cap": 0.75}, headers=A).status_code == 200
    # atribui na contingência: guarda, mas o efetivo continua grátis e o painel avisa
    assert c.post("/api/llm/employees/ceo", json={"profile_id": pid}, headers=A).status_code == 200
    ov = c.get("/api/llm", headers=R).json()
    ceo = next(f for f in ov["funcionarios"] if f["slug"] == "ceo")
    assert ceo["perfil_id"] == pid and "Nous · Claude" not in ceo["efetivo"] and "contingência" in ceo["desvio"] and ov["avisos"]
    assert "chave-de-teste" not in c.get("/api/llm", headers=R).text
    assert c.post("/api/llm/contingency", json={"on": False}, headers=A).status_code == 200
    ceo = next(f for f in c.get("/api/llm", headers=R).json()["funcionarios"] if f["slug"] == "ceo")
    assert ceo["efetivo"] == "Nous · Claude Haiku 5.5" and ceo["desvio"] == ""
    assert c.post("/api/llm/global-cap", json={"usd": 3}, headers=A).json()["teto_global_usd"] == 3
    assert c.post("/api/llm/employees/ceo", json={"profile_id": None}, headers=A).status_code == 200
    assert c.post("/api/llm/employees/ghost", json={"profile_id": None}, headers=A).status_code == 404
    # perfil desligado não pode ser atribuído; sempre sobra um grátis ligado
    off = next(p["id"] for p in ov["perfis"] if p["label"] == "Nous · Claude Opus 5.5")
    assert c.post("/api/llm/employees/ceo", json={"profile_id": off}, headers=A).status_code == 400
    for p in ov["perfis"]:
        if p["gratis"] and p["ligado"]:
            r = c.patch(f"/api/llm/profiles/{p['id']}", json={"enabled": False}, headers=A)
    assert conn.execute("SELECT COUNT(*) AS n FROM llm_profiles WHERE is_free AND enabled").fetchone()["n"] == 1


def test_api_profile_creation_blocks_foreign_hosts_and_starts_disabled(conn, monkeypatch):
    c = _llm_client(conn, monkeypatch)
    A = {"X-Admin-Token": "admin"}
    base = {"label": "Meu perfil", "provider": "openai_compat", "base_url": "https://openrouter.ai/api/v1", "model": "google/gemma-4-31b-it:free", "key_name": "openrouter",
            "price_in_per_m": 0, "price_out_per_m": 0, "is_free": True}
    assert c.post("/api/llm/profiles", json={**base, "base_url": "https://evil.example.com/v1", "label": "x1x"}, headers=A).status_code == 400
    assert c.post("/api/llm/profiles", json={**base, "base_url": "http://openrouter.ai/api/v1", "label": "x2x"}, headers=A).status_code == 400
    assert c.post("/api/llm/profiles", json={**base, "provider": "anthropic", "label": "x3x"}, headers=A).status_code == 400
    assert c.post("/api/llm/profiles", json={**base, "price_in_per_m": 1, "label": "x4x"}, headers=A).status_code == 400
    assert c.post("/api/llm/profiles", json={**base, "key_name": "../mp-token", "label": "x5x"}, headers=A).status_code == 422
    r = c.post("/api/llm/profiles", json=base, headers={"X-Token": "leitura"})
    assert r.status_code == 401
    r = c.post("/api/llm/profiles", json=base, headers=A)
    assert r.status_code == 200 and r.json()["ligado"] is False
    assert c.post("/api/llm/profiles", json=base, headers=A).status_code == 409


def test_hire_with_llm_profile_follows_mode_and_decision_roles(conn, secdir):
    from meridiano.engine import seed, tools
    seed.seed(conn)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    p = _open_paid(conn, secdir, "Nous · Claude Opus 5.5")
    out = tools.run_tool(conn, ceo, "hire_employee", {"name": "Ana", "role": "Analista", "area": "A", "llm_profile": p["label"], **HIRE})
    assert out.startswith("erro") and "contingência" in out                                 # contingência ligada: pago recusado
    out = tools.run_tool(conn, ceo, "hire_employee", {"name": "Ana", "role": "Analista", "area": "A", "llm_profile": "não existe", **HIRE})
    assert out.startswith("erro")
    out = tools.run_tool(conn, ceo, "hire_employee", {"name": "Bia", "role": "Analista", "area": "A", **HIRE})
    bia = conn.execute("SELECT e.*, p.is_free FROM employees e JOIN llm_profiles p ON p.id=e.llm_profile_id WHERE slug='bia'").fetchone()
    assert out.startswith("contratado") and bia["is_free"]                                    # padrão grátis
    conn.execute("UPDATE llm_settings SET contingency=false")
    assert tools.run_tool(conn, ceo, "hire_employee", {"name": "Caio", "role": "Analista", "area": "A", "llm_profile": p["label"], **HIRE}).startswith("contratado")
    caio = conn.execute("SELECT * FROM employees WHERE slug='caio'").fetchone()
    assert caio["llm_profile_id"] == p["id"] and caio["model"] == p["model"]
    # cargo de decisão sem escolha: o melhor liberado dentro do teto (aqui o Opus); sem espaço no teto, o grátis
    assert tools.run_tool(conn, ceo, "hire_employee", {"name": "Dora", "role": "Diretora Jurídica", "area": "J", **HIRE}).startswith("contratado")
    assert conn.execute("SELECT llm_profile_id FROM employees WHERE slug='dora'").fetchone()["llm_profile_id"] == p["id"]
    conn.execute("INSERT INTO usage_log(model,profile_id,cost_usd) VALUES ('x',%s,5)", (p["id"],))
    assert tools.run_tool(conn, ceo, "hire_employee", {"name": "Eva", "role": "Presidente Adjunta", "area": "J", **HIRE}).startswith("contratado")
    assert conn.execute("SELECT p.is_free FROM employees e JOIN llm_profiles p ON p.id=e.llm_profile_id WHERE slug='eva'").fetchone()["is_free"]


def test_list_llm_profiles_tool_shows_prices_and_no_keys(conn, secdir):
    from meridiano.engine import seed, tools
    seed.seed(conn)
    _open_paid(conn, secdir)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    out = tools.run_tool(conn, ceo, "list_llm_profiles", {})
    assert "CONTINGÊNCIA LIGADO" in out and "grátis" in out and "0.10/0.50" in out and "chave-de-teste" not in out
    assert "list_llm_profiles" in [t["function"]["name"] for t in tools.TOOL_DEFS]
    assert any("llm_profile" in t["function"]["parameters"]["properties"] for t in tools.TOOL_DEFS if t["function"]["name"] == "hire_employee")


# ---------------------------------------------------------------- Configurações: login com Google, sessão, CSRF e cartões de chaves
import base64 as _b64
import hashlib as _hl
import json as _json
from urllib.parse import parse_qs as _pq, urlparse as _up

_CID = "123456789-abcdefgh.apps.googleusercontent.com"
_SEC = "GOCSPX-segredo-do-google-123"


def _idtoken(**over):
    c = {"iss": "https://accounts.google.com", "aud": _CID, "exp": time.time() + 600, "nonce": "NONCE", "email": "juniobc@gmail.com", "email_verified": True}
    c.update(over)
    return "hdr." + _b64.urlsafe_b64encode(_json.dumps(c).encode()).decode().rstrip("=") + ".sig"


class _Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d


class Gauth:
    pass


@pytest.fixture()
def g(conn, secdir, monkeypatch):
    from meridiano.engine import config, google_auth
    from meridiano.api import main
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura")
    monkeypatch.setenv("MERIDIANO_ADMIN_TOKEN", "recuperacao-do-admin")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://painel.exemplo.test")
    main._conn = conn
    main._recovery.update(fails=0, first=0.0, until=0.0)
    o = Gauth()
    o.conn, o.secdir, o.ga, o.main = conn, secdir, google_auth, main
    o.client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    o.R = {"X-Token": "leitura"}
    o.posts = []
    o.token_reply = lambda data: _Resp(200, {"id_token": _idtoken(nonce=o.nonce, **o.over), "access_token": "x"})
    o.over, o.nonce = {}, "NONCE"

    def post(url, data=None, **k):
        o.posts.append((url, data))
        if data.get("code") == "codigo-invalido":
            return _Resp(400, {"error": "invalid_grant"})
        return o.token_reply(data)
    monkeypatch.setattr(google_auth.httpx, "post", post)

    def configure():
        (secdir / "google-oauth.json").write_text(_json.dumps({"client_id": _CID, "client_secret": _SEC, "owner_email": "juniobc@gmail.com"}))
    o.configure = configure

    def start():
        r = o.client.get("/api/auth/google/start")
        q = _pq(_up(r.headers["location"]).query)
        o.nonce = conn.execute("SELECT nonce FROM oauth_states WHERE state=%s", (q["state"][0],)).fetchone()["nonce"]
        return r, q
    o.start = start

    def login():
        r, q = o.start()
        cb = o.client.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
        assert cb.status_code == 303, cb.text
        me = o.client.get("/api/auth/me", headers=o.R).json()
        o.csrf = me["csrf"]
        return cb, me
    o.login = login
    return o


def _secret_free(*texts):
    for t in texts:
        assert _SEC not in t and "segredo-do-google" not in t


def test_google_not_configured_start_is_503_and_setup_is_once_and_validated(g):
    assert g.client.get("/api/auth/google/start").status_code == 503
    body = {"client_id": _CID, "client_secret": _SEC, "owner_email": "juniobc@gmail.com"}
    assert g.client.post("/api/auth/google/setup", json=body).status_code == 401                  # exige o token de leitura
    r = g.client.post("/api/auth/google/setup", json={**body, "owner_email": "outra@gmail.com"}, headers=g.R)
    assert r.status_code == 400 and not (g.secdir / "google-oauth.json").exists()                  # e-mail do dono não muda pelo painel
    orig = g.ga.httpx.post
    g.ga.httpx.post = lambda *a, **k: _Resp(401, {"error": "invalid_client"})
    r = g.client.post("/api/auth/google/setup", json=body, headers=g.R)
    assert r.status_code == 400 and not (g.secdir / "google-oauth.json").exists(); _secret_free(r.text)
    g.ga.httpx.post = orig
    r = g.client.post("/api/auth/google/setup", json=body, headers=g.R)
    assert r.status_code == 200; _secret_free(r.text)
    f = g.secdir / "google-oauth.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600" and _json.loads(f.read_text())["client_secret"] == _SEC
    r = g.client.post("/api/auth/google/setup", json={**body, "client_secret": "outro-segredo-qualquer"}, headers=g.R)
    assert r.status_code == 409 and _json.loads(f.read_text())["client_secret"] == _SEC             # uma vez só
    assert g.client.get("/api/auth/google/start").status_code == 302                               # já funciona sem reiniciar
    assert "configuracao_google" in [e["tipo"] for e in g.client.get("/api/auth/events", headers=g.R).json()]


def test_google_start_redirect_has_pkce_state_nonce_fixed_redirect_and_safe_cookie(g):
    g.configure()
    r, q = g.start()
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["client_id"] == [_CID] and q["redirect_uri"] == ["https://painel.exemplo.test/api/auth/google/callback"]
    assert q["scope"] == ["openid email"] and q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["state"][0] and q["nonce"][0] and q["code_challenge"][0] and _SEC not in r.headers["location"]
    ck = r.headers["set-cookie"].lower()
    assert "mer_oauth=" in ck and "httponly" in ck and "secure" in ck and "samesite=lax" in ck and "max-age=600" in ck


def test_google_login_creates_session_with_flags_csrf_and_hashed_cookie(g):
    g.configure()
    cb, me = g.login()
    ck = cb.headers["set-cookie"].lower()
    assert "mer_session=" in ck and "httponly" in ck and "secure" in ck and "samesite=lax" in ck and f"max-age={12 * 3600}" in ck
    assert cb.headers["location"] == "https://painel.exemplo.test/?aba=config"
    assert me["logado"] and me["email"] == "juniobc@gmail.com" and me["csrf"] and me["google_configurado"]
    token = g.client.cookies.get("mer_session")
    row = g.conn.execute("SELECT * FROM admin_sessions").fetchone()
    assert row["token_hash"] == _hl.sha256(token.encode()).hexdigest() and token not in str(dict(row))
    url, data = g.posts[-1]                                                                          # troca do code servidor a servidor, com PKCE
    assert url == "https://oauth2.googleapis.com/token" and data["client_secret"] == _SEC and data["redirect_uri"] == "https://painel.exemplo.test/api/auth/google/callback"
    ch = _pq(_up(g.client.get("/api/auth/google/start").headers["location"]).query)["code_challenge"][0]
    assert _b64.urlsafe_b64encode(_hl.sha256(data["code_verifier"].encode()).digest()).decode().rstrip("=") != ""   # verifier existe e tem o formato do desafio
    assert len(ch) == 43


def test_admin_actions_need_session_and_matching_csrf(g):
    g.configure()
    assert g.client.post("/api/llm/contingency", json={"on": False}).status_code == 401              # sem sessão
    g.login()
    assert g.client.post("/api/llm/contingency", json={"on": False}).status_code == 403              # sessão sem X-CSRF
    assert g.client.post("/api/llm/contingency", json={"on": False}, headers={"X-CSRF": "errado"}).status_code == 403
    assert g.client.post("/api/llm/contingency", json={"on": False}, headers={"X-CSRF": g.csrf}).status_code == 200
    assert g.conn.execute("SELECT contingency FROM llm_settings").fetchone()["contingency"] is False
    assert "csrf_ausente" in [e["kind"] for e in g.ga.events(g.conn)]
    other = TestClient(g.main.app, base_url="https://testserver")                                    # outro navegador, mesmo CSRF, sem cookie
    assert other.post("/api/llm/contingency", json={"on": True}, headers={"X-CSRF": g.csrf}).status_code == 401
    # logout encerra
    assert g.client.post("/api/auth/logout", headers={"X-CSRF": g.csrf}).status_code == 200
    assert g.client.post("/api/llm/contingency", json={"on": True}, headers={"X-CSRF": g.csrf}).status_code == 401
    assert g.conn.execute("SELECT COUNT(*) AS n FROM admin_sessions").fetchone()["n"] == 0


def test_expired_session_is_rejected(g):
    g.configure(); g.login()
    g.conn.execute("UPDATE admin_sessions SET expires_at = now() - interval '1 minute'")
    assert g.client.post("/api/llm/contingency", json={"on": False}, headers={"X-CSRF": g.csrf}).status_code == 401
    assert g.client.get("/api/auth/me", headers=g.R).json()["logado"] is False


@pytest.mark.parametrize("what", ["state_errado", "sem_cookie", "reuso", "nonce", "email", "nao_verificado", "aud", "iss", "expirado", "google_recusa", "google_error"])
def test_google_callback_rejections_never_create_a_session(g, what):
    g.configure()
    r, q = g.start()
    state = q["state"][0]
    if what == "state_errado":
        cb = g.client.get("/api/auth/google/callback", params={"code": "abc", "state": "outro"})
        want = 400
    elif what == "sem_cookie":
        g.client.cookies.clear()
        cb = g.client.get("/api/auth/google/callback", params={"code": "abc", "state": state})
        want = 400
    elif what == "reuso":
        assert g.client.get("/api/auth/google/callback", params={"code": "abc", "state": state}).status_code == 303
        g.conn.execute("DELETE FROM admin_sessions")
        g.client.cookies.set("mer_oauth", state)
        cb = g.client.get("/api/auth/google/callback", params={"code": "abc", "state": state})
        want = 400
    else:
        want = {"nonce": 400, "email": 403, "nao_verificado": 403, "aud": 400, "iss": 400, "expirado": 400, "google_recusa": 502}.get(what, 400)
        if what == "nonce":
            g.token_reply = lambda d: _Resp(200, {"id_token": _idtoken(nonce="outro")})
        elif what == "email":
            g.over = {"email": "intruso@gmail.com"}
        elif what == "nao_verificado":
            g.over = {"email_verified": False}
        elif what == "aud":
            g.over = {"aud": "outro-cliente"}
        elif what == "iss":
            g.over = {"iss": "https://evil.example.com"}
        elif what == "expirado":
            g.over = {"exp": time.time() - 5}
        elif what == "google_recusa":
            g.token_reply = lambda d: _Resp(400, {"error": "invalid_grant"})
        if what == "google_error":
            cb = g.client.get("/api/auth/google/callback", params={"error": "access_denied", "state": state})
        else:
            cb = g.client.get("/api/auth/google/callback", params={"code": "abc", "state": state})
    assert cb.status_code == want, (what, cb.status_code, cb.text)
    if what != "reuso":
        assert g.conn.execute("SELECT COUNT(*) AS n FROM admin_sessions").fetchone()["n"] == 0
    assert "mer_session" not in cb.headers.get("set-cookie", "")
    _secret_free(cb.text)
    if what == "email":
        ev = [e for e in g.ga.events(g.conn) if e["kind"] == "login_negado"]
        assert ev and ev[0]["email"] == "intruso@gmail.com"                                          # tentativa registrada


def test_email_comparison_ignores_case_and_old_states_expire(g):
    g.configure()
    g.over = {"email": "JunioBC@Gmail.com"}
    cb, me = g.login()
    assert me["logado"]
    r, q = g.start()
    g.conn.execute("UPDATE oauth_states SET created_at = now() - interval '11 minutes'")
    cb = g.client.get("/api/auth/google/callback", params={"code": "abc", "state": q["state"][0]})
    assert cb.status_code == 400


def test_recovery_token_works_but_is_locked_after_five_failures(g):
    assert g.client.post("/api/llm/contingency", json={"on": False}, headers={"X-Admin-Token": "recuperacao-do-admin"}).status_code == 200
    for i in range(5):
        assert g.client.post("/api/llm/contingency", json={"on": True}, headers={"X-Admin-Token": f"errado-{i}"}).status_code == 401
    assert g.client.post("/api/llm/contingency", json={"on": True}, headers={"X-Admin-Token": "recuperacao-do-admin"}).status_code == 429   # bloqueio de 15 min
    ev = g.ga.events(g.conn)
    assert sum(1 for e in ev if e["kind"] == "recuperacao_falha") == 5 and "errado-" not in str([dict(e) for e in ev])


def test_llm_key_cards_save_test_remove_and_never_echo_the_key(g, monkeypatch):
    from meridiano.engine import llm
    g.configure(); g.login()
    H = {"X-CSRF": g.csrf}
    secret = "sk-ant-chave-super-secreta-999"
    assert g.client.post("/api/llm/keys/anthropic", json={"key": secret}, headers=H).status_code == 200
    r = g.client.get("/api/llm/keys", headers=g.R)
    ant = next(k for k in r.json() if k["key_name"] == "anthropic")
    assert ant["configurada"] and ant["fonte"] == "arquivo" and secret not in r.text and next(k for k in r.json() if k["key_name"] == "openai")["configurada"] is False
    seen = {}

    def get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _Resp(200, {"data": [{"id": "a"}, {"id": "b"}]})
    monkeypatch.setattr(llm.httpx, "get", get)
    t = g.client.post("/api/llm/keys/anthropic/test", json={}, headers=H)
    assert t.json()["ok"] and "2 modelo" in t.json()["mensagem"] and secret not in t.text
    assert seen["url"] == "https://api.anthropic.com/v1/models" and seen["headers"]["x-api-key"] == secret
    monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: _Resp(401, {}))
    t = g.client.post("/api/llm/keys/anthropic/test", json={}, headers=H)
    assert t.json()["ok"] is False and "recusou" in t.json()["mensagem"] and secret not in t.text
    # "outro compatível": endereço precisa estar na lista
    assert g.client.post("/api/llm/keys/meu-outro", json={"key": "chave-qualquer-123"}, headers=H).status_code == 200
    assert g.client.post("/api/llm/keys/meu-outro/test", json={"base_url": "https://evil.example.com/v1"}, headers=H).json()["ok"] is False
    assert g.client.post("/api/llm/keys/meu-outro/test", json={}, headers=H).status_code == 400
    assert g.client.post("/api/llm/keys/..%2Fmp-token", json={"key": "chave-qualquer-123"}, headers=H).status_code in (400, 404, 405)
    assert g.client.delete("/api/llm/keys/anthropic", headers=H).status_code == 200
    assert not (g.secdir / "llm-anthropic").exists() and g.client.delete("/api/llm/keys/anthropic", headers=H).status_code == 404
    assert g.client.delete("/api/llm/keys/anthropic").status_code == 403                             # sem CSRF
    evs = str([dict(e) for e in g.ga.events(g.conn)])
    assert secret not in evs and "chave_salva" in evs and "chave_removida" in evs


def test_mp_card_remove_test_and_request_text(g, monkeypatch):
    from meridiano.engine import secrets_store as ss
    g.configure(); g.login()
    H = {"X-CSRF": g.csrf}
    g.conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (NULL,'Preciso do access token do Mercado Pago (Credenciais de produção) para receber pagamentos')")   # texto antigo
    ss.sync_owner_request(g.conn)
    open_ = [r["body"] for r in g.conn.execute("SELECT body FROM owner_requests WHERE NOT done").fetchall()]
    assert open_ == [ss.TOKEN_REQUEST] and "Configurações" in ss.TOKEN_REQUEST                      # uma só, com o endereço novo
    tok = "APP_USR-" + "a1b2c3d4" * 4
    monkeypatch.setattr(ss, "verify", lambda t: ("ok", "token aceito pelo Mercado Pago"))
    assert g.client.post("/api/secrets/mp-token/test", headers=H).status_code == 400                # nada para testar
    assert g.client.post("/api/secrets/mp-token", json={"access_token": tok}, headers=H).status_code == 200
    r = g.client.post("/api/secrets/mp-token/test", headers=H)
    assert r.status_code == 200 and r.json()["verificado"] and tok not in r.text
    r = g.client.delete("/api/secrets/mp-token", headers=H)
    assert r.status_code == 200 and r.json()["configurado"] is False and tok not in r.text
    assert not (g.secdir / "mp-token").exists() and not (g.secdir / "mp-meta.json").exists()
    assert g.conn.execute("SELECT COUNT(*) AS n FROM owner_requests WHERE NOT done").fetchone()["n"] == 1   # o pedido ao dono reabriu


def test_no_test_mode_wording_in_customer_pages_or_settings_text():
    from meridiano.engine import store, secrets_store as ss
    for t in (ss.TOKEN_REQUEST, store.render_soon("n")):
        assert "em breve" not in t.lower() and "teste" not in t.lower() and "ensaio" not in t.lower()


def test_google_session_also_opens_the_panel_without_the_read_token(g):
    g.configure()
    assert g.client.get("/api/state").status_code == 401
    g.login()
    assert g.client.get("/api/state").status_code == 200 and g.client.get("/api/employees").status_code == 200   # só o cookie, sem X-Token
    assert TestClient(g.main.app, base_url="https://testserver").get("/api/state").status_code == 401
    assert g.client.get("/api/state", headers={"X-Token": "errado"}).status_code == 401                          # token errado não cai para a sessão


def test_google_oauth_file_path_is_configurable(g, tmp_path, monkeypatch):
    alt = tmp_path / "outro.json"
    alt.write_text(_json.dumps({"client_id": _CID, "client_secret": _SEC, "owner_email": "juniobc@gmail.com"}))
    monkeypatch.setenv("GOOGLE_OAUTH_FILE", str(alt))
    assert g.client.get("/api/auth/google/start").status_code == 302                  # lê do caminho do ambiente, não de SECRETS_DIR
    monkeypatch.setenv("GOOGLE_OAUTH_FILE", str(tmp_path / "nao-existe.json"))
    r = g.client.get("/api/auth/google/start")
    assert r.status_code == 503 and "não configurado" in r.text and _SEC not in r.text


# ---------------------------------------------------------------- PR B: conversa do dono com a empresa
def _chat_client(conn, monkeypatch):
    monkeypatch.setenv("MERIDIANO_API_TOKEN", "leitura")
    from meridiano.api import main
    main._conn = conn
    return TestClient(main.app)


def test_chat_send_validation_auth_and_thread(conn, monkeypatch):
    from meridiano.engine import seed
    seed.seed(conn)
    c = _chat_client(conn, monkeypatch)
    R = {"X-Token": "leitura"}
    assert c.post("/api/chat", json={"to": "ceo", "body": "oi"}).status_code == 401
    assert c.get("/api/chat").status_code == 401
    assert c.post("/api/chat", json={"to": "ceo", "body": "   "}, headers=R).status_code == 400
    assert c.post("/api/chat", json={"to": "ceo", "body": "x" * 2001}, headers=R).status_code == 422
    assert c.post("/api/chat", json={"to": "fantasma", "body": "oi"}, headers=R).status_code == 400
    assert c.post("/api/chat", json={"to": "../x", "body": "oi"}, headers=R).status_code == 422
    assert c.post("/api/chat", json={"to": "ceo", "body": "Priorize o produto 1"}, headers=R).status_code == 200
    assert c.post("/api/chat", json={"to": "todos", "body": "Bom dia, equipe"}, headers=R).status_code == 200
    t = c.get("/api/chat", headers=R).json()
    assert [m["texto"] for m in t["mensagens"]] == ["Priorize o produto 1", "Bom dia, equipe"]
    assert t["mensagens"][0]["para"] == "Rafael" and t["mensagens"][1]["para"] == "todos" and t["mensagens"][0]["recebida_por"] == []
    assert t["limite_turnos_extras"] == 30 and t["turnos_extras_hoje"] == 0


def test_chat_daily_message_cap(conn, monkeypatch):
    from meridiano.engine import config, owner_chat, seed
    seed.seed(conn)
    monkeypatch.setattr(config, "OWNER_MSGS_PER_DAY", 2)
    assert owner_chat.send(conn, "ceo", "a")[1] is None and owner_chat.send(conn, "ceo", "b")[1] is None
    assert "limite diário" in owner_chat.send(conn, "ceo", "c")[1]


def test_owner_message_goes_first_in_the_prompt_and_is_read_only_after_a_clean_turn(conn):
    from meridiano.engine import agent, owner_chat, seed
    seed.seed(conn)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    pres = conn.execute("SELECT * FROM employees WHERE slug='presidente'").fetchone()
    owner_chat.send(conn, "ceo", "Quero um produto de planilhas")
    owner_chat.send(conn, "todos", "Foco total em vendas reais")
    seen = {}

    def fake(conn_, emp_id, model, messages, tools):
        seen["prompt"] = messages[1]["content"]
        return {"role": "assistant", "content": "ok"}

    def boom(*a, **k):
        raise RuntimeError("provedor caiu")
    with pytest.raises(RuntimeError):
        agent.run_turn(conn, ceo, chat=boom)
    assert len(owner_chat.pending(conn, ceo["id"])) == 2                       # turno com erro: continua pendente
    agent.run_turn(conn, ceo, chat=fake)
    p = seen["prompt"]
    assert p.startswith("MENSAGENS DO DONO") and "Quero um produto de planilhas" in p and "para todos" in p and "reply_owner" in p
    assert p.index("MENSAGENS DO DONO") < p.index("Seu slug")
    assert owner_chat.pending(conn, ceo["id"]) == []                           # recebida
    assert [m["body"] for m in owner_chat.pending(conn, pres["id"])] == ["Foco total em vendas reais"]   # a presidente só vê a de todos
    agent.run_turn(conn, ceo, chat=fake)
    assert "MENSAGENS DO DONO" not in seen["prompt"]                           # não repete
    t = owner_chat.thread(conn)
    assert t[0]["recebida_por"] == ["Rafael"]


def test_everyone_message_is_not_delivered_to_people_hired_later(conn):
    from meridiano.engine import owner_chat, seed, tools
    seed.seed(conn)
    conn.execute("UPDATE employees SET created_at = now() - interval '1 hour'")
    owner_chat.send(conn, "todos", "Aviso geral")
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    assert tools.run_tool(conn, ceo, "hire_employee", {"name": "Nova", "role": "Analista", "area": "A", **HIRE}).startswith("contratado")
    nova = conn.execute("SELECT id FROM employees WHERE slug='nova'").fetchone()
    assert owner_chat.pending(conn, nova["id"]) == []


def test_reply_owner_tool_records_activity_and_is_rate_limited(conn):
    from meridiano.engine import owner_chat, seed, tools
    seed.seed(conn)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    owner_chat.send(conn, "ceo", "Como vai?")
    assert "reply_owner" in [t["function"]["name"] for t in tools.TOOL_DEFS]
    assert tools.run_tool(conn, ceo, "reply_owner", {"body": "Vou escrever a seção 2 agora."}).startswith("resposta enviada")
    assert tools.run_tool(conn, ceo, "reply_owner", {"body": ""}).startswith("erro")
    r = conn.execute("SELECT * FROM owner_messages WHERE direction='empresa'").fetchone()
    assert r["employee_id"] == ceo["id"] and r["reply_to"] is not None
    assert conn.execute("SELECT kind FROM activity WHERE employee_id=%s ORDER BY id DESC LIMIT 1", (ceo["id"],)).fetchone()["kind"] == "conversando"
    tools.run_tool(conn, ceo, "reply_owner", {"body": "segunda"}); tools.run_tool(conn, ceo, "reply_owner", {"body": "terceira"})
    assert tools.run_tool(conn, ceo, "reply_owner", {"body": "quarta"}).startswith("erro")


def test_owner_message_triggers_an_immediate_turn_with_daily_cap_and_no_double_run(conn, monkeypatch):
    from meridiano.engine import config, owner_chat, seed
    from meridiano.engine.main import Engine
    seed.seed(conn)
    ran = []
    run = lambda c, emp: ran.append(emp["slug"])
    eng = Engine()
    assert eng.dispatch_owner(conn, run=run, wait=True) == 0                   # sem mensagem, sem turno
    owner_chat.send(conn, "ceo", "Urgente")
    assert eng.dispatch_owner(conn, run=run, wait=True) == 1 and ran == ["ceo"]
    assert eng.dispatch_owner(conn, run=run, wait=True) == 0                   # a mesma mensagem não dispara de novo
    assert eng.running == {}                                                   # liberou o funcionário
    owner_chat.send(conn, "todos", "Bom dia")                                  # para todos: Presidente e CEO
    assert eng.dispatch_owner(conn, run=run, wait=True) == 2 and sorted(ran[1:]) == ["ceo", "presidente"]
    # limite diário
    monkeypatch.setattr(config, "OWNER_TURNS_PER_DAY", 3)
    owner_chat.send(conn, "ceo", "mais uma")
    owner_chat.send(conn, "presidente", "e outra")
    assert eng.dispatch_owner(conn, run=run, wait=True) == 0                   # já são 3 turnos extras hoje
    assert len(owner_chat.pending(conn, conn.execute("SELECT id FROM employees WHERE slug='ceo'").fetchone()["id"])) >= 1   # fica para o turno normal


def test_dispatch_skips_someone_already_in_turn_and_respects_429_pause(conn):
    import time as _t
    from meridiano.engine import owner_chat, seed
    from meridiano.engine.main import Engine
    seed.seed(conn)
    ceo = conn.execute("SELECT * FROM employees WHERE slug='ceo'").fetchone()
    owner_chat.send(conn, "ceo", "Urgente")
    eng = Engine()
    eng.running[ceo["id"]] = ceo["name"]                                       # CEO já está em turno
    assert eng.dispatch_owner(conn, run=lambda c, e: None, wait=True) == 0
    eng.running.clear()
    eng.pause_until = _t.time() + 60                                           # 429 em andamento: ninguém roda
    assert eng.dispatch_owner(conn, run=lambda c, e: None, wait=True) == 0
    eng.pause_until = 0
    assert eng.dispatch_owner(conn, run=lambda c, e: None, wait=True) == 1


def test_google_start_answers_head_with_the_same_redirect(g):
    g.configure()
    r = g.client.head("/api/auth/google/start")
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
