"""Perfis de LLM: leitura com token de leitura; trocar modelo, ligar perfil, chave e contingência exigem o token de administrador.
Chaves nunca são devolvidas: só "configurada: sim/não"."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from ..engine import llm


class NewProfile(BaseModel):
    label: str = Field(min_length=3, max_length=80)
    provider: str = Field(pattern="^(openai_compat|anthropic)$")
    base_url: str = Field(min_length=10, max_length=200)
    model: str = Field(min_length=2, max_length=120)
    key_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    price_in_per_m: float = Field(ge=0, le=1000)
    price_out_per_m: float = Field(ge=0, le=1000)
    supports_tools: bool = True
    is_free: bool = False
    daily_usd_cap: float | None = Field(None, ge=0, le=10000)
    notes: str = Field("", max_length=300)


class EditProfile(BaseModel):
    enabled: bool | None = None
    daily_usd_cap: float | None = Field(None, ge=0, le=10000)
    price_in_per_m: float | None = Field(None, ge=0, le=1000)
    price_out_per_m: float | None = Field(None, ge=0, le=1000)
    notes: str | None = Field(None, max_length=300)


class Assign(BaseModel):
    profile_id: int | None = None   # None = volta ao padrão grátis do rodízio


class Contingency(BaseModel):
    on: bool


class GlobalCap(BaseModel):
    usd: float = Field(ge=0, le=10000)


class KeyBody(BaseModel):
    key: str = Field(min_length=8, max_length=400)


def overview(c):
    st = llm.settings(c)
    spent = {r["profile_id"]: r for r in c.execute(
        "SELECT profile_id, COUNT(*) FILTER (WHERE ok) AS calls, COALESCE(SUM(prompt_tokens+completion_tokens) FILTER (WHERE ok),0) AS tokens, "
        "COALESCE(SUM(cost_usd),0) AS cost FROM usage_log WHERE created_at >= date_trunc('day', now()) GROUP BY profile_id").fetchall()}
    perfis = []
    for p in c.execute("SELECT * FROM llm_profiles ORDER BY is_free DESC, id").fetchall():
        ok, why = llm.usable(c, p, st)
        u = spent.get(p["id"]) or {"calls": 0, "tokens": 0, "cost": 0}
        perfis.append({"id": p["id"], "label": p["label"], "provedor": p["provider"], "modelo": p["model"], "gratis": p["is_free"], "ligado": p["enabled"],
                       "preco_entrada": float(p["price_in_per_m"]), "preco_saida": float(p["price_out_per_m"]), "tools": p["supports_tools"],
                       "chave_configurada": llm.key_configured(p),
                       "key_name": p["key_name"], "limite_diario_usd": float(p["daily_usd_cap"]) if p["daily_usd_cap"] is not None else None,
                       "gasto_hoje_usd": float(u["cost"]), "chamadas_hoje": int(u["calls"]), "tokens_hoje": int(u["tokens"]),
                       "chamavel_agora": ok, "motivo": why, "notas": p["notes"]})
    funcs = []
    for e in c.execute("SELECT e.id,e.slug,e.name,e.role,e.model,e.llm_profile_id FROM employees e WHERE e.active ORDER BY e.id").fetchall():
        r = llm.resolve(c, e)
        funcs.append({"slug": e["slug"], "nome": e["name"], "cargo": e["role"],
                      "perfil_id": r["wanted"]["id"] if r["wanted"] else None, "perfil": r["wanted"]["label"] if r["wanted"] else e["model"],
                      "efetivo": r["profile"]["label"], "desvio": r["reason"],
                      "gasto_hoje_usd": llm.spent_today(c, employee_id=e["id"])})
    por_agente = [{"nome": r["name"], "chamadas": int(r["calls"]), "tokens": int(r["tokens"]), "custo_usd": float(r["cost"])} for r in c.execute(
        "SELECT e.name, COUNT(*) FILTER (WHERE u.ok) AS calls, COALESCE(SUM(u.prompt_tokens+u.completion_tokens) FILTER (WHERE u.ok),0) AS tokens, "
        "COALESCE(SUM(u.cost_usd),0) AS cost FROM usage_log u JOIN employees e ON e.id=u.employee_id WHERE u.created_at >= date_trunc('day', now()) "
        "GROUP BY e.name ORDER BY cost DESC, tokens DESC").fetchall()]
    return {"contingencia": st["contingency"], "teto_global_usd": float(st["global_usd_cap"]), "gasto_hoje_usd": llm.spent_today(c),
            "perfis": perfis, "funcionarios": funcs, "uso_por_agente": por_agente,
            "avisos": [f"{f['nome']}: usa {f['efetivo']} no lugar de {f['perfil']} ({f['desvio']})" for f in funcs if f["desvio"]]}


def register(app, read_auth, admin_auth, conn_fn):
    @app.get("/api/llm", dependencies=[Depends(read_auth)])
    def llm_overview():
        return overview(conn_fn())

    @app.post("/api/llm/contingency", dependencies=[Depends(admin_auth)])
    def set_contingency(b: Contingency):
        c = conn_fn()
        c.execute("UPDATE llm_settings SET contingency=%s WHERE id=1", (b.on,))
        return {"contingencia": b.on}

    @app.post("/api/llm/global-cap", dependencies=[Depends(admin_auth)])
    def set_global_cap(b: GlobalCap):
        conn_fn().execute("UPDATE llm_settings SET global_usd_cap=%s WHERE id=1", (b.usd,))
        return {"teto_global_usd": b.usd}

    @app.post("/api/llm/profiles", dependencies=[Depends(admin_auth)])
    def add_profile(b: NewProfile):
        if not llm.host_ok(b.base_url):
            raise HTTPException(400, "endereço fora da lista permitida (https e um dos provedores conhecidos)")
        if b.provider == "anthropic" and not b.base_url.startswith("https://api.anthropic.com/"):
            raise HTTPException(400, "o provedor Anthropic só aceita api.anthropic.com")
        if b.is_free and (b.price_in_per_m or b.price_out_per_m):
            raise HTTPException(400, "perfil grátis não pode ter preço")
        c = conn_fn()
        try:
            r = c.execute("INSERT INTO llm_profiles(label,provider,base_url,model,key_name,price_in_per_m,price_out_per_m,supports_tools,is_free,enabled,daily_usd_cap,notes) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,false,%s,%s) RETURNING id",
                          (b.label.strip(), b.provider, b.base_url.rstrip("/"), b.model.strip(), b.key_name, b.price_in_per_m, b.price_out_per_m, b.supports_tools,
                           b.is_free, b.daily_usd_cap, b.notes)).fetchone()
        except Exception:
            raise HTTPException(409, "já existe um perfil com esse nome ou com esse endereço e modelo")
        return {"id": r["id"], "ligado": False}

    @app.patch("/api/llm/profiles/{pid}", dependencies=[Depends(admin_auth)])
    def edit_profile(pid: int, b: EditProfile):
        c = conn_fn()
        p = c.execute("SELECT * FROM llm_profiles WHERE id=%s", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "perfil não existe")
        new = {"enabled": p["enabled"] if b.enabled is None else b.enabled,
               "daily_usd_cap": p["daily_usd_cap"] if b.daily_usd_cap is None else b.daily_usd_cap,
               "price_in_per_m": p["price_in_per_m"] if b.price_in_per_m is None else b.price_in_per_m,
               "price_out_per_m": p["price_out_per_m"] if b.price_out_per_m is None else b.price_out_per_m,
               "notes": p["notes"] if b.notes is None else b.notes}
        if p["is_free"] and (new["price_in_per_m"] or new["price_out_per_m"]):
            raise HTTPException(400, "perfil grátis não pode ter preço")
        if new["enabled"] and not p["is_free"]:
            if not llm.key_configured(p):
                raise HTTPException(400, f"configure primeiro a chave '{p['key_name']}' (campo de chave do painel ou arquivo llm-{p['key_name']})")
            if not new["daily_usd_cap"] or float(new["daily_usd_cap"]) <= 0:
                raise HTTPException(400, "defina o limite diário em USD antes de ligar um perfil pago")
        if new["enabled"] and not p["supports_tools"]:
            raise HTTPException(400, "perfil sem suporte a tools não pode ser ligado")
        if not new["enabled"] and p["is_free"] and p["enabled"]:   # sempre sobra ao menos um grátis ligado
            if c.execute("SELECT COUNT(*) AS n FROM llm_profiles WHERE is_free AND enabled AND supports_tools AND id<>%s", (pid,)).fetchone()["n"] < 1:
                raise HTTPException(400, "precisa sobrar pelo menos um perfil grátis ligado")
        c.execute("UPDATE llm_profiles SET enabled=%s, daily_usd_cap=%s, price_in_per_m=%s, price_out_per_m=%s, notes=%s WHERE id=%s",
                  (new["enabled"], new["daily_usd_cap"], new["price_in_per_m"], new["price_out_per_m"], new["notes"], pid))
        return {"ok": True}

    @app.post("/api/llm/employees/{slug}", dependencies=[Depends(admin_auth)])
    def assign(slug: str, b: Assign):
        c = conn_fn()
        e = c.execute("SELECT id FROM employees WHERE slug=%s AND active", (slug,)).fetchone()
        if not e:
            raise HTTPException(404, "funcionário não existe")
        if b.profile_id is None:
            c.execute("UPDATE employees SET llm_profile_id=NULL WHERE id=%s", (e["id"],))
            return {"ok": True}
        p = c.execute("SELECT * FROM llm_profiles WHERE id=%s", (b.profile_id,)).fetchone()
        if not p:
            raise HTTPException(404, "perfil não existe")
        if not p["enabled"]:
            raise HTTPException(400, "perfil desligado: ligue antes")
        # a configuração é guardada mesmo com a contingência ligada (ela só vale quando o modo desligar)
        c.execute("UPDATE employees SET llm_profile_id=%s, model=%s WHERE id=%s", (p["id"], p["model"], e["id"]))
        return {"ok": True}

    @app.post("/api/llm/keys/{key_name}", dependencies=[Depends(admin_auth)])
    def set_key(key_name: str, b: KeyBody):
        ok, msg = llm.save_key(key_name, b.key)
        if not ok:
            raise HTTPException(400, msg)
        return {"configurada": True}
