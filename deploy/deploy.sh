#!/usr/bin/env bash
# Deploy automático da Meridiano. Roda como usuário `empresa`, sem sudo. Idempotente.
# Só a branch `main` vira deploy. Chamado pelo cron a cada minuto e no @reboot.
set -uo pipefail

ROOT="${MERIDIANO_HOME:-$HOME/meridiano}"
REPO="$ROOT/repo"
REPO_URL="${REPO_URL:-https://github.com/juniobc/juniobc.github.io.git}"
BRANCH="main"                       # fixo de propósito
PORT="${MERIDIANO_PORT:-8100}"
LOG="$ROOT/deploy.log"

mkdir -p "$ROOT/run"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

exec 9>"$ROOT/run/deploy.lock"
flock -n 9 || exit 0                # já tem um deploy rodando

# arquivo de ambiente (600). Segredos só aqui, nunca no Git.
if [ ! -f "$ROOT/meridiano.env" ]; then
  (
    umask 077
    {
      grep -E '^(export )?DATABASE_URL=' "$HOME/.pg_env" 2>/dev/null | sed 's/^export //' || echo "DATABASE_URL="
      echo "NOUS_API_KEY="
      echo "MERIDIANO_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
      echo "MERIDIANO_ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    } > "$ROOT/meridiano.env"
  )
  log "criado meridiano.env (preencher NOUS_API_KEY)"
fi

# migra só os limites antigos que o próprio deploy gravou (300/400000 e 600/1500000); valores escolhidos pelo Junio ficam como estão
sed -i 's/^DAILY_CALL_LIMIT=\(300\|600\|2000\)$/DAILY_CALL_LIMIT=8000/; s/^DAILY_TOKEN_LIMIT=\(400000\|1500000\)$/DAILY_TOKEN_LIMIT=8000000/' "$ROOT/meridiano.env" 2>/dev/null

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS -m 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

install_deps() {
  [ -x "$ROOT/venv/bin/pip" ] || python3 -m venv "$ROOT/venv" || return 1
  "$ROOT/venv/bin/pip" install -q -r "$REPO/meridiano/requirements.txt" >> "$LOG" 2>&1
}

restart_all() {
  local pids=""
  for n in api engine; do
    if [ -f "$ROOT/run/$n.pid" ]; then
      local p; p="$(cat "$ROOT/run/$n.pid")"
      kill "$p" 2>/dev/null && pids="$pids $p"
    fi
  done
  # espera o processo antigo morrer, senão o healthcheck responde por ele
  for _ in $(seq 1 20); do
    local alive=0
    for p in $pids; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ "$alive" = 0 ] && break
    sleep 1
  done
}

ensure_supervisor() {
  if ! flock -n "$ROOT/run/supervisor.lock" true 2>/dev/null; then return 0; fi  # lock livre = supervisor morto
  log "iniciando supervisor"
  MERIDIANO_HOME="$ROOT" MERIDIANO_PORT="$PORT" setsid nohup bash "$REPO/deploy/supervisor.sh" >> "$ROOT/supervisor.log" 2>&1 < /dev/null 9>&- &
}

# --- clone inicial ---
if [ ! -d "$REPO/.git" ]; then
  git clone -q --branch "$BRANCH" "$REPO_URL" "$REPO" >> "$LOG" 2>&1 || { log "ERRO: clone falhou"; exit 1; }
fi

cd "$REPO" || exit 1
git fetch -q origin "$BRANCH" >> "$LOG" 2>&1 || { log "git fetch falhou (rede?)"; ensure_supervisor; exit 0; }
CUR="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
BAD="$(cat "$ROOT/run/bad_commit" 2>/dev/null || true)"

if [ "$CUR" = "$NEW" ] && [ -x "$ROOT/venv/bin/uvicorn" ]; then
  ensure_supervisor; exit 0
fi
if [ "$NEW" = "$BAD" ]; then
  ensure_supervisor; exit 0         # esse commit já falhou; espera um novo na main
fi

log "deploy $CUR -> $NEW"
git checkout -q -f -B "$BRANCH" "origin/$BRANCH" >> "$LOG" 2>&1
if install_deps && ( set -a; . "$ROOT/meridiano.env"; set +a; "$ROOT/venv/bin/python" -m meridiano.engine.migrate >> "$LOG" 2>&1 ); then
  ensure_supervisor
  restart_all
  if healthy; then
    rm -f "$ROOT/run/bad_commit"
    log "OK: $NEW no ar"
    exit 0
  fi
  log "FALHOU healthcheck em $NEW"
else
  log "FALHOU instalação/migração em $NEW"
fi

# rollback
echo "$NEW" > "$ROOT/run/bad_commit"
log "rollback para $CUR"
git checkout -q -f -B "$BRANCH" "$CUR" >> "$LOG" 2>&1
install_deps
restart_all
if healthy; then log "rollback OK"; else log "ERRO: rollback também não passou no healthcheck"; fi
exit 1
