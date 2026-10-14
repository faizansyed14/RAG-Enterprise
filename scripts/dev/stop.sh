#!/usr/bin/env bash
# Stop the dev stack.   bash scripts/dev/stop.sh [--wipe] [--models]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
source scripts/lib.sh
parse_flags "$@"
WIPE=0; for a in "${REST_ARGS[@]:-}"; do [[ "$a" == "--wipe" ]] && WIPE=1; done

# --env-file: the compose file's credentials are ${VAR:?...} placeholders resolved from .env.dev --
# compose parses (and requires) them even for `down`, not just `up`.
COMPOSE=(docker compose --env-file .env.dev -f docker-compose.dev.yml ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"})

if [[ "$WIPE" == 1 ]]; then
  "${COMPOSE[@]}" down -v
  echo "Dev stack stopped and volumes wiped (including indexed documents, vectors and cached model-server downloads)."
else
  "${COMPOSE[@]}" down
  echo "Dev stack stopped. Volumes kept -- run with --wipe to also drop them."
fi
