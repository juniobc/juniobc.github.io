#!/usr/bin/env bash
# Instala a Meridiano em /home/empresa/meridiano (sem sudo). Rode como o usuário `empresa`.
set -euo pipefail
ROOT="${MERIDIANO_HOME:-$HOME/meridiano}"
REPO="${REPO_DIR:?defina REPO_DIR com o clone do repositório juniobc.github.io}"
mkdir -p "$ROOT"
python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install -q -r "$REPO/meridiano/requirements.txt"
# .env (permissão 600). NOUS_API_KEY e tokens ficam SÓ aqui, nunca no Git.
if [ ! -f "$ROOT/meridiano.env" ]; then
  umask 077
  {
    echo "# cole aqui a DATABASE_URL de ~/.pg_env"
    grep -E '^(export )?DATABASE_URL=' "$HOME/.pg_env" | sed 's/^export //' || echo "DATABASE_URL="
    echo "NOUS_API_KEY="
    echo "MERIDIANO_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    echo "MERIDIANO_ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
    echo "DAILY_CALL_LIMIT=300"
    echo "DAILY_TOKEN_LIMIT=400000"
  } > "$ROOT/meridiano.env"
  chmod 600 "$ROOT/meridiano.env"
  echo "Criado $ROOT/meridiano.env. Preencha NOUS_API_KEY e rode de novo/ inicie."
fi
cat > "$ROOT/run.sh" <<RUN
#!/usr/bin/env bash
set -a; . "$ROOT/meridiano.env"; set +a
cd "$REPO"
case "\$1" in
  api)    exec "$ROOT/venv/bin/uvicorn" meridiano.api.main:app --host 127.0.0.1 --port 8080 ;;
  engine) exec "$ROOT/venv/bin/python" -m meridiano.engine.main ;;
esac
RUN
chmod +x "$ROOT/run.sh"
echo "Pronto. Iniciar: nohup $ROOT/run.sh api >$ROOT/api.log 2>&1 &  e  nohup $ROOT/run.sh engine >$ROOT/engine.log 2>&1 &"
