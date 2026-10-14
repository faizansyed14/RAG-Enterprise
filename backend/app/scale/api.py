"""HTTP surface for the scale layer (always mounted).

  GET  /api/scale/search?q=...      access-filtered hybrid search, no LLM: what the retrieval stage sees
  GET  /api/scale/status            queue depth, item states, skipped/failed reasons   (admin)
  POST /api/scale/promote/{id}      queue the full section tree build for one document (admin)

Chunk editor (read: anyone who may read the document; change: admin)
  GET    /api/scale/documents/{id}/chunks            the chunks the index holds for a document, with edits, a page at a time
  GET    /api/scale/documents/{id}/chunks/{n}
  PATCH  /api/scale/documents/{id}/chunks/{n}        text / keywords / questions / available
  POST   /api/scale/documents/{id}/chunks            add a chunk by hand
  DELETE /api/scale/documents/{id}/chunks/{n}        delete a hand-written chunk
  GET    /api/scale/documents/{id}/chunk-history     who changed what                                  (admin)
  GET    /api/scale/retrieval-test?q=...             search with every score shown: why a chunk was or was not found
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.security import CurrentUser, get_current_user, require_admin
from app.scale import acl as acl_mod
from app.scale import queue
from app.scale.db import engine
from app.scale.search import search_chunks
from app.scale.settings import get_scale_settings

router = APIRouter(prefix="/api/scale", tags=["scale"])


@router.get("/search")
async def search(q: str = Query(min_length=2, max_length=500), k: int = Query(10, ge=1, le=50),
                 user: CurrentUser = Depends(get_current_user)) -> dict:
    cfg = get_scale_settings()
    bypass = cfg.admin_bypass_acl and user.role == "admin"
    principals = None if bypass else await asyncio.to_thread(acl_mod.principals_for_user, user.user_id, user.username)
    hits = await asyncio.to_thread(
        lambda: search_chunks(q, principals=principals, limit=k, allow_unfiltered=bypass)
    )
    allowed = (
        {h.document_id for h in hits} if bypass
        else await asyncio.to_thread(acl_mod.visible_document_ids, list({h.document_id for h in hits}), principals)
    )
    return {
        "results": [
            {"document_id": h.document_id, "name": h.name, "path": h.path, "page": h.page,
             "score": round(h.score, 4), "snippet": h.text[:400]}
            for h in hits if h.document_id in allowed
        ]
    }


def _status() -> dict:
    with engine().connect() as conn:
        states = dict(conn.execute(text("SELECT state, count(*) FROM source_items WHERE NOT deleted GROUP BY 1")).all())
        reasons = [
            {"state": r[0], "reason": r[1], "count": r[2]}
            for r in conn.execute(
                text(
                    "SELECT state, left(coalesce(error, ''), 80), count(*) FROM source_items "
                    "WHERE state IN ('skipped', 'failed') AND NOT deleted GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25"
                )
            )
        ]
        tiers = dict(conn.execute(text("SELECT tier, count(*) FROM documents GROUP BY 1")).all())
        workers = [
            {"id": r[0].split(":", 1)[1], "seen_seconds": round(float(r[1]), 1)}
            for r in conn.execute(
                text("SELECT key, extract(epoch FROM (now() - updated_at)) FROM scale_state "
                     "WHERE key LIKE 'worker:%' AND updated_at > now() - interval '1 day' ORDER BY updated_at DESC LIMIT 50")
            )
        ]
    from app.scale import migration, reconcile

    try:
        moving = migration.status()
    except Exception:  # noqa: BLE001 -- the health screen must load even if the index cannot be reached
        moving = None
    return {"migration": moving, "items": states, "reasons": reasons, "documents_by_tier": tiers, "jobs": queue.stats(), "workers": workers,
            "oldest_queued_seconds": queue.oldest_queued_age_seconds(), "index_health": reconcile.summary()}


@router.get("/migration")
async def migration_status(_admin: CurrentUser = Depends(require_admin)) -> dict:
    """Is a changed embedding model still being rebuilt, and how far along is it?"""
    from app.scale import migration

    return await asyncio.to_thread(migration.status)


@router.get("/status")
async def status(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return await asyncio.to_thread(_status)


@router.post("/reconcile")
async def reconcile_now(force: bool = False, _admin: CurrentUser = Depends(require_admin)) -> dict:
    """Queue a comparison of the documents with the search index (and the repair of any difference). `force` compares every
    document now instead of only those that are due."""
    job = await asyncio.to_thread(queue.enqueue, "reconcile", {"force": bool(force), "orphans": bool(force)}, dedupe_key="reconcile")
    if job is None:
        raise HTTPException(409, "A comparison is already queued or running")
    return {"job_id": job}


@router.post("/promote/{document_id}")
async def promote(document_id: uuid.UUID, _admin: CurrentUser = Depends(require_admin)) -> dict:
    from app.core.config import get_settings

    if not get_settings().tree_generation:
        raise HTTPException(409, "Tree generation is switched off (TREE_GENERATION=false in .env). Switch it on and restart to build trees.")
    job = await asyncio.to_thread(
        queue.enqueue, "promote_tree", {"document_id": str(document_id)}, dedupe_key=f"promote:{document_id}"
    )
    if job is None:
        raise HTTPException(409, "A promotion for this document is already queued")
    return {"job_id": job}


# ── chunk editor ──


async def _principals(user: CurrentUser) -> set[str] | None:
    """The caller's principals, or None for the explicit admin bypass."""
    cfg = get_scale_settings()
    if cfg.admin_bypass_acl and user.role == "admin":
        return None
    return await asyncio.to_thread(acl_mod.principals_for_user, user.user_id, user.username)


async def _require_read(document_id: uuid.UUID, user: CurrentUser) -> None:
    principals = await _principals(user)
    if principals is None:
        return
    visible = await asyncio.to_thread(acl_mod.visible_document_ids, [document_id], principals)
    if str(document_id) not in visible:
        raise HTTPException(404, "No such document")           # not 403: do not confirm that it exists


def _edit_error(exc) -> HTTPException:
    status = {"not_found": 404, "not_indexed": 409, "conflict": 409, "invalid": 422}.get(exc.code, 400)
    return HTTPException(status, exc.message)


class ChunkPatch(BaseModel):
    text: str | None = None                    # null = back to the extracted text
    keywords: list[str] | None = None
    questions: list[str] | None = None
    available: bool | None = None


class ChunkNew(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    page: int = Field(1, ge=1)
    keywords: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


@router.get("/documents/{document_id}/chunks")
async def list_chunks(document_id: uuid.UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                      q: str = Query("", max_length=200), kind: str = Query("", max_length=60), unavailable: bool = False,
                      user: CurrentUser = Depends(get_current_user)) -> dict:
    from app.scale import chunk_edit

    await _require_read(document_id, user)
    kinds = {k for k in kind.split(",") if k} or None
    return await asyncio.to_thread(chunk_edit.list_chunks, str(document_id), offset=offset, limit=limit, query=q, kinds=kinds,
                                   unavailable_only=unavailable)


@router.get("/documents/{document_id}/chunks/{chunk}")
async def get_chunk(document_id: uuid.UUID, chunk: int, user: CurrentUser = Depends(get_current_user)) -> dict:
    from app.scale import chunk_edit

    await _require_read(document_id, user)
    try:
        return await asyncio.to_thread(chunk_edit.get_chunk, str(document_id), chunk)
    except chunk_edit.ChunkEditError as exc:
        raise _edit_error(exc) from exc


@router.patch("/documents/{document_id}/chunks/{chunk}")
async def patch_chunk(document_id: uuid.UUID, chunk: int, body: ChunkPatch, admin: CurrentUser = Depends(require_admin)) -> dict:
    from app.scale import chunk_edit

    changes = {k: getattr(body, k) for k in body.model_fields_set}
    try:
        return await asyncio.to_thread(lambda: chunk_edit.update_chunk(str(document_id), chunk, actor=admin.username, **changes))
    except chunk_edit.ChunkEditError as exc:
        raise _edit_error(exc) from exc


@router.post("/documents/{document_id}/chunks", status_code=201)
async def add_chunk(document_id: uuid.UUID, body: ChunkNew, admin: CurrentUser = Depends(require_admin)) -> dict:
    from app.scale import chunk_edit

    try:
        return await asyncio.to_thread(lambda: chunk_edit.add_chunk(
            str(document_id), body.text, page=body.page, keywords=body.keywords, questions=body.questions, actor=admin.username))
    except chunk_edit.ChunkEditError as exc:
        raise _edit_error(exc) from exc


@router.delete("/documents/{document_id}/chunks/{chunk}")
async def delete_chunk(document_id: uuid.UUID, chunk: int, admin: CurrentUser = Depends(require_admin)) -> dict:
    from app.scale import chunk_edit

    try:
        return await asyncio.to_thread(lambda: chunk_edit.delete_chunk(str(document_id), chunk, actor=admin.username))
    except chunk_edit.ChunkEditError as exc:
        raise _edit_error(exc) from exc


@router.get("/documents/{document_id}/chunk-history")
async def chunk_history(document_id: uuid.UUID, limit: int = Query(100, ge=1, le=500), _admin: CurrentUser = Depends(require_admin)) -> dict:
    from app.scale import chunk_edit

    return {"history": await asyncio.to_thread(chunk_edit.history, str(document_id), limit)}


@router.get("/retrieval-test")
async def retrieval_test(q: str = Query(min_length=2, max_length=500), k: int = Query(10, ge=1, le=50), rerank: bool = True,
                         user: CurrentUser = Depends(get_current_user)) -> dict:
    """Search exactly as the application does, with every number shown, for the person tuning chunks."""
    principals = await _principals(user)
    cfg = get_scale_settings()
    hits = await asyncio.to_thread(lambda: search_chunks(q, principals=principals, limit=k, rerank=rerank, allow_unfiltered=principals is None))
    allowed = ({h.document_id for h in hits} if principals is None
               else await asyncio.to_thread(acl_mod.visible_document_ids, list({h.document_id for h in hits}), principals))
    return {
        "settings": {"dense_weight": cfg.fusion_dense_weight, "rerank_mode": cfg.rerank_mode,
                     "query_keywords": cfg.query_keywords},
        "results": [
            {"document_id": h.document_id, "name": h.name, "page": h.page, "chunk": h.chunk, "kind": h.kind or "extracted",
             "score": round(h.score, 4), "dense": None if h.dense_score is None else round(h.dense_score, 4),
             "keyword": None if h.sparse_score is None else round(h.sparse_score, 4), "text": h.text[:600]}
            for h in hits if h.document_id in allowed
        ],
    }

