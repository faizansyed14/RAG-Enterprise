# Enterprise layer: scale, permissions and retrieval

This is the part of the system that takes the application from "works to about 1,000 documents" to a
permission-aware index over millions of files, without replacing what already worked: the tree-search agent, page
citations, the evidence gate and the guardrails.

```
question --> [ Stage A: enterprise index ]                    --> [ Stage B: tree-search agent ]
             hybrid search (dense + sparse + exact IDs)            reads the <= 40 candidate documents page by
             inside the user's permissions, rerank,                page, cites pages, applies the evidence gate
             re-check in Postgres -> <= 40 documents
```

Stage A is always on; there is no switch to turn it off.

## How a file becomes searchable

```
 connectors --> scan --> source_items --> scale_jobs (Postgres queue) --> N workers
                                                  parse -> pages (Postgres) -> chunks -> embeddings -> Qdrant
 users --> API --> Stage A (Qdrant, filtered by principals) --> Postgres re-check --> agent --> answer + citations
```

* Workers are stateless: add more with `docker compose ... up -d --scale worker=N`. A killed worker's jobs are re-leased.
* Permissions: `document_access` (Postgres) is the truth; chunk payloads follow it. A permission change is one SQL
  transaction plus one payload write, with no re-embedding. A file with no matching rule gets no readers (fail closed).
* Deletes: removed at the source, unreadable at once, physically deleted by a job.
* Parsing: text formats directly; scanned PDFs and images through the local OCR / layout / table models (`DOCPARSE.md`);
  legacy Office through LibreOffice. Every file type is in the table under "Which files can it read".

## The index repairs itself

Qdrant holds a derived copy of what Postgres knows (one point per chunk). A derived copy can drift without any error at
query time: a volume restored from an old backup, a dropped collection, a point or only its vector deleted, an embedding job
that died half way, a changed embedding model or chunk size, a restored database that no longer knows documents the index still
holds. Searches would simply stop finding things. None of the open-source RAG systems reviewed (`OTHER_SYSTEMS.md`) detects this;
the worker here does (`app/scale/reconcile.py`, migration `0015`):

| Found | Meaning | Repair |
|---|---|---|
| `missing` | the document has no chunks in the index | re-embed from the stored pages |
| `partial` | fewer chunks than the last indexing wrote (+ hand-written ones) | re-embed |
| `extra` | more chunks than expected (leftovers of an interrupted rebuild) | re-embed, leftovers removed |
| `no_vector` | a chunk whose dense vector is gone | re-embed |
| `signature` | built with another embedding model, text analysis or chunk size | re-embed into the current collection |
| orphan | chunks whose document is not in Postgres | deleted |

* Nothing is fetched from the source: the pages are in Postgres. A rebuild writes the new chunks first and removes
  leftovers afterwards, so the document stays searchable while it is rebuilt, and a rebuild that fails (embedding service down)
  leaves the old chunks in place.
* Each document is compared at least every `SCALE_RECONCILE_INTERVAL_HOURS` (24); one comparison asks Qdrant for the chunk
  count of 500 documents in one call. Every 5 minutes a cheaper whole-index check (chunks in Qdrant against the sum Postgres
  expects) notices a wiped or old volume at once and then compares every document without waiting for its daily turn. Damage
  to a few documents inside a healthy index is found on that document's daily turn (or by `reconcile --force`). A run spends at most `SCALE_RECONCILE_SECONDS` and continues at the next tick; at most
  `SCALE_RECONCILE_REQUEUE_CAP` documents wait for re-embedding at once, so a lost collection refills at the workers' pace.
* Changing `SCALE_EMBED_MODEL` therefore no longer needs a manual re-scan: the new collection fills by itself while the old one keeps
  answering. Changing the chunk size rebuilds in place.
* Embedder output is checked before it is stored (right number of vectors, right dimension, finite, not empty).
* By hand: `bash scripts/cli.sh local reconcile --dry-run` (report only), `reconcile` (repair what is due), `--force` (compare
  everything now), `--orphans`. `cli status` and `GET /api/scale/status` (admin) show the last result under "index health"; `POST /api/scale/reconcile?force=true` (admin) queues a full comparison.
* Tests that destroy the index on purpose: `tests/scale/test_reconcile.py` (21 tests, embedded and real Qdrant). Cost on a real
  500,000-chunk collection: about 0.1 s per 500 documents, so a pass over a million documents is a few minutes
  (`BENCHMARKS.md`, section 21).

## Which files can it read

| Kind | Extensions | How |
|---|---|---|
| PDF with a text layer | `pdf` | the text layer, page by page |
| Scanned PDF, photos, scans | `pdf` without text, `png jpg jpeg tif tiff bmp webp` | local OCR + layout + table models (`DOCPARSE.md`); with `SCALE_DOCPARSE_MODE=off`, the vision model for PDFs and images refused |
| Word, Excel, PowerPoint | `docx xlsx pptx` and `docm dotx xlsm xltx pptm ppsx potx` | built-in readers; tables stay tables |
| Old Office, OpenDocument sheets and slides | `doc dot xls xlt ppt pps pot ods odp` | converted by LibreOffice (headless, in the image), then read like the modern formats |
| OpenDocument text, RTF, web pages | `odt rtf html htm` | built-in readers |
| Email, spreadsheets as text | `eml csv json xer` | built-in readers |
| Text and code | `txt md markdown rst adoc log xml yaml yml ini cfg conf toml sql sh bat ps1 js ts java c cpp cs go rs rb php py css tex tsv ...` | read as text; code keeps its indentation |
| Any other extension, or none | | sniffed once downloaded: readable text is indexed as text, a binary is skipped with the reason `unsupported_type` |
| Known binaries | `zip 7z exe dll mp3 mp4 gif dwg db ...` | not downloaded; listed as skipped with the reason |

A file that cannot be read is never silently dropped: its row on the Sources screen says `skipped` and why (unreadable, empty,
no converter, no OCR packages, unsupported type). Uploads through the UI accept the named extensions only (the list is in
`frontend/src/lib/fileTypes.ts`); sources accept anything and sort it out as above.
Not read: audio and video (no transcription), CAD drawings, password-protected files, archives (they are not unpacked), images
inside Word / PDF files that carry no text layer of their own.

## What was measured (`BENCHMARKS.md` has the method and the limits)

Real public documents (Kubernetes and MDN docs, 826 licence texts, Reuters news, plus 200 look-alike synthetic contracts,
invoices and tickets), a real embedding model (all-MiniLM-L6-v2, English only), a real Qdrant server, one machine with
2 CPUs. **No language model, no real scanned paper, no real connector service, no 5 TB.**

| Question | Result |
|---|---|
| Does it return the right document among **28,230**? | Right document first for 82.7 % of 1,330 questions, in the top 10 for 98.0 %, in the top 40 (what the agent reads) for 99.5 %. Bare identifiers and identifiers inside questions: 100 % first (400 of 400). Natural-language questions are the weak point: 61 % first, 94.5 % in the top 10 |
| On questions never used for any setting (200 held-out) | 84.5 % first, 98.5 % top 10, 0 never found |
| Is it the **real file's** text? (1,500 files, 641 converted to real PDF / DOCX / HTML / PPTX / RTF) | File hash = SHA-256 on disk 299 / 299; right file first 97.3 %, in the top 5 100 %; 897 returned passages: 840 exact + 57 near + 0 unmatched; the same check against random other documents matched 0 of 300; 600 permission probes: 0 leaks; edits, additions and deletes all picked up on rescan |
| Score fusion | **Weighted score fusion** (min-max normalised dense and keyword scores, dense weight 0.3) was the biggest gain over reciprocal-rank fusion: 72.4 % -> 82.7 % first-rank among 28,230 documents (73.0 -> 84.5 % held-out). Coverage re-rank, title vector, word-pair terms and extra stemming were within noise or harmful, so they are off |
| Do you need Elasticsearch? | **No, not for accuracy.** Lucene BM25 (the Elasticsearch engine) on the same chunks: 82.3 % vs 82.7 % first-rank, 12 vs 7 never found. Reasons to add it anyway (Arabic and other-language analysers, phrase / regex / highlighting / facets, an existing cluster): `ELASTICSEARCH_DECISION.md` |
| Typos | 69.6 % first-rank on misspelled questions (75.4 % before the typos) |
| Says "nothing relevant" instead of guessing (optional, off by default) | Out-of-sample: keeps 97.1 % of answerable questions, turns away 85 % of unanswerable ones (112 questions). Calibrate on your own data (`scripts/scale_calibrate.py`) |
| Reranker (a small local cross-encoder, actually run) | Used alone it is **worse** (82.6 -> 79.7 % first). Blended with the fused score and skipped for short keyword questions and codes (the default): **+1.0 point** first (83.6 %), +4 points on natural-language questions; ~0.4-1 s per question on 2 CPU cores. Chosen on the same questions, so in-sample |
| OCR + layout + tables (local models, no LLM) | On synthetic scans: word error 5 % (clean, scan), 13 % (poor); reading order 94 %; table cells 94 %. Searching OCR'd text finds the right document first 94-95 % of the time vs 98 % for the original text. **Real scanned paper not tested** (`DOCPARSE.md`) |
| Knowledge graph, RAPTOR summaries | **Built, measured, removed.** Graph: 82.6 -> 82.9 % first at the default weight, worse at a high one. RAPTOR: 82.8 -> 82.6 %. Neither helped on passage-level questions; the questions contain no multi-hop or whole-document cases, so "no gain here" is all this shows (`LLM_FEATURES.md`) |
| All file types, real models | 19 files in 18 formats (including legacy `.doc` `.xls` `.ppt`, a scanned PDF and a PNG) among 48 look-alikes, real BGE-small and MiniLM: right file first 19 / 19, passage returned word for word 19 / 19, every chunk a literal slice of the stored page. A small corpus: a floor on correctness, not a recall estimate (`TESTING.md`) |
| Model-assisted enrichment, query rewriting | **Built, off by default, mechanics tested with a fake model, accuracy benefit NOT measured** (`LLM_FEATURES.md`) |
| Connectors | 31 kinds, about 1,000 tests against fakes of each service's API, **none run against a real service** (`CONNECTORS.md`) |
| 500,000 chunks, permissions of different sizes (synthetic growth of real vectors) | Meaning search with a permission filter: recall@10 0.99 for most users but **0.49 for a user whose filter let ~13,000 chunks through**; no search-width setting fixed it. Fixed by exact search below 40,000 visible chunks: 1.00 at 4-15 ms. Keyword leg 5-6 ms. int8 vectors: -5 points unless rescored (then within 1 point). Section 21 |
| Dozens of near-identical copies of a file | 1,800 copies of 150 documents pushed relevant documents from rank 2 to 14-19; capping each family at 3 candidates restored hit@10 (96.9 -> 99.2 on copied families); identical results where there are no copies. Section 22 |
| Index damage (vectors or collection deleted, model changed, stray chunks) | Detected and repaired from Postgres by `reconcile`; 21 tests destroy the index on purpose, on embedded and on a real Qdrant. Checking 500 documents costs ~0.1 s on the 500,000-chunk collection |
| Speed | Search p50 10.5 ms / p95 12.7 ms at about 98,000 chunks with the permission filter inside the query (excludes embedding the question); real-file ingest 1.54 documents / s on 2 shared CPUs with CPU embedding |

One result worth knowing: with this small English embedder the hybrid is about level with good keyword search at
first rank (82.7 vs 82.2 %). Its value is fewer total misses (a dense-only search misses 109 of 1,330 questions), typo
robustness and no blind spots. A multilingual embedder (BGE-M3, `--models`) is wired and was not run here.

## What is in the code

| Path | What |
|---|---|
| `backend/app/scale/` | ACL, hybrid index, search, queue, worker, pipeline, sync, HTTP API (`api.py`, `sources_api.py`), CLI; `connectors/` (31 kinds); `docparse/` (OCR, layout, tables); `readers.py`, `legacy_office.py` (every non-PDF file type); `llm.py`, `enrich.py` (optional model-assisted features); `chunk_edit.py`, `pieces.py` (the chunk editor); `reconcile.py` (self-healing index), `families.py` (copies do not crowd the candidates) |
| `backend/alembic/versions/0012_scale_core.py` to `0015_index_reconcile.py` | Tables: `source_connections`, `connector_state`, `source_items`, `document_access`, `user_principals`, `scale_jobs`, name index, `documents.tier`, `chunk_edits`, `chunk_edit_log`, `scale_state`; `documents.chunks_expected / index_signature / reconciled_at` |
| `backend/tests/scale/` | The test suite (real Postgres; embedded **or** real Qdrant) |
| `backend/requirements-scale.txt`, `requirements-docparse.txt`, `requirements-connectors.txt` | Dependencies: always needed / OCR and layout / connectors that need a package |
| `backend/scripts/` | `scale_realeval.py` (retrieval quality on a real corpus), `scale_e2e_real.py` (real PDF / DOCX / PPTX / HTML / RTF through the production path), `scale_calibrate.py`, `scale_make_typos.py`, `scale_ref_strategy.py`, `lucene_reference/` (Lucene BM25 on your chunks), `scale_bench.py`, `scale_eval.py` (golden-set retrieval + permission leaks), `scale_ab.py` (same questions, Stage A off vs on), `scale_single_doc.py`, `scale_docparse_eval.py` |
| `frontend/src/components/admin/` | **Sources** (connect, scan, status, problems, access groups), **Chunks** (edit / add / switch off chunks, try a search), **Health** (workers online, queue, index check and repair, failed files), **Users** |
| `docker-compose.{local,dev,prod}.yml`, `scripts/` | The three stacks and their start / stop scripts; `commands.txt` at the repository root lists every command |
| `docs/scale/` | `BENCHMARKS.md`, `TESTING.md`, `SINGLE_DOC.md`, `ELASTICSEARCH_DECISION.md`, `DOCPARSE.md`, `LLM_FEATURES.md`, `CHUNK_EDITOR.md`, `CONNECTORS.md`, `OTHER_SYSTEMS.md` |

## Run it

The full command list is `commands.txt`. The short version:

```bash
cp .env.dev.example .env.dev                 # set OPENROUTER_API_KEY and ADMIN_PASSWORD
bash scripts/get-models.sh                   # local embedding model + reranker into ./models
bash scripts/local/start.sh                  # then http://localhost:3000 -> Sources -> Add source
```

Without a browser, the same actions are on the CLI (`bash scripts/cli.sh local --help`):

```bash
cat > sources/conn.json <<'JSON'
{"root": "/mnt/sources",
 "acl_rules": [{"prefix": "Finance", "principals": ["group:finance"]},
               {"prefix": "Public",  "principals": ["*"]}]}
JSON
# A file under a folder with no rule gets NO readers (fail closed); add {"prefix": "", ...} for a default.

bash scripts/cli.sh local connect add --kind local --name "Shared drive" --config /mnt/sources/conn.json
bash scripts/cli.sh local principals add --user alice --principal group:finance
bash scripts/cli.sh local scan --connection "Shared drive"
bash scripts/cli.sh local status
```

On by default: the local embedding model, the local reranker, local OCR / layout / tables for scans and images, LibreOffice for
old Office files, the chunk editor. Off by default, each with its own document: language-model enrichment and query expansion
(`SCALE_LLM_MODE`, `LLM_FEATURES.md`), "nothing relevant" abstention (calibrate first, `SCALE_MIN_*`), and the model servers (`--models`).
A slimmer image without OCR and LibreOffice: `--slim`.

### Documents uploaded through the UI before the layer was on

They have pages in Postgres but no chunk index, so Stage A cannot find them. Back-fill once; the worker drains it:

```bash
bash scripts/cli.sh local index-existing
bash scripts/cli.sh local status
```

Documents uploaded in the UI are readable by everyone. Documents that come from a source carry that source's
permissions.

### Embedding models

| Setting | Model | Size | Notes |
|---|---|---|---|
| default (`SCALE_EMBED_BACKEND=onnx`) | BGE-small (8-bit), 384 dimensions | ~35 MB, on CPU | English; most measurements above used all-MiniLM-L6-v2, a model of the same size class; the all-file-types test used BGE-small itself |
| `--models` (`SCALE_EMBED_BACKEND=openai`) | BGE-M3, 1024 dimensions + bge-reranker-v2-m3 | ~2 GB each | multilingual; needs RAM, or a GPU for speed |
| `hashing` | none | none | offline test mode, NOT semantic |

Changing the model creates a new collection, which the worker fills by itself from the stored pages (no re-scan; see "The index repairs itself"); the old one keeps answering until then.

## Tests

```bash
cd backend
pytest tests/scale -q                                                # embedded Qdrant
SCALE_TEST_QDRANT_URL=http://localhost:6333 pytest tests/scale -q    # a real Qdrant server
```

`TESTING.md` says what each test proves and how to repeat the real-document tests on your own files.

## Limits you must know before showing a client

Not verified: answer quality of the full system on your own questions (run `scripts/scale_ab.py` with your models),
anything a real language model would add (enrichment, query rewriting), OCR on real scanned paper (and how it compares with the vision model),
any of the 31 connectors against a real service, a live Microsoft tenant, NTFS permissions, SharePoint site groups, the
Docker images and the compose stacks in a running environment (they were validated statically), and the 5 TB claim
itself. The quality numbers come from model-written questions and a small English embedder. Per-type chunk templates
(laws, manuals, papers, Q&A) are not built: one general chunker serves every type.

### Open items from outside reviews (checked against the code)

Fixed: a selection longer than `SCALE_SCOPE_FILTER_MAX` is searched in parts and merged by score; one embedder per configuration is
shared by every thread; ONNX pooling and the query / passage prefixes are part of the collection name when they differ from the
defaults; finished jobs are deleted after `SCALE_JOB_RETENTION_DAYS` (7) and dead ones after `SCALE_DEAD_JOB_RETENTION_DAYS` (60);
a changed embedding model no longer means "nothing found" while it is rebuilt (the old index is read until every document is rebuilt, then
`migration retire`); UI uploads are lease-based so replicas never resume the same document and any replica can stream its progress; the
orphan sweep covers the whole index in bounded runs with a saved cursor; the document list pages by keyset cursor with a trigram index on
names; the sparse index and payload can live on disk; shard and replica counts are settings for a new collection. See `CAPACITY.md`.

Real and still open (none is needed below roughly a few million chunks):

* The model fingerprint has no checksum of the model file, and nothing for the parser, OCR or enrichment prompt version.
* A model change reads the old index only if its settings were recorded (an index built before v6 has none: see `CAPACITY.md`, section 5).
* Sparse terms are hashed to 32 bits, so two different terms can share an id. With a million distinct terms expect on the
  order of a hundred colliding pairs; a term dictionary or a separate lexical engine removes this.
* Candidate pools (60 dense, 60 sparse) are filled before near-duplicate demotion, so hundreds of copies can crowd them.
* The three-node Qdrant file (`docker-compose.cluster.yml`) is written but was not run; PostgreSQL is still a single node; no Kubernetes chart.
* No online relevance telemetry or feedback loop, no metadata filters or query router, multilingual retrieval is unmeasured,
  and no SSO/SCIM.
