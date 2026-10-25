"""Guarda o access token do Mercado Pago. Regras: arquivo com permissão 600 numa pasta 700, nunca devolvido por API,
nunca em log, nunca em tabela do banco (agentes e ferramentas não leem nada daqui)."""
import datetime
import json
import os
import re

import httpx

from . import config

TOKEN_RE = re.compile(r"^(APP_USR|TEST)-[A-Za-z0-9_\-]{20,200}$")
TOKEN_REQUEST = "Preciso do access token do Mercado Pago (Credenciais de produção) para receber pagamentos"
TOKEN_FILE = "mp_access_token"
META_FILE = "mp_meta.json"


def _path(name):
    return os.path.join(config.SECRETS_DIR, name)


def _write_private(name, text):
    os.makedirs(config.SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    tmp = _path(name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path(name))


def configured():
    return os.path.isfile(_path(TOKEN_FILE)) and os.path.getsize(_path(TOKEN_FILE)) > 0


def read_token():
    """Só para o código de pagamento do servidor (próximas etapas). Nunca expor por API ou ferramenta."""
    with open(_path(TOKEN_FILE), encoding="utf-8") as f:
        return f.read().strip()


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


def save_token(raw):
    """Valida o formato, confere com o Mercado Pago e grava. Devolve (ok, mensagem, estado). Nunca devolve o token."""
    token = (raw or "").strip()
    if not TOKEN_RE.match(token):
        return False, "formato inválido: o Access Token começa com APP_USR- (produção) ou TEST- (teste)", None
    estado, texto = verify(token)
    if estado == "invalido":
        return False, texto, None
    _write_private(TOKEN_FILE, token)
    meta = {"modo": "teste" if token.startswith("TEST-") else "producao", "verificado": estado == "ok",
            "atualizado_em": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    _write_private(META_FILE, json.dumps(meta))
    return True, texto, meta


def status():
    if not configured():
        return {"configurado": False, "modo": None, "verificado": False, "atualizado_em": None}
    try:
        with open(_path(META_FILE), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    return {"configurado": True, "modo": m.get("modo"), "verificado": bool(m.get("verificado")), "atualizado_em": m.get("atualizado_em")}


def sync_owner_request(conn):
    """O ÚNICO pedido permanente ao dono: token + identificação do vendedor (mesmo cartão no painel). Fecha quando os dois existem."""
    from . import store
    if configured() and store.store_ready(conn):
        conn.execute("UPDATE owner_requests SET done=TRUE, resolution='token e identificação recebidos' WHERE body=%s AND NOT done", (TOKEN_REQUEST,))
    elif not conn.execute("SELECT 1 FROM owner_requests WHERE body=%s AND NOT done", (TOKEN_REQUEST,)).fetchone():
        conn.execute("INSERT INTO owner_requests(from_id,body) VALUES (NULL,%s)", (TOKEN_REQUEST,))
