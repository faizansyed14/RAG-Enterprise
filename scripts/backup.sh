#!/usr/bin/env bash
# Back up the one thing that cannot be rebuilt: the Postgres database (documents, section trees, page text, users,
# access rules, source state). The search index (Qdrant) is rebuilt from it by the worker's reconcile job, and original
# files live in S3/MinIO, which has its own versioning/replication.
#
#   bash scripts/backup.sh <local|dev|prod> [output-dir]        writes <output-dir>/rag-YYYYmmdd-HHMMSS.sql.gz
#   gunzip -c rag-....sql.gz | docker compose -f docker-compose.prod.yml exec -T postgres psql -U rag -d rag_engine   (restore into an EMPTY database)
#
# After a restore: start the stack, then   bash scripts/cli.sh <stack> reconcile --force   to rebuild the search index.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

stack="${1:?usage: bash scripts/backup.sh <local|dev|prod> [output-dir]}"
out="${2:-./backups}"
case "$stack" in
  local) compose=docker-compose.local.yml; envf=.env.dev ;;
  dev)   compose=docker-compose.dev.yml;   envf=.env.dev ;;
  prod)  compose=docker-compose.prod.yml;  envf=.env.prod ;;
  *) echo "stack must be local, dev or prod" >&2; exit 1 ;;
esac
mkdir -p "$out"
file="$out/rag-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose --env-file "$envf" -f "$compose" exec -T postgres pg_dump -U rag -d rag_engine --no-owner | gzip -9 > "$file.partial"
gzip -t "$file.partial"                         # refuse to keep a file that is not a complete gzip stream
mv "$file.partial" "$file"
echo "wrote $file ($(du -h "$file" | cut -f1))"
