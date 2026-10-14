"""Every SCALE_* setting, declared once. Defaults are safe for a laptop; production values are in
.env.prod.example."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScaleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCALE_", env_file=".env", extra="ignore")

    # Folders an administrator may add as a "local" source from the web UI (the CLI has no such limit). Mount the
    # files here: compose mounts SCALE_SOURCE_DIR at /mnt/sources.
    api_local_roots: list[str] = Field(default_factory=lambda: ["/mnt/sources"])

    # --- Hybrid index (Qdrant) ---
    collection: str = "chunks_v1"
    # ":memory:" or a directory path runs qdrant-client's embedded engine (tests / quick demos only).
    # Empty => use the app's QDRANT_URL / QDRANT_API_KEY.
    qdrant_location: str = ""
    # False => chunk text lives only in Postgres (document_pages) and is read back by (doc, page,
    # offsets) for reranking. Use False at multi-TB scale: it avoids storing every chunk twice.
    store_chunk_text: bool = True
    on_disk_vectors: bool = False
    # Cluster layout of a NEW collection (an existing one keeps the layout it was created with). 0 = the server's default.
    # Plan shards for growth: twelve shards can later spread over 1, 2, 3, 4, 6 or 12 nodes without recreating anything.
    qdrant_shards: int = 0
    qdrant_replication_factor: int = 0         # copies of every shard; 2 or 3 in a cluster so a node can be lost
    qdrant_write_consistency: int = 0          # replicas that must confirm a write
    qdrant_on_disk_payload: bool = False       # payload (names, ACL) on disk instead of RAM
    sparse_on_disk: bool = False               # keyword index on disk instead of RAM: much less memory, a little slower
    hnsw_m: int = 16
    hnsw_ef_construct: int = 128
    # Scalar int8 quantization keeps the dense vectors ~4x smaller in RAM; originals stay on disk.
    quantize_int8: bool = False
    quantize_oversampling: float = 2.0   # with int8: fetch this many times more candidates from the compressed vectors, then re-score them with the originals

    # --- Embeddings. Default: a local ONNX model (BGE-small, 384 dimensions, CPU, no server, no cost); fetch it with
    # scripts/get-models.sh. `openai` talks to any OpenAI-compatible /embeddings endpoint (the BGE-M3 server started by
    # --models, vLLM, TEI, Ollama /v1, OpenRouter): set embed_base_url, embed_model and embed_dim to match. ---
    embed_base_url: str = "http://embed:80/v1"
    embed_api_key: str = ""
    embed_model: str = "bge-small-en-v1.5-int8"   # changing the model creates a NEW collection: re-scan, then it takes over
    embed_dim: int = 384
    embed_batch: int = 8                      # a batch of 64 at 512 tokens ran out of memory on an 8 GB machine
    embed_timeout_s: float = 60.0
    # 'hashing' is a deterministic offline embedder for tests and plumbing checks. It is NOT semantic.
    embed_backend: str = "onnx"  # onnx (local model files) | openai (a model server) | hashing (tests only)
    # Some models want a different prefix for queries and passages (E5: "query: " / "passage: "; BGE-en: a query instruction).
    embed_query_prefix: str = ""
    embed_doc_prefix: str = ""
    # backend=onnx: a directory with tokenizer.json and onnx/model.onnx (MiniLM, BGE, E5, GTE ...).
    onnx_model_dir: str = "/models/bge-small-en-v1.5-int8"
    onnx_max_tokens: int = 256
    onnx_threads: int = 2
    onnx_pooling: str = "cls"           # cls (BGE) | mean (MiniLM, E5, GTE)

    # --- Reranker: any Cohere/Jina/vLLM-style POST {model, query, documents, top_n} -> results[] ---
    rerank_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    rerank_style: str = "cohere"  # cohere (Cohere / Jina / vLLM: documents=[...]) | tei (HF text-embeddings-inference: texts=[...])
    rerank_timeout_s: float = 30.0
    rerank_candidates: int = 60
    # rerank_mode=onnx: a local cross-encoder (ms-marco-MiniLM-L-6 and the like) run by onnxruntime, no server needed.
    # The directory holds tokenizer.json and onnx/model.onnx. SCALE_RERANK_URL, when set, wins over the local model.
    rerank_onnx_dir: str = "/models/ms-marco-MiniLM-L-6-v2"
    rerank_onnx_max_tokens: int = 384    # query + passage tokens the cross-encoder reads (longer passages are cut)
    rerank_onnx_batch: int = 16
    rerank_onnx_threads: int = 4
    rerank_cache_size: int = 20000       # (question, passage) scores kept in memory; 0 = off
    query_cache_size: int = 2000         # question vectors kept in memory; 0 = off
    rerank_top: int = 30                 # how many of the fused best chunks the cross-encoder scores (cost grows with it)
    # The cross-encoder's verdict is blended with the fused retrieval score: (1-rerank_blend) * fused + rerank_blend *
    # cross-encoder, both min-max scaled over the scored chunks. (Using the cross-encoder alone measured worse: 79.7 % vs
    # 82.6 % first-rank.)
    rerank_blend: float = 0.5
    # Which questions the reranker sees. A cross-encoder trained on questions reads a bag of keywords badly (measured:
    # it lowered top-1 accuracy on keyword queries and raised it on full questions), and a question that names a code
    # (INV-0070, CTR-2025-2186-TD) is an exact lookup keyword search already answers. "on": skip questions shorter than
    # rerank_min_words and questions with a code. "off": rerank everything. "auto": on for the local model (these
    # numbers were measured on it), off for a remote reranker URL (not measured -- set "on" if you measure it).
    rerank_gate: str = "auto"
    rerank_min_words: int = 6
    rerank_skip_identifiers: bool = True

    # --- Chunking (word-based approximation of tokens) ---
    chunk_words: int = 160               # sized for the default model, which reads 256 tokens; use 320 / 40 with BGE-M3
    chunk_overlap_words: int = 20
    min_chunk_words: int = 12

    # --- Text analysis: changes WHICH TERMS are indexed, so it is part of the collection name (change = re-index) ---
    stem_langs: str = "en,ar"           # stem these languages (terminated / termination meet); "" = off. Script-gated: English rules touch only Latin words, Arabic rules only Arabic
    sparse_avgdl: float = 260.0         # BM25 length normalisation: set near the typical number of terms per chunk
    ctx_dense: bool = True              # put "name | title | folder | heading" in front of the text that is EMBEDDED
    ctx_sparse: bool = True             # ... and in front of the text that is KEYWORD-indexed
    query_stopwords: bool = True        # drop function / question words from the SEARCH side only

    # --- Retrieval ---
    dense_k: int = 60
    sparse_k: int = 60
    # Chunks are scored by weighted min-max fusion: w*dense + (1-w)*keyword. This keeps the raw cosine and BM25 for the
    # confidence check and measured best on real documents and on exact-identifier lookups (docs/scale/BENCHMARKS.md).
    fusion_dense_weight: float = 0.3    # share of the dense (meaning) score; the rest is keyword score
    # onnx: a local cross-encoder blended with the retrieval score (+1.0 point first-rank measured; the models are
    # fetched by scripts/get-models.sh --reranker). none: no reranking. If the model files are missing the search runs
    # without it and says so in the log.
    rerank_mode: str = "onnx"           # none | onnx
    # "Nothing relevant" check, all 0 = off Three independent
    # signals about the best chunk: how close it is in MEANING (top cosine), how strongly it matches the KEYWORDS (top
    # BM25), and how much of the question's wording it COVERS (0-1). The search returns no documents instead of forty
    # irrelevant ones only when every signal you switched on is weak. An identifier lookup is weak in meaning but strong
    # in keywords; a paraphrase is the opposite; only a question the corpus knows nothing about is weak on all three.
    # The values depend on the embedding model and the corpus: calibrate with scripts/scale_calibrate.py.
    min_confidence: float = 0.0
    min_lexical: float = 0.0
    min_coverage: float = 0.0
    final_k: int = 20
    # How the agent's search_content merges the existing Postgres keyword search with the hybrid index:
    #   union  keep the keyword hits first, append hybrid-only hits. This is the safe default because the
    #          project's own testing found rank fusion pushed good keyword hits out on near-duplicates.
    #   hybrid use the hybrid ranking only.
    # Decide with scripts/scale_eval.py on YOUR golden set, not by opinion.
    merge_mode: str = "union"
    # Below this many visible chunks the meaning search is exact (see index._dense_search_params). 0 = always the graph search.
    exact_below: int = 40000
    exact_probe_ttl_s: float = 30.0
    # --- Self-healing index (app/scale/reconcile.py): compare Postgres with the chunk index and repair the difference ---
    reconcile_enabled: bool = True
    reconcile_interval_hours: float = 24.0   # every document is compared with the index at least this often
    reconcile_batch: int = 500               # documents compared per index round-trip
    reconcile_seconds: float = 20.0          # time one scheduled run may spend (it continues on the next tick)
    reconcile_requeue_cap: int = 2000        # most documents one run sends back to be re-embedded (the rest follow next run)
    reconcile_grace_minutes: float = 3.0     # a document indexed this recently is not judged yet (a write may be in flight)
    reconcile_orphans_every_hours: float = 24.0   # sweep for chunks whose document no longer exists
    reconcile_quick_check_seconds: float = 300.0   # how often the whole-index size check runs
    reconcile_short_ratio: float = 0.97      # fewer chunks than this share of the expected count = the index was damaged
    job_retention_days: float = 7.0          # finished jobs older than this are deleted (0 = keep forever)
    dead_job_retention_days: float = 60.0    # jobs that gave up are kept longer so they can still be retried (0 = keep forever)
    reconcile_orphan_scan_points: int = 250000   # chunks examined per orphan-sweep run; the sweep resumes where it stopped
    # Near-identical copies of one document (drafts, the same file in several folders) rank together; at most this many of a family keep
    # their place in the candidate list, the rest move behind the other documents (nothing is dropped). 0 = off. See families.py.
    family_cap: int = 3
    family_similarity: float = 0.8
    candidate_docs: int = 40          # documents handed to the agent after stage A
    candidate_chunks: int = 120       # chunks examined to choose those documents
    scope_filter_max: int = 5000      # a user's document selection up to this long is applied inside the index query
    scope_partition_max: int = 40     # a longer selection is searched in parts of scope_filter_max, this many parts at most
    # GET /api/documents with no `limit` (what the current UI sends) returns up to this many of the newest
    # documents the user may read. New clients should page with ?limit=&offset= (max 1000 per page).
    legacy_list_limit: int = 5000

    # --- Access control ---
    # Sharing-link scopes treated as a grant. Empty (default) = links never widen access.
    link_scopes_as_access: list[str] = Field(default_factory=list)
    # Hard cap on principals carried by one user at query time (guards the filter size).
    max_principals: int = 2000
    # False (default): the app's 'admin' role manages the system but does NOT get to read every
    # source file; an admin sees only what their own principals allow. True restores the old behaviour.
    admin_bypass_acl: bool = False

    # --- Queue / worker ---
    worker_concurrency: int = 4
    lease_seconds: int = 300
    poll_seconds: float = 1.0
    max_attempts: int = 5
    retry_base_seconds: int = 30

    max_queued_jobs: int = 200_000      # scanning pauses (and resumes from its cursor) above this
    scan_interval_seconds: int = 900
    acl_sweep_hours: float = 24.0       # re-read permissions of files not checked for this long
    failed_retry_hours: float = 24.0    # a scan re-queues a failed file after this long (requeue-dead retries sooner)

    # --- Pipeline policy ---
    max_file_mb: int = 200
    # Admin "upload files" source: where the files are kept (default: <RAG_STORAGE_PATH>/scale-uploads, a volume the
    # API and the worker share), how much one request may carry and how many files.
    upload_dir: str = ""
    upload_request_mb: int = 512
    upload_max_files: int = 100
    # With TREE_GENERATION=true, source files of at least this many pages are queued for the full section tree (LLM cost!).
    # 0 or 1 = every file. Has no effect with TREE_GENERATION=false.
    auto_promote_min_pages: int = 0
    # A PDF whose text layer averages fewer words per page than this (and under 200 words in total) is
    # treated as scanned. Page numbers and stamps alone are ~5 words, so 8 separates them from prose.
    min_words_per_page: int = 8
    # --- Vision reading of pages the local parsers cannot read (a model call per page; needs OPENROUTER_API_KEY and MODEL_VISION) ---
    # true : a PDF is read with the ordinary local reader first. Pages that have no text layer (scans, photos, CAD sheets exported as
    #        pictures) and pages that are mostly one large picture with little text (a brochure floor plan, a map) are then read by the
    #        vision model, and image files (png / jpg / tif / bmp / webp) are accepted. Pages that already have their text are NOT
    #        sent: a picture inside a text page (a chart in a paper) stays unread, the page's own text is what is indexed.
    # false: such pages are not read: a PDF with no text at all is skipped (needs_ocr), image files are refused, and a mixed PDF
    #        is indexed from its text pages only.
    # Ignored while SCALE_DOCPARSE_MODE reads scans locally. The page rules and the per-document cap below keep the bill small.
    ocr_enabled: bool = False
    vision_min_chars: int = 60          # a page with fewer characters than this has no usable text layer
    vision_image_ratio: float = 0.35    # a page whose images cover at least this share of it (and has under vision_max_text_chars) is sent
    vision_max_text_chars: int = 1500
    vision_max_pages: int = 40          # at most this many pages of one document go to the model; the rest keep their own text
    # --- docparse: local layout analysis + OCR + table structure (no LLM, no network; see docs/scale/DOCPARSE.md) ---
    #   off      scanned PDFs follow ocr_enabled (a vision-model call per page); image files are not accepted
    #   scanned  (default) PDFs with no text layer, and image files (png / jpg / tif / bmp / webp), are OCR'd and laid out
    #            locally; if the packages are missing it says so in the log and falls back to the off behaviour
    #   all      every PDF page also goes through layout analysis (reading order across columns, tables as rows,
    #            running headers / footers / page numbers removed), using the PDF's own text where it has any
    docparse_mode: str = "scanned"
    docparse_ocr: str = "auto"         # auto: the text layer where a page has one, OCR otherwise | always | never
    docparse_layout: bool = True
    docparse_tables: bool = True
    docparse_drop_furniture: bool = True
    docparse_restore_spaces: bool = True
    docparse_deskew: bool = True
    docparse_table_style: str = "labeled"   # labeled: "Item: Widget B | Price: 99.00" per row | markdown
    docparse_dpi: int = 150
    docparse_max_pages: int = 300
    docparse_threads: int = 2
    docparse_det_box_thresh: float = 0.3   # the OCR text detector's box threshold (its own default 0.5 drops faint lines); see docs/scale/DOCPARSE.md
    docparse_det_unclip: float = 1.8
    docparse_det_limit_side: int = 0          # 0 = the engine's own (736, scale up small images). 960 with type "max" (scale down large ones) made no measurable difference
    docparse_det_limit_type: str = "min"      # min | max
    # Legacy Office files (doc xls ppt ods odp ...) are converted to their modern form by LibreOffice (headless).
    # Installed by default in the Docker images (INSTALL_LEGACY_OFFICE=1); if soffice is missing these files are skipped
    # with a clear reason.
    legacy_convert_timeout_s: float = 180.0

    # --- A language model for the optional features below (enrichment and query rewriting) ---
    # Everything that needs a model is OFF by default, and every one of those features degrades to "not there" or to a
    # no-model fallback when the model is missing or fails: ingestion and search never depend on it.
    #   off     no model
    #   repo    the application's own gateway (OPENROUTER_BASE_URL / OPENROUTER_API_KEY) and its RAG_INDEX_MODEL
    #   openai  any OpenAI-compatible /chat/completions at SCALE_LLM_BASE_URL: Ollama, vLLM, llama.cpp server, LM Studio, OpenAI ...
    llm_mode: str = "off"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 120.0
    llm_max_tokens: int = 1024
    llm_concurrency: int = 4           # parallel requests while enriching one document
    llm_retries: int = 2

    # --- Enrichment (needs the model): extra search terms per chunk, and a smarter question ---
    enrich_keywords: int = 0           # N keywords per chunk, added to the keyword index (0 = off)
    enrich_questions: int = 0          # N questions each chunk answers, added to the keyword index (0 = off)
    enrich_dense: bool = False         # also put the keywords / questions in front of the text that is embedded
    enrich_max_chunks: int = 3000      # a document with more chunks than this is not enriched (cost guard)
    query_keywords: bool = False       # the model adds the question's key terms to the keyword side of the search
    query_translate: str = ""          # e.g. "ar,en": the model also searches the question translated into these languages
    query_llm_timeout_s: float = 8.0   # a slow model must not stall a search: past this the plain question is used

    # --- Chunk editor ---
    chunk_edit_overlay: bool = True    # the agent's keyword search shows the corrected text of edited chunks

    # Graph / SMB credentials are read from env vars named in the connection config, never stored in the DB.


@lru_cache
def get_scale_settings() -> ScaleSettings:
    return ScaleSettings()
