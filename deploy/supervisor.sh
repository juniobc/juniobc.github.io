#!/usr/bin/env bash
# Mantém API e motor vivos como processos do usuário `empresa`. Uma só instância (flock).
ROOT="${MERIDIANO_HOME:-$HOME/meridiano}"
REPO="$ROOT/repo"
PORT="${MERIDIANO_PORT:-8100}"
mkdir -p "$ROOT/run"
exec 8>"$ROOT/run/supervisor.lock"
flock -n 8 || exit 0

keep() {   # keep <nome> <comando...> : reinicia sempre que cair (ou for morto pelo deploy)
  local name="$1"; shift
  while true; do
    ( set -a; . "$ROOT/meridiano.env"; set +a
      cd "$REPO" || exit 1
      if [ "$name" = engine ] && [ -z "${NOUS_API_KEY:-}" ]; then echo "$(date '+%F %T') engine parado: NOUS_API_KEY vazia"; rm -f "$ROOT/run/engine.pid"; sleep 60; exit 0; fi
      echo "$BASHPID" > "$ROOT/run/$name.pid"
      exec "$@" 8>&- ) >> "$ROOT/$name.log" 2>&1
    sleep 3
  done
}
keep api    "$ROOT/venv/bin/uvicorn" meridiano.api.main:app --host 127.0.0.1 --port "$PORT" &
keep engine "$ROOT/venv/bin/python" -m meridiano.engine.main &
wait
