"""
Query-time orchestration: drives the tree engine's own agentic search
(app/rag_service.chat_stream) across every accessible document in one call,
bridges its synchronous event stream into an async generator the API layer
can turn into SSE, and -- for visual-sounding queries -- runs the diagram
path (Qdrant search + vision verification) alongside it.

The bridge (thread + asyncio.Queue) exists because chat_stream() is a
synchronous generator making blocking network calls under the hood (see
app/rag_service.py's module docstring) -- consuming it directly on the
event loop would stall every other request.
"""

import asyncio
import json
import logging
import re
import threading
import uuid
from typing import Any, AsyncIterator

from app.core.config import get_settings
from app.core.guardrails import (
    EVIDENCE_TOOLS,
    GENERIC_ERROR,
    METADATA_TOOLS,
    REFUSAL_OFF_TOPIC,
    SEARCH_LIMIT_REPLY,
    leaks_internals,
)
from app.rag_core.errors import MaxTurnsError
from app.rag_service import chat_stream, resolve_citations
from app.retrieval.diagram_search import diagram_search
from app.retrieval.embeddings import embed_text
from app.retrieval.vision_verify import vision_verify

log = logging.getLogger(__name__)

_VISUAL_WORDS = ("diagram", "drawing", "show me", "figure", "image", "photo", "picture", "plan", "sheet")


def _looks_visual(query: str) -> bool:
    lowered = query.lower()
    return any(w in lowered for w in _VISUAL_WORDS)


def _bridge_sync_stream(
    query: str, rag_doc_ids: list[str], history: list[dict[str, str]] | None
) -> AsyncIterator[dict]:
    """Runs chat_stream() on a worker thread, forwarding each event into an
    asyncio.Queue this coroutine can await -- turns a blocking sync
    generator into a proper async iterator."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def worker() -> None:
        try:
            for event in chat_stream(query, rag_doc_ids, history):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except MaxTurnsError:
            # Not a crash: the agent kept searching and found nothing conclusive. Say so instead of "something went wrong".
            log.warning("chat agent ran out of steps without an answer: %r", query[:200])
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "out_of_steps"})
        except Exception:  # noqa: BLE001 -- logged here, generic message to the client
            log.exception("chat engine failed")
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": GENERIC_ERROR})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    threading.Thread(target=worker, daemon=True).start()

    async def generator() -> AsyncIterator[dict]:
        while True:
            event = await queue.get()
            if event is _DONE:
                return
            yield event

    return generator()


async def _diagram_evidence(query: str, document_ids: list[uuid.UUID]) -> list[dict]:
    query_vector = await embed_text(query)
    candidates = await diagram_search(query_vector, k=5, document_ids=document_ids)
    return await vision_verify(query, candidates)


def diagrams_for_cited_documents(diagrams: list[dict], citations: list[dict]) -> list[dict]:
    """The reference images that belong to the answer. The diagram search ranks every selected document, so a question about one
    project can bring back a near-identical page of another (same title, other numbers); when the answer cites documents, only
    those documents' images are shown. With no citations (nothing to go by) the list is left as it is."""
    cited = {str(c["document_id"]) for c in citations if c.get("document_id")}
    if not cited:
        return diagrams
    return [d for d in diagrams if str(d.get("document_id")) in cited]


def _tool_succeeded(output: Any) -> bool:
    """Tool outputs arrive as {"type": "text", "text": "<json>"}; a failed call has success=false."""
    text = output.get("text") if isinstance(output, dict) else output
    if not isinstance(text, str):
        return bool(text)
    try:
        return bool(json.loads(text).get("success", True))
    except (ValueError, AttributeError):
        return True


# Questions about the library itself ("how many files do we have?") are answerable from a
# listing alone, even when the reply is just a number.
_LIBRARY_QUESTION = re.compile(r"\b(documents?|files?|sources?|library|knowledge base|folders?|uploaded)\b")

_NAME_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _document_names(output: Any) -> set[str]:
    """Lower-cased names (and readable variants) of the documents a listing tool returned."""
    text = output.get("text") if isinstance(output, dict) else output
    if not isinstance(text, str):
        return set()
    names: set[str] = set()
    for raw in _NAME_RE.findall(text):
        try:
            name = json.loads(f'"{raw}"').lower()
        except ValueError:
            name = raw.lower()
        stem = name.rsplit(".", 1)[0]
        names.update({name, stem, re.sub(r"[_\-]+", " ", stem)})
    return {n for n in names if len(n) >= 3}


async def _safe_diagrams(task: "asyncio.Task[list[dict]] | None") -> list[dict]:
    if task is None:
        return []
    try:
        return await task
    except Exception:  # noqa: BLE001 -- diagrams are a bonus; never break the answer
        log.exception("diagram evidence failed")
        return []


async def stream_chat(
    query: str,
    rag_doc_ids: list[str],
    document_ids: list[uuid.UUID],
    history: list[dict[str, str]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yields events: tool_call/tool_result/thinking/answer (from the tree
    engine, verbatim -- see rag_service.chat_stream's docstring for shapes),
    then a final "citations" event ({"citations": [...], "diagrams": [...]})
    once the answer is complete, then "done".

    document_ids: our own document ids (not the tree engine's rag_doc_id
    strings) for the exact set of documents this call is allowed to touch
    -- Qdrant's diagram payloads are keyed by this id, and the diagram
    search used to run unfiltered across the whole collection regardless
    of what was selected. Always a concrete list here (api/chat.py already
    resolves "no selection" down to every indexed document before calling
    this), so the diagram path stays scoped exactly like the tree-search
    agent's own document allowlist.

    history: prior turns of this session, oldest first, NOT including
    `query` -- see rag_service.chat_stream's docstring."""
    diagram_task = (asyncio.create_task(_diagram_evidence(query, document_ids))
                    if get_settings().store_diagrams and _looks_visual(query) else None)

    answer_parts: list[str] = []
    held: list[dict] = []  # answer deltas withheld until document content has actually been read
    evidence = False
    known_names: set[str] = set()  # documents named by listing tools (metadata, not content)
    listed = False
    errored = False
    out_of_steps = False
    async for event in _bridge_sync_stream(query, rag_doc_ids, history):
        event_type = event.get("type")
        if event_type == "out_of_steps":
            out_of_steps = True
            continue
        if event_type == "thinking":
            continue  # model reasoning is never sent to the browser
        if event_type == "tool_result":
            name = event.get("name")
            if name in EVIDENCE_TOOLS and _tool_succeeded(event.get("output")):
                evidence = True
            elif name in METADATA_TOOLS and _tool_succeeded(event.get("output")):
                listed = True
                known_names |= _document_names(event.get("output"))
            # The browser never uses raw tool output (up to ~95 KB of page text per call).
            yield {"type": "tool_result", "call_id": event.get("call_id"), "name": name, "output": {"ok": True}}
            if evidence and held:
                for pending in held:
                    yield pending
                held.clear()
            continue
        if event_type == "error":
            errored = True
            log.warning("engine reported an error: %s", event.get("message"))
            yield {"type": "error", "message": GENERIC_ERROR}
            continue
        if event_type == "answer" and event.get("delta"):
            answer_parts.append(event["delta"])
            if not evidence:
                held.append(event)
                continue
        yield event

    answer_text = "".join(answer_parts)

    if out_of_steps:
        # The agent searched until it ran out of steps. Whatever it wrote before that is kept only if it had read document
        # content; either way the user is told plainly, not shown a generic error or the off-topic refusal.
        if diagram_task:
            diagram_task.cancel()
        kept = answer_text if evidence else ""
        if evidence:
            for pending in held:
                yield pending
        held.clear()
        separator = "\n\n" if kept.strip() else ""
        reply = (kept.rstrip() + separator if kept.strip() else "") + SEARCH_LIMIT_REPLY
        yield {"type": "answer", "delta": separator + SEARCH_LIMIT_REPLY}
        yield {"type": "citations", "citations": [], "diagrams": [], "resolved_answer": reply, "refused": not evidence}
        yield {"type": "done"}
        return

    # Evidence gate: an answer that was produced without reading any document
    # content is general-knowledge chatter (or an injection), not a grounded
    # answer -- replace it. Errors keep their own path (no evidence is expected).
    answer_lower = answer_text.lower()
    grounded_by_listing = listed and (
        any(name in answer_lower for name in known_names) or bool(_LIBRARY_QUESTION.search(query.lower()))
    )
    if not errored and (not (evidence or grounded_by_listing) or leaks_internals(answer_text)):
        if diagram_task:
            diagram_task.cancel()
        yield {
            "type": "citations",
            "citations": [],
            "diagrams": [],
            "resolved_answer": REFUSAL_OFF_TOPIC,
            "refused": True,
        }
        yield {"type": "done"}
        return

    # Answers grounded only by a listing (held back until now) are released here.
    for pending in held:
        yield pending
    held.clear()

    citations: list[dict] = []
    resolved_answer = answer_text
    if answer_text.strip():
        resolved = await asyncio.to_thread(resolve_citations, answer_text, rag_doc_ids)
        citations = resolved.get("citations", [])
        # "[[1]](#rag-citation-01)"-style numbered links, one per
        # distinct citation, replacing the inline <cite .../> tags -- lets
        # the frontend render a clickable pill at the exact point in the
        # sentence the model cited, not just a chip list at the end.
        resolved_answer = resolved.get("answer", answer_text)

    diagrams = await _safe_diagrams(diagram_task)

    yield {"type": "citations", "citations": citations, "diagrams": diagrams, "resolved_answer": resolved_answer}
    yield {"type": "done"}
