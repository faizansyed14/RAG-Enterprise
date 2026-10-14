"""Agent tools: the cloud MCP tool contract, executed against a RagEngineClient.

Tool names and the surviving input-schema structure follow the
hosted tool contract — the local surface hides the documented cloud-only
parameters — so agent prompts port across the cloud MCP connection and
this in-process layer. Only the tools that exist in every mode are
registered (no folders, search_documents, or get_document_image), and the
guidance strings (tool descriptions) adapt to the local surface the same
way the agent instructions do — they never teach capabilities that only
exist on the cloud.

Tools never raise: every outcome, including errors, is returned as the
same JSON envelope the cloud emits ({"success": true, ...} /
{"error": ...}) — arguments outside a pruned local signature come back as
that envelope too, on the direct and the call_tool path alike, except
browse_documents' ``recursive``: call_tool honors it, because the flat
no-folders shape it asks for is trivially true here. The exceptions to
never-raise, all cloud: a 401/403, a 429/5xx that outlived the bridge's
retries, an unreachable server and a RATE_LIMITED / USAGE_LIMIT_REACHED
tool error re-raise RagEngineError.
"""
from __future__ import annotations

import copy
import difflib
import inspect
import json
import re
import threading
import time
import weakref
from typing import Any, Callable, Optional

import requests

from .errors import RagEngineError
from .mcp_bridge import render_prompt_text, render_text

TOOL_RESPONSE_CHAR_LIMIT = 100_000
STRUCTURE_FIRST_PAGE_THRESHOLD = 20

_CHAR_BUDGET = int(TOOL_RESPONSE_CHAR_LIMIT * 0.95)
_MAX_REQUESTED_PAGES = 10_000
_SIMILAR_NAMES_LIMIT = 3
_TOOL_WAIT_TIMEOUT = 180.0  # "up to 3 minutes", per the wait_for_completion schema
_TOOL_WAIT_INTERVAL = 5.0

_DOC_NAME_DESCRIPTION = (
    'Copy the `name` field verbatim from a browse_documents() or '
    'search_documents() response (case-sensitive, include extension). '
    'Example: "Q3 Report.pdf". If the response shows two documents with the '
    'same name, pass `folder_id` alongside to disambiguate.'
)
_FOLDER_ID_DISAMBIGUATOR_DESCRIPTION = (
    'Disambiguator for same-name documents. Copy the `folder_id` from the '
    'intended browse/search result; use "root" for root-level documents, or '
    '"shared-with-me"/"following" for the read-only folders at the library '
    'root; omit if `doc_name` is unique. Copy any folder_id verbatim from a '
    'browse_documents()/get_folder_structure() response, never construct one.'
)
_WAIT_FOR_COMPLETION_DESCRIPTION = (
    "If true and document is processing, automatically wait up to 3 minutes "
    "until completed. Reduces repeated tool calls."
)

#: Tool names, descriptions, and parameter schemas, identical to the cloud
#: MCP server's tools/list.
TOOL_CONTRACT: dict[str, dict[str, Any]] = {
    "browse_documents": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Primary document retrieval tool. After orienting with "
            "get_folder_structure() (when available), use this for all "
            "document-related questions. The bare call returns root-level "
            "sub-folders and documents; pass folder_id to drill into a "
            'sub-folder level by level. Use sort="relevance" + query for '
            "semantic ranking. Do NOT jump to search_documents() first — it "
            "is an escalation path, only after "
            'browse_documents(sort="relevance") has failed.'
        ),
        "schema": {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "default": "root",
                    "description": (
                        'Folder scope (default "root"). Pass a specific folder '
                        'ID to scope into that folder, or "root" to reference '
                        "the library root. The read-only \"shared-with-me\" and "
                        '"following" folders live at the library root — pass '
                        "one of those ids to browse them. Copy any folder_id "
                        "verbatim from a browse/tree response, never construct "
                        "one. Combine with `recursive` to control breadth."
                    ),
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Whether to include documents from descendant folders. "
                        "When false (default), returns the direct contents of "
                        "folder_id along with its sub-folders — prefer this for "
                        "level-by-level exploration so you retain folder "
                        "hierarchy context. When true, flattens all descendant "
                        "documents into one list and omits sub-folders — use "
                        "only when a non-recursive browse of the target folder "
                        "returned no relevant results and you need to widen the "
                        "scope, or the user explicitly requests a flat listing."
                    ),
                },
                "sort": {
                    "type": "string",
                    "enum": ["time", "relevance"],
                    "default": "time",
                    "description": (
                        'Sort order. "time" (default) sorts by upload date '
                        '(newest first); "relevance" orders documents by '
                        "semantic relevance to `query`. Relevance also works "
                        "inside the read-only shared folders — pass their "
                        "folder_id — but at the library root it ranks only "
                        "your own documents."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Search query for relevance ranking. "
                        'Required when sort="relevance".'
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9007199254740991,
                    "default": 0,
                    "description": (
                        "Zero-based pagination offset. Pass the value of "
                        "`next_offset` from the previous response to fetch the "
                        "next page."
                    ),
                },
                "limit": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": (
                        "Number of documents to return per page (1-50, "
                        "default 10)"
                    ),
                },
            },
            "required": [],
        },
    },
    "get_document": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Check a document's processing status and metadata. `status` is "
            'one of "pending", "queued", "processing", "completed", or '
            '"failed" — call this before `get_document_structure()` or '
            "`get_page_content()` to confirm the document is ready."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name"],
        },
    },
    "get_document_structure": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Extract a document's hierarchical outline (headers, sections, "
            f"page references). REQUIRED for documents over "
            f"{STRUCTURE_FIRST_PAGE_THRESHOLD} pages — call this first to "
            "locate relevant sections, then pass their page numbers to "
            "`get_page_content()`. Use the `part` parameter to iterate large "
            "outlines until `pagination.has_more` is false."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "part": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9007199254740991,
                    "default": 1,
                    "description": (
                        "Part number for pagination (1-based, default 1). For "
                        "large outlines, increment until the response's "
                        "`pagination.has_more` becomes false."
                    ),
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name"],
        },
    },
    "get_page_content": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Extract page content from a processed document. Use tight, "
            "targeted page ranges — never the whole document at once. For "
            f"documents over {STRUCTURE_FIRST_PAGE_THRESHOLD} pages, call "
            "`get_document_structure()` first to pick relevant sections. "
            "Embedded image paths in the response feed into "
            "`get_document_image()`."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_name": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DOC_NAME_DESCRIPTION,
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
                "pages": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"^(\d+(-\d+)?)(,\s*\d+(-\d+)?)*$",
                    "description": (
                        'Page specification: "5", "3,7,10", "5-10", or '
                        '"1-3,7,9-12"'
                    ),
                },
                "wait_for_completion": {
                    "type": "boolean",
                    "default": False,
                    "description": _WAIT_FOR_COMPLETION_DESCRIPTION,
                },
            },
            "required": ["doc_name", "pages"],
        },
    },
    "remove_document": {
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": True, "openWorldHint": False},
        "description": (
            "Permanently delete documents and all associated data. Only invoke "
            "when the user explicitly names the documents AND confirms "
            "deletion. Returns `results` — one entry per requested document: "
            '`{ doc_name, status: "deleted" | "not_found" | "failed", '
            "error? }`. Inspect each entry for per-document failures. This "
            "action is irreversible."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "doc_names": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 10,
                    "description": (
                        "Array of document names to delete. Each name must be "
                        "copied verbatim from the `name` field of a "
                        "browse_documents() or search_documents() response "
                        "(case-sensitive, include extension). Example: "
                        '["Q3 Report.pdf", "draft.pdf"]. Max 10 per call.'
                    ),
                },
                "folder_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": _FOLDER_ID_DISAMBIGUATOR_DESCRIPTION,
                },
            },
            "required": ["doc_names"],
        },
    },
    # Not part of the cloud MCP contract -- added for the local store (see
    # postgres_store.py::search_pages). Local-only, like the browse `query` below.
    "search_content": {
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "description": (
            "Full-text search over the PAGES of your documents, not just their "
            "names. Use it first for any question about a specific fact: a "
            "record, drawing or RFI number (SUB-084, RFI-062, A-109), a name, "
            "a figure or a distinctive phrase. It matches every word first and "
            "falls back to matching any word only if nothing matches, so put "
            "only the distinctive words in the query. Returns matching "
            "document names, page numbers and a snippet of each page. Snippets "
            "are leads, not evidence: read the best pages with "
            "get_page_content() before answering."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The distinctive words to look for: record IDs, "
                        "project or entity names, subject terms."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                    "description": "Maximum pages to return (1-20, default 8).",
                },
            },
            "required": ["query"],
        },
    },
}

_READ_TOOLS = ("browse_documents", "search_content", "get_document",
               "get_document_structure", "get_page_content")
_MANAGEMENT_TOOLS = ("remove_document",)


# ── response envelopes ──

_ToolResult = tuple[dict, bool]


def _success(data: dict[str, Any], next_steps: dict[str, Any]) -> tuple[dict, bool]:
    return {"success": True, **data, "next_steps": next_steps}, False


def _failure(error: str, details: Optional[dict[str, Any]],
             next_steps: dict[str, Any], error_code: Optional[str] = None,
             ) -> tuple[dict, bool]:
    payload: dict[str, Any] = {"error": error}
    if error_code:
        payload["errorCode"] = error_code
    if details:
        payload.update(details)
    payload["next_steps"] = next_steps
    return payload, True


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ── document listing / name resolution ──

_SWEEP_PAGE_SIZE = 1000


def _all_documents(client, stop_ids=None) -> list[dict[str, Any]]:
    """Every document the client can list, newest first (both modes list
    newest-first; paging preserves that order). With ``stop_ids``, paging
    stops early once every one of those ids has been seen — for callers
    that only need those entries; an id absent from the listing still
    costs a full sweep."""
    documents: list[dict[str, Any]] = []
    offset = 0
    remaining = {str(one_id) for one_id in stop_ids} if stop_ids else None
    while True:
        # Each local listing call reads every document's metadata, so the page size sets how
        # many full reads a sweep costs: 100 per page was ~11 reads at 1,011 documents on every
        # get_page_content/get_document call, and grows quadratically with the library.
        page = client.list_documents(limit=_SWEEP_PAGE_SIZE, offset=offset)
        batch = page.get("documents") or []
        documents.extend(batch)
        if remaining is not None:
            remaining.difference_update(str(doc.get("id")) for doc in batch)
            if not remaining:
                return documents
        # Advance by what actually arrived — stepping by the requested
        # limit skips documents whenever a server caps its page size.
        offset += len(batch)
        total = page.get("total")
        # An empty page is the reliable terminator; `total` (absent or
        # None on some backends) only saves the final empty-page request.
        if not batch or (isinstance(total, int) and offset >= total):
            return documents


def _normalize_created_at(value: Any) -> str:
    """Emit the cloud tool format (ISO-8601 UTC with 'Z', millisecond
    precision) from either mode's createdAt string."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        return value


def _flat_metadata(value: Any) -> Optional[dict[str, Any]]:
    """User-facing string|number|boolean metadata fields only, or None."""
    if not isinstance(value, dict):
        return None
    flat = {key: val for key, val in value.items()
            if isinstance(val, (str, int, float, bool))}
    return flat or None


def _scope_documents(documents: list[dict[str, Any]],
                     allowed_ids: Optional[frozenset]) -> list[dict[str, Any]]:
    if allowed_ids is None:
        return documents
    return [doc for doc in documents if doc.get("id") in allowed_ids]


def _lookup_by_name(client, doc_name: str, allowed_ids: Optional[frozenset]) -> list[dict[str, Any]]:
    """Documents named exactly `doc_name`, scoped to `allowed_ids`, via the local indexed lookup.
    Returns [] when the client cannot do it (cloud mode) or nothing matches."""
    try:
        page = client.list_documents(limit=50, offset=0, name=doc_name)
    except Exception:  # noqa: BLE001 -- any failure falls back to the full sweep
        return []
    found = page.get("documents") or []
    return _scope_documents(found, allowed_ids)


def _resolve_document(
    client, doc_name: str,
    documents: Optional[list[dict[str, Any]]] = None,
    allowed_ids: Optional[frozenset] = None,
) -> "tuple[Optional[dict[str, Any]], Optional[_ToolResult]]":
    """Resolve doc_name to a list entry. Same-name duplicates resolve to the
    newest match. Returns (entry, None) or (None, error_payload_pair)."""
    if documents is None:
        # Fast path: an exact-name lookup is one indexed query. The full sweep below (which reads every
        # document's metadata, once per page, on every tool call) is kept only as the fallback for a
        # name that matches nothing, where it builds the "did you mean" suggestions.
        exact = _lookup_by_name(client, doc_name, allowed_ids)
        if exact:
            return max(exact, key=lambda d: d.get("createdAt") or ""), None
        documents = _all_documents(client, stop_ids=allowed_ids)
    documents = _scope_documents(documents, allowed_ids)
    matches = [doc for doc in documents if doc.get("name") == doc_name]
    if matches:
        return max(matches, key=lambda d: d.get("createdAt") or ""), None
    names = [str(doc.get("name")) for doc in documents if doc.get("name")]
    similar = difflib.get_close_matches(str(doc_name), names,
                                        n=_SIMILAR_NAMES_LIMIT, cutoff=0.5)
    message = (
        "Document not found. Did you mean: "
        + ", ".join(f'"{name}"' for name in similar) + "?"
        if similar else "Document not found or you do not have access to it"
    )
    return None, _failure(
        message,
        {"doc_name": doc_name, "similar_files": similar},
        {
            "summary": "The requested document does not exist or is not accessible",
            "options": [
                "Verify the document name is correct",
                "Use browse_documents() to see your recent documents",
                "Check if the document was deleted",
            ],
        },
        "NOT_FOUND",
    )


def _refetch_entry(client, doc_id: str) -> Optional[dict[str, Any]]:
    try:
        return client.get_document(doc_id)
    except RagEngineError:
        return None


def _await_completion(client, entry: dict[str, Any], wait: bool) -> dict[str, Any]:
    """Re-poll a processing document for up to 3 minutes when wait is set.
    Local documents are stored already terminal, so the wait never engages
    there."""
    doc_id = entry.get("id")
    if not wait or not doc_id or entry.get("status") in ("completed", "failed"):
        return entry
    deadline = time.monotonic() + _TOOL_WAIT_TIMEOUT
    current = entry
    while time.monotonic() < deadline:
        time.sleep(_TOOL_WAIT_INTERVAL)
        refreshed = _refetch_entry(client, doc_id)
        if refreshed is None:
            continue  # transient refetch failure: poll on to the deadline
        if refreshed.get("metadata") is None:
            # Status refetches omit (or null out) custom metadata; keep the
            # listing's copy.
            refreshed["metadata"] = current.get("metadata")
        current = {**current, **refreshed}
        if current.get("status") in ("completed", "failed"):
            return current
    return current


def _not_ready_error(doc_name: str, status: Any, operation: str,
                     timed_out: bool) -> tuple[dict, bool]:
    if status == "failed":
        return _failure(
            f"Document processing failed. Current status: {status}",
            {"doc_name": doc_name},
            {
                "summary": "Document processing has failed",
                "options": [
                    "Index the document again with "
                    "RagEngineClient.submit_document()",
                    "Use browse_documents() to work with other documents",
                ],
            },
            "INVALID_INPUT",
        )
    if timed_out:
        return _failure(
            f"Document is still processing. Current status: {status}",
            {"doc_name": doc_name},
            {
                "summary": "Document processing timeout",
                "options": [
                    "Try again later when processing is complete",
                    "Check status with get_document()",
                ],
            },
            "INVALID_INPUT",
        )
    return _failure(
        f"Document is not ready for {operation}. Current status: {status}",
        {"doc_name": doc_name},
        {
            "summary": "Document is still processing",
            "options": [
                "Wait for document processing to complete",
                "Check status with browse_documents() or get_document()",
            ],
        },
        "INVALID_INPUT",
    )


def _folder_unsupported(param: str) -> tuple[dict, bool]:
    return _failure(
        f"Folders are not supported in local mode yet — omit {param}.",
        None,
        {
            "summary": "This local library does not have folders yet",
            "options": ["Retry the call without a folder_id",
                        "Use browse_documents() to list the library root",
                        "Folders are available in cloud mode (a hosted deployment with an API key)"],
        },
        "INVALID_INPUT",
    )


# ── page spec handling ──

class _PageSpecError(ValueError):
    """Shared page-spec rejection; ``code`` picks the caller's rendering."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# re.ASCII mirrors the ECMA regex semantics the contract's pattern carries
_PAGE_SPEC_RE = re.compile(
    TOOL_CONTRACT["get_page_content"]["schema"]["properties"]["pages"]["pattern"],
    re.ASCII)


def _expand_pages(pages) -> list[int]:
    """Expand '1-3,7' into sorted distinct pages — the one parser for the
    SDK surface and the tool layer, holding both to the contract's pages
    pattern. Raises _PageSpecError with code 'invalid', 'too_many', or
    'nonpositive'."""
    if not isinstance(pages, str):
        raise _PageSpecError("invalid",
                             f"Invalid page specification: {pages!r}")
    if not _PAGE_SPEC_RE.fullmatch(pages):
        raise _PageSpecError(
            "invalid", f"Invalid page specification '{pages}'")
    too_many = (f"Page specification '{pages}' spans more than "
                f"{_MAX_REQUESTED_PAGES} pages; request a narrower range")
    expanded: set[int] = set()
    for part in pages.split(","):
        part = part.strip()
        try:
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
                if start > end:
                    raise _PageSpecError(
                        "invalid",
                        f"Invalid range '{part}': start must be <= end")
            else:
                start = end = int(part)
        except _PageSpecError:
            raise
        except ValueError as exc:
            raise _PageSpecError(
                "invalid", f"Invalid page specification '{pages}'") from exc
        # Bound each part arithmetically before materializing it: a spec like
        # "1-1000000000" would otherwise expand to billions of integers
        # inside the caller's process. The cap is on distinct pages, so
        # overlapping parts (a parent section plus its children) don't
        # double-count.
        if end - start + 1 > _MAX_REQUESTED_PAGES:
            raise _PageSpecError("too_many", too_many)
        expanded.update(range(start, end + 1))
        if len(expanded) > _MAX_REQUESTED_PAGES:
            raise _PageSpecError("too_many", too_many)
    if min(expanded) < 1:
        raise _PageSpecError(
            "nonpositive",
            "Invalid page numbers. Page numbers must be positive integers")
    return sorted(expanded)


def _parse_page_spec(
    pages: str, doc_name: str,
) -> "tuple[Optional[list[int]], Optional[_ToolResult]]":
    """Expand '1-3,7' into a sorted, deduplicated page list, or an error."""
    try:
        return _expand_pages(pages), None
    except _PageSpecError as exc:
        if exc.code == "too_many":
            return None, _failure(
                f"Too many pages requested (over {_MAX_REQUESTED_PAGES})",
                {"doc_name": doc_name},
                {
                    "summary": "The page specification spans too many pages",
                    "options": [
                        "Request a narrower page range",
                        "The response holds only a few pages per call - page through with several smaller requests",
                    ],
                },
                "INVALID_INPUT",
            )
        if exc.code == "nonpositive":
            return None, _failure(
                "Invalid page numbers. Page numbers must be positive integers",
                {"doc_name": doc_name},
                {
                    "summary": "Invalid page numbers provided",
                    "options": [
                        "Page numbers must be positive integers (>= 1)",
                        "Check the page specification format",
                    ],
                },
                "INVALID_INPUT",
            )
        return None, _failure(
            "Invalid page specification format",
            {"doc_name": doc_name},
            {
                "summary": "Failed to parse the pages parameter",
                "options": [
                    'Use valid formats: "5", "3,7,10", "5-10", or "1-3,7,9-12"',
                    "Ensure page numbers are positive integers",
                ],
            },
            "INVALID_INPUT",
        )


def _format_page_spec(pages: list[int]) -> str:
    """Compress [1,2,3,5] into '1-3,5'."""
    if not pages:
        return ""
    ordered = sorted(set(pages))
    ranges = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = page
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


# ── structure formatting / splitting ──

_STRUCTURE_KEY_ORDER = ("title", "node_id", "start_index", "end_index",
                        "page_number", "prefix_summary", "summary", "nodes")


def _format_structure(node: Any) -> Any:
    """Drop node text and normalize key order, recursively."""
    if isinstance(node, list):
        return [_format_structure(item) for item in node]
    if isinstance(node, dict):
        stripped = {key: value for key, value in node.items() if key != "text"}
        if "nodes" in stripped:
            stripped["nodes"] = _format_structure(stripped["nodes"])
        ordered = {key: stripped[key] for key in _STRUCTURE_KEY_ORDER
                   if key in stripped}
        ordered.update({key: value for key, value in stripped.items()
                        if key not in ordered})
        return ordered
    return node


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _split_structure(structure: Any, budget: int) -> list[Any]:
    """Split a formatted structure into chunks of at most ~budget serialized
    chars. The paginated response shape matches the cloud tool (its chunk
    type admits node-or-list); chunk boundaries are implementation-defined.
    An unsplit structure keeps its natural shape; once split, every chunk
    is a list of nodes — the `structure` field must not change JSON type
    between parts of one paginated response."""
    if _serialized_size(structure) <= budget:
        return [structure]
    nodes = structure if isinstance(structure, list) else [structure]
    chunks: list[Any] = []
    group: list[Any] = []
    group_size = 0
    for node in nodes:
        size = _serialized_size(node)
        if size > budget:
            if group:
                chunks.append(group)
                group, group_size = [], 0
            chunks.extend([part]
                          for part in _split_oversized_node(node, budget))
            continue
        if group and group_size + size > budget:
            chunks.append(group)
            group, group_size = [], 0
        group.append(node)
        group_size += size
    if group:
        chunks.append(group)
    return chunks or [structure]


def _split_oversized_node(node: Any, budget: int) -> list[Any]:
    children = node.get("nodes") if isinstance(node, dict) else None
    if not children:
        return [node]
    shell = {key: value for key, value in node.items() if key != "nodes"}
    shell_size = _serialized_size(shell)
    child_budget = max(budget - shell_size, budget // 2)
    parts = []
    for chunk in _split_structure(children, child_budget):
        # A recursive result is either the unsplit children (natural shape)
        # or always-list chunks; normalize for the shell's "nodes".
        parts.append({**shell,
                      "nodes": chunk if isinstance(chunk, list) else [chunk]})
    return parts


# ── tool implementations (client-backed; mode-blind) ──

def _browse_documents(client, folder_id: str = "root", recursive: bool = False,
                      sort: str = "time", query: Optional[str] = None,
                      offset: int = 0, limit: int = 10,
                      _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id != "root":
        return _folder_unsupported("folder_id")
    if sort not in ("time", "relevance"):
        return _failure(
            'Invalid sort mode — only the default "time" sort is available '
            "in local mode.", None,
            {"summary": "Invalid sort mode",
             "options": ['Use sort="time" (newest first) or omit sort',
                         "Semantic ranking is available in cloud mode (a hosted deployment with an API key)"]},
            "INVALID_INPUT",
        )
    has_query = bool(query and query.strip())
    if sort == "relevance" and not has_query:
        return _failure(
            'sort="relevance" requires a non-empty `query`.', None,
            {"summary": "Missing query for relevance sort",
             "options": ['Pass a `query` string together with sort="relevance"',
                         'Or omit both to use the default "time" sort']},
            "INVALID_INPUT",
        )
    try:
        offset = max(int(offset), 0)
        limit = min(max(int(limit), 1), 50)
    except (TypeError, ValueError):
        return _failure("offset and limit must be numbers", None,
                        {"summary": "Invalid pagination parameters",
                         "options": ["Pass integer offset and limit values"]},
                        "INVALID_INPUT")

    # A query ranks by keyword match (Postgres full-text over name/description/
    # section titles) regardless of the exact `sort` value passed — this is a
    # lexical ranking, not semantic/embedding search; the response says so below.
    if has_query:
        listing = client.list_documents(limit=limit, offset=offset, query=query,
                                        doc_ids=_allowed_ids)
        window = listing.get("documents") or []
        total = listing.get("total")
    elif _allowed_ids is None:
        listing = client.list_documents(limit=limit, offset=offset)
        window = listing.get("documents") or []
        total = listing.get("total")
    else:
        scoped = _scope_documents(_all_documents(client, stop_ids=_allowed_ids),
                                  _allowed_ids)
        window, total = scoped[offset:offset + limit], len(scoped)
    window_end = offset + len(window)
    has_more = bool(window) and (window_end < total if isinstance(total, int)
                                 else len(window) == limit)
    next_offset = window_end if has_more else None

    page_has_processing = False
    page_has_failed = False
    items = []
    for doc in window:
        status = doc.get("status") or "unknown"
        if status == "failed":
            page_has_failed = True
        elif status != "completed":
            page_has_processing = True
        item = {
            "name": doc.get("name") or "Unknown Document",
            "description": doc.get("description") or "No description provided",
            "status": status,
            "created_at": _normalize_created_at(doc.get("createdAt")),
        }
        metadata = _flat_metadata(doc.get("metadata"))
        if metadata is not None:
            item["metadata"] = metadata
        items.append(item)

    data: dict[str, Any] = {
        "documents": items,
        "sort": "relevance" if has_query else sort,
        "next_offset": next_offset,
        "has_more": has_more,
    }
    if not recursive:
        data["folders"] = []

    if not items and offset == 0:
        if has_query:
            next_steps = {
                "summary": "No keyword matches for this query",
                "options": [
                    "This is a lexical (keyword) match, not semantic search — a "
                    "document phrased very differently from the query can be "
                    "missed even though it exists.",
                    "Retry with different/broader keywords",
                    "Facts that live inside a page rather than in a document's "
                    "name or description are found with search_content(), not here",
                    "Or call browse_documents() without `query` to page "
                    "through the full library instead",
                ],
            }
        else:
            next_steps = {
                "summary": "Nothing to show",
                "options": ["Nothing here. Index documents with "
                            "RagEngineClient.submit_document() to get started."],
                "auto_retry": "Index a document with "
                              "RagEngineClient.submit_document() to get started",
            }
        return _success(data, next_steps)

    options = []
    if items:
        options.append("Use get_document() with a document name to view details")
        if has_query:
            options.append(
                "Ranked by keyword match (lexical full-text search over name, "
                "description, and section titles) — not semantic/meaning-based. "
                "Verify these documents match the user's actual intent before "
                "proceeding; a real match phrased differently from the query "
                "may not appear here. Do NOT use general knowledge as a substitute."
            )
        else:
            options.append(
                "Results returned ≠ correct results. Verify these documents match "
                "the user's actual intent (topic, time period, document type) "
                "before proceeding."
                + (" If they do not match, page through the rest of the library."
                   if has_more else "")
                + " Do NOT use general knowledge as a substitute."
            )
    if page_has_processing:
        options.append("Some documents on this page are still processing. "
                       "Use get_document() to check individual status.")
    if page_has_failed:
        options.append("Some documents on this page failed processing. "
                       "Use get_document() to see error details.")
    if has_more:
        options.append("Use browse_documents() with `offset: next_offset` to "
                       "load more documents")
    summary = (f"Showing {len(items)} document(s)"
               + (" (more available)" if has_more else "")
               if items else "Nothing to show")
    return _success(data, {"summary": summary, "options": options})


# A record-ID-shaped token: SUB-084, FCL-LTR-0142, A1209, RFI-062 -- letters, optional -LETTERS
# groups, then 2+ digits. Looked for first when cutting a snippet, since an ID is the thing the
# reader is hunting for; plain words come second.
_ID_TOKEN = re.compile(r"[A-Za-z]{1,5}(?:-[A-Za-z]{2,5})*-?\d{2,}[A-Za-z0-9]*")
_WORD_TOKEN = re.compile(r"[A-Za-z]{4,}")
_SNIPPET_RADIUS = 140


def _snippet(page_text: str, query: str) -> str:
    """A short window of the page around the first place the query's ID-shaped tokens, then its
    plain words, appear -- or the page's opening if none can be located (the match may have been
    on the document's name/description rather than the text)."""
    flat = " ".join(str(page_text or "").split())
    for token in _ID_TOKEN.findall(query) + _WORD_TOKEN.findall(query):
        found = re.search(re.escape(token), flat, re.IGNORECASE)
        if found:
            start = max(found.start() - _SNIPPET_RADIUS, 0)
            end = found.end() + _SNIPPET_RADIUS
            return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
    return flat[:2 * _SNIPPET_RADIUS] + ("…" if len(flat) > 2 * _SNIPPET_RADIUS else "")


def _search_content(client, query: str, limit: int = 8,
                    _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if not isinstance(query, str) or not query.strip():
        return _failure(
            "`query` must be a non-empty string.", None,
            {"summary": "Missing query",
             "options": ["Pass the distinctive words to look for, e.g. a record ID "
                         "or a project name"]},
            "INVALID_INPUT",
        )
    try:
        limit = min(max(int(limit), 1), 20)
    except (TypeError, ValueError):
        return _failure("limit must be a number", None,
                        {"summary": "Invalid limit",
                         "options": ["Pass an integer limit between 1 and 20"]},
                        "INVALID_INPUT")
    found = client.search_pages(query.strip(), limit=limit, doc_ids=_allowed_ids)
    hits = found.get("results") or []
    match = found.get("match") or "no match"
    results = [{"document": hit["name"], "page": hit["page"],
                "snippet": _snippet(hit.get("text", ""), query)} for hit in hits]

    if not results:
        return _success(
            {"results": [], "match": match},
            {"summary": "No page matches this query",
             "options": [
                 "Nothing in the selected documents contains these words. This is a "
                 "keyword match, not semantic search — a page phrased differently "
                 "from the query can be missed.",
                 "Retry with different distinctive words (an ID, a name, a different "
                 "spelling), or fewer words",
                 "Use browse_documents(query=...) to find documents by project or "
                 "topic instead",
             ]},
        )
    options = [
        "Snippets are leads, not evidence: read the best pages with "
        "get_page_content(doc_name, pages) before answering. Copy `document` "
        "verbatim as doc_name.",
        "Several documents can look alike (the same document type for different "
        "projects): check each result's document name against the project or "
        "entity the user asked about before using it.",
    ]
    if match == "any term":
        options.insert(0, "No page contained ALL of the query's words, so these pages "
                          "match ANY of them — broader and noisier. Check each one, or "
                          "retry with fewer, more distinctive words.")
    return _success({"results": results, "match": match},
                    {"summary": f"{len(results)} matching page(s) ({match})",
                     "options": options})


def _get_document(client, doc_name: str, folder_id: Optional[str] = None,
                  wait_for_completion: bool = False,
                  _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    entry = _await_completion(client, entry, wait_for_completion)

    status = entry.get("status") or "unknown"
    is_processing = status not in ("completed", "failed")
    is_ready = status == "completed"
    page_num = entry.get("pageNum") or 0
    name = entry.get("name") or "Unknown Document"

    suggestions: list[str] = []
    if is_processing:
        suggestions.append("Document is still processing. Processing status "
                           "can be checked later.")
    elif is_ready:
        suggestions.append("Document is ready for analysis.")
        if page_num > 0:
            if page_num <= 5:
                suggestions.extend([
                    f"This is a short document with {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then extract all content: get_page_content(doc_name: "{name}", pages: "1-{page_num}")',
                ])
            elif page_num <= STRUCTURE_FIRST_PAGE_THRESHOLD:
                suggestions.extend([
                    f"This document has {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then extract key pages: get_page_content(doc_name: "{name}", pages: "1,5,10")',
                ])
            else:
                suggestions.extend([
                    f"This is a large document with {page_num} pages.",
                    f'First explore structure: get_document_structure(doc_name: "{name}")',
                    f'Then target specific sections: get_page_content(doc_name: "{name}", pages: "1-3")',
                ])
    else:
        suggestions.append("Document processing failed. Index the document "
                           "again with RagEngineClient.submit_document().")

    data: dict[str, Any] = {
        "name": name,
        "description": entry.get("description") or "No description provided",
        "status": status,
        "created_at": _normalize_created_at(entry.get("createdAt")),
        "page_count": page_num or None,
        "folder_id": entry.get("folderId"),
    }
    metadata = _flat_metadata(entry.get("metadata"))
    if metadata is not None:
        data["metadata"] = metadata

    return _success(data, {
        "summary": ("Document is ready for analysis and querying." if is_ready
                    else "Document is still being processed." if is_processing
                    else "Document processing has failed."),
        "options": suggestions,
        **({"auto_retry": "Document processing status can be monitored periodically"}
           if is_processing else {}),
    })


def _get_document_structure(client, doc_name: str,
                            folder_id: Optional[str] = None, part: int = 1,
                            wait_for_completion: bool = False,
                            _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    waited = wait_for_completion and entry.get("status") not in ("completed", "failed")
    entry = _await_completion(client, entry, wait_for_completion)
    if entry.get("status") != "completed":
        return _not_ready_error(doc_name, entry.get("status"),
                                "structure retrieval",
                                waited and entry.get("status") != "failed")

    try:
        raw_tree = getattr(getattr(client, "_api", None), "raw_tree", None)
        tree = raw_tree(entry["id"]) if raw_tree is not None else None
        if tree is None:
            # _format_structure strips text anyway — don't download it.
            tree = client.get_tree(entry["id"], node_summary=True,
                                   include_text=False).get("result")
    except RagEngineError as exc:
        return _failure(
            f"Failed to retrieve document structure: {exc}",
            {"doc_name": doc_name},
            {
                "summary": "Failed to retrieve document structure due to an error",
                "options": [
                    "The document may not exist or is not accessible",
                    "Check if the document name is correct",
                    "Try again in a few moments",
                ],
            },
            "INTERNAL_ERROR",
        )
    if tree is None:
        return _failure(
            "Structure not available for this document",
            {"doc_name": doc_name},
            {
                "summary": "Structure not available for this document",
                "options": [
                    "The document may not have been processed correctly or structure extraction may have failed",
                    "Try processing the document again if possible",
                ],
            },
            "INTERNAL_ERROR",
        )

    formatted = _format_structure(tree)
    chunks = _split_structure(formatted, _CHAR_BUDGET)
    total_parts = max(1, len(chunks))
    try:
        requested_part = int(part)
    except (TypeError, ValueError):
        requested_part = 1
    current = min(max(requested_part, 1), total_parts)

    if total_parts == 1:
        return _success(
            {"doc_name": doc_name, "structure": chunks[0]},
            {
                "summary": "Document structure retrieved successfully.",
                "options": [
                    "Use get_page_content() to extract specific content from pages",
                ],
            },
        )

    next_steps = (
        {
            "summary": f"Showing part {current} of {total_parts}.",
            "options": [
                f"Request next part with part: {current + 1}",
                f"Jump to last part with part: {total_parts}",
                "Proceed to get_page_content() for specific sections",
            ],
        }
        if current < total_parts else
        {
            "summary": "All parts retrieved for current pagination.",
            "options": [
                "Use get_page_content() to extract specific content from pages",
            ],
        }
    )
    return _success(
        {
            "doc_name": doc_name,
            "total_parts": total_parts,
            "structure": chunks[current - 1],
            "pagination": {
                "part": current,
                "total_parts": total_parts,
                "has_more": current < total_parts,
            },
        },
        next_steps,
    )


def _get_page_content(client, doc_name: str, pages: str,
                      folder_id: Optional[str] = None,
                      wait_for_completion: bool = False,
                      _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    entry, error = _resolve_document(client, doc_name, allowed_ids=_allowed_ids)
    if error is not None:
        return error
    assert entry is not None
    waited = wait_for_completion and entry.get("status") not in ("completed", "failed")
    entry = _await_completion(client, entry, wait_for_completion)
    if entry.get("status") != "completed":
        return _not_ready_error(doc_name, entry.get("status"),
                                "page content retrieval",
                                waited and entry.get("status") != "failed")

    requested, error = _parse_page_spec(pages, doc_name)
    if error is not None:
        return error
    assert requested is not None

    try:
        page_data = client.get_ocr(entry["id"], format="page").get("result") or []
    except RagEngineError as exc:
        return _failure(
            f"Failed to retrieve page content: {exc}",
            {"doc_name": doc_name},
            {
                "summary": "Unable to retrieve page content due to a service issue.",
                "options": [
                    "Verify the document name is correct using browse_documents()",
                    "Check if the document processing is complete with get_document()",
                    "Ensure the requested page numbers are valid",
                ],
                "auto_retry": "This may be a temporary issue - you can try "
                              "the request again",
            },
            "INTERNAL_ERROR",
        )

    by_index = {item["page_number"]: item for item in page_data
                if isinstance(item, dict)
                and isinstance(item.get("page_number"), int)}
    max_page = max(by_index, default=0)

    out_of_range = [page for page in requested if page > max_page]
    valid_pages = [page for page in requested if page <= max_page]
    if out_of_range and not valid_pages:
        return _failure(
            f"All requested pages are out of range. Document has {max_page} "
            f"pages, but you requested pages: {_format_page_spec(out_of_range)}",
            {
                "doc_name": doc_name,
                "max_pages": max_page,
                "requested_pages": _format_page_spec(out_of_range),
            },
            {
                "summary": "All requested pages are out of range for this document",
                "options": [
                    f"Request pages between 1 and {max_page}",
                    "Use get_document() to check document page count",
                ],
            },
            "INVALID_INPUT",
        )

    content = []
    included: list[int] = []
    remaining: list[int] = []
    budget = _CHAR_BUDGET
    for page in valid_pages:
        item = by_index.get(page)
        markdown = item.get("markdown") if item else None
        text = (markdown if isinstance(markdown, str)
                else f"Page {page} content not available")
        entry = {"page": page, "text": text}
        size = _serialized_size(entry) + 2  # +2: json ", " item separator
        if not included or budget - size >= 0:
            content.append(entry)
            included.append(page)
            budget -= size
        else:
            remaining.append(page)

    options = [
        "Use get_document_structure() to understand document organization",
        "Request additional pages as needed",
    ]
    if remaining:
        options.insert(0, f"For remaining pages, request: {_format_page_spec(remaining)}")
    if out_of_range:
        options.insert(0, f"Document has {max_page} pages total - request "
                          f"pages 1-{max_page}")
    if remaining or out_of_range:
        parts = [f"Retrieved {len(included)} of {len(requested)} "
                 "requested pages."]
        if remaining:
            parts.append(f"Pages {_format_page_spec(remaining)} were "
                         "omitted due to response size limits.")
        if out_of_range:
            parts.append(f"Pages {_format_page_spec(out_of_range)} "
                         "were out of range.")
        summary = " ".join(parts)
    else:
        summary = (f"Successfully retrieved content for {len(content)} "
                   f"page{'' if len(content) == 1 else 's'}.")
    return _success(
        {
            "doc_name": doc_name,
            "total_pages": max_page,
            "requested_pages": _format_page_spec(requested),
            "returned_pages": _format_page_spec(included),
            "content": content,
        },
        {"summary": summary, "options": options},
    )


def _remove_document(client, doc_names: list[str],
                     folder_id: Optional[str] = None,
                     _allowed_ids: Optional[frozenset] = None) -> tuple[dict, bool]:
    if folder_id not in (None, "root"):
        return _folder_unsupported("folder_id")
    if not isinstance(doc_names, list) or not doc_names:
        return _failure("At least one document name is required", None,
                        {"summary": "No document names provided",
                         "options": ["Pass doc_names as a non-empty array"]},
                        "INVALID_INPUT")
    # Validate every element before deleting anything: a rejection envelope
    # must mean nothing was destroyed.
    if not all(isinstance(name, str) and name.strip() for name in doc_names):
        return _failure(
            "doc_names must be an array of non-empty document name strings",
            None,
            {"summary": "Invalid document names",
             "options": ["Copy each name verbatim from a browse_documents() "
                         "response"]},
            "INVALID_INPUT")
    doc_names = list(dict.fromkeys(doc_names))
    if len(doc_names) > 10:
        return _failure("Maximum 10 documents can be deleted at once", None,
                        {"summary": "Too many documents in one call",
                         "options": ["Delete at most 10 documents per call"]},
                        "INVALID_INPUT")
    documents = _all_documents(client, stop_ids=_allowed_ids)
    results = []
    for doc_name in doc_names:
        entry, error = _resolve_document(client, doc_name, documents=documents,
                                         allowed_ids=_allowed_ids)
        if error is not None or entry is None:
            results.append({"doc_name": doc_name, "status": "not_found"})
            continue
        try:
            client.delete_document(entry["id"])
            results.append({"doc_name": doc_name, "status": "deleted"})
        except Exception as exc:
            # Any escape here (OSError, transport errors) would discard the
            # entries for documents already irreversibly deleted.
            results.append({"doc_name": doc_name, "status": "failed",
                            "error": str(exc)})
    deleted = sum(1 for item in results if item["status"] == "deleted")
    return _success(
        {"results": results},
        {
            "summary": f"Deleted {deleted} of {len(doc_names)} document(s).",
            "options": ["Use browse_documents() to review the remaining library"],
        },
    )


_IMPLEMENTATIONS: dict[str, Callable[..., tuple[dict, bool]]] = {
    "browse_documents": _browse_documents,
    "search_content": _search_content,
    "get_document": _get_document,
    "get_document_structure": _get_document_structure,
    "get_page_content": _get_page_content,
    "remove_document": _remove_document,
}


def tool_names(include_management: bool = False) -> tuple[str, ...]:
    return _READ_TOOLS + (_MANAGEMENT_TOOLS if include_management else ())


def _coerce_bool_args(schema: dict, kwargs: dict[str, Any]) -> None:
    """Models routinely send booleans as JSON strings ("false"); the bare
    truthiness tests downstream would read those as True. Runs on both
    dispatch paths — call_tool and the cloud bridge invoker."""
    properties = (schema or {}).get("properties", {})
    for key, spec in properties.items():
        value = kwargs.get(key)
        if spec.get("type") == "boolean" and isinstance(value, str):
            kwargs[key] = value.strip().lower() not in ("false", "no", "0", "")


def call_tool(client, name: str, arguments: dict[str, Any],
              doc_ids=None) -> tuple[str, bool]:
    """Run one contract tool; returns (envelope_json, is_error). Never raises
    for tool-level failures — unexpected exceptions become error envelopes.
    ``doc_ids`` restricts every document lookup to that allowlist (the local
    chat surfaces' doc_id scope)."""
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        payload, _ = _failure(
            f"Unknown tool: {name}",
            {"tool_name": name, "available_tools": list(_IMPLEMENTATIONS)},
            {"summary": "Tool not found",
             "options": [f"Available tools: {', '.join(_IMPLEMENTATIONS)}"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), True
    if arguments is not None and not isinstance(arguments, dict):
        payload, is_error = _failure(
            f"Invalid arguments for {name}: expected a JSON object, got "
            f"{type(arguments).__name__}", None,
            {"summary": "Invalid tool arguments",
             "options": [f"Pass {name}() arguments as a JSON object of its "
                         "parameters"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), is_error
    # Underscore-prefixed keys are the SDK's private channel (the scope
    # below), never model arguments. None ≡ omitted (the contract's
    # "omit if ..." semantics, same as the cloud bridge invoker).
    kwargs = {key: value for key, value in (arguments or {}).items()
              if not key.startswith("_") and value is not None}
    _coerce_bool_args(TOOL_CONTRACT.get(name, {}).get("schema", {}), kwargs)
    try:
        if doc_ids is not None:
            ids = [doc_ids] if isinstance(doc_ids, str) else doc_ids
            kwargs["_allowed_ids"] = frozenset(str(one_id) for one_id in ids)
        bound = inspect.signature(implementation).bind(client, **kwargs)
    except TypeError as exc:
        payload, is_error = _failure(
            f"Invalid arguments for {name}: {exc}", None,
            {"summary": "Invalid tool arguments",
             "options": [f"Check the {name}() parameter names and types"]},
            "INVALID_INPUT",
        )
        return _dumps(payload), is_error
    try:
        payload, is_error = implementation(*bound.args, **bound.kwargs)
    except Exception as exc:  # tool calls must never raise into the agent loop
        payload, is_error = _failure(
            f"{name} failed: {exc}", None,
            {"summary": "Unexpected error while running the tool",
             "options": ["Try the request again"],
             "auto_retry": "This is likely a temporary issue - you can try "
                           "the request again"},
            "INTERNAL_ERROR",
        )
    return _dumps(payload), is_error


# ── plain-function materialization (the `client.agent_tools()` surface) ──

def _tool_docstring(description: str, properties: dict[str, Any]) -> str:
    lines = [description, "", "Args:"]
    for param, spec in properties.items():
        lines.append(f"    {param}: {spec.get('description', '')}")
    return "\n".join(lines)


_LOCAL_HIDDEN_PARAMS: dict[str, tuple[str, ...]] = {
    # `query` is deliberately NOT hidden: it is the keyword search over name/description/section
    # titles (postgres_store.py::search_metas). It used to be hidden here, which left the model
    # able only to page through the whole library newest-first -- measured at 1,011 documents, a
    # question whose document was findable by name still cost 24 listing calls without it.
    "browse_documents": ("folder_id", "recursive", "sort"),
    "get_document": ("folder_id",),
    "get_document_structure": ("folder_id",),
    "get_page_content": ("folder_id",),
    "remove_document": ("folder_id",),
}

_LOCAL_DOC_NAME_DESCRIPTION = (
    'Copy the `name` field verbatim from a browse_documents() response '
    '(case-sensitive, include extension). Example: "Q3 Report.pdf". '
    "Document names are unique in a local library."
)

_LOCAL_DESCRIPTIONS: dict[str, str] = {
    "browse_documents": (
        "Find documents by name, description and section titles. Pass "
        "`query` — the distinctive words of the question, such as a project "
        "or entity name, a record ID or a subject — to rank documents by "
        "keyword match (lexical, not semantic: a document phrased very "
        "differently may be missed; it matches all words first, then any "
        "word). Without `query` it lists your documents newest first with "
        "names and descriptions. Page through with `offset: next_offset` "
        "(limit up to 50) while `has_more` is true. It does not search "
        "inside pages — use search_content() for a fact that lives in a page. "
        "Folder browsing is not supported in local mode."
    ),
    # Drop the sentence naming the cloud-only image tool, whatever its
    # wording; the contract-refresh test pins that something was removed.
    "get_page_content": re.sub(
        r"\s*[^.]*`get_document_image\(\)`[^.]*\.", "",
        TOOL_CONTRACT["get_page_content"]["description"]),
}

_LOCAL_PARAM_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("browse_documents", "query"): (
        "Distinctive keywords to rank documents by: a project or entity name, "
        "a record ID, a subject. Matches all words first, then any word. "
        "Omit to list newest first."
    ),
    ("get_document", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("get_document_structure", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("get_page_content", "doc_name"): _LOCAL_DOC_NAME_DESCRIPTION,
    ("remove_document", "doc_names"): (
        "Array of document names to delete. Each name must be copied "
        "verbatim from the `name` field of a browse_documents() response "
        '(case-sensitive, include extension). Example: ["Q3 Report.pdf", '
        '"draft.pdf"]. Max 10 per call.'
    ),
}


def _local_description(name: str) -> str:
    return _LOCAL_DESCRIPTIONS.get(name) or TOOL_CONTRACT[name]["description"]


def _local_schema(name: str) -> dict[str, Any]:
    schema = copy.deepcopy(TOOL_CONTRACT[name]["schema"])
    for param in _LOCAL_HIDDEN_PARAMS.get(name, ()):
        schema["properties"].pop(param, None)
    for (tool_name, param), text in _LOCAL_PARAM_DESCRIPTIONS.items():
        if tool_name == name and param in schema["properties"]:
            schema["properties"][param]["description"] = text
    return schema




_SCHEMA_TYPE_MAP = {"string": str, "integer": int, "number": float,
                    "boolean": bool, "array": list, "object": dict}


def _annotation_for(spec: dict) -> Any:
    schema_type = spec.get("type")
    if schema_type is None and isinstance(spec.get("anyOf"), list):
        # Nullable unions arrive as anyOf: [{type: string}, {type: null}].
        options = [option for option in spec["anyOf"]
                   if isinstance(option, dict) and option.get("type")]
        schema_type = [option["type"] for option in options]
        # `items` lives on the array option, not the union shell.
        spec = next((option for option in options
                     if option["type"] == "array"), spec)
    nullable = False
    if isinstance(schema_type, list):
        nullable = "null" in schema_type
        bases = [t for t in schema_type if t != "null"]
        schema_type = bases[0] if bases else None
    base = _SCHEMA_TYPE_MAP.get(schema_type or "", Any)
    if base is list:
        # Strict function calling rejects arrays whose item type was lost
        # in the annotation round-trip; parameterize when it is known.
        item_type = (spec["items"].get("type")
                     if isinstance(spec.get("items"), dict) else None)
        element = (_SCHEMA_TYPE_MAP.get(item_type)
                   if isinstance(item_type, str) else None)
        if element is not None:
            base = list[element]
    return Optional[base] if nullable else base


_ACCOUNT_LIMITS = {"RATE_LIMITED": 429, "USAGE_LIMIT_REACHED": 402}


def _raise_account_limit(blocks: list) -> None:
    """The cloud's account-level tool errors (retried server-side already;
    the model can act on neither) re-raise as the status they stand for."""
    try:
        payload = json.loads(blocks[0]["text"])
        status = _ACCOUNT_LIMITS[payload["errorCode"]]
    except (LookupError, TypeError, ValueError):
        return
    message = str(payload.get("error") or payload["errorCode"])
    for key in ("retry_after_seconds", "open_url"):
        if key in payload:
            message += f" ({key}: {payload[key]})"
    raise RagEngineError(message, status_code=status)


def _bridge_invoker(bridge, name: str, schema: dict,
                    ) -> "Callable[[dict], tuple[list, bool]]":
    """One cloud tool call proxied over MCP: string booleans are coerced
    (same as call_tool), None-valued arguments are dropped (None ≡ omitted,
    matching the contract's "omit if ..." semantics) and failures are
    contained in the error envelope — except auth failures, what
    survived the bridge's own retries (401/403/429/5xx), an unreachable
    server and the cloud's RATE_LIMITED / USAGE_LIMIT_REACHED tool errors
    (429 / 402), which re-raise: the model can act on none of them. Returns
    (content blocks, is_error), like the bridge."""
    def _invoke(arguments: dict[str, Any]) -> tuple[list, bool]:
        try:
            arguments = {key: value for key, value in arguments.items()
                         if value is not None}
            _coerce_bool_args(schema, arguments)
            blocks, is_error = bridge.call_tool(name, arguments)
        except Exception as exc:
            if isinstance(exc, RagEngineError) and (
                    exc.status_code in (401, 403, 429)
                    or (exc.status_code or 0) >= 500
                    or (exc.status_code is None and isinstance(
                        exc.__cause__, requests.RequestException))):
                raise
            payload, _ = _failure(
                f"{name} failed: {exc}", None,
                {"summary": "Unexpected error while running the tool",
                 "options": ["Try the request again"],
                 "auto_retry": "This is likely a temporary issue - you can "
                               "try the request again"},
                "INTERNAL_ERROR",
            )
            return [{"type": "text", "text": _dumps(payload)}], True
        if is_error:
            _raise_account_limit(blocks)
        return blocks, is_error
    return _invoke


def _make_tool_function(name: str, description: str, schema: dict,
                        invoke: "Callable[[dict], tuple[list, bool]]",
                        ) -> Callable[..., str]:
    """One plain function for a tool: real signature and docstring from the
    schema, the result rendered as text, errors contained by the invoker;
    arguments the signature rejects come back as the guided envelope
    instead of raising."""
    import keyword

    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    _invoke = invoke

    params_usable = all(param.isidentifier() and not keyword.iskeyword(param)
                        and param not in ("_invoke", "_render")
                        for param in properties)
    if not params_usable:
        def inner(**kwargs: Any) -> str:
            return render_text(_invoke(kwargs)[0])
    else:
        ordered = ([p for p in properties if p in required]
                   + [p for p in properties if p not in required])
        rendered = ", ".join(
            p if p in required else f"{p}={properties[p].get('default')!r}"
            for p in ordered
        )
        args_literal = "{" + ", ".join(f"'{p}': {p}" for p in ordered) + "}"
        namespace: dict[str, Any] = {"_invoke": _invoke,
                                     "_render": render_text}
        exec(f"def _synthesized({rendered}):\n"
             f"    return _render(_invoke({args_literal})[0])", namespace)
        inner = namespace["_synthesized"]
        # binding TypeErrors quote __qualname__, not __name__
        inner.__name__ = inner.__qualname__ = name or "tool"
        annotations: dict[str, Any] = {}
        for p in ordered:
            annotation = _annotation_for(properties[p])
            if p not in required and "default" not in properties[p]:
                # Absent-but-non-nullable params must admit None, or strict
                # schemas force the model to always send a value.
                annotation = Optional[annotation]
            annotations[p] = annotation
        annotations["return"] = str
        inner.__annotations__ = annotations

    def proxy(*args: Any, **kwargs: Any) -> str:
        # The invoker lets only RagEngineError through, so a TypeError
        # here is the binding rejecting the arguments.
        try:
            return inner(*args, **kwargs)
        except TypeError as exc:
            payload, _ = _failure(
                f"Invalid arguments for {name}: {exc}", None,
                {"summary": "Invalid tool arguments",
                 "options": [f"Check the {name}() parameter names "
                             "and types"]},
                "INVALID_INPUT",
            )
            return _dumps(payload)
    proxy.__signature__ = inspect.signature(inner)  # type: ignore[attr-defined]
    proxy.__annotations__ = dict(inner.__annotations__)
    proxy.__name__ = proxy.__qualname__ = name or "tool"
    proxy.__doc__ = _tool_docstring(description or "", properties)
    return proxy


_BRIDGES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_BRIDGES_LOCK = threading.Lock()


def _cloud_bridge(client, gated: bool = False):
    """One bridge per client and endpoint gate (``gated`` = the read-only
    ?tools=read endpoint); tool discovery and instructions share a session
    per gate. Weak-keyed off the instance so clients stay picklable; the
    lock closes the check-then-set race under concurrent first calls."""
    with _BRIDGES_LOCK:
        # A rotated api_key or moved BASE_URL rebuilds the bridges.
        auth = (client.BASE_URL, client.api_key)
        bridges, seen = _BRIDGES.get(client) or ({}, None)
        if seen != auth:
            bridges = {}
        bridge = bridges.get(gated)
        if bridge is None:
            from .mcp_bridge import McpBridge
            bridge = McpBridge(
                f"{auth[0]}/mcp" + ("?tools=read" if gated else ""),
                {"Authorization": f"Bearer {auth[1]}"},
            )
            bridges[gated] = bridge
            _BRIDGES[client] = (bridges, auth)
        return bridge


def _require_doc_selection(doc_ids) -> None:
    """An empty selection fails loud: washed to None it would mean
    "everything", while the tool-layer allowlist would mean "nothing"."""
    if doc_ids is not None and not doc_ids:
        raise RagEngineError(
            "doc_id is empty. Pass one or more document IDs, or omit "
            "doc_id to give the agent the whole library.")


def _tool_specs(client, include_management: bool = False, doc_ids=None,
                ) -> "list[tuple[str, str, dict, Callable[[dict], tuple[list, bool]]]]":
    """(name, description, schema, invoke) per tool, for adapters that take
    the wire schema verbatim. ``invoke`` returns (content blocks, is_error):
    the MCP content as the server sent it, one text block from the local
    tools. Schemas are copies (frameworks keep the dict by reference).
    ``doc_ids`` is the local chat scope, already validated and dropped on
    cloud by ``_local_doc_scope``."""
    if getattr(client, "api_key", None):
        bridge = _cloud_bridge(client, gated=not include_management)
        tools_meta = bridge.list_tools()
        if not tools_meta:
            raise RagEngineError(
                "The MCP server returned no tools — a zero-tool agent would "
                "answer from the model's own knowledge, not the documents, "
                "with nothing to signal it."
            )
        return [(str(meta.get("name") or "tool"),
                 meta.get("description") or "",
                 copy.deepcopy(meta.get("inputSchema"))
                 or {"type": "object", "properties": {}},
                 _bridge_invoker(bridge, str(meta.get("name") or "tool"),
                                 meta.get("inputSchema") or {}))
                for meta in tools_meta]

    def local_invoke(name: str) -> "Callable[[dict], tuple[list, bool]]":
        def invoke(arguments: dict) -> tuple[list, bool]:
            text, is_error = call_tool(client, name, arguments,
                                       doc_ids=doc_ids)
            return [{"type": "text", "text": text}], is_error
        return invoke

    return [(name, _local_description(name), _local_schema(name),
             local_invoke(name))
            for name in tool_names(include_management)]


def build_agent_tools(client, include_management: bool = False,
                      ) -> list[Callable[..., str]]:
    """Plain synchronous functions bound to `client`.

    Cloud: one function per tool of the live cloud MCP tool set, signatures
    synthesized from the server's schemas, calls proxied over MCP. Local:
    the built-in contract tools over the local store. Every function returns
    the JSON envelope as a string (binary content, such as a cloud page
    image, as a size stub) and never raises for arguments its
    signature accepts — except a cloud 401/403, a 429/5xx that outlived
    the bridge's retries, an unreachable server or a RATE_LIMITED /
    USAGE_LIMIT_REACHED tool error, which re-raise
    RagEngineError (cloud-only parameters are absent from the local
    signatures; the call_tool path answers them with the guided envelope).
    """
    return [_make_tool_function(name, description, schema, invoke)
            for name, description, schema, invoke
            in _tool_specs(client, include_management)]


# ── agent instructions ──

_INSTRUCTIONS_HEADER = (
    "RAG is a document platform for uploading and "
    "managing long PDFs (research papers, financial reports, legal docs, "
    "textbooks, etc.)."
)

_READING_WORKFLOW = f"""\
READING WORKFLOW:
- For documents over {STRUCTURE_FIRST_PAGE_THRESHOLD} pages: call get_document_structure() first to locate relevant sections, then get_page_content() with targeted page ranges.
- For small documents ({STRUCTURE_FIRST_PAGE_THRESHOLD} pages or fewer): call get_page_content() directly."""

_TOOL_USAGE_RULES = """\
TOOL USAGE RULES:
- Invoke a tool only when all required parameters are present or clearly inferable. Never invent placeholder values.
- If a tool returns an error, present the provided next_steps/options to the user instead of retrying blindly."""

_DISCOVERY = """\
DOCUMENT DISCOVERY:
- search_content(query) — FIRST choice for any question about a specific fact: a record, drawing or RFI number, a name, a figure, a distinctive phrase. It searches the PAGES of all your documents and returns document names, page numbers and snippets. Put only the distinctive words in the query.
- browse_documents(query) — finds documents by name, description and section titles. Use it to narrow to the right project, entity or topic when several documents look alike, and for "what do I have" questions (the bare call lists your documents newest first with names and descriptions)."""

_DECISION = """\
DECISION:
- "What do I have / list / recent" → browse_documents()
- A specific fact, identifier or figure → search_content(), then read the best-matching pages with get_page_content()
- A topic, a project, or "find THE paper about Y" → browse_documents(query=...) with the distinctive words, then pick the documents whose name/description matches the question"""

_AFTER_DISCOVERY = """\
- If a question has NO possible connection to the documents (e.g., "capital of France"), do not answer it and do not use general knowledge: reply only that you can answer questions about the documents in this knowledge base.
- After discovery: 1 match or 1 clearly best match → proceed to read and answer without asking. Multiple equally relevant → ask user to pick.
- Results returned ≠ correct results. If the returned documents do not clearly match the user's intent (e.g., wrong topic, wrong time period, wrong document type), treat it the same as "not found" and continue the PERSISTENCE protocol below."""

_PERSISTENCE = """\
PERSISTENCE (before concluding the target document is not in the library):
This protocol applies both when results are empty AND when results are returned but none match the user's intent. Do NOT give up after a single discovery attempt. Follow these steps in order:
1. search_content() for the distinctive words, and browse_documents(query=...) for the project or topic; compare every returned name/description/snippet against the user's intent
2. Rephrase the query with synonyms, alternative spellings or different distinctive words and search again
3. Small library (has_more is false after a page or two): page through the ENTIRE library with `limit: 50` and `offset: next_offset` until has_more is false — MANDATORY, must be completed before concluding "not found". Large library (many pages of has_more): repeat steps 1-2 with at least two further, differently-worded queries instead of paging through every document.
Only after ALL steps have been tried may you conclude the document is not in the library. Do NOT fall back to general knowledge — if the user's question references their own documents, exhaust every discovery path first."""

_SEARCH_DISCIPLINE = """\
SEARCH DISCIPLINE (large libraries):
- Run at least two differently-worded searches (different distinctive words) before concluding that something is absent or that a document does not exist.
- Several documents can look alike — the same document type for different projects, towers or phases. Take the project or entity named in the question and check it against each candidate's name, description or page text before using it. Never mix facts from different projects.
- If the same identifier or figure appears in more than one project's documents, say so and report each one with its project instead of picking one.
- In the answer, name the document and project the facts came from."""

# Added on top of the bundled engine's original instruction set after a real query mismatched a table row on a partial field
# match and missed a non-English sentence buried in a long chat-export
# page -- both are verification/attention failures a plain "cite what you
# read" instruction doesn't guard against.
_EVIDENCE_REASONING = """\
EVIDENCE AND DATA REASONING:
- For a specific entity, activity, date, drawing number, villa/unit, cluster, clause, revision, or metric, verify ALL identifying fields in the supporting evidence before using a value — do not select a table row because only some fields match.
- For tabular data, confirm the project/entity, activity/metric, date, unit, and value all belong to the SAME row before reporting it. Different activities (e.g. "MEP first fix" vs. "villa superstructure") are different metrics even under the same cluster or villa.
- When comparing two dates, independently locate the exact value for each requested date and metric before calculating a difference.
- If multiple rows exist for the same entity/date/metric with different values, do not arbitrarily pick one — report the conflicting values and say the source data is inconsistent.
- Content may be in English, Arabic, or another language. A user's English query can and should be answered from relevant non-English document text — do not skip it.
- When asked what happened to a specific identifier (a unit number, an RFI/VO number, a drawing or clause number), inspect its exact occurrence in the retrieved text before concluding the information is absent. Absence claims need stronger evidence than normal answers — check all retrieved content likely to contain it first.
- Distinguish fact from inference: if evidence only suggests a possible cause, say so as an association, not as something the document states outright.
- Show the source values used in a calculation, and calculate only after verifying them. For progress questions, state both the absolute change and the percentage-point change, not just one."""

AGENT_INSTRUCTIONS = "\n\n".join([
    _INSTRUCTIONS_HEADER,
    _READING_WORKFLOW,
    _TOOL_USAGE_RULES,
    _DISCOVERY,
    _DECISION,
    _AFTER_DISCOVERY,
    _PERSISTENCE,
    _SEARCH_DISCIPLINE,
    _EVIDENCE_REASONING,
])


# Frozen from the cloud MCP server's ``cited_answer`` prompt, minus the
# bullet naming the cloud-only get_document_image() tool. Local page
# content carries no block_id, so the block rules stay dormant and
# citations resolve to pages.
LOCAL_CITATION_PROMPTS: dict[str, str] = {
    "markdown": """\
GROUNDING
- Answer only from the user's documents. Call get_page_content() and state only what was actually read there.
- Never fill a gap from general knowledge. When the documents do not answer the question, say so.

CITATIONS
- Cite only statements supported by tool outputs, as a bracketed reference: [{docName}, p. {pageNumber}] or [{docName}, p. {pageNumber}, block {blockId}]. Place immediately after the claim.
- When page content includes block_id values, citations MUST be block-level: copy the exact block_id of the supporting block. Page-only cites are allowed ONLY when the tool output carries no block_id (legacy documents, structure outlines). NEVER invent or alter block_id values.
- For a claim drawn from multiple blocks on one page, add one reference per supporting block (at most 3); beyond that, cite the single strongest block.
- Each reference must reference a SINGLE page integer. For multi-page citations, use separate references.""",
    "cite": """\
GROUNDING
- Answer only from the user's documents. Call get_page_content() and state only what was actually read there.
- Never fill a gap from general knowledge. When the documents do not answer the question, say so.

CITATIONS
- Cite only statements supported by tool outputs: <cite doc="{docName}" page="{pageNumber}"/> or <cite doc="{docName}" page="{pageNumber}" block="{blockId}"/>. Place immediately after the claim.
- When page content includes block_id values, citations MUST be block-level: copy the exact block_id of the supporting block. Page-only cites are allowed ONLY when the tool output carries no block_id (legacy documents, structure outlines). NEVER invent or alter block_id values.
- For a claim drawn from multiple blocks on one page, add one tag per supporting block (at most 3); beyond that, cite the single strongest block.
- Each tag must reference a SINGLE page integer. For multi-page citations, use separate tags.""",
}


def fetch_citation_prompt(client, format: str) -> str:
    """The MCP server's ``cited_answer`` prompt as system-prompt text;
    ``format`` rides as its one argument. Local: the frozen copy,
    page-level."""
    if format not in LOCAL_CITATION_PROMPTS:
        raise RagEngineError(
            f"citations format {format!r} is not one of "
            f"{', '.join(LOCAL_CITATION_PROMPTS)}.")
    if not getattr(client, "api_key", None):
        return LOCAL_CITATION_PROMPTS[format]
    _, messages = _cloud_bridge(client, gated=True).get_prompt(
        "cited_answer", {"format": format})
    text = render_prompt_text(messages)
    if not text.strip():
        raise RagEngineError(
            "The MCP server returned an empty cited_answer prompt.")
    return text


def _base_instructions(client, include_management: bool = False) -> str:
    """Cloud: the live instructions the MCP server serves for the tool set
    actually shipped. Local: the built-in subset instructions."""
    if not getattr(client, "api_key", None):
        base = AGENT_INSTRUCTIONS
    else:
        base = _cloud_bridge(
            client, gated=not include_management).instructions()
        if not isinstance(base, str) or not base.strip():
            raise RagEngineError(
                "The MCP server returned no agent instructions — refusing "
                "to substitute the SDK's local-subset guidance, which does "
                "not cover the cloud tool set."
            )
    own = getattr(client, "instructions", None)
    return f"{base}\n\n{own}" if own else base


#: Up to this many selected documents the targeting block lists each one's metadata (the managed
#: chat's rendering). Above it, the block is a short constant-size notice instead: every selected
#: document's metadata is ~130 tokens, re-sent on every model turn -- measured 65k tokens at 500
#: documents and 132k at 1,011 (~10x the cost per question), and it overflows a 400k context near
#: ~3,000. Scoping is unaffected: the tool layer enforces the allowlist (``_allowed_ids``)
#: regardless of what the prompt says.
TARGETING_FULL_BLOCK_MAX = 25


def _compact_targeting_block(client, doc_ids: list) -> str:
    wanted = {str(one_id) for one_id in doc_ids}
    found = {str(doc.get("id")) for doc in _all_documents(client, stop_ids=wanted)}
    missing = [one_id for one_id in doc_ids if str(one_id) not in found]
    if missing:
        raise RagEngineError(
            "Documents not found or access denied: " + ", ".join(map(str, missing)))
    return (
        f"The user has specified {len(doc_ids)} documents.\n"
        "Every tool is restricted to exactly these documents; no other document exists for "
        "this conversation. They are not listed here because there are many: find the relevant "
        "ones with search_content() (a specific fact or identifier) or browse_documents(query=...) "
        "(a project, entity or topic), then read them with get_page_content()."
    )


def doc_targeting_block(client, doc_id) -> Optional[str]:
    """The doc_id targeting text, rendered as the cloud's managed chat
    renders its own: the documents' metadata rows and the directive to
    work within them -- or, above ``TARGETING_FULL_BLOCK_MAX`` documents, a
    constant-size notice (see that constant). Conversation content, never
    system prompt: the chat lanes prepend it as the first user message,
    and document_context() hands it to callers who own the conversation."""
    if doc_id is None:
        return None
    if not isinstance(doc_id, (str, list)):
        raise RagEngineError("doc_id must be a string or a list of "
                                "strings.")
    doc_ids = [doc_id] if isinstance(doc_id, str) else list(doc_id)
    _require_doc_selection(doc_ids)
    if len(doc_ids) > TARGETING_FULL_BLOCK_MAX:
        return _compact_targeting_block(client, doc_ids)
    details = []
    missing = []
    for one_id in doc_ids:
        try:
            details.append(client.get_document(one_id))
        except RagEngineError as exc:
            # Batch only a definite not-found/denied (local raises carry no
            # status); a cloud transport failure (429/5xx) propagates raw.
            if exc.status_code not in (None, 403, 404):
                raise
            missing.append(str(one_id))
    if missing:
        raise RagEngineError(
            "Documents not found or access denied: " + ", ".join(missing))
    if len(details) == 1:
        return (
            f"The user has specified document: {details[0].get('name')}\n"
            f"Document metadata: {json.dumps(details[0], ensure_ascii=False)}\n"
            "Use this document's name to retrieve its content with "
            "get_document_structure() and get_page_content()."
        )
    names = ", ".join(str(item.get("name")) for item in details)
    return (
        f"The user has specified documents: {names}\n"
        f"Documents metadata: {json.dumps(details, ensure_ascii=False)}\n"
        "Use these documents' names to retrieve their content with "
        "get_document_structure() and get_page_content()."
    )


def folder_targeting_block(client, folder_id) -> Optional[str]:
    """The folder_id targeting text, rendered as the cloud's managed chat
    renders its own: the folder's name and metadata and the directive to
    discover its documents there. None for no folder — None, "", and
    "root", the library itself, which the managed chat leaves untargeted.
    A folder proper is cloud-only: local libraries have none."""
    if folder_id is None:
        return None
    if not isinstance(folder_id, str):
        raise RagEngineError("folder_id must be a string.")
    if folder_id in ("", "root"):
        return None
    if not getattr(client, "api_key", None):
        raise RagEngineError(
            "folder_id is cloud-only — folders are not supported in local "
            "mode. Create the client with an api_key to use folders.")
    folders = client.list_folders().get("folders") or []
    folder = next((f for f in folders if f.get("id") == folder_id), None)
    if folder is None:
        raise RagEngineError(
            f"Folder not found or access denied: {folder_id}")
    metadata = {key: folder[key] for key in ("id", "name", "description")
                if folder.get(key)}
    return (
        f"The user has specified folder: {folder.get('name')}\n"
        f"Folder metadata: {json.dumps(metadata, ensure_ascii=False)}\n"
        "Discover its documents with "
        f'browse_documents(folder_id="{folder_id}", recursive=true) '
        f'or search_documents(query, folder_id="{folder_id}", '
        "recursive=true)."
    )


def targeting_block(client, doc_id, folder_id=None) -> Optional[str]:
    """The chat lanes' leading user message: the folder block, then the
    document block, joined as the managed chat joins them; None when
    there is nothing to place."""
    blocks = [folder_targeting_block(client, folder_id),
              doc_targeting_block(client, doc_id)]
    return "\n\n".join(block for block in blocks if block) or None
