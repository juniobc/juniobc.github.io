"""Cliente da API Nous (formato OpenAI) com lista fixa de modelos e limite diário."""
import httpx
from . import config


class BudgetExceeded(Exception):
    pass


class EmptyResponse(Exception):
    pass


def usage_today(conn):
    """Chamadas e tokens ÚTEIS de hoje (ok=true). Vazias e erros não consomem o limite útil."""
    r = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens "
        "FROM usage_log WHERE ok AND created_at >= date_trunc('day', now())"
    ).fetchone()
    return int(r["calls"]), int(r["tokens"])


def failures_today(conn):
    r = conn.execute("SELECT COUNT(*) AS n FROM usage_log WHERE NOT ok AND created_at >= date_trunc('day', now())").fetchone()
    last = conn.execute("SELECT error FROM usage_log WHERE NOT ok ORDER BY id DESC LIMIT 1").fetchone()
    return int(r["n"]), (last["error"] if last else None)


def chat(conn, employee_id, model, messages, tools=None):
    if model not in config.ALLOWED_MODELS:
        raise ValueError(f"modelo fora da lista permitida: {model}")
    calls, tokens = usage_today(conn)
    fails, _ = failures_today(conn)
    if calls >= config.DAILY_CALL_LIMIT or tokens >= config.DAILY_TOKEN_LIMIT or fails >= config.DAILY_FAIL_LIMIT:
        raise BudgetExceeded(f"limite diário atingido ({calls} chamadas, {tokens} tokens, {fails} falhas)")
    key = config.env("NOUS_API_KEY")
    if not key:
        raise RuntimeError("NOUS_API_KEY não definida")
    body = {"model": model, "messages": messages, "max_tokens": config.MAX_TOKENS}
    if tools:
        body["tools"] = tools
    try:
        r = httpx.post(config.API_BASE + "/chat/completions", json=body, timeout=120,
                       headers={"Authorization": "Bearer " + key})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        conn.execute("INSERT INTO usage_log(employee_id,model,ok,error) VALUES (%s,%s,false,%s)",
                     (employee_id, model, str(e)[:300]))
        raise
    u = data.get("usage") or {}
    msg = data["choices"][0]["message"]
    empty = not (msg.get("content") or msg.get("tool_calls"))
    conn.execute("INSERT INTO usage_log(employee_id,model,prompt_tokens,completion_tokens,ok,error) VALUES (%s,%s,%s,%s,%s,%s)",
                 (employee_id, model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), not empty,
                 "resposta vazia (sem content nem tool_calls; provável max_tokens gasto pensando)" if empty else None))
    if empty:
        raise EmptyResponse(model)
    return msg


def chat_fallback(conn, employee_id, model, messages, tools=None):
    """Tenta o modelo do funcionário e, se vier vazio/erro, até 2 outros do rodízio."""
    order = [model] + [m for m in config.DEFAULT_ROTATION if m != model][:2]
    last = None
    for m in order:
        try:
            return chat(conn, employee_id, m, messages, tools)
        except BudgetExceeded:
            raise
        except Exception as e:
            last = e
    raise last
