#!/usr/bin/env bash
# Run the enterprise CLI inside the backend container.
#
#   bash scripts/cli.sh <local|dev|prod> <command...>
#
#   bash scripts/cli.sh local status
#   bash scripts/cli.sh local connect add --kind local --name "Shared drive" --config /mnt/sources/conn.json
#   bash scripts/cli.sh local principals add --user alice --principal group:finance
#   bash scripts/cli.sh local scan --connection "Shared drive"
#   bash scripts/cli.sh local index-existing        # queue chunk-indexing for documents uploaded before the upgrade
#
# `connect add --config` reads a path INSIDE the container: put the file under ./sources (mounted at /mnt/sources).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

stack="${1:?usage: bash scripts/cli.sh <local|dev|prod> <command...>}"; shift
case "$stack" in
  local) compose=docker-compose.local.yml; envf=.env.dev ;;
  dev)   compose=docker-compose.dev.yml;   envf=.env.dev ;;
  prod)  compose=docker-compose.prod.yml;  envf=.env.prod ;;
  *) echo "stack must be local, dev or prod" >&2; exit 1 ;;
esac
exec docker compose --env-file "$envf" -f "$compose" exec -T backend python -m app.scale.cli "$@"
