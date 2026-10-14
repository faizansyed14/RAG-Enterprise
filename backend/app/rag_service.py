"""
Thin wiring layer around the vendored tree-indexing/tree-search engine
(app/rag_core/ -- a full copy of the local-mode-relevant engine source,
renamed to this project's naming, kept under our own control rather than
as a pip dependency). Local mode only -- no cloud call, no cloud API key,
anywhere in this app.

Facts this module leans on, straight from that vendored source:

- Document submission is **local-mode PDF-only**. DOCX, CSV, XLSX, EML,
  TXT, JSON, XER, and scanned pages never go in directly -- see
  ingestion/blocks_ingest.py (shared render+submit path), one
  ingestion/<format>_ingest.py per format (extraction only -- docx, csv,
  xlsx, eml, txt, json, xer), and ingestion/pdf_ingest.py, which all render
  to a real PDF first (ingestion/text_to_pdf.py).
- Indexing is **synchronous** in local mode: submission blocks until the
  tree is built and returns a doc id. There is no polling.
- The node-summary model auto-inherits from the tree-build model when not
  set separately -- pinning one index-side model is enough.
- Local-mode LLM calls route through litellm, which treats a
  `provider/model` string's prefix as *its own* provider key, not
  OpenRouter's vendor namespace -- "qwen/qwen3-vl-..." would 404 ("qwen"
  isn't a litellm provider). Routing through OpenRouter needs litellm's
  `openrouter/` provider prefix, which reads OPENROUTER_API_KEY from the
  environment automatically. That prefixing happens only here;
  core/config.py keeps the bare pinned slug.
- Asking a question with a list of document ids runs the engine's own
  agentic tool-calling search *across every id in the list in one call* --
  it decides which document(s) to read via its own search/read tools,
  enforced at the tool layer. This is why retrieval/chat_service.py never
  loops one call per document.
- Citations are inline tags the model writes into the answer text (asking
  with citations enabled adds the citation-discipline system prompt); a
  separate resolve call parses them back out into structured entries with
  real page numbers.
- Streaming returns a synchronous iterator of typed events (thinking/answer
  deltas, tool_call, tool_result). Consuming it makes blocking network
  calls, so callers (retrieval/chat_service.py) run it on a thread and
  bridge to asyncio.
"""

from functools import lru_cache
from typing import Any, Iterator

from app.rag_core import RagEngineClient

from app.core.config import get_settings


def _openrouter(model: str) -> str:
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


@lru_cache
def get_client() -> RagEngineClient:
    settings = get_settings()
    return RagEngineClient(
        index_model=_openrouter(settings.rag_index_model),
        chat_model=_openrouter(settings.rag_chat_model),
        storage_path=settings.rag_storage_path,
    )


def submit_pdf(file_path: str, mode: str = "flash") -> dict[str, Any]:
    """Blocks until indexed (local mode is synchronous). mode: "flash" (default,
    fast) or "standard" (fuller LLM-built tree, slower). Returns {"doc_id", "name"}."""
    return get_client().submit_document(file_path, mode=mode)


def get_tree(doc_id: str) -> dict[str, Any]:
    """{'doc_id','status','retrieval_ready','result': [tree nodes]} -- each
    node: {'title','node_id','page_number',('summary'|'prefix_summary',)'nodes'}."""
    return get_client().get_tree(doc_id, node_summary=True, include_text=False)


def get_index_dump(doc_id: str) -> dict[str, Any]:
    """Everything the local tree store holds for one doc: metadata, per-page
    markdown chunks (pages.json), node-level chunks, and the tree with text."""
    client = get_client()
    meta = client.get_document(doc_id)
    pages = client.get_ocr(doc_id, format="page")
    try:
        node_ocr = client.get_ocr(doc_id, format="node")
        node_chunks = node_ocr.get("result") or []
    except Exception:  # noqa: BLE001 -- node format optional
        node_chunks = []
    tree = client.get_tree(doc_id, node_summary=True, include_text=True)
    return {
        "rag_doc_id": doc_id,
        "meta": meta,
        "pages": pages.get("result") or [],
        "node_chunks": node_chunks,
        "tree": tree.get("result") or [],
    }


def delete_document(doc_id: str) -> None:
    get_client().delete_document(doc_id)


def chat_stream(query: str, doc_ids: list[str], history: list[dict[str, str]] | None = None) -> Iterator[dict]:
    """Synchronous generator of typed events -- run on a thread; see module
    docstring. {"type": "thinking"|"answer", "delta": ...} | {"type":
    "tool_call", "call_id", "name", "arguments"} | {"type": "tool_result",
    "call_id", "name", "output"}.

    history: prior {"role": "user"|"assistant", "content": str} turns,
    oldest first, NOT including `query`. The engine takes multi-turn as one
    role/content list (see client.py's chat() docstring); passing history
    only when present keeps a fresh chat's citation-prompt/instructions
    request byte-identical to before this was added."""
    messages: str | list[dict[str, str]] = (
        [*history, {"role": "user", "content": query}] if history else query
    )
    settings = get_settings()
    # Both optional knobs are only sent when set (empty env value == unset): by default the
    # request is exactly what it was before -- the model's own reasoning depth, the engine's
    # own turn limit raised only to the configured headroom.
    kwargs: dict[str, Any] = {"max_turns": settings.rag_chat_max_turns}
    if settings.rag_chat_reasoning_effort:
        kwargs["reasoning_effort"] = settings.rag_chat_reasoning_effort
    stream = get_client().chat(messages, doc_id=doc_ids, stream=True, citations=True, **kwargs)
    yield from stream.events


def resolve_citations(answer: str, doc_ids: list[str]) -> dict[str, Any]:
    """{'answer': <tags replaced with numbered links>, 'citations': [{'anchor',
    'index','document','doc_id','page', ...}]}."""
    return get_client().resolve_citations(answer, doc_id=doc_ids)
