#!/usr/bin/env bash
# Start the dev stack.   bash scripts/dev/start.sh [--models] [--slim]   (flags: scripts/lib.sh)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source scripts/lib.sh
parse_flags "$@"

if [[ ! -f .env.dev ]]; then
  echo ".env.dev not found. Copy .env.dev.example and fill it in first." >&2
  exit 1
fi

# nginx bind-mounts this dir (docker-compose.dev.yml) -- a missing cert here
# surfaces as a confusing Docker mount error otherwise. See .env.dev.example's
# EC2 TLS section for how to generate a local cert or get a real one.
tls_cert_dir="$(grep -m1 '^TLS_CERT_DIR=' .env.dev | cut -d= -f2-)"
if [[ -z "$tls_cert_dir" || ! -f "$tls_cert_dir/fullchain.pem" || ! -f "$tls_cert_dir/privkey.pem" ]]; then
  echo "TLS_CERT_DIR (${tls_cert_dir:-unset}) is missing fullchain.pem/privkey.pem." >&2
  echo "See .env.dev.example's EC2 TLS section to generate a local cert or get a real one." >&2
  exit 1
fi

check_local_models .env.dev
check_reranker .env.dev

# --env-file is required here, not just env_file: in the compose YAML -- that only
# injects vars into a container's own process environment. The ${VAR} interpolation
# used for credentials in docker-compose.dev.yml is resolved by the compose CLI itself.
COMPOSE=(docker compose --env-file .env.dev -f docker-compose.dev.yml ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"})

"${COMPOSE[@]}" up -d --build

wait_for_postgres "${COMPOSE[@]}"

echo "Running migrations..."
"${COMPOSE[@]}" exec -T backend alembic upgrade head

app_url="$(grep -m1 '^NEXT_PUBLIC_API_BASE_URL=' .env.dev | cut -d= -f2-)"
echo "Dev stack up (via nginx): ${app_url:-https://localhost}"
echo "Enterprise worker: ${COMPOSE[*]} logs -f worker      CLI: bash scripts/cli.sh dev status"
