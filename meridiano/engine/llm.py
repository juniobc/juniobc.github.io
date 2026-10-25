"""Clientes de LLM por perfil (OpenAI-compatível e Anthropic nativo), custo real por chamada, tetos diários e modo contingência.
Chaves vivem só em arquivos ~/meridiano/secrets/llm-<key_name> (600): nunca no banco, no log nem visíveis a agentes e ferramentas."""
import json
import os
import re
import time
from urllib.parse import urlparse

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


KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
ANTHROPIC_VERSION = "2023-06-01"


# ---------------------------------------------------------------- perfis, chaves e estado
def host_ok(url):
    """Só https e só hosts conhecidos, sem usuário/senha na URL, sem porta estranha (evita mandar a chave para um servidor qualquer)."""
    try:
        u = urlparse(str(url))
        return u.scheme == "https" and u.hostname in config.LLM_HOSTS and not u.username and not u.password and u.port in (None, 443)
    except Exception:
        return False


def key_path(key_name):
    if not KEY_RE.match(str(key_name or "")):
        return None
    return os.path.join(config.SECRETS_DIR, "llm-" + key_name)


def read_key(profile):
    """Só para o código de chamada. Nunca devolver por API, log ou ferramenta."""
    p = key_path(profile["key_name"])
    if p:
        try:
            with open(p, encoding="utf-8") as f:
                line = next((x.strip() for x in f.read().splitlines() if x.strip() and not x.strip().startswith("#")), "")
            if "=" in line:
                line = line.split("=", 1)[1]
            line = line.strip().strip("'\"").strip()
            if len(line) >= 8:
                return line
        except OSError:
            pass
    if profile["key_name"] == "nous":    # compatível com o arquivo de ambiente atual do servidor
        return config.env("NOUS_API_KEY") or None
    return None


def key_configured(profile):
    return read_key(profile) is not None


def save_key(key_name, raw):
    """Admin: grava a chave em arquivo 600. Devolve (ok, mensagem). Nunca devolve a chave."""
    p = key_path(key_name)
    if not p:
        return False, "nome de chave inválido (use letras minúsculas, números, - e _)"
    key = (raw or "").strip().strip("'\"").strip()
    if not 8 <= len(key) <= 400 or re.search(r"\s", key):
        return False, "chave com formato inválido"
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return True, "chave guardada em arquivo protegido"


def test_key(key_name, base_url):
    """Chamada mínima (listar modelos) para ver se a chave vale. Devolve (ok, mensagem) sem nunca incluir a chave."""
    if not host_ok(base_url):
        return False, "endereço fora da lista permitida"
    key = read_key({"key_name": key_name})
    if not key:
        return False, "nenhuma chave configurada com esse nome"
    base = base_url.rstrip("/")
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION} if urlparse(base).hostname == "api.anthropic.com" else {"Authorization": "Bearer " + key}
    try:
        r = httpx.get(base + "/models", headers=headers, timeout=20)
    except Exception:
        return False, "não consegui falar com o provedor agora"
    if r.status_code in (401, 403):
        return False, f"o provedor recusou esta chave ({r.status_code})"
    if r.status_code != 200:
        return False, f"resposta inesperada do provedor ({r.status_code})"
    try:
        n = len((r.json() or {}).get("data") or [])
    except ValueError:
        n = 0
    return True, f"chave aceita; {n} modelo(s) listado(s)" if n else "chave aceita"


def settings(conn):
    r = conn.execute("SELECT contingency, global_usd_cap FROM llm_settings WHERE id=1").fetchone()
    return r or {"contingency": True, "global_usd_cap": 2}


def spent_today(conn, profile_id=None, employee_id=None):
    q, a = "SELECT COALESCE(SUM(cost_usd),0) AS c FROM usage_log WHERE created_at >= date_trunc('day', now())", []
    if profile_id is not None:
        q += " AND profile_id=%s"
        a.append(profile_id)
    if employee_id is not None:
        q += " AND employee_id=%s"
        a.append(employee_id)
    return float(conn.execute(q, a).fetchone()["c"])


def usable(conn, p, st=None):
    """(ok, motivo): este perfil pode ser chamado agora?"""
    st = st or settings(conn)
    if not p["enabled"]:
        return False, "desligado"
    if not p["supports_tools"]:
        return False, "sem suporte a tools"
    if not host_ok(p["base_url"]):
        return False, "endereço fora da lista permitida"
    if p["is_free"]:
        return True, ""
    if st["contingency"]:
        return False, "modo contingência (só perfis grátis)"
    if not key_configured(p):
        return False, "sem chave configurada"
    cap = float(p["daily_usd_cap"] or 0)
    if cap <= 0:
        return False, "sem limite diário em USD"
    if spent_today(conn, p["id"]) >= cap:
        return False, f"limite diário de US$ {cap:.2f} atingido"
    if spent_today(conn) >= float(st["global_usd_cap"]):
        return False, f"teto global de US$ {float(st['global_usd_cap']):.2f} atingido"
    return True, ""


def free_chain(conn, prefer_model=None):
    """Perfis grátis chamáveis, na ordem do rodízio: o do modelo preferido primeiro."""
    rows = conn.execute("SELECT * FROM llm_profiles WHERE is_free AND enabled AND supports_tools ORDER BY id").fetchall()
    rows = [r for r in rows if r["model"] not in config.AVOID and host_ok(r["base_url"])]
    rows.sort(key=lambda r: 0 if r["model"] == prefer_model else 1)
    return rows


def resolve(conn, emp):
    """Perfil EFETIVO de um funcionário: o configurado, se puder ser chamado agora; senão o grátis padrão.
    Devolve {profile, wanted, reason}; reason vazio = sem desvio. A configuração do funcionário nunca é apagada."""
    wanted = None
    if emp.get("llm_profile_id"):
        wanted = conn.execute("SELECT * FROM llm_profiles WHERE id=%s", (emp["llm_profile_id"],)).fetchone()
    if wanted is None:
        wanted = conn.execute("SELECT * FROM llm_profiles WHERE model=%s ORDER BY is_free DESC, id LIMIT 1", (emp.get("model"),)).fetchone()
    if wanted is not None:
        ok, why = usable(conn, wanted)
        if ok:
            return {"profile": wanted, "wanted": wanted, "reason": ""}
    else:
        why = "perfil não encontrado"
    fb = free_chain(conn, emp.get("model"))
    if not fb:
        raise RuntimeError("nenhum perfil grátis habilitado")
    return {"profile": fb[0], "wanted": wanted, "reason": why if wanted is not None and wanted["id"] != fb[0]["id"] else ""}


def best_profile(conn):
    """Melhor perfil disponível agora para cargos de decisão: o pago mais caro (proxy de qualidade) que passa em todas as regras, senão o grátis padrão."""
    st = settings(conn)
    best = None
    for p in conn.execute("SELECT * FROM llm_profiles WHERE NOT is_free ORDER BY price_out_per_m DESC, id").fetchall():
        if usable(conn, p, st)[0]:
            best = p
            break
    return best or free_chain(conn)[0]


def allowed_for_hire(conn, ident):
    """Perfil escolhido por label ou id na contratação: (perfil, erro)."""
    ident = str(ident).strip()
    p = None
    if ident.isdigit():
        p = conn.execute("SELECT * FROM llm_profiles WHERE id=%s", (int(ident),)).fetchone()
    if p is None:
        p = conn.execute("SELECT * FROM llm_profiles WHERE lower(label)=lower(%s) OR lower(model)=lower(%s) ORDER BY is_free DESC, id LIMIT 1", (ident, ident)).fetchone()
    if p is None:
        return None, "perfil de LLM não existe (veja list_llm_profiles)"
    ok, why = usable(conn, p)
    if not ok:
        return None, f"perfil '{p['label']}' não pode ser usado agora: {why}"
    return p, None


# ---------------------------------------------------------------- chamadas
def cost_usd(p, pt, ct):
    return round((pt * float(p["price_in_per_m"]) + ct * float(p["price_out_per_m"])) / 1_000_000, 6)


def to_anthropic(messages, tools):
    """Formato OpenAI -> Messages API (tool_use/tool_result). O prompt de sistema vira o campo `system`."""
    system, out = [], []

    def push(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(str(m.get("content") or ""))
        elif role == "user":
            push("user", [{"type": "text", "text": str(m.get("content") or "(vazio)")}])
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    inp = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": fn.get("name"), "input": inp if isinstance(inp, dict) else {}})
            push("assistant", blocks or [{"type": "text", "text": "(vazio)"}])
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": str(m.get("content") or "")}])
    body = {"system": "\n\n".join(system), "messages": out}
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}} for t in tools]
    return body


def from_anthropic(data):
    """Resposta da Messages API -> mensagem no formato OpenAI (content + tool_calls)."""
    text, calls = [], []
    for b in data.get("content") or []:
        if b.get("type") == "text":
            text.append(b.get("text") or "")
        elif b.get("type") == "tool_use":
            calls.append({"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    msg = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        msg["tool_calls"] = calls
    u = data.get("usage") or {}
    return msg, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)


def call(conn, employee_id, p, messages, tools=None):
    """Uma chamada a um perfil já aprovado por `usable`. Registra uso, tokens e custo em USD."""
    if not host_ok(p["base_url"]):
        raise ValueError(f"endereço do perfil fora da lista permitida: {p['label']}")
    key = read_key(p)
    if not key:
        raise RuntimeError(f"chave do perfil '{p['label']}' não configurada")
    base = p["base_url"].rstrip("/")
    model = p["model"]
    try:
        if p["provider"] == "anthropic":
            body = {"model": model, "max_tokens": config.MAX_TOKENS, **to_anthropic(messages, tools)}
            r = httpx.post(base + "/messages", json=body, timeout=config.LLM_TIMEOUT,
                           headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
        else:
            body = {"model": model, "messages": messages}
            body["max_completion_tokens" if urlparse(base).hostname == "api.openai.com" else "max_tokens"] = config.MAX_TOKENS
            if tools:
                body["tools"] = tools
            r = httpx.post(base + "/chat/completions", json=body, timeout=config.LLM_TIMEOUT, headers={"Authorization": "Bearer " + key})
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
        if resp is not None:  # corpo do erro da API
            detail = f"{resp.status_code}: {resp.text[:400]}"
        detail = detail.replace(key, "***")
        conn.execute("INSERT INTO usage_log(employee_id,model,profile_id,ok,error) VALUES (%s,%s,%s,false,%s)",
                     (employee_id, model, p["id"], f"[{model}] {detail}"[:500]))
        raise
    if p["provider"] == "anthropic":
        msg, pt, ct = from_anthropic(data)
    else:
        u = data.get("usage") or {}
        msg, pt, ct = data["choices"][0]["message"], int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    empty = not (msg.get("content") or msg.get("tool_calls"))
    conn.execute("INSERT INTO usage_log(employee_id,model,profile_id,prompt_tokens,completion_tokens,cost_usd,ok,error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 (employee_id, model, p["id"], pt, ct, cost_usd(p, pt, ct), not empty,
                  "resposta vazia (sem content nem tool_calls; provável max_tokens gasto pensando)" if empty else None))
    if empty:
        raise EmptyResponse(model)
    return msg


def chat(conn, employee_id, model, messages, tools=None):
    """Chamada direta por nome de modelo (uso interno e testes): só modelos que existem como perfil chamável."""
    p = conn.execute("SELECT * FROM llm_profiles WHERE model=%s ORDER BY is_free DESC, id LIMIT 1", (model,)).fetchone()
    if p is None:
        raise ValueError(f"modelo fora da lista permitida: {model}")
    if not p["is_free"]:
        ok, why = usable(conn, p)
        if not ok:
            raise ValueError(f"perfil pago não permitido agora: {why}")
    return call(conn, employee_id, p, messages, tools)


def budget_fraction(conn):
    """Fração do limite diário já usada (a maior entre tokens e chamadas)."""
    calls, tokens = usage_today(conn)
    return max(tokens / config.DAILY_TOKEN_LIMIT, calls / config.DAILY_CALL_LIMIT)


def chat_fallback(conn, employee_id, model, messages, tools=None, sleep=time.sleep):
    """Usa o perfil efetivo do funcionário (configurado, ou o grátis padrão se pago não puder agora) e, se vier vazio/erro,
    até 2 perfis grátis do rodízio. Em 429 espera com recuo exponencial (2, 4, 8 s, ou o Retry-After) antes de desistir daquele perfil."""
    emp = conn.execute("SELECT id,model,llm_profile_id FROM employees WHERE id=%s", (employee_id,)).fetchone() if employee_id else None
    emp = emp or {"id": None, "model": model, "llm_profile_id": None}
    first = resolve(conn, emp)["profile"]
    order = [first] + [p for p in free_chain(conn, model) if p["id"] != first["id"]][:2]
    last = None
    for p in order:
        for attempt in range(3):
            try:
                return call(conn, employee_id, p, messages, tools)
            except RateLimited as e:
                last = e
                if attempt < 2:
                    sleep(min(30, e.retry_after or 2 ** (attempt + 1)))
            except Exception as e:
                last = e
                break
    raise last
