"""Descobrir modelos grátis com suporte a tools nos provedores (GET /models) e registrar propostas de agentes.
Só lê catálogos públicos; nada é ligado aqui: adotar cria um perfil DESLIGADO que precisa passar na avaliação e do clique do admin."""
import re

import httpx

from . import config, llm

SOURCES = (("nous", "Nous", "https://inference-api.nousresearch.com/v1"), ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(entries, key_name, base_url):
    """Normaliza a lista de modelos. Candidato = preço zero informado (ou sufixo ':free' quando o provedor não informa preço)
    e não declarar que NÃO suporta tools. Devolve lista de dicts."""
    out = []
    for e in entries or []:
        mid = str(e.get("id") or "").strip()
        if not mid or len(mid) > 160:
            continue
        pr = e.get("pricing") or {}
        pin, pout = _num(pr.get("prompt")), _num(pr.get("completion"))
        if pin is not None and pout is not None:
            free = pin == 0 and pout == 0
        else:
            free = mid.endswith(":free")
        if not free:
            continue
        sp = e.get("supported_parameters")
        tools = "desconhecido" if sp is None else ("sim" if "tools" in sp else "nao")
        if tools == "nao":
            continue
        ctx = e.get("context_length")
        out.append({"model": mid, "name": str(e.get("name") or mid)[:120], "context_length": int(ctx) if isinstance(ctx, (int, float)) else None,
                    "tools": tools, "key_name": key_name, "base_url": base_url})
    return out


def fetch(key_name, base_url):
    """(lista, aviso). Usa a chave do provedor se existir (a Nous exige; a do OpenRouter é grátis)."""
    if not llm.host_ok(base_url):
        return [], "endereço fora da lista permitida"
    key = llm.read_key({"key_name": key_name})
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        r = httpx.get(base_url.rstrip("/") + "/models", headers=headers, timeout=30)
    except Exception:
        return [], "não consegui falar com o provedor agora"
    if r.status_code in (401, 403):
        return [], f"o provedor recusou a consulta ({r.status_code})" + ("" if key else f"; configure a chave '{key_name}' na aba Configurações")
    if r.status_code != 200:
        return [], f"resposta inesperada do provedor ({r.status_code})"
    try:
        data = (r.json() or {}).get("data")
    except ValueError:
        return [], "resposta ilegível do provedor"
    if not isinstance(data, list):
        return [], "resposta sem lista de modelos"
    return parse(data, key_name, base_url), ("" if key else f"sem chave '{key_name}': consulta feita sem login")


def discover(conn, fetcher=None):
    """Consulta Nous e OpenRouter e grava os candidatos que ainda não são perfil. Devolve {novos, avisos, total}."""
    fetcher = fetcher or fetch
    known = {(r["base_url"].rstrip("/"), r["model"]) for r in conn.execute("SELECT base_url, model FROM llm_profiles").fetchall()}
    novos, avisos = [], []
    for key_name, label, base in SOURCES:
        items, warn = fetcher(key_name, base)
        if warn:
            avisos.append(f"{label}: {warn}")
        for it in items:
            if (base.rstrip("/"), it["model"]) in known:
                continue
            r = conn.execute(
                "INSERT INTO llm_candidates(source,provider,base_url,model,key_name,name,context_length,free,tools) VALUES ('descoberta','openai_compat',%s,%s,%s,%s,%s,true,%s) "
                "ON CONFLICT (base_url, model) DO NOTHING RETURNING id", (base.rstrip("/"), it["model"], key_name, it["name"], it["context_length"], it["tools"])).fetchone()
            if r:
                novos.append({"id": r["id"], "model": it["model"], "provedor": label})
    total = conn.execute("SELECT COUNT(*) AS n FROM llm_candidates WHERE status='novo'").fetchone()["n"]
    return {"novos": novos, "avisos": avisos, "total_candidatos": total}


def propose(conn, me, args):
    """Tool propose_llm_profile: o agente sugere; só o admin adota e liga. Proposta nasce não-grátis (preço a confirmar pelo dono)."""
    if not re.search(r"analista|oportunidade|presidente|ceo", me["role"], re.I):
        return "erro: só o Analista de Oportunidades, a Presidente e o CEO propõem modelos"
    model = str(args.get("model") or "").strip()
    base = str(args.get("base_url") or "").strip().rstrip("/")
    reason = str(args.get("reason") or "").strip()
    if not model or len(model) > 160 or re.search(r"\s", model):
        return "erro: informe o id exato do modelo (sem espaços)"
    if not llm.host_ok(base):
        return "erro: o endereço precisa ser de um provedor permitido (Nous, OpenRouter, OpenAI ou Anthropic), em https"
    if len(reason) < 30:
        return "erro: explique em pelo menos 30 caracteres por que este modelo ajudaria (preço, tools, qualidade em português) e cite a fonte"
    if conn.execute("SELECT COUNT(*) AS n FROM llm_candidates WHERE source='agente' AND proposer_id=%s AND discovered_at >= date_trunc('day', now())", (me["id"],)).fetchone()["n"] >= 3:
        return "erro: no máximo 3 propostas de modelo por dia"
    host = base.split("/")[2]
    key_name, provider = {"inference-api.nousresearch.com": ("nous", "openai_compat"), "openrouter.ai": ("openrouter", "openai_compat"),
                          "api.openai.com": ("openai", "openai_compat"), "api.anthropic.com": ("anthropic", "anthropic")}[host]
    pin, pout = max(0.0, float(args.get("price_in_per_m") or 0)), max(0.0, float(args.get("price_out_per_m") or 0))
    r = conn.execute("INSERT INTO llm_candidates(source,provider,base_url,model,key_name,name,price_in_per_m,price_out_per_m,proposer_id,reason) VALUES ('agente',%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                     "ON CONFLICT (base_url, model) DO NOTHING RETURNING id", (provider, base, model, key_name, model[:120], pin, pout, me["id"], reason[:600])).fetchone()
    if not r:
        return "esse modelo já está na lista de candidatos ou nos perfis"
    return f"proposta {r['id']} registrada; o dono decide se adota e liga (depois da avaliação automática)"
