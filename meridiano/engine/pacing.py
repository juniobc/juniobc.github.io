"""Cadência do motor: constante, sem esperar por "ritmo do dia". Só o limite duro diário deixa o ritmo lento."""
from . import config, llm


def hard_limit_hit(conn):
    """Passou do limite diário (tokens, chamadas) ou do teto de falhas? Então cadência lenta, não parada."""
    fails, _ = llm.failures_today(conn)
    return llm.budget_fraction(conn) >= 1 or fails >= config.DAILY_FAIL_LIMIT


def next_delay(conn, rate_backoff=0, errored=False):
    """(segundos até o próximo turno, motivo). Motivos: nenhum, limite, 429, erro."""
    if rate_backoff:
        return rate_backoff, "429"
    if hard_limit_hit(conn):
        return config.SLOW_TICK_SECONDS, "limite"
    if errored:
        return config.TICK_SECONDS, "erro"
    return config.TICK_SECONDS, "nenhum"


def beat(conn, current=None, last_turn=None, delay=0, reason="nenhum"):
    """Atualiza a linha de status que o painel lê."""
    conn.execute(
        "UPDATE engine_status SET heartbeat_at=now(), current_employee_id=%s::integer, "
        "last_turn_employee_id=COALESCE(%s::integer, last_turn_employee_id), last_turn_at=CASE WHEN %s::integer IS NULL THEN last_turn_at ELSE now() END, "
        "next_turn_at=now() + make_interval(secs => %s::integer), sleep_s=%s::integer, wait_reason=%s WHERE id=1",
        (current, last_turn, last_turn, delay, delay, reason))


def record_isolation(conn, refresh=True):
    """Roda o autoteste de isolamento (se refresh) e grava o resultado para o painel. Se o portão acabou de abrir,
    processa os rascunhos de ferramentas. Devolve o estado do portão."""
    from . import sandbox, tools
    before = sandbox.enabled()
    g = sandbox.refresh_gate() if refresh else sandbox.gate()
    conn.execute("UPDATE engine_status SET tools_enabled=%s, tools_reason=%s, tools_help=%s, tools_backend=%s, isolation=%s WHERE id=1",
                 (g["enabled"], g["reason"][:1000], g["help"][:1000], g["backend"], ("ligadas: " if g["enabled"] else "desligadas: ") + g["reason"][:400]))
    if g["enabled"] and not before:
        tools.process_drafts(conn)
    return g
