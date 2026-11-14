#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null
command -v npm >/dev/null
if [ ! -x .venv/bin/python ]; then make install; fi
if [ ! -d apps/web/node_modules ]; then (cd apps/web && npm ci); fi
docker compose up -d --wait postgres
# Respect the owner's local port override without reading/copying their secrets.
port=$(docker compose port postgres 5432 | awk -F: '{print $NF}')
docker compose exec -T postgres psql -U jobrunner -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE DATABASE jobrunner_demo'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'jobrunner_demo')\gexec
SQL
# Reviewers can keep every generated file outside the checkout.
demo_root=${DEMO_ROOT:-"$PWD/storage/demo"}
mkdir -p "$demo_root"
demo_root=$(cd "$demo_root" && pwd)
cat > "$demo_root/demo.env" <<EOF
DATABASE_URL=postgresql://jobrunner:jobrunner@127.0.0.1:${port}/jobrunner_demo
AUTO_SUBMIT=false
LLM_PROVIDER=stub
LLM_TASK_TAILOR=stub
LLM_TASK_COVER_LETTER=stub
LLM_TASK_OPEN_ENDED=stub
EMBEDDING_BACKEND=lexical
VECTOR_BACKEND=pgvector
STORAGE_ROOT=$demo_root
VAULT_ROOT=$demo_root/.secrets
KAGGLE_CONNECTION_FILE=$demo_root/kaggle-connection.json
BACKUP_ROOT=$demo_root/backups
ALLOW_NON_LOCAL=false
EOF
export JOBRUNNER_ENV_FILE="$demo_root/demo.env"
# Ignore credentials/URL overrides inherited from an owner's development shell.
unset DATABASE_URL MAINTENANCE_DATABASE_URL AUTO_SUBMIT STORAGE_ROOT VAULT_ROOT LLM_PROVIDER VECTOR_BACKEND
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_demo
printf '\nDemo database: jobrunner_demo (separate from your real profile).\n'
printf 'Explore /matches, /review, and /tracker on the dashboard.\n'
printf 'No worker is started. Ctrl-C stops the API and dashboard.\n'
if [ "${DEMO_PREPARE_ONLY:-0}" = 1 ]; then exit 0; fi
api_port=${DEMO_API_PORT:-8000}
web_port=${DEMO_WEB_PORT:-3001}
.venv/bin/python - "$api_port" "$web_port" <<'PYPORTS'
import socket, sys
for port in sys.argv[1:]:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", int(port)))
        except OSError:
            raise SystemExit(f"Demo port {port} is in use. Set DEMO_API_PORT/DEMO_WEB_PORT to free ports.")
PYPORTS
export JOBRUNNER_WEB_HOST=127.0.0.1 JOBRUNNER_WEB_PORT="$web_port"
export JOBRUNNER_API="http://127.0.0.1:$api_port"
printf 'Demo dashboard: http://127.0.0.1:%s\n' "$web_port"
.venv/bin/uvicorn apps.api.main:app --host 127.0.0.1 --port "$api_port" --timeout-graceful-shutdown 3 &
api_pid=$!
(cd apps/web && npm run dev) &
web_pid=$!
trap 'kill "$api_pid" "$web_pid" 2>/dev/null || true' EXIT INT TERM
wait "$api_pid" "$web_pid"
