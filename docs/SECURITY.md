# Security, guardrails and rate limits

## Who can do what
- **admin**: documents, folders, uploads, the Users page, unmetered chat.
- **user**: chat only (read-only source picker), limited by credits (see `core/quota.py`).
- Every request re-loads the user from the database: disabling a user or changing their password/role takes effect immediately (`token_version`). Tokens last `ACCESS_TOKEN_TTL_MINUTES` (24 h by default).
- The account named by `ADMIN_USERNAME` is **owned by `.env`**: created on start, password re-synced to `ADMIN_PASSWORD` on every start (existing sessions are signed out when it changes), and the Users page/API refuse to change its username, password, role or status, or delete it. Change the password by editing `.env` and restarting the backend. Other accounts (including other admins) are managed from the Users page.
- Per-document access control is always on (next section): every query, document list and download is filtered by the caller's permissions.

## Document permissions
- Every file carries its readers (`document_access`): users, groups and Windows SIDs, taken from the source's permissions or from
  the prefix rules in the source's configuration. **A file with no matching rule has no readers** (fail closed).
- A login's groups are set on the Sources page (or `scripts/cli.sh <stack> principals add`). A login with no groups sees only
  documents open to everyone. Documents uploaded in the UI are open to everyone.
- The permission filter is part of the vector query, and the candidate list is re-checked in Postgres before the agent reads
  anything. Search, document lists, previews and chat all use it. A document the user may not read looks the same as one that
  does not exist (404, no result, no count).
- Administrators do not bypass it unless `SCALE_ADMIN_BYPASS_ACL=true`.
- Source credentials are never stored in the database: a source's configuration names an environment variable
  (`password_env`, `client_secret_env`, ...), and the Sources API refuses a configuration that carries a secret inline.
  Folder sources can only point inside `SCALE_API_LOCAL_ROOTS` (default `/mnt/sources`), so a browser session cannot aim the
  crawler at an arbitrary server path.
- Not enforced from the source: NTFS security descriptors on SMB shares (access follows the rules you write), and SharePoint
  site-group membership. Both are listed in `docs/scale/CONNECTORS.md`.

## Topic guardrails (`core/guardrails.py`, `retrieval/chat_service.py`)
The assistant answers only from the knowledge base. Layers, cheapest first, none of them costs a model call:
1. **Query screen** -- jailbreak / system-prompt extraction / role-marker patterns and bare greetings get a canned reply before any credit or LLM use.
2. **History sanitising** -- the browser sends prior turns back, so they are size-capped and injection-screened.
3. **Prompt** -- `SCOPE_POLICY` in `rag_core/local_chat.py` (documented byte-exact in `docs/PROMPTS.md`).
4. **Evidence gate (the hard guarantee)** -- answer text is held back until the agent has read real document content (`get_page_content` / `get_document_structure`). No evidence = the answer is discarded and replaced by a refusal. Nothing the model produced without reading documents ever reaches the user.
5. **Output check** -- answers that echo tool names / instructions are replaced.
6. **Wire hygiene** -- `thinking` events dropped, raw `tool_result` text replaced by `{"ok": true}`, engine errors replaced by a generic message (details go to the server log with a request id).

Refusals (layers 1, 4, 5) never cost credits.

## Rate limiting (`core/ratelimit.py`, Postgres-backed, correct across workers)
| What | Default |
|---|---|
| Login | 10 requests/min/IP; **lockout** after 5 failed attempts per username or 15 per IP (15 min) |
| Chat | 6 requests/min/user + the credit allowance (multiple streams per user may run concurrently) |
| Upload | 20/hour/user; size cap `MAX_UPLOAD_MB`; magic-byte / zip-bomb checks; sanitised filenames |
| Admin writes | 60/min |
| Everything else | 300/min/IP in-process backstop (health excluded) |

All knobs are `RL_*` settings in `core/config.py`. Behind a proxy set `TRUSTED_PROXY_HOPS=1`; otherwise `X-Forwarded-For` is ignored.

## Hardening checklist
- `ENVIRONMENT=prod` **refuses to start** with a default/short `AUTH_SECRET`, weak `ADMIN_PASSWORD`, `change_me` DB or S3 secrets, or localhost CORS origins; `/docs` and `/openapi.json` are disabled.
- Security headers on the API (nosniff, frame deny, no-referrer, HSTS in prod, `no-store`) and a CSP + the same headers on the frontend (`frontend/next.config.js`).
- Presigned file URLs live 10 minutes (`PRESIGNED_URL_TTL_SECONDS`).
- `/api/health` returns only `ok` / `unavailable`.
- Security events (`login_ok`, `login_failed`, `login_locked`, `rate_limited`, `chat_screened`, `chat_refused`, `user_*`, `document_uploaded`) are logged on the `security` logger with the request id. Passwords, tokens and chat text are never logged.
- Dependencies: `npm audit` is clean; backend versions are pinned in `backend/requirements.lock` (regenerate with `pip freeze` from a tested image).

## Deploying
`bash scripts/prod/start.sh` (copy `.env.prod.example` to `.env.prod` and fill it in first; same as `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`). Only nginx publishes ports; Postgres and Qdrant are internal; use a private S3 bucket. Put real certificates in `TLS_CERT_DIR`. `scripts/prod/stop.sh` never removes volumes unless run with `--wipe`.

`docker-compose.local.yml` (`bash scripts/local/start.sh`) is plain HTTP on purpose -- no nginx, no TLS certificate. Pure local dev never leaves the loopback interface, so encryption buys nothing there, only the cost of a self-signed cert's per-origin browser trust click (confirmed live: this is exactly what broke document preview when nginx+TLS was tried for local -- MinIO's presigned URLs are a different origin from the main app, so trusting one doesn't trust the other). `scripts/local/stop.sh` never removes volumes unless run with `--wipe`.

Deploying this same dev stack to a public EC2 instance instead of only localhost needs real TLS -- use `docker-compose.dev.yml` instead (`bash scripts/dev/start.sh` / `scripts/dev/stop.sh`), which fronts it with nginx the same way prod does (Postgres, Qdrant, backend and frontend become internal-only). One deliberate exception there too: MinIO's API port (9000) stays directly published because the browser fetches presigned preview URLs from it directly -- see that file's `minio` service comment -- but it gets its own TLS cert so that traffic isn't plaintext. `TLS_CERT_DIR` needs a real cert (see `.env.dev.example`'s EC2 section for a local self-signed cert to test with, or a real `certbot` one for a domain).

## Known limits
- The JWT is kept in `localStorage` (readable by any script that runs on the page); the CSP and short token life reduce, not remove, that risk.
- The CSP allows inline scripts because Next.js hydration needs them; a nonce-based policy needs a middleware and dynamic rendering.
- A per-username login lockout can be used to lock a real user out for 15 minutes by someone who knows their username.
