"""Login com Google (OIDC), sessão do administrador e cartões de configuração (chaves). Nenhuma resposta devolve segredo."""
import html
import os

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..engine import config, google_auth, llm, secrets_store

STATE_COOKIE = "mer_oauth"
SESSION_COOKIE = "mer_session"
COOKIE_FLAGS = dict(httponly=True, secure=True, samesite="lax")
PROVIDERS = (("nous", "Nous", "https://inference-api.nousresearch.com/v1"), ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
             ("anthropic", "Anthropic", "https://api.anthropic.com/v1"), ("openai", "OpenAI", "https://api.openai.com/v1"))


class GoogleSetup(BaseModel):
    client_id: str = Field(min_length=10, max_length=200)
    client_secret: str = Field(min_length=8, max_length=300)
    owner_email: str = Field(min_length=6, max_length=190)


class TestBody(BaseModel):
    base_url: str | None = Field(None, max_length=200)


def _page(title, msg, status):
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
                        f"<body style='font:16px system-ui;margin:2rem;max-width:30rem'><h1>{html.escape(title)}</h1><p>{html.escape(msg)}</p><p><a href='/'>Voltar ao painel</a></p></body>",
                        status_code=status, headers={"Cache-Control": "no-store"})


def register(app, read_auth, admin_auth, conn_fn):
    # ------------------------------------------------------------ login
    @app.get("/api/auth/google/start")
    def google_start():
        url, state = google_auth.begin(conn_fn())
        if url is None:
            if state == "não configurado":
                raise HTTPException(503, "login com Google não configurado")
            raise HTTPException(429, "muitas tentativas de login; aguarde alguns minutos")
        r = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        r.set_cookie(STATE_COOKIE, state, max_age=google_auth.STATE_TTL, path="/api/auth/google", **COOKIE_FLAGS)
        return r

    @app.get("/api/auth/google/callback")
    def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
        c = conn_fn()
        cookie_state = request.cookies.get(STATE_COOKIE)
        try:
            if error:
                google_auth.event(c, "login_negado", detail=f"Google devolveu erro: {error[:60]}")
                resp = _page("Login cancelado", "O Google não concluiu o login (" + error[:60] + ").", 400)
                resp.delete_cookie(STATE_COOKIE, path="/api/auth/google")
                return resp
            email = google_auth.finish(c, code, state, cookie_state)
        except google_auth.LoginError as e:
            google_auth.event(c, e.kind, e.email, str(e))
            resp = _page("Não foi possível entrar", str(e), e.status)
            resp.delete_cookie(STATE_COOKIE, path="/api/auth/google")
            return resp
        token = google_auth.create_session(c, email)
        google_auth.event(c, "login_ok", email)
        resp = RedirectResponse(config.PUBLIC_BASE_URL + "/?aba=config", status_code=303, headers={"Cache-Control": "no-store"})
        resp.set_cookie(SESSION_COOKIE, token, max_age=config.SESSION_HOURS * 3600, path="/", **COOKIE_FLAGS)
        resp.delete_cookie(STATE_COOKIE, path="/api/auth/google")
        return resp

    @app.post("/api/auth/logout", dependencies=[Depends(admin_auth)])
    def logout(request: Request):
        c = conn_fn()
        google_auth.event(c, "sessao_encerrada", detail="saída")
        google_auth.end_session(c, request.cookies.get(SESSION_COOKIE))
        r = JSONResponse({"ok": True})
        r.delete_cookie(SESSION_COOKIE, path="/")
        return r

    @app.get("/api/auth/me", dependencies=[Depends(read_auth)])
    def me(request: Request):
        """Estado do login para a aba Configurações. O CSRF só vai para quem tem a sessão."""
        c = conn_fn()
        cfg = google_auth.load()
        sess = google_auth.session(c, request.cookies.get(SESSION_COOKIE))
        return {"google_configurado": cfg is not None, "dono": cfg["owner_email"] if cfg else config.OWNER_EMAIL, "redirect_uri": google_auth.redirect_uri(),
                "logado": sess is not None, "email": sess["email"] if sess else None, "csrf": sess["csrf"] if sess else None,
                "recuperacao_por_token": bool(config.env("MERIDIANO_ADMIN_TOKEN"))}

    @app.post("/api/auth/google/setup", dependencies=[Depends(read_auth)])
    def google_setup(b: GoogleSetup):
        """Cadastro único das credenciais do Google, só com o token de leitura. Depois que o arquivo existe: 409 (só sessão Google altera)."""
        if google_auth.configured() or os.path.exists(google_auth.path()):
            raise HTTPException(409, "o login com Google já está configurado")
        cid, sec, mail = b.client_id.strip(), b.client_secret.strip(), b.owner_email.strip().lower()
        if not google_auth.CLIENT_ID_RE.match(cid):
            raise HTTPException(400, "Client ID com formato inválido")
        if not google_auth.EMAIL_RE.match(mail):
            raise HTTPException(400, "e-mail inválido")
        if mail != config.OWNER_EMAIL:
            raise HTTPException(400, "por segurança, o e-mail do dono só muda no arquivo do servidor (OWNER_EMAIL / google-oauth.json)")
        estado, texto = google_auth.check_credentials(cid, sec)
        if estado == "invalido":
            raise HTTPException(400, texto)
        ok, why = google_auth.save_once(cid, sec, mail)
        if not ok:
            raise HTTPException(409, "o login com Google já está configurado")
        google_auth.event(conn_fn(), "configuracao_google", mail, "credenciais cadastradas pelo painel")
        return {"configurado": True, "mensagem": texto}

    @app.get("/api/auth/events", dependencies=[Depends(read_auth)])
    def auth_events():
        return [{"em": r["at"].isoformat(), "tipo": r["kind"], "email": r["email"], "detalhe": r["detail"]} for r in google_auth.events(conn_fn())]

    # ------------------------------------------------------------ Mercado Pago: remover / testar
    @app.delete("/api/secrets/mp-token", dependencies=[Depends(admin_auth)])
    def remove_mp_token():
        secrets_store.remove_token()
        secrets_store.sync_owner_request(conn_fn())
        google_auth.event(conn_fn(), "chave_removida", detail="token do Mercado Pago")
        return secrets_store.status()

    @app.post("/api/secrets/mp-token/test", dependencies=[Depends(admin_auth)])
    def test_mp_token():
        if not secrets_store.configured():
            raise HTTPException(400, "nenhum token configurado")
        secrets_store.refresh_meta(force=True)
        return secrets_store.status()

    # ------------------------------------------------------------ chaves de LLM
    @app.get("/api/llm/keys", dependencies=[Depends(read_auth)])
    def llm_keys():
        names = [(k, label, url) for k, label, url in PROVIDERS]
        known = {k for k, _, _ in names}
        extra = set()
        try:
            extra = {f[4:] for f in os.listdir(config.SECRETS_DIR) if f.startswith("llm-") and llm.KEY_RE.match(f[4:])}
        except OSError:
            pass
        extra |= {r["key_name"] for r in conn_fn().execute("SELECT DISTINCT key_name FROM llm_profiles").fetchall()}
        out = []
        for k, label, url in names + [(k, k, None) for k in sorted(extra - known)]:
            f = llm.key_path(k)
            file_ok = bool(f and os.path.isfile(f) and os.path.getsize(f) > 0)
            env_ok = k == "nous" and bool(config.env("NOUS_API_KEY"))
            out.append({"key_name": k, "nome": label, "base_url": url, "configurada": file_ok or env_ok, "fonte": "arquivo" if file_ok else ("ambiente do servidor" if env_ok else None)})
        return out

    @app.delete("/api/llm/keys/{key_name}", dependencies=[Depends(admin_auth)])
    def delete_llm_key(key_name: str):
        p = llm.key_path(key_name)
        if not p:
            raise HTTPException(400, "nome de chave inválido")
        try:
            os.unlink(p)
        except FileNotFoundError:
            raise HTTPException(404, "não há chave em arquivo com esse nome")
        google_auth.event(conn_fn(), "chave_removida", detail=f"llm-{key_name}")
        return {"removida": True}

    @app.post("/api/llm/keys/{key_name}/test", dependencies=[Depends(admin_auth)])
    def test_llm_key(key_name: str, b: TestBody = None):
        base = (b.base_url if b and b.base_url else None) or next((u for k, _, u in PROVIDERS if k == key_name), None)
        if not base:
            raise HTTPException(400, "informe o endereço (base_url) deste provedor")
        ok, msg = llm.test_key(key_name, base)
        return {"ok": ok, "mensagem": msg}
