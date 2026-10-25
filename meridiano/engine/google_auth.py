"""Login do administrador com Google (OpenID Connect, code flow com PKCE) e sessões.
Sem dependência nova: o id_token vem direto do endpoint de token do Google (canal TLS servidor a servidor) e só as claims são validadas.
Escopos: openid e email. As credenciais (Client ID/Secret) ficam em ~/meridiano/secrets/google-oauth.json (600); nunca voltam por API."""
import base64
import hashlib
import json
import os
import re
import secrets
import time

import httpx

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
FILE = "google-oauth.json"
STATE_TTL = 600
MAX_PENDING = 200
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{10,200}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")


def path():
    return os.path.join(config.SECRETS_DIR, FILE)


def redirect_uri():
    return config.PUBLIC_BASE_URL + "/api/auth/google/callback"


def load():
    """{'client_id','client_secret','owner_email'} ou None. Só para o servidor."""
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        cid, sec = str(d["client_id"]).strip(), str(d["client_secret"]).strip()
        mail = str(d.get("owner_email") or config.OWNER_EMAIL).strip().lower()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not CLIENT_ID_RE.match(cid) or not 8 <= len(sec) <= 300 or not EMAIL_RE.match(mail):
        return None
    return {"client_id": cid, "client_secret": sec, "owner_email": mail}


def configured():
    return load() is not None


def save_once(client_id, client_secret, owner_email):
    """Grava o arquivo (600) só se ele ainda não existe. Devolve (ok, motivo)."""
    if os.path.exists(path()):
        return False, "já configurado"
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    tmp = path() + f".{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret, "owner_email": owner_email}, f)
    try:
        os.link(tmp, path())      # atômico: falha se alguém criou o arquivo no meio tempo
    except FileExistsError:
        return False, "já configurado"
    finally:
        os.unlink(tmp)
    os.chmod(path(), 0o600)
    return True, ""


def check_credentials(client_id, client_secret):
    """Chama o endpoint de token com um code inválido só para distinguir invalid_client de invalid_grant.
    ('ok'|'invalido'|'nao_verificado', texto). Não grava nada."""
    try:
        r = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": "codigo-invalido", "client_id": client_id, "client_secret": client_secret,
                                        "redirect_uri": redirect_uri(), "code_verifier": secrets.token_urlsafe(48)}, timeout=15)
        err = (r.json() or {}).get("error")
    except Exception:
        return "nao_verificado", "não consegui falar com o Google agora; o primeiro login mostrará se as credenciais valem"
    if err == "invalid_client":
        return "invalido", "o Google não reconhece este Client ID e Client Secret"
    return "ok", "credenciais aceitas pelo Google"


# ---------------------------------------------------------------- eventos
def event(conn, kind, email=None, detail=""):
    """Registro de tentativas: nunca recebe senha, token ou chave."""
    conn.execute("INSERT INTO auth_events(kind,email,detail) VALUES (%s,%s,%s)", (kind, (email or "")[:190] or None, str(detail)[:300]))
    conn.execute("DELETE FROM auth_events WHERE id < (SELECT COALESCE(MAX(id),0) - 500 FROM auth_events)")


def events(conn, limit=20):
    return conn.execute("SELECT at, kind, email, detail FROM auth_events ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


# ---------------------------------------------------------------- fluxo
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def begin(conn):
    """Cria state, nonce e code_verifier (10 min) e devolve a URL de autorização do Google, ou None se faltam credenciais / há pendências demais."""
    cfg = load()
    if not cfg:
        return None, "não configurado"
    conn.execute("DELETE FROM oauth_states WHERE created_at < now() - make_interval(secs => %s)", (STATE_TTL,))
    if conn.execute("SELECT COUNT(*) AS n FROM oauth_states").fetchone()["n"] >= MAX_PENDING:
        return None, "muitas tentativas pendentes"
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    conn.execute("INSERT INTO oauth_states(state,nonce,verifier) VALUES (%s,%s,%s)", (state, nonce, verifier))
    q = httpx.QueryParams({"client_id": cfg["client_id"], "redirect_uri": redirect_uri(), "response_type": "code", "scope": "openid email",
                           "state": state, "nonce": nonce, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
                           "login_hint": cfg["owner_email"], "prompt": "select_account"})
    return f"{AUTH_URL}?{q}", state


class LoginError(Exception):
    def __init__(self, status, msg, kind="login_negado", email=None):
        super().__init__(msg)
        self.status, self.kind, self.email = status, kind, email


def _claims(id_token):
    try:
        p = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        raise LoginError(502, "resposta do Google ilegível", "token_invalido")


def finish(conn, code, state, cookie_state):
    """Valida tudo e devolve o e-mail do dono. Levanta LoginError (com status HTTP) em qualquer divergência."""
    cfg = load()
    if not cfg:
        raise LoginError(503, "login com Google não configurado", "token_invalido")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise LoginError(400, "pedido de login inválido ou expirado: comece de novo", "state_invalido")
    row = conn.execute("DELETE FROM oauth_states WHERE state=%s AND created_at > now() - make_interval(secs => %s) RETURNING nonce, verifier", (state, STATE_TTL)).fetchone()
    if not row:
        raise LoginError(400, "pedido de login inválido ou expirado: comece de novo", "state_invalido")
    if not code:
        raise LoginError(400, "o Google não devolveu o código de acesso", "state_invalido")
    try:
        r = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                        "redirect_uri": redirect_uri(), "code_verifier": row["verifier"]}, timeout=15)
        data = r.json()
    except Exception:
        raise LoginError(502, "não consegui falar com o Google; tente de novo", "token_invalido")
    if r.status_code != 200 or not data.get("id_token"):
        raise LoginError(502, "o Google recusou o login (" + str((data or {}).get("error", r.status_code))[:60] + ")", "token_invalido")
    c = _claims(data["id_token"])
    email = str(c.get("email") or "").lower()
    aud = c.get("aud")
    if c.get("iss") not in ISSUERS:
        raise LoginError(400, "emissor inválido", "token_invalido", email)
    if aud != cfg["client_id"] and not (isinstance(aud, list) and cfg["client_id"] in aud):
        raise LoginError(400, "destinatário inválido", "token_invalido", email)
    try:
        if float(c.get("exp", 0)) <= time.time():
            raise LoginError(400, "login expirado", "token_invalido", email)
    except (TypeError, ValueError):
        raise LoginError(400, "login sem validade", "token_invalido", email)
    if not secrets.compare_digest(str(c.get("nonce") or ""), row["nonce"]):
        raise LoginError(400, "nonce inválido", "state_invalido", email)
    if c.get("email_verified") not in (True, "true"):
        raise LoginError(403, "e-mail não verificado pelo Google", "login_negado", email)
    if not email or not secrets.compare_digest(email, cfg["owner_email"]):
        raise LoginError(403, "esta conta não é a do dono", "login_negado", email)
    return email


# ---------------------------------------------------------------- sessão
def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, email):
    conn.execute("DELETE FROM admin_sessions WHERE expires_at < now()")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO admin_sessions(token_hash,csrf,email,expires_at) VALUES (%s,%s,%s, now() + make_interval(hours => %s))", (_h(token), csrf, email, config.SESSION_HOURS))
    return token


def session(conn, token):
    """{'email','csrf'} da sessão válida, ou None."""
    if not token or len(token) > 200:
        return None
    return conn.execute("SELECT email, csrf FROM admin_sessions WHERE token_hash=%s AND expires_at > now()", (_h(token),)).fetchone()


def end_session(conn, token):
    if token:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash=%s", (_h(token),))
