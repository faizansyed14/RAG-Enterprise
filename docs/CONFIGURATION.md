# Configuration reference

Generated from the settings classes by `python -m scripts.make_config_reference`. Do not edit by hand.
A fresh install needs only the values in `.env.dev.example` / `.env.prod.example`; everything here has a default.

## App settings (environment variables, no prefix)

| variable | type | default | what it does |
|---|---|---|---|
| `ENVIRONMENT` | `str` | `"dev"` |  |
| `AUTH_SECRET` | `str` | `(set in .env)` |  |
| `ADMIN_USERNAME` | `str` | `"admin"` | Bootstrap admin: seeded into the users table on first boot only (see core/bootstrap.py). After that, accounts are managed from the Users page. |
| `ADMIN_PASSWORD` | `str` | `(set in .env)` |  |

### Web / security

| variable | type | default | what it does |
|---|---|---|---|
| `CORS_ORIGINS` | `str` | `"http://localhost:3000"` | Comma-separated browser origins allowed to call the API (CORS). |
| `TRUSTED_PROXY_HOPS` | `int` | `0` | How many reverse proxies sit in front of the API. 0 = none: X-Forwarded-For is ignored (it's client-controlled). With N > 0 the Nth entry from the right is used. |
| `ACCESS_TOKEN_TTL_MINUTES` | `int` | `60 * 24` |  |
| `MAX_UPLOAD_MB` | `int` | `50` |  |
| `INGEST_CONCURRENCY` | `int` | `1` | Uploads through the Documents screen: how many documents one API process indexes at once (1 = one after another; each one already fans out into many parallel model calls). |
| `RECOVER_STUCK_INGESTS` | `bool` | `True` | Resume documents whose process died (checked at boot and every minute; a document another replica is working on is left alone). |
| `PRESIGNED_URL_TTL_SECONDS` | `int` | `600` | How long a file preview / download link stays valid. |

### Rate limits (see core/ratelimit.py). limit / window in seconds.

| variable | type | default | what it does |
|---|---|---|---|
| `RL_LOGIN_PER_IP` | `int` | `10` | login attempts per IP per minute |
| `RL_LOGIN_MAX_FAILURES` | `int` | `5` | failed logins per username before a lockout |
| `RL_LOGIN_IP_MAX_FAILURES` | `int` | `15` | failed logins per IP before a lockout |
| `RL_LOGIN_LOCKOUT_SECONDS` | `int` | `900` |  |
| `RL_CHAT_PER_MINUTE` | `int` | `6` | chat requests per user per minute (on top of credits) |
| `RL_ADMIN_WRITE_PER_MINUTE` | `int` | `60` |  |
| `RL_ME_PER_MINUTE` | `int` | `60` |  |
| `RL_GLOBAL_PER_MINUTE` | `int` | `300` | coarse per-IP backstop, in-process |

### Per-user chat quota (see core/quota.py)

| variable | type | default | what it does |
|---|---|---|---|
| `CHAT_CREDIT_COST` | `int` | `10` | credits charged per chat message |
| `CHAT_BLOCK_SECONDS` | `int` | `3600` | block length once a user's allowance is spent |
| `DEFAULT_CREDIT_LIMIT` | `int` | `100` | allowance for new users (100 / 10 = 10 messages) |

### Postgres (metadata: documents, diagram pages)

| variable | type | default | what it does |
|---|---|---|---|
| `POSTGRES_DSN` | `str` | `(set in .env)` |  |

### Qdrant: ONE server. The search index (collections of document chunks, tuned by SCALE_*) and the diagram page vectors both use it

| variable | type | default | what it does |
|---|---|---|---|
| `QDRANT_URL` | `str` | `"http://localhost:6333"` |  |
| `QDRANT_API_KEY` | `str or None` | `None` |  |

### Object storage (original files, page renders, diagram crops)

| variable | type | default | what it does |
|---|---|---|---|
| `S3_ENDPOINT_URL` | `str or None` | `None` | unset -> real AWS S3; the backend's own MinIO endpoint in dev |
| `S3_PUBLIC_ENDPOINT_URL` | `str or None` | `None` | Only needed in dev: presigned URLs are handed to the *browser*, which can't resolve the Docker-internal "minio" hostname s3_endpoint_url uses -- see core/object_store.py's module docstring. Unset in prod (real S3 is publicly reachable, so s3_endpoint_url's None already works). |
| `S3_BUCKET` | `str` | `"rag-engine-dev"` |  |
| `S3_REGION` | `str` | `"us-east-1"` |  |
| `S3_ACCESS_KEY` | `str` | `""` |  |
| `S3_SECRET_KEY` | `str` | `""` |  |

### Model gateway: OpenRouter, OpenAI-compatible

| variable | type | default | what it does |
|---|---|---|---|
| `OPENROUTER_API_KEY` | `str` | `""` |  |
| `OPENROUTER_BASE_URL` | `str` | `"https://openrouter.ai/api/v1"` |  |

### Tree engine local storage

| variable | type | default | what it does |
|---|---|---|---|
| `RAG_STORAGE_PATH` | `str` | `"./.rag-data"` | Where the tree engine (app/rag_core/, driven by app/rag_service.py) persists its per-document tree/page data on disk -- the actual source of truth for every rag_doc_id this app issues. Must be a persistent volume in Docker. |

### Pinned models

| variable | type | default | what it does |
|---|---|---|---|
| `RAG_INDEX_MODEL` | `str` | `(required)` | No defaults here on purpose: .env.dev (or .env.prod) is the single source of truth for which model runs each role. The app refuses to start rather than silently falling back to a model choice you can't see -- set all five in .env.dev before running. See docs/MODELS.md for exactly which call site each one drives and how many calls to expect per document/query. tree build + node summaries (app/rag_service.py) |
| `RAG_CHAT_MODEL` | `str` | `(required)` | tree-search reasoning agent (app/rag_service.py) |
| `MODEL_VISION` | `str` | `(required)` | diagram OCR/caption/description + citation verification |
| `MODEL_EMBEDDING` | `str` | `(required)` | diagram vector embeddings (Qdrant) |
| `MODEL_EMBEDDING_DIMENSION` | `int` | `(required)` | must match model_embedding's real output size |

### Chat agent behaviour (optional; see app/rag_service.py::chat_stream)

| variable | type | default | what it does |
|---|---|---|---|
| `RAG_CHAT_REASONING_EFFORT` | `str or None` | `None` | Unset (the default) sends nothing: the model's own default reasoning depth applies, which is the accuracy-first choice -- "low" cut latency ~39% and cost ~31% on an 11-document library with no accuracy change, but was never measured at scale, so it is opt-in. One of minimal/low/medium/high; what a model accepts is its own. |
| `RAG_CHAT_MAX_TURNS` | `int` | `16` | How many model turns one answer may take. The engine's own default (10) ran out on a 1,011-document library while the agent searched; accuracy-first, so more headroom. |

### Section trees (TREE_GENERATION)

| variable | type | default | what it does |
|---|---|---|---|
| `TREE_GENERATION` | `bool` | `True` | true : a tree (section outline + a model-written summary per section) is built for every UI upload, and for every file that arrives from a source (SharePoint, OneDrive, shared folders ...). Costs model calls on RAG_INDEX_MODEL. false: no tree is built and no model is called at ingest, whether or not an API key is set. Pages are still stored and chunked, so search and answers work; the chat agent finds pages by keyword search instead of by section. |

### Original files (STORE_ORIGINALS)

| variable | type | default | what it does |
|---|---|---|---|
| `STORE_ORIGINALS` | `bool` | `True` | true : files that come from a source are also copied into object storage (MinIO / S3) so they can be previewed from there. false: the copy is not kept. Only the extracted pages (Postgres) and the search index (Qdrant) are stored; a citation opens the file at its source (SharePoint / OneDrive link) and shows the stored text of the cited page. Files uploaded through the screen are always stored: there is no other copy of them. |

### Page images of scanned / drawing PDFs (STORE_DIAGRAMS)

| variable | type | default | what it does |
|---|---|---|---|
| `STORE_DIAGRAMS` | `bool` | `False` | false (default): the vision model still reads every scanned page, and what it read (the text, plus a caption and a description of what the page shows) becomes the page's searchable text. No page image is saved, no separate image index is built, and the chat shows no extra pictures: a citation opens the cited page of the PDF itself. true : the older behaviour. Every such page is also saved as a PNG in object storage with its own vector (a second Qdrant collection), and chat answers about drawings show the best-matching page images next to the answer. |

## Search, index and worker settings (prefix SCALE_)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_API_LOCAL_ROOTS` | `list[str]` | `Field(default_factory=lambda: ["/mnt/sources"])` | Folders an administrator may add as a "local" source from the web UI (the CLI has no such limit). Mount the files here: compose mounts SCALE_SOURCE_DIR at /mnt/sources. |

### Hybrid index (Qdrant)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_COLLECTION` | `str` | `"chunks_v1"` |  |
| `SCALE_QDRANT_LOCATION` | `str` | `""` | ":memory:" or a directory path runs qdrant-client's embedded engine (tests / quick demos only). Empty => use the app's QDRANT_URL / QDRANT_API_KEY. |
| `SCALE_STORE_CHUNK_TEXT` | `bool` | `True` | False => chunk text lives only in Postgres (document_pages) and is read back by (doc, page, offsets) for reranking. Use False at multi-TB scale: it avoids storing every chunk twice. |
| `SCALE_ON_DISK_VECTORS` | `bool` | `False` |  |
| `SCALE_QDRANT_SHARDS` | `int` | `0` | Cluster layout of a NEW collection (an existing one keeps the layout it was created with). 0 = the server's default. Plan shards for growth: twelve shards can later spread over 1, 2, 3, 4, 6 or 12 nodes without recreating anything. |
| `SCALE_QDRANT_REPLICATION_FACTOR` | `int` | `0` | copies of every shard; 2 or 3 in a cluster so a node can be lost |
| `SCALE_QDRANT_WRITE_CONSISTENCY` | `int` | `0` | replicas that must confirm a write |
| `SCALE_QDRANT_ON_DISK_PAYLOAD` | `bool` | `False` | payload (names, ACL) on disk instead of RAM |
| `SCALE_SPARSE_ON_DISK` | `bool` | `False` | keyword index on disk instead of RAM: much less memory, a little slower |
| `SCALE_HNSW_M` | `int` | `16` |  |
| `SCALE_HNSW_EF_CONSTRUCT` | `int` | `128` |  |
| `SCALE_QUANTIZE_INT8` | `bool` | `False` | Scalar int8 quantization keeps the dense vectors ~4x smaller in RAM; originals stay on disk. |
| `SCALE_QUANTIZE_OVERSAMPLING` | `float` | `2.0` | with int8: fetch this many times more candidates from the compressed vectors, then re-score them with the originals |
| `SCALE_EMBED_BASE_URL` | `str` | `"http://embed:80/v1"` | --- Embeddings. Default: a local ONNX model (BGE-small, 384 dimensions, CPU, no server, no cost); fetch it with scripts/get-models.sh. `openai` talks to any OpenAI-compatible /embeddings endpoint (the BGE-M3 server started by --models, vLLM, TEI, Ollama /v1, OpenRouter): set embed_base_url, embed_model and embed_dim to match. --- |
| `SCALE_EMBED_API_KEY` | `str` | `""` |  |
| `SCALE_EMBED_MODEL` | `str` | `"bge-small-en-v1.5-int8"` | changing the model creates a NEW collection: re-scan, then it takes over |
| `SCALE_EMBED_DIM` | `int` | `384` |  |
| `SCALE_EMBED_BATCH` | `int` | `8` | a batch of 64 at 512 tokens ran out of memory on an 8 GB machine |
| `SCALE_EMBED_TIMEOUT_S` | `float` | `60.0` |  |
| `SCALE_EMBED_BACKEND` | `str` | `"onnx"` | 'hashing' is a deterministic offline embedder for tests and plumbing checks. It is NOT semantic. onnx (local model files) / openai (a model server) / hashing (tests only) |
| `SCALE_EMBED_QUERY_PREFIX` | `str` | `""` | Some models want a different prefix for queries and passages (E5: "query: " / "passage: "; BGE-en: a query instruction). |
| `SCALE_EMBED_DOC_PREFIX` | `str` | `""` |  |
| `SCALE_ONNX_MODEL_DIR` | `str` | `"/models/bge-small-en-v1.5-int8"` | backend=onnx: a directory with tokenizer.json and onnx/model.onnx (MiniLM, BGE, E5, GTE ...). |
| `SCALE_ONNX_MAX_TOKENS` | `int` | `256` |  |
| `SCALE_ONNX_THREADS` | `int` | `2` |  |
| `SCALE_ONNX_POOLING` | `str` | `"cls"` | cls (BGE) / mean (MiniLM, E5, GTE) |

### Reranker: any Cohere/Jina/vLLM-style POST {model, query, documents, top_n} -> results[]

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_RERANK_URL` | `str` | `""` |  |
| `SCALE_RERANK_API_KEY` | `str` | `""` |  |
| `SCALE_RERANK_MODEL` | `str` | `""` |  |
| `SCALE_RERANK_STYLE` | `str` | `"cohere"` | cohere (Cohere / Jina / vLLM: documents=[...]) / tei (HF text-embeddings-inference: texts=[...]) |
| `SCALE_RERANK_TIMEOUT_S` | `float` | `30.0` |  |
| `SCALE_RERANK_CANDIDATES` | `int` | `60` |  |
| `SCALE_RERANK_ONNX_DIR` | `str` | `"/models/ms-marco-MiniLM-L-6-v2"` | rerank_mode=onnx: a local cross-encoder (ms-marco-MiniLM-L-6 and the like) run by onnxruntime, no server needed. The directory holds tokenizer.json and onnx/model.onnx. SCALE_RERANK_URL, when set, wins over the local model. |
| `SCALE_RERANK_ONNX_MAX_TOKENS` | `int` | `384` | query + passage tokens the cross-encoder reads (longer passages are cut) |
| `SCALE_RERANK_ONNX_BATCH` | `int` | `16` |  |
| `SCALE_RERANK_ONNX_THREADS` | `int` | `4` |  |
| `SCALE_RERANK_CACHE_SIZE` | `int` | `20000` | (question, passage) scores kept in memory; 0 = off |
| `SCALE_QUERY_CACHE_SIZE` | `int` | `2000` | question vectors kept in memory; 0 = off |
| `SCALE_RERANK_TOP` | `int` | `30` | how many of the fused best chunks the cross-encoder scores (cost grows with it) |
| `SCALE_RERANK_BLEND` | `float` | `0.5` | The cross-encoder's verdict is blended with the fused retrieval score: (1-rerank_blend) * fused + rerank_blend * cross-encoder, both min-max scaled over the scored chunks. (Using the cross-encoder alone measured worse: 79.7 % vs 82.6 % first-rank.) |
| `SCALE_RERANK_GATE` | `str` | `"auto"` | Which questions the reranker sees. A cross-encoder trained on questions reads a bag of keywords badly (measured: it lowered top-1 accuracy on keyword queries and raised it on full questions), and a question that names a code (INV-0070, CTR-2025-2186-TD) is an exact lookup keyword search already answers. "on": skip questions shorter than rerank_min_words and questions with a code. "off": rerank everything. "auto": on for the local model (these numbers were measured on it), off for a remote reranker URL (not measured -- set "on" if you measure it). |
| `SCALE_RERANK_MIN_WORDS` | `int` | `6` |  |
| `SCALE_RERANK_SKIP_IDENTIFIERS` | `bool` | `True` |  |

### Chunking (word-based approximation of tokens)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_CHUNK_WORDS` | `int` | `160` | sized for the default model, which reads 256 tokens; use 320 / 40 with BGE-M3 |
| `SCALE_CHUNK_OVERLAP_WORDS` | `int` | `20` |  |
| `SCALE_MIN_CHUNK_WORDS` | `int` | `12` |  |

### Text analysis: changes WHICH TERMS are indexed, so it is part of the collection name (change = re-index)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_STEM_LANGS` | `str` | `"en,ar"` | stem these languages (terminated / termination meet); "" = off. Script-gated: English rules touch only Latin words, Arabic rules only Arabic |
| `SCALE_SPARSE_AVGDL` | `float` | `260.0` | BM25 length normalisation: set near the typical number of terms per chunk |
| `SCALE_CTX_DENSE` | `bool` | `True` | put "name / title / folder / heading" in front of the text that is EMBEDDED |
| `SCALE_CTX_SPARSE` | `bool` | `True` | ... and in front of the text that is KEYWORD-indexed |
| `SCALE_QUERY_STOPWORDS` | `bool` | `True` | drop function / question words from the SEARCH side only |

### Retrieval

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_DENSE_K` | `int` | `60` |  |
| `SCALE_SPARSE_K` | `int` | `60` |  |
| `SCALE_FUSION_DENSE_WEIGHT` | `float` | `0.3` | Chunks are scored by weighted min-max fusion: w*dense + (1-w)*keyword. This keeps the raw cosine and BM25 for the confidence check and measured best on real documents and on exact-identifier lookups (docs/scale/BENCHMARKS.md). share of the dense (meaning) score; the rest is keyword score |
| `SCALE_RERANK_MODE` | `str` | `"onnx"` | onnx: a local cross-encoder blended with the retrieval score (+1.0 point first-rank measured; the models are fetched by scripts/get-models.sh --reranker). none: no reranking. If the model files are missing the search runs without it and says so in the log. none / onnx |
| `SCALE_MIN_CONFIDENCE` | `float` | `0.0` | "Nothing relevant" check, all 0 = off Three independent signals about the best chunk: how close it is in MEANING (top cosine), how strongly it matches the KEYWORDS (top BM25), and how much of the question's wording it COVERS (0-1). The search returns no documents instead of forty irrelevant ones only when every signal you switched on is weak. An identifier lookup is weak in meaning but strong in keywords; a paraphrase is the opposite; only a question the corpus knows nothing about is weak on all three. The values depend on the embedding model and the corpus: calibrate with scripts/scale_calibrate.py. |
| `SCALE_MIN_LEXICAL` | `float` | `0.0` |  |
| `SCALE_MIN_COVERAGE` | `float` | `0.0` |  |
| `SCALE_FINAL_K` | `int` | `20` |  |
| `SCALE_MERGE_MODE` | `str` | `"union"` | How the agent's search_content merges the existing Postgres keyword search with the hybrid index: union  keep the keyword hits first, append hybrid-only hits. This is the safe default because the project's own testing found rank fusion pushed good keyword hits out on near-duplicates. hybrid use the hybrid ranking only. Decide with scripts/scale_eval.py on YOUR golden set, not by opinion. |
| `SCALE_EXACT_BELOW` | `int` | `40000` | Below this many visible chunks the meaning search is exact (see index._dense_search_params). 0 = always the graph search. |
| `SCALE_EXACT_PROBE_TTL_S` | `float` | `30.0` |  |

### Self-healing index (app/scale/reconcile.py): compare Postgres with the chunk index and repair the difference

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_RECONCILE_ENABLED` | `bool` | `True` |  |
| `SCALE_RECONCILE_INTERVAL_HOURS` | `float` | `24.0` | every document is compared with the index at least this often |
| `SCALE_RECONCILE_BATCH` | `int` | `500` | documents compared per index round-trip |
| `SCALE_RECONCILE_SECONDS` | `float` | `20.0` | time one scheduled run may spend (it continues on the next tick) |
| `SCALE_RECONCILE_REQUEUE_CAP` | `int` | `2000` | most documents one run sends back to be re-embedded (the rest follow next run) |
| `SCALE_RECONCILE_GRACE_MINUTES` | `float` | `3.0` | a document indexed this recently is not judged yet (a write may be in flight) |
| `SCALE_RECONCILE_ORPHANS_EVERY_HOURS` | `float` | `24.0` | sweep for chunks whose document no longer exists |
| `SCALE_RECONCILE_QUICK_CHECK_SECONDS` | `float` | `300.0` | how often the whole-index size check runs |
| `SCALE_RECONCILE_SHORT_RATIO` | `float` | `0.97` | fewer chunks than this share of the expected count = the index was damaged |
| `SCALE_JOB_RETENTION_DAYS` | `float` | `7.0` | finished jobs older than this are deleted (0 = keep forever) |
| `SCALE_DEAD_JOB_RETENTION_DAYS` | `float` | `60.0` | jobs that gave up are kept longer so they can still be retried (0 = keep forever) |
| `SCALE_RECONCILE_ORPHAN_SCAN_POINTS` | `int` | `250000` | chunks examined per orphan-sweep run; the sweep resumes where it stopped |
| `SCALE_FAMILY_CAP` | `int` | `3` | Near-identical copies of one document (drafts, the same file in several folders) rank together; at most this many of a family keep their place in the candidate list, the rest move behind the other documents (nothing is dropped). 0 = off. See families.py. |
| `SCALE_FAMILY_SIMILARITY` | `float` | `0.8` |  |
| `SCALE_CANDIDATE_DOCS` | `int` | `40` | documents handed to the agent after stage A |
| `SCALE_CANDIDATE_CHUNKS` | `int` | `120` | chunks examined to choose those documents |
| `SCALE_SCOPE_FILTER_MAX` | `int` | `5000` | a user's document selection up to this long is applied inside the index query |
| `SCALE_SCOPE_PARTITION_MAX` | `int` | `40` | a longer selection is searched in parts of scope_filter_max, this many parts at most |
| `SCALE_LEGACY_LIST_LIMIT` | `int` | `5000` | GET /api/documents with no `limit` (what the current UI sends) returns up to this many of the newest documents the user may read. New clients should page with ?limit=&offset= (max 1000 per page). |

### Access control

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_LINK_SCOPES_AS_ACCESS` | `list[str]` | `Field(default_factory=list)` | Sharing-link scopes treated as a grant. Empty (default) = links never widen access. |
| `SCALE_MAX_PRINCIPALS` | `int` | `2000` | Hard cap on principals carried by one user at query time (guards the filter size). |
| `SCALE_ADMIN_BYPASS_ACL` | `bool` | `False` | False (default): the app's 'admin' role manages the system but does NOT get to read every source file; an admin sees only what their own principals allow. True restores the old behaviour. |

### Queue / worker

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_WORKER_CONCURRENCY` | `int` | `4` |  |
| `SCALE_LEASE_SECONDS` | `int` | `300` |  |
| `SCALE_POLL_SECONDS` | `float` | `1.0` |  |
| `SCALE_MAX_ATTEMPTS` | `int` | `5` |  |
| `SCALE_RETRY_BASE_SECONDS` | `int` | `30` |  |
| `SCALE_MAX_QUEUED_JOBS` | `int` | `200_000` | scanning pauses (and resumes from its cursor) above this |
| `SCALE_SCAN_INTERVAL_SECONDS` | `int` | `900` |  |
| `SCALE_ACL_SWEEP_HOURS` | `float` | `24.0` | re-read permissions of files not checked for this long |
| `SCALE_FAILED_RETRY_HOURS` | `float` | `24.0` | a scan re-queues a failed file after this long (requeue-dead retries sooner) |

### Pipeline policy

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_MAX_FILE_MB` | `int` | `200` |  |
| `SCALE_UPLOAD_DIR` | `str` | `""` | Admin "upload files" source: where the files are kept (default: <RAG_STORAGE_PATH>/scale-uploads, a volume the API and the worker share), how much one request may carry and how many files. |
| `SCALE_UPLOAD_REQUEST_MB` | `int` | `512` |  |
| `SCALE_UPLOAD_MAX_FILES` | `int` | `100` |  |
| `SCALE_AUTO_PROMOTE_MIN_PAGES` | `int` | `0` | With TREE_GENERATION=true, source files of at least this many pages are queued for the full section tree (LLM cost!). 0 or 1 = every file. Has no effect with TREE_GENERATION=false. |
| `SCALE_MIN_WORDS_PER_PAGE` | `int` | `8` | A PDF whose text layer averages fewer words per page than this (and under 200 words in total) is treated as scanned. Page numbers and stamps alone are ~5 words, so 8 separates them from prose. |

### Vision reading of pages the local parsers cannot read (a model call per page; needs OPENROUTER_API_KEY and MODEL_VISION)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_OCR_ENABLED` | `bool` | `False` | true : a PDF is read with the ordinary local reader first. Pages that have no text layer (scans, photos, CAD sheets exported as pictures) and pages that are mostly one large picture with little text (a brochure floor plan, a map) are then read by the vision model, and image files (png / jpg / tif / bmp / webp) are accepted. Pages that already have their text are NOT sent: a picture inside a text page (a chart in a paper) stays unread, the page's own text is what is indexed. false: such pages are not read: a PDF with no text at all is skipped (needs_ocr), image files are refused, and a mixed PDF is indexed from its text pages only. Ignored while SCALE_DOCPARSE_MODE reads scans locally. The page rules and the per-document cap below keep the bill small. |
| `SCALE_VISION_MIN_CHARS` | `int` | `60` | a page with fewer characters than this has no usable text layer |
| `SCALE_VISION_IMAGE_RATIO` | `float` | `0.35` | a page whose images cover at least this share of it (and has under vision_max_text_chars) is sent |
| `SCALE_VISION_MAX_TEXT_CHARS` | `int` | `1500` |  |
| `SCALE_VISION_MAX_PAGES` | `int` | `40` | at most this many pages of one document go to the model; the rest keep their own text |

### docparse: local layout analysis + OCR + table structure (no LLM, no network; see docs/scale/DOCPARSE.md)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_DOCPARSE_MODE` | `str` | `"scanned"` | off      scanned PDFs follow ocr_enabled (a vision-model call per page); image files are not accepted scanned  (default) PDFs with no text layer, and image files (png / jpg / tif / bmp / webp), are OCR'd and laid out locally; if the packages are missing it says so in the log and falls back to the off behaviour all      every PDF page also goes through layout analysis (reading order across columns, tables as rows, running headers / footers / page numbers removed), using the PDF's own text where it has any |
| `SCALE_DOCPARSE_OCR` | `str` | `"auto"` | auto: the text layer where a page has one, OCR otherwise / always / never |
| `SCALE_DOCPARSE_LAYOUT` | `bool` | `True` |  |
| `SCALE_DOCPARSE_TABLES` | `bool` | `True` |  |
| `SCALE_DOCPARSE_DROP_FURNITURE` | `bool` | `True` |  |
| `SCALE_DOCPARSE_RESTORE_SPACES` | `bool` | `True` |  |
| `SCALE_DOCPARSE_DESKEW` | `bool` | `True` |  |
| `SCALE_DOCPARSE_TABLE_STYLE` | `str` | `"labeled"` | labeled: "Item: Widget B / Price: 99.00" per row / markdown |
| `SCALE_DOCPARSE_DPI` | `int` | `150` |  |
| `SCALE_DOCPARSE_MAX_PAGES` | `int` | `300` |  |
| `SCALE_DOCPARSE_THREADS` | `int` | `2` |  |
| `SCALE_DOCPARSE_DET_BOX_THRESH` | `float` | `0.3` | the OCR text detector's box threshold (its own default 0.5 drops faint lines); see docs/scale/DOCPARSE.md |
| `SCALE_DOCPARSE_DET_UNCLIP` | `float` | `1.8` |  |
| `SCALE_DOCPARSE_DET_LIMIT_SIDE` | `int` | `0` | 0 = the engine's own (736, scale up small images). 960 with type "max" (scale down large ones) made no measurable difference |
| `SCALE_DOCPARSE_DET_LIMIT_TYPE` | `str` | `"min"` | min / max |
| `SCALE_LEGACY_CONVERT_TIMEOUT_S` | `float` | `180.0` | Legacy Office files (doc xls ppt ods odp ...) are converted to their modern form by LibreOffice (headless). Installed by default in the Docker images (INSTALL_LEGACY_OFFICE=1); if soffice is missing these files are skipped with a clear reason. |

### A language model for the optional features below (enrichment and query rewriting)

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_LLM_MODE` | `str` | `"off"` | Everything that needs a model is OFF by default, and every one of those features degrades to "not there" or to a no-model fallback when the model is missing or fails: ingestion and search never depend on it. off     no model repo    the application's own gateway (OPENROUTER_BASE_URL / OPENROUTER_API_KEY) and its RAG_INDEX_MODEL openai  any OpenAI-compatible /chat/completions at SCALE_LLM_BASE_URL: Ollama, vLLM, llama.cpp server, LM Studio, OpenAI ... |
| `SCALE_LLM_BASE_URL` | `str` | `"http://localhost:11434/v1"` |  |
| `SCALE_LLM_API_KEY` | `str` | `""` |  |
| `SCALE_LLM_MODEL` | `str` | `""` |  |
| `SCALE_LLM_TIMEOUT_S` | `float` | `120.0` |  |
| `SCALE_LLM_MAX_TOKENS` | `int` | `1024` |  |
| `SCALE_LLM_CONCURRENCY` | `int` | `4` | parallel requests while enriching one document |
| `SCALE_LLM_RETRIES` | `int` | `2` |  |

### Enrichment (needs the model): extra search terms per chunk, and a smarter question

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_ENRICH_KEYWORDS` | `int` | `0` | N keywords per chunk, added to the keyword index (0 = off) |
| `SCALE_ENRICH_QUESTIONS` | `int` | `0` | N questions each chunk answers, added to the keyword index (0 = off) |
| `SCALE_ENRICH_DENSE` | `bool` | `False` | also put the keywords / questions in front of the text that is embedded |
| `SCALE_ENRICH_MAX_CHUNKS` | `int` | `3000` | a document with more chunks than this is not enriched (cost guard) |
| `SCALE_QUERY_KEYWORDS` | `bool` | `False` | the model adds the question's key terms to the keyword side of the search |
| `SCALE_QUERY_TRANSLATE` | `str` | `""` | e.g. "ar,en": the model also searches the question translated into these languages |
| `SCALE_QUERY_LLM_TIMEOUT_S` | `float` | `8.0` | a slow model must not stall a search: past this the plain question is used |

### Chunk editor

| variable | type | default | what it does |
|---|---|---|---|
| `SCALE_CHUNK_EDIT_OVERLAY` | `bool` | `True` | the agent's keyword search shows the corrected text of edited chunks |

