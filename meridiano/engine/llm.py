"""Cliente da API Nous (formato OpenAI) com lista fixa de modelos e limite diário."""
import time

import httpx
from . import config


class BudgetExceeded(Exception):
    pass


class EmptyResponse(Exception):
    pass


class RateLimited(Exception):
    """HTTP 429 do provedor: não é falha nossa, não entra na conta de falhas."""
    def __init__(self, model, retry_after=None):
        super().__init__(f"429 em {model}")
        self.retry_after = retry_after


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
    key = config.env("NOUS_API_KEY")
    if not key:
        raise RuntimeError("NOUS_API_KEY não definida")
    body = {"model": model, "messages": messages, "max_tokens": config.MAX_TOKENS}
    if tools:
        body["tools"] = tools
    try:
        r = httpx.post(config.API_BASE + "/chat/completions", json=body, timeout=120,
                       headers={"Authorization": "Bearer " + key})
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", ""))
            except ValueError:
                ra = None
            raise RateLimited(model, ra)
        r.raise_for_status()
        data = r.json()
    except RateLimited:
        raise
    except Exception as e:
        detail = str(e)
        resp = getattr(e, "response", None)
        if resp is not None:  # corpo do erro da API (nunca contém a nossa chave)
            detail = f"{resp.status_code}: {resp.text[:400]}"
        conn.execute("INSERT INTO usage_log(employee_id,model,ok,error) VALUES (%s,%s,false,%s)",
                     (employee_id, model, f"[{model}] {detail}"[:500]))
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


def budget_fraction(conn):
    """Fração do limite diário já usada (a maior entre tokens e chamadas)."""
    calls, tokens = usage_today(conn)
    return max(tokens / config.DAILY_TOKEN_LIMIT, calls / config.DAILY_CALL_LIMIT)


def chat_fallback(conn, employee_id, model, messages, tools=None, sleep=time.sleep):
    """Tenta o modelo do funcionário e, se vier vazio/erro, até 2 outros do rodízio.
    Em 429 espera com recuo exponencial (2, 4, 8 s, ou o Retry-After) antes de desistir daquele modelo."""
    order = [model] + [m for m in config.DEFAULT_ROTATION if m != model][:2]
    last = None
    for m in order:
        for attempt in range(3):
            try:
                return chat(conn, employee_id, m, messages, tools)
            except RateLimited as e:
                last = e
                if attempt < 2:
                    sleep(min(30, e.retry_after or 2 ** (attempt + 1)))
            except Exception as e:
                last = e
                break
    raise last
