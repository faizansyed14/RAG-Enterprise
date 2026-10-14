"""Decide which documents a chat question may read.

Replaces api/chat.py's "load every indexed document id" step (millions of rows per question at scale)
with: an explicit, access-checked selection, or hybrid-retrieved candidates.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import select

from app.models.db import Document, async_session
from app.scale import acl as acl_mod
from app.scale.search import Candidate, candidate_documents
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)


@dataclass
class ScopeRow:
    document_id: uuid.UUID
    rag_doc_id: str
    filename: str


def retrieval_text(query: str, history: list[dict[str, str]] | None) -> str:
    """A short follow-up ('and the second one?') carries no searchable meaning on its own: borrow the
    previous user turn."""
    if history and len(re.findall(r"\w+", query)) < 8:
        previous = [m["content"] for m in history if m.get("role") == "user"]
        if previous:
            return f"{previous[-1][:400]} {query}"
    return query


async def resolve_scope(
    query: str,
    selected_ids: list[uuid.UUID] | None,
    user,
    history: list[dict[str, str]] | None = None,
) -> list[ScopeRow]:
    cfg = get_scale_settings()
    bypass = cfg.admin_bypass_acl and user.role == "admin"
    principals = None if bypass else await asyncio.to_thread(acl_mod.principals_for_user, user.user_id, user.username)

    async def rows_for(ids: list[uuid.UUID | str]) -> list[ScopeRow]:
        if not ids:
            return []
        async with async_session() as session:
            result = await session.execute(
                select(Document.document_id, Document.rag_doc_id, Document.filename).where(
                    Document.document_id.in_([uuid.UUID(str(i)) for i in ids]),
                    Document.status == "indexed",
                    Document.rag_doc_id.is_not(None),
                )
            )
            return [ScopeRow(r.document_id, r.rag_doc_id, r.filename) for r in result.all()]

    scope: list[str] | None = None
    if selected_ids is not None:
        if bypass:
            chosen = [str(i) for i in selected_ids]
        else:
            visible = await asyncio.to_thread(acl_mod.visible_document_ids, selected_ids, principals)
            chosen = [str(i) for i in selected_ids if str(i) in visible]   # a forbidden id is silently dropped
        if len(chosen) <= cfg.candidate_docs:
            # A hand-picked handful: exactly what the user chose, in the order they chose it.
            return await rows_for(chosen)
        # A long list is a scope ("everything I can see", which is what the current UI sends when nothing is
        # narrowed), not a reading list. Handing 1,000+ documents to the agent is the problem this layer
        # exists to remove, so retrieve inside that scope.
        scope = chosen

    text = retrieval_text(query, history)
    if scope is None or len(scope) <= cfg.scope_filter_max:
        candidates = await asyncio.to_thread(candidate_documents, text, principals, None, scope)
    else:
        candidates = await asyncio.to_thread(_partitioned_candidates, text, principals, scope, cfg)
    by_id = {c.document_id: c for c in candidates}
    rows = await rows_for(list(by_id))
    order = {d: i for i, d in enumerate(by_id)}
    rows.sort(key=lambda r: order.get(str(r.document_id), 1 << 30))
    return rows


def _partitioned_candidates(query: str, principals: set[str] | None, scope: list[str], cfg) -> list[Candidate]:
    """A scope too long for one index filter is split into parts that each fit, every part is searched inside the index,
    and the results are merged by score. Searching everything and discarding what is outside the scope afterwards would lose
    relevant selected documents that never reached the global top candidates (a false "nothing found")."""
    size = max(1, cfg.scope_filter_max)
    parts = [scope[i: i + size] for i in range(0, len(scope), size)]
    if len(parts) > cfg.scope_partition_max:
        log.warning("scope of %d documents needs %d index searches (limit %d); searching the whole authorised corpus and "
                    "filtering afterwards, which can miss selected documents. Raise SCALE_SCOPE_PARTITION_MAX or narrow the selection.",
                    len(scope), len(parts), cfg.scope_partition_max)
        wanted = set(scope)
        return [c for c in candidate_documents(query, principals, None, None) if c.document_id in wanted]
    with ThreadPoolExecutor(max_workers=min(4, len(parts))) as pool:
        results = list(pool.map(lambda part: candidate_documents(query, principals, None, part), parts))
    merged = sorted((c for r in results for c in r), key=lambda c: -c.best_score)
    return merged[: cfg.candidate_docs]
