# Operations guide

For the person who runs the system. Architecture is in `ARCHITECTURE.md`; the scale layer is in `scale/README.md`.

## Before the first production start

`ENVIRONMENT=prod` makes the API refuse to start while any of these is true (see `Settings.production_problems`):

- `AUTH_SECRET` is shorter than 32 characters or is the development default
- `ADMIN_PASSWORD` is shorter than 12 characters or a common default
- `POSTGRES_DSN` still contains `change_me`
- `S3_ACCESS_KEY` / `S3_SECRET_KEY` are empty or `change_me`
- `CORS_ORIGINS` is empty or mentions `localhost`
- `QDRANT_API_KEY` is empty
- `OPENROUTER_API_KEY` is empty

Then: `cp .env.prod.example .env.prod`, fill it in, put `fullchain.pem` and `privkey.pem` in `TLS_CERT_DIR`, and run
`bash scripts/prod/start.sh`. Open the site, sign in as the admin, add a source under **Sources**, and watch **Health**.

Running everything on one EC2 instance (EBS for Postgres and Qdrant, S3 for files): see [DEPLOY_EC2.md](DEPLOY_EC2.md).

## What to watch

The **Health** page (admin sidebar) is the first place to look. It shows:

| Item | Healthy | Action when not |
|---|---|---|
| Workers online | 1 or more | Start or restart the worker container. Until one runs, uploads and scans wait in the queue. |
| Waiting to index | 0 (briefly higher after a large upload) | Add workers: `docker compose ... up -d --scale worker=N`. |
| Older build | 0 | Appears after changing the embedding model or chunk size. A check re-indexes them; the old index keeps answering meanwhile. |
| Failed files | 0 | Open **Sources**, then the failed count, to see the reason per file. |
| Last check | Recent, few "missing" verdicts | Many "missing" verdicts mean the index was lost or restored from an old copy; press **Check everything now**. |

Machine checks: `GET /api/health` (database, object store, search index, model key), and the worker container has its own
Docker health check (it touches a file every 20 s). `docker compose ps` shows both.

## Scaling

- **More ingest speed:** more worker containers (`--scale worker=N`) or a higher `SCALE_WORKER_CONCURRENCY`. Workers share the Postgres queue and never process the same job twice.
- **More chat users:** run more API replicas behind the proxy. Run `alembic upgrade head` once as a separate step before starting replicas (the API's start command does it for single-instance setups).
- **Search size:** exact search is used while a user can see 40,000 chunks or fewer; above that, HNSW with the permission filter. Measured limits are in `scale/BENCHMARKS.md` (sections 21 and 22).
- **Memory:** `SCALE_QUANTIZE_INT8=true` cuts index memory to about a quarter at a small recall cost (`scale/README.md`).

## Backup and restore

The database is the only store that cannot be rebuilt.

```
bash scripts/backup.sh prod ./backups                 # nightly, from cron
gunzip -c backups/rag-YYYYmmdd-HHMMSS.sql.gz | docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T postgres psql -U rag -d rag_engine
bash scripts/cli.sh prod reconcile --force            # rebuild the search index after a restore
```

- Original files are in the S3 bucket: turn on bucket versioning and cross-region replication there.
- The search index is rebuilt from the stored pages by `reconcile`; restoring a Qdrant snapshot is optional.
- Test a restore into an empty database before you rely on it.

## Upgrading

1. Back up. 2. Pull the new code. 3. `bash scripts/prod/start.sh` rebuilds and restarts; migrations run on start. 4. Open **Health** and press **Check everything now** if the release changed the embedding model, chunk size or ranking settings.

## Known limits

- Docker images and the full stack have not been built or run in the environment this release was prepared in (no Docker there). Build once in staging first.
- Tested up to 4,059 documents with real files, and 500,000 synthetic chunks for search speed. Larger sizes (millions of documents, multi-terabyte sources) are projections.
- Answers depend on the language model you configure. Retrieval is tested; answer wording and citation quality are not graded by any automated test.
- Tables of six rows or fewer, and two-column scanned certificates, can lose which label belongs to which value in the stored text.
- Connectors for external services (SharePoint, S3, SFTP and others) are tested against fakes, not against live accounts.
