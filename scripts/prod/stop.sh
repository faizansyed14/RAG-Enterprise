#!/usr/bin/env bash
# Stop the production stack.   bash scripts/prod/stop.sh [--wipe] [--models]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source scripts/lib.sh
parse_flags "$@"
WIPE=0; for a in "${REST_ARGS[@]:-}"; do [[ "$a" == "--wipe" ]] && WIPE=1; done

# --env-file: docker-compose.prod.yml's postgres/qdrant credentials and cert
# path are ${VAR:?...} placeholders resolved from .env.prod -- compose parses
# (and requires) them even for `down`, not just `up`.
COMPOSE=(docker compose --env-file .env.prod -f docker-compose.prod.yml ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"})

if [[ "$WIPE" == 1 ]]; then
  "${COMPOSE[@]}" down -v
  echo "Prod stack stopped and volumes wiped -- Postgres and Qdrant data is gone. Object" \
       "storage is real S3 in prod, so that part is untouched. Make sure you meant this."
else
  "${COMPOSE[@]}" down
  echo "Prod stack stopped. Volumes kept -- run with --wipe to also drop them."
fi
