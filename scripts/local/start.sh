#!/usr/bin/env bash
# Start the local stack.   bash scripts/local/start.sh [--models] [--slim]   (flags: scripts/lib.sh)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source scripts/lib.sh
parse_flags "$@"

if [[ ! -f .env.dev ]]; then
  echo ".env.dev not found. Copy .env.dev.example and fill it in first." >&2
  exit 1
fi

check_local_models .env.dev
check_reranker .env.dev

# --env-file is required here, not just env_file: in the compose YAML -- that only
# injects vars into a container's own process environment. The ${VAR} interpolation
# used for credentials in docker-compose.local.yml is resolved by the compose CLI itself.
COMPOSE=(docker compose --env-file .env.dev -f docker-compose.local.yml ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"})

"${COMPOSE[@]}" up -d --build

wait_for_postgres "${COMPOSE[@]}"

echo "Running migrations..."
"${COMPOSE[@]}" exec -T backend alembic upgrade head

echo "Local stack up. Frontend: http://localhost:3000  Backend: http://localhost:8000"
echo "Enterprise worker: ${COMPOSE[*]} logs -f worker      CLI: bash scripts/cli.sh local status"
