# Models

`.env.dev` (or `.env.prod`) is the **only** place these are set.
`backend/app/core/config.py` has no Python-side default for any of the five
model fields below -- the app raises a `pydantic.ValidationError` and
refuses to start if one is missing, on purpose, so a model choice can never
be silently wrong. To change a model, edit `.env.dev` and restart the
backend container; nothing in the Python code needs to change.

## The five settings

| Env var | Current value | Drives |
|---|---|---|
| `RAG_INDEX_MODEL` | `openai/gpt-4o-mini` | Tree building: node summaries + document description, at ingestion time |
| `RAG_CHAT_MODEL` | `openai/gpt-5-mini` | The tree-search agent that answers a chat query |
| `MODEL_VISION` | `anthropic/claude-haiku-5.5` | Diagram OCR/caption/description at ingestion; citation verification at query time |
| `MODEL_EMBEDDING` | `openai/text-embedding-3-small` | Diagram vectors (Qdrant) |
| `MODEL_EMBEDDING_DIMENSION` | `1536` | Must match `MODEL_EMBEDDING`'s real output size -- Qdrant's collection is created with this dimension |

Index runs `openai/gpt-4o-mini`; chat runs `openai/gpt-5-mini`. Vision is its
own model (`anthropic/claude-haiku-5.5`), used only for diagram
OCR/caption/description at ingestion and citation verification at query time.
**`MODEL_EMBEDDING` can't be either of those** -- it's a chat-completion
model, not an embedding model, and OpenRouter's `/embeddings` endpoint will
reject it. It has to stay a real embedding model; `text-embedding-3-small`
is the only one currently wired up (`app/retrieval/embeddings.py`).

Every one of these routes through OpenRouter, using your `OPENROUTER_API_KEY`.
`RAG_INDEX_MODEL`/`RAG_CHAT_MODEL` get an extra `openrouter/` prefix applied
internally (`app/rag_service.py`) so the vendored tree engine's litellm
calls route through OpenRouter instead of straight to the provider named in
the slug -- `.env.dev` keeps the bare slug either way.

## Exact call sites (grep-verified, not assumed)

```
RAG_INDEX_MODEL / RAG_CHAT_MODEL -> app/rag_service.py (client construction only)
MODEL_VISION                     -> app/ingestion/diagram_pipeline.py, app/retrieval/vision_verify.py
MODEL_EMBEDDING                  -> app/ingestion/diagram_pipeline.py, app/retrieval/embeddings.py, app/retrieval/vector_store.py
```

`RAG_INDEX_MODEL`/`RAG_CHAT_MODEL` are only ever read in one place
(`rag_service.get_client()`) -- from there on, every actual LLM call goes
through the **vendored tree engine's own code** (`app/rag_core/`), not
anything this app wrote. See `docs/ARCHITECTURE.md` for what's vendored
vs. custom.

## How many LLM calls, per operation

These come straight from the vendored engine's source
(`app/rag_core/utils.py::summarize_tree`, `local_chat.py`), not a guess:

**Ingesting one document (RAG_INDEX_MODEL):** this whole block applies only when `TREE_GENERATION=true`. With `TREE_GENERATION=false` (`.env`) no tree is built and **no model is called at ingest for any file, from the UI or from a source, even with a key set**; the pages are stored and chunked as usual. With `true`, UI uploads and source files both get a tree.
- Flash mode calls `_index_flash()` with `optimize="full"`
  (`app/rag_core/local_api.py`), which runs an **expand pass** before
  summarization: any node whose page span exceeds `TRIGGER_PAGES = 5`
  (`rag_core/tree_optimize.py`) gets one model call proposing subsection
  splits (`propose_children` / `EXPAND_PROMPT`). Most short/flat documents
  (our CSV/XLSX/EML/TXT/JSON/XER renders, and short DOCX/PDF files) never
  trigger this -- every node stays under 5 pages. Long, unstructured
  documents can.
- One call per tree node for its summary -- *except* a leaf node whose raw
  text is under ~200 GPT-4o tokens, which reuses that text as its summary
  with **no model call** (`SUMMARY_RAW_TEXT_TOKENS` in `rag_core/utils.py`).
  Node summary calls run concurrently, up to 64 at a time
  (`SUMMARY_CONCURRENCY`).
- Plus exactly **one** more call for the document's one-sentence description
  (`generate_doc_description`).
- So: **(expand-pass calls, usually 0) + (tree nodes above the small-leaf
  threshold) + 1.** A short document (a few sections) might be 1-6 calls
  total; a long, deeply-sectioned one could be dozens.
- **Failure mode, not just a cost variable:** if flash mode finds no layout
  structure at all, it falls back to one node per page
  (`_page_nodes()`, `flash/api.py`). If that fallback produces **more than
  `FLAT_TREE_MAX_NODES = 10`** nodes, `tree_flash()` returns early
  -- skipping optimize *and* summarize entirely -- and `local_api.py`
  raises, so the document **fails to ingest** rather than costing more.
  This only bites a long, genuinely unstructured document (no headings
  detected); confirmed by reading `flash/api.py` and
  `flash_rejection_reason()` directly, not inferred from behavior.

**Ingesting one scanned/image-only page (MODEL_VISION + MODEL_EMBEDDING):**
- **One** vision call per page (OCR text + caption + description + figure
  id + callouts, all in one call -- `diagram_pipeline.py`), run in
  parallel across pages, four at a time
  (`_MAX_CONCURRENT_VISION_CALLS = 4`).
- **One** embedding call per page.
- The resulting OCR text is then synthesized into a PDF and goes through
  the same tree-indexing pass above (so add that document's node-count
  calls on top).

**One chat query (RAG_CHAT_MODEL):**
- The tree-search agent runs 1 to `RAG_CHAT_MAX_TURNS` turns (default 16;
  the vendored engine's own default of 10 ran out on a 1,011-document
  library) before answering -- each turn is roughly one model
  call, and the agent stops as soon as it has enough to answer, so a
  simple question is often 1-3 calls, not 10. There's no way to pin this
  to an exact number ahead of time; it's genuinely agentic.
- If the query looks visual ("diagram", "drawing", "show me", ...): +1
  embedding call (the query) + up to 5 vision calls in parallel (one per
  Qdrant candidate, verifying each against its real page image before it's
  allowed to become a citation -- `vision_verify.py`).

Nothing here is metered or capped beyond `max_turns` -- if you need a hard
ceiling on spend, that's a gap to close before any real usage, not
something the current code enforces.


## Enterprise layer: models and calls

The enterprise layer (`SCALE_*` settings, `docs/scale/README.md`) adds models that run **locally** by default, so it adds no
paid calls:

| Job | Default | Alternatives |
|---|---|---|
| Chunk and question embeddings | BGE-small ONNX on CPU (`SCALE_EMBED_BACKEND=onnx`, 384 dimensions, `SCALE_ONNX_MODEL_DIR`) | BGE-M3 through the model server (`--models`, `SCALE_EMBED_BACKEND=openai`, 1024 dimensions, multilingual); any OpenAI-compatible embedding endpoint |
| Reranker | ms-marco-MiniLM cross-encoder on CPU, blended with the fused score (`SCALE_RERANK_MODE=onnx`; downloaded by `scripts/get-models.sh`; if the files are missing search runs without it and the log says so) | `none`; `SCALE_RERANK_URL`: bge-reranker-v2-m3 server (`--models`) |
| OCR, layout, tables for scanned PDFs and images | `scanned` (`SCALE_DOCPARSE_MODE`): PP-OCRv4, layout and SLANet table models on CPU, built into the images | `all` (every PDF gets layout analysis); `off`: scanned PDFs go to the vision model, one call per page (`MODEL_VISION`), which also captions diagrams for visual search; images are then refused |
| Legacy Office files (.doc .xls .ppt .ods .odp) | LibreOffice, headless, inside the image (converted, then read like .docx / .xlsx / .pptx) | `--slim` build: these files are skipped with a reason |
| Optional model-assisted features (`SCALE_LLM_MODE`) | off | `repo`: the application's own OpenRouter gateway; `openai`: any OpenAI-compatible server (Ollama, vLLM, LM Studio) |

**Calls per operation with the defaults**

* Uploading or scanning one file: the enterprise layer makes **no** language-model call; it embeds chunks locally. If the
  file also gets a tree in the agent path, that is the tree-building calls described above. A scanned page sent through the
  vision path (`SCALE_DOCPARSE_MODE=off`) costs one vision call (and one embedding call per diagram page); with the default
  `scanned` the local OCR models are used instead and no model call is made. The tree is still built with the index model.
* Asking one question: Stage A makes no language-model call (one local query embedding, one Qdrant query, one Postgres
  check). Stage B is the agent's 1 to `RAG_CHAT_MAX_TURNS` calls as described above, so a question over millions of files
  costs about the same as a question over a few.
* With the optional features on: keyword or question enrichment is one call per chunk; query rewriting one call per question. All are off by default (`docs/scale/LLM_FEATURES.md`).
