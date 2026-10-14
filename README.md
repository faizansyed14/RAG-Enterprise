# Enterprise RAG

A self-hosted document question-answering system for large, permissioned document estates: file shares, SharePoint and
OneDrive, object stores and local folders. Free and open-source components only; everything runs on your own
machines.

* **Answers with evidence.** A tree-search agent reads the candidate documents page by page, cites the page, and an
  evidence gate refuses to answer when the pages do not support it.
* **Scales past the agent's reach.** A permission-aware hybrid index (dense + keyword + exact identifiers, in Qdrant)
  narrows millions of files to at most 40 candidate documents before the agent starts.
* **Permissions are enforced, not hoped for.** Every chunk carries its readers; the filter runs inside the vector
  query and is re-checked in Postgres. A document you may not read is indistinguishable from one that does not exist.
* **Reads what companies actually have.** PDF, Word, Excel, PowerPoint (old and new formats), OpenDocument, RTF, HTML, email,
  text and code of any kind, images and scans (local OCR, layout and table models), from 31 kinds of source. A file it cannot
  read is listed as skipped with the reason, never silently dropped (`docs/scale/README.md`, "Which files can it read").
* **Heals itself.** A job compares what Postgres says is indexed with what Qdrant holds and re-embeds whatever is missing, partial or built with an old model; chunks of documents that no longer exist are removed.
* **Operable.** A Sources screen for connecting and monitoring sources, a chunk editor, user management with usage
  limits, a CLI, and three Docker stacks (local, dev, prod).

```
 sources (shares, SharePoint, S3 ...)        users
        |                                      |
   scan + permissions                      question
        v                                      v
  Postgres job queue --> workers       Stage A: hybrid search in Qdrant, inside the user's permissions
        |  parse (OCR / layout / tables)        -> at most 40 candidate documents, re-checked in Postgres
        |  chunk -> embed -> Qdrant              v
        v                              Stage B: tree-search agent reads the pages, cites them,
  pages + trees in Postgres                      the evidence gate checks the answer
```

## Run it

```bash
cp .env.dev.example .env.dev           # set OPENROUTER_API_KEY and ADMIN_PASSWORD
bash scripts/get-models.sh             # local embedding model + reranker (~130 MB)
bash scripts/local/start.sh            # http://localhost:3000
```

Everything else (dev and prod stacks, optional model servers, OCR, the CLI, tests) is in **`commands.txt`**.

| Stack | Script | What it is |
|---|---|---|
| local | `scripts/local/start.sh` | your machine, plain HTTP |
| dev | `scripts/dev/start.sh` | one server, nginx + TLS, self-hosted Postgres / Qdrant / MinIO |
| prod | `scripts/prod/start.sh` | nginx + TLS, managed Postgres and S3 |

A 16 GB laptop runs the default setup (local ONNX embedding model and reranker, local OCR, LibreOffice, no model servers).
The first build is large (about 1.3 GB of extra packages for OCR and LibreOffice); `--slim` leaves them out.

## Documentation

| | |
|---|---|
| `commands.txt` | every command to run, stop, clean, scale and test |
| `docs/ARCHITECTURE.md` | how the application is built, request by request |
| `docs/OPERATIONS.md` | running it: first-start checklist, the Health page, scaling, backup and restore, upgrades, known limits |
| `docs/DEPLOY_EC2.md` | one EC2 instance with a large EBS volume (Postgres + Qdrant), S3 for files: sizing, IAM role, bring-up, backups |
| `docs/CONFIGURATION.md` | every setting, generated from the code |
| `docs/scale/README.md` | the enterprise layer: queue, workers, permissions, index, measurements |
| `docs/scale/BENCHMARKS.md`, `TESTING.md` | every measurement with its method and limits; how to repeat it on your own files |
| `docs/scale/CAPACITY.md` | planning for terabytes: sizing formulas, what is built for scale, what is not, how to prove it on your hardware |
| `docs/scale/CONNECTORS.md`, `OTHER_SYSTEMS.md` | the 31 source kinds and their settings; what other open-source RAG systems store and retrieve with, what was adopted and what was not |
| `docs/scale/DOCPARSE.md`, `LLM_FEATURES.md`, `CHUNK_EDITOR.md` | local OCR / layout / tables (and when to use the vision model instead); optional model-assisted features and what was tried and removed; the chunk editor |
| `docs/MODELS.md`, `docs/PROMPTS.md` | which model does what, how many calls per upload and per question, every prompt |
| `docs/SECURITY.md` | what is enforced and what stays with your infrastructure |
| `NOTICE` | licences of bundled and downloaded components |

## Layout

```
backend/    FastAPI application, tree engine (app/rag_core), enterprise layer (app/scale), migrations, tests
frontend/   Next.js application
scripts/    start / stop per stack, CLI wrapper, model download
docker-compose.{local,dev,prod}.yml
models/     local model files (downloaded by scripts/get-models.sh)
sources/    folder mounted at /mnt/sources for local-folder sources
infra/      nginx configuration
```

## What is and is not verified

The retrieval, permission, queue and parsing code is covered by an automated suite and by benchmarks on real public
documents and real files (`docs/scale/BENCHMARKS.md`). Not verified: answer quality on your own questions and models,
the 31 connectors against real services, OCR on real scanned paper, 5 TB of data, answer quality against any other RAG product (never compared), and the Docker images and compose
stacks in a running environment. Read the limits section of `docs/scale/README.md` before showing it to a client.
