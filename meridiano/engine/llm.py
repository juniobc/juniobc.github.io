"""Cliente da API Nous (formato OpenAI) com lista fixa de modelos e limite diário."""
import httpx
from . import config


class BudgetExceeded(Exception):
    pass


def usage_today(conn):
    r = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens "
        "FROM usage_log WHERE created_at >= date_trunc('day', now())"
    ).fetchone()
    return int(r["calls"]), int(r["tokens"])


def chat(conn, employee_id, model, messages, tools=None):
    if model not in config.ALLOWED_MODELS:
        raise ValueError(f"modelo fora da lista permitida: {model}")
    calls, tokens = usage_today(conn)
    if calls >= config.DAILY_CALL_LIMIT or tokens >= config.DAILY_TOKEN_LIMIT:
        raise BudgetExceeded(f"limite diário atingido ({calls} chamadas, {tokens} tokens)")
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
    conn.execute("INSERT INTO usage_log(employee_id,model,prompt_tokens,completion_tokens) VALUES (%s,%s,%s,%s)",
                 (employee_id, model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)))
    return data["choices"][0]["message"]
