#!/usr/bin/env bash
# Start the production stack.   bash scripts/prod/start.sh [--models] [--slim]   (flags: scripts/lib.sh)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source scripts/lib.sh
parse_flags "$@"

if [[ ! -f .env.prod ]]; then
  echo ".env.prod not found. Copy .env.prod.example and fill it in first." >&2
  exit 1
fi

# nginx bind-mounts this (docker-compose.prod.yml) -- a missing cert here
# surfaces as a confusing Docker mount error otherwise.
tls_cert_dir="$(grep -m1 '^TLS_CERT_DIR=' .env.prod | cut -d= -f2-)"
if [[ -z "$tls_cert_dir" || ! -f "$tls_cert_dir/fullchain.pem" || ! -f "$tls_cert_dir/privkey.pem" ]]; then
  echo "TLS_CERT_DIR (${tls_cert_dir:-unset}) is missing fullchain.pem/privkey.pem." >&2
  exit 1
fi

check_local_models .env.prod
check_reranker .env.prod

# No separate migration step here, unlike local/dev: the prod backend image's
# own CMD runs `alembic upgrade head` before starting uvicorn (see
# backend/Dockerfile), and nginx's `depends_on: condition: service_healthy`
# already waits for the backend's healthcheck -- which only passes once that's
# done -- before it starts routing traffic. The worker waits for the schema itself.
docker compose --env-file .env.prod -f docker-compose.prod.yml ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d --build

echo "Prod stack up."
echo "Scale ingestion out:  docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --scale worker=4"
