"""Credenciais de pagamento. Dois caminhos para entregar o access token do Mercado Pago, ambos terminando no mesmo arquivo:
 (a) campo do painel (POST /api/secrets/mp-token, com o token de admin), ou
 (b) o administrador cria ~/meridiano/secrets/mp-token (chmod 600, pasta 700). O sistema passa a ler dele sem reiniciar e reverifica a cada 60 s.
Regras: o token nunca é devolvido por API, nunca vai para log nem para tabela do banco, e nenhum agente ou ferramenta lê esta pasta."""
import datetime
import hashlib
import json
import os
import re
import threading
import time

import httpx

from . import config

TOKEN_RE = re.compile(r"^(APP_USR|TEST)-[A-Za-z0-9_\-]{20,200}$")
TOKEN_REQUEST = "Preciso do access token do Mercado Pago (Credenciais de produção) para receber pagamentos"
TOKEN_FILE = "mp-token"
LEGACY_FILE = "mp_access_token"     # nome usado na primeira versão do painel
META_FILE = "mp-meta.json"
WEBHOOK_FILE = "mp-webhook-secret"   # opcional: "Assinatura secreta" do webhook (Suas integrações > Webhooks)
STORE_FILE = "store.json"           # {"seller_name": "...", "contact_email": "..."}: identificação do vendedor, pelo caminho (b)
REVERIFY_SECONDS = 60

_state = {"fp": None, "last": 0.0}
_thread = None
_lock = threading.Lock()


def _path(name):
    return os.path.join(config.SECRETS_DIR, name)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_private(name, text):
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    tmp = _path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(name))


def _token_file():
    for n in (TOKEN_FILE, LEGACY_FILE):
        p = _path(n)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return None


def _parse(text):
    """Aceita o token puro ou KEY=token, com ou sem aspas e quebra de linha (como o administrador costuma criar o arquivo)."""
    line = next((x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith("#")), "")
    if "=" in line:
        line = line.split("=", 1)[1].strip()
    return line.strip("'\"").strip()


def webhook_secret():
    """Assinatura secreta do webhook do Mercado Pago, se o administrador configurou (arquivo mp-webhook-secret). None se não há."""
    try:
        with open(_path(WEBHOOK_FILE), encoding="utf-8") as f:
            v = _parse(f.read())
        return v if len(v) >= 16 else None
    except OSError:
        return None


def read_token():
    """Só para o código de pagamento do servidor (próximas etapas). Nunca expor por API ou ferramenta."""
    p = _token_file()
    if not p:
        raise FileNotFoundError("token do Mercado Pago ainda não configurado")
    with open(p, encoding="utf-8") as f:
        t = _parse(f.read())
    if not TOKEN_RE.match(t):
        raise ValueError("formato inválido")
    return t


def problem():
    """None se está tudo certo ou se o arquivo não existe; texto se existe mas não presta."""
    p = _token_file()
    if not p:
        return None
    try:
        read_token()
        return None
    except (ValueError, OSError):
        return f"o arquivo {os.path.basename(p)} existe, mas o conteúdo não parece um Access Token (esperado APP_USR-… ou TEST-…)"


def configured():
    try:
        read_token()
        return True
    except (OSError, ValueError):
        return False


def ensure_private():
    """Corrige permissões frouxas (pasta 700, arquivos 600) quando o processo é dono. Devolve True se ficou tudo privado."""
    ok = True
    try:
        if os.path.isdir(config.SECRETS_DIR):
            if os.stat(config.SECRETS_DIR).st_mode & 0o077:
                os.chmod(config.SECRETS_DIR, 0o700)
            for n in (TOKEN_FILE, LEGACY_FILE, META_FILE, STORE_FILE, WEBHOOK_FILE):
                p = _path(n)
                if os.path.isfile(p) and os.stat(p).st_mode & 0o077:
                    os.chmod(p, 0o600)
            ok = not (os.stat(config.SECRETS_DIR).st_mode & 0o077) and all(
                not (os.stat(_path(n)).st_mode & 0o077) for n in (TOKEN_FILE, LEGACY_FILE) if os.path.isfile(_path(n)))
    except OSError:
        ok = False
    return ok


def verify(token):
    """Pergunta ao Mercado Pago se o token vale. ('ok'|'invalido'|'nao_verificado', texto sem dados do token)."""
    try:
        r = httpx.get("https://api.mercadopago.com/users/me", headers={"Authorization": "Bearer " + token}, timeout=15)
    except Exception:
        return "nao_verificado", "não foi possível falar com o Mercado Pago agora"
    if r.status_code in (401, 403):
        return "invalido", "o Mercado Pago recusou este token"
    if r.status_code == 200:
        return "ok", "token aceito pelo Mercado Pago"
    return "nao_verificado", f"resposta inesperada do Mercado Pago ({r.status_code})"


def _read_meta():
    try:
        with open(_path(META_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _fp(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]    # só na memória, para saber se o arquivo mudou


def refresh_meta(force=False):
    """Reverifica o token no Mercado Pago a cada 60 s (ou na hora, se o arquivo mudou). Não toca no banco."""
    try:
        token = read_token()
    except (OSError, ValueError):
        _state.update(fp=None, last=0.0)
        return None
    fp, now = _fp(token), time.time()
    if not force and fp == _state["fp"] and now - _state["last"] < REVERIFY_SECONDS:
        return _read_meta()
    estado, texto = verify(token)
    old = _read_meta()
    meta = {"modo": "teste" if token.startswith("TEST-") else "producao", "verificado": estado == "ok", "estado": estado, "detalhe": texto,
            "verificado_em": _now_iso(), "atualizado_em": old.get("atualizado_em") if old.get("fp") == fp else _now_iso(), "fp": fp}
    try:
        _write_private(META_FILE, json.dumps(meta))
    except OSError:
        pass
    _state.update(fp=fp, last=now)
    return meta


def refresh_meta_async():
    """Usado pela API: dispara a reverificação em segundo plano, sem travar a resposta."""
    global _thread
    if not _stale() or not _lock.acquire(blocking=False):
        return None

    def run():
        try:
            refresh_meta()
        finally:
            _lock.release()
    _thread = threading.Thread(target=run, daemon=True)
    _thread.start()
    return _thread


def _stale():
    try:
        fp = _fp(read_token())
    except (OSError, ValueError):
        return False
    return fp != _state["fp"] or time.time() - _state["last"] >= REVERIFY_SECONDS


def save_token(raw):
    """Caminho (a): valida o formato, confere com o Mercado Pago e grava. Devolve (ok, mensagem, estado). Nunca devolve o token."""
    token = _parse(raw or "")
    if not TOKEN_RE.match(token):
        return False, "formato inválido: o Access Token começa com APP_USR- (produção) ou TEST- (teste)", None
    estado, texto = verify(token)
    if estado == "invalido":
        return False, texto, None
    _write_private(TOKEN_FILE, token)
    iso = _now_iso()
    meta = {"modo": "teste" if token.startswith("TEST-") else "producao", "verificado": estado == "ok", "estado": estado, "detalhe": texto,
            "verificado_em": iso, "atualizado_em": iso, "fp": _fp(token)}
    _write_private(META_FILE, json.dumps(meta))
    _state.update(fp=meta["fp"], last=time.time())
    return True, texto, meta


def status():
    """Sem rede: o que está configurado agora. Nunca inclui o token nem pedaço dele."""
    prob = problem()
    if not configured():
        return {"configurado": False, "modo": None, "verificado": False, "verificado_em": None, "detalhe": None, "problema": prob,
                "permissoes_ok": ensure_private(), "atualizado_em": None, "webhook_assinado": webhook_secret() is not None}
    m = _read_meta()
    token = read_token()
    same = m.get("fp") == _fp(token)   # a verificação só vale para o token que foi verificado: arquivo trocado = não verificado até reverificar
    return {"configurado": True, "modo": m.get("modo") if same and m.get("modo") else ("teste" if token.startswith("TEST-") else "producao"), "verificado": bool(m.get("verificado")) and same,
            "verificado_em": m.get("verificado_em") if same else None, "detalhe": m.get("detalhe") if same else "aguardando a verificação do token atual", "problema": None, "permissoes_ok": ensure_private(), "atualizado_em": m.get("atualizado_em"), "webhook_assinado": webhook_secret() is not None}


def import_store_file(conn):
    """Caminho (b) para a identificação do vendedor: store.json na pasta de segredos. Devolve True se atualizou o banco."""
    from . import store
    try:
        with open(_path(STORE_FILE), encoding="utf-8") as f:
            d = json.load(f)
        name, email = str(d["seller_name"]).strip(), str(d["contact_email"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not (3 <= len(name) <= 120) or not store.CONTACT_RE.match(email):
        return False
    cur = store.get_settings(conn)
    if cur.get("seller_name") == name and cur.get("contact_email") == email:
        return False
    for k, v in (("seller_name", name), ("contact_email", email)):
        conn.execute("INSERT INTO store_settings(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (k, v))
    return True


def sync_owner_request(conn):
    """O ÚNICO pedido permanente ao dono: token + identificação do vendedor (mesmo cartão no painel). Fecha quando os dois existem,
    venham do painel ou dos arquivos do servidor."""
    from . import store
    import_store_file(conn)
    if configured() and store.store_ready(conn):
        conn.execute("UPDATE owner_requests SET done=TRUE, resolution='token e identificação recebidos' WHERE body=%s AND NOT done", (TOKEN_REQUEST,))
    elif not conn.execute("SELECT 1 FROM owner_requests WHERE body=%s AND NOT done", (TOKEN_REQUEST,)).fetchone():
        conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (NULL,%s)", (TOKEN_REQUEST,))


def refresh(conn):
    """Chamado pelo motor a cada 60 s: corrige permissões, reverifica no Mercado Pago e atualiza o pedido ao dono."""
    ensure_private()
    refresh_meta()
    sync_owner_request(conn)
