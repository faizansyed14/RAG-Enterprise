"""The chunk editor: look at the chunks a document was cut into, and fix what search does with them.

What a person can do, per chunk
  * correct its text (an OCR slip, a mangled table row) -- the chunk is re-embedded and re-indexed with the new text
  * add keywords and questions people would search with -- indexed on the keyword side AND embedded
  * switch it off (boilerplate, a disclaimer repeated on every page) -- it stops being found, without being deleted
  * add a chunk by hand (an FAQ answer, a note that is not in the file) and delete such a chunk again

What an edit does and does not change
  * The edit is stored in Postgres (chunk_edits) and applied to the search index. It changes what search FINDS and the text of
    the hit that search shows. The file, its stored pages and the page citations are the source's and are not rewritten.
  * With SCALE_CHUNK_EDIT_OVERLAY (default on) the agent's keyword search tool also shows the corrected text of a page.
  * An edit follows its chunk by a hash of the chunk's ORIGINAL text, not by its position. If the file is re-ingested and the
    chunk is unchanged, the edit is re-applied even if the chunk moved. If the chunk's text changed, the edit is kept but marked
    `orphaned` and is not applied (it would be editing text that is no longer there); the editor lists it so a person can redo it.
  * A chunk added by hand has no original: it belongs to the document, is re-applied on every re-index, and inherits the
    document's access rules like any other chunk (they are filtered by document_id and acl in every search).

Numbers: extracted chunks are 0..N-1; chunks added by hand start at MANUAL_BASE.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import text as sql

from app.scale import index as ix
from app.scale.chunking import Chunk, chunk_document
from app.scale.db import engine
from app.scale.pieces import Piece, index_pieces
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)

MANUAL_BASE = 1_000_000
MAX_TEXT = 20_000
MAX_TERMS = 40

UNSET = object()
_WS = re.compile(r"\s+")


class ChunkEditError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code            # not_found | not_indexed | conflict | invalid
        self.message = message


def chunk_hash(chunk_text: str) -> str:
    return hashlib.sha1(_WS.sub(" ", chunk_text or "").strip().encode("utf-8")).hexdigest()[:24]


def _norm(value: str) -> str:
    return _WS.sub(" ", value or "").strip()


# ── rows ──


@dataclass
class EditRow:
    edit_id: str
    document_id: str
    kind: str
    chunk: int
    occurrence: int = 0
    orig_hash: str | None = None
    orig_text: str | None = None
    page: int | None = None
    text: str | None = None
    keywords: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    available: bool = True
    orphaned: bool = False

    def snapshot(self) -> dict:
        return {"text": self.text, "keywords": list(self.keywords), "questions": list(self.questions), "available": self.available}


_COLS = "edit_id, document_id, kind, chunk, occurrence, orig_hash, orig_text, page, text, keywords, questions, available, orphaned"


def _row(m) -> EditRow:
    return EditRow(
        edit_id=str(m["edit_id"]), document_id=str(m["document_id"]), kind=m["kind"], chunk=m["chunk"],
        occurrence=m["occurrence"], orig_hash=m["orig_hash"], orig_text=m["orig_text"], page=m["page"], text=m["text"],
        keywords=list(m["keywords"] or []), questions=list(m["questions"] or []), available=m["available"],
        orphaned=m["orphaned"],
    )


def rows_for(document_id: str) -> list[EditRow]:
    with engine().connect() as conn:
        return [_row(m) for m in conn.execute(
            sql(f"SELECT {_COLS} FROM chunk_edits WHERE document_id = :d ORDER BY kind, chunk"), {"d": document_id}).mappings()]


def _clean_terms(values, *, limit: int, max_chars: int, what: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ChunkEditError("invalid", f"{what} must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            raise ChunkEditError("invalid", f"{what} must be a list of strings")
        v = _norm(v)
        if not v or v.casefold() in seen:
            continue
        if len(v) > max_chars:
            raise ChunkEditError("invalid", f"a {what[:-1] if what.endswith('s') else what} is longer than {max_chars} characters")
        seen.add(v.casefold())
        out.append(v)
    if len(out) > limit:
        raise ChunkEditError("invalid", f"at most {limit} {what} per chunk")
    return out


def _clean_text(value, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ChunkEditError("invalid", "text is required")
        return None
    if not isinstance(value, str):
        raise ChunkEditError("invalid", "text must be a string")
    value = value.replace("\x00", "").strip()
    if required and not value:
        raise ChunkEditError("invalid", "text must not be empty")
    if len(value) > MAX_TEXT:
        raise ChunkEditError("invalid", f"text is longer than {MAX_TEXT} characters")
    return value


# ── the document a chunk belongs to ──


@dataclass
class DocContext:
    document_id: str
    rag_doc_id: str
    name: str
    path: str
    source: str
    acl: list[str]
    _pages: list[str] | None = None
    _chunks: list[Chunk] | None = None

    def pages(self) -> list[str]:
        if self._pages is None:
            from app.rag_core.postgres_store import PostgresDocStore

            self._pages = [p.get("markdown", "") for p in (PostgresDocStore().get_pages(self.rag_doc_id) or []) if isinstance(p, dict)]
        return self._pages

    def chunks(self) -> list[Chunk]:
        if self._chunks is None:
            self._chunks = chunk_document(self.pages(), title=self.name, path=self.path)
        return self._chunks


def context_for(document_id: str) -> DocContext:
    """The facts every chunk of a document shares (name, path, source, access), read from the index itself so that an
    edited chunk is written exactly like its neighbours."""
    with engine().connect() as conn:
        row = conn.execute(sql("SELECT rag_doc_id, status FROM documents WHERE document_id = :d"), {"d": document_id}).first()
    if row is None:
        raise ChunkEditError("not_found", "no such document")
    if row[1] != "indexed" or not row[0]:
        raise ChunkEditError("not_indexed", "the document has not finished indexing")
    first = ix.first_point(document_id)
    if first is None:
        raise ChunkEditError("not_indexed", "the document is not in the search index yet")
    p = first.payload or {}
    return DocContext(document_id=str(document_id), rag_doc_id=p.get("doc_id") or row[0], name=p.get("name", ""),
                      path=p.get("path", ""), source=p.get("source", ""), acl=list(p.get("acl") or []))


# ── applying edits when a document is (re-)indexed ──


@dataclass
class IndexingPlan:
    edits: dict[int, EditRow] = field(default_factory=dict)        # extracted chunk index -> its edit
    manual: list[EditRow] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.edits or self.manual)


def occurrences(chunks: list[Chunk]) -> list[tuple[str, int]]:
    """(hash of original text, how many identical chunks came before it) for each chunk."""
    seen: dict[str, int] = {}
    out = []
    for c in chunks:
        h = chunk_hash(c.text)
        out.append((h, seen.get(h, 0)))
        seen[h] = seen.get(h, 0) + 1
    return out


def plan_for_indexing(document_id: str, chunks: list[Chunk]) -> IndexingPlan:
    """Match saved edits to the chunks the file has NOW. An edit whose chunk text is gone is marked orphaned."""
    try:
        rows = rows_for(document_id)
    except Exception:  # noqa: BLE001 -- a database without the editor tables (migration 0014 not applied) indexes as before
        log.warning("chunk_edits could not be read; indexing without editor changes", exc_info=True)
        return IndexingPlan()
    if not rows:
        return IndexingPlan()
    keys = {key: i for i, key in enumerate(occurrences(chunks))}
    plan = IndexingPlan()
    moves: list[tuple[str, int, bool]] = []
    for r in rows:
        if r.kind == "manual":
            plan.manual.append(r)
            continue
        idx = keys.get((r.orig_hash, r.occurrence))
        if idx is None:
            moves.append((r.edit_id, r.chunk, True))
            continue
        plan.edits[idx] = r
        moves.append((r.edit_id, idx, False))
    with engine().begin() as conn:
        for edit_id, idx, orphaned in moves:
            conn.execute(sql("UPDATE chunk_edits SET chunk = :c, orphaned = :o WHERE edit_id = :e"), {"c": idx, "o": orphaned, "e": edit_id})
    return plan


def carry_over(from_document: str, to_document: str) -> int:
    """A changed file becomes a NEW document (documents are identified by their content), so the editor's work on the previous
    version is copied to it. Whether each edit still applies is decided when the new version is indexed (plan_for_indexing)."""
    try:
        with engine().begin() as conn:
            return conn.execute(
                sql("INSERT INTO chunk_edits (document_id, kind, chunk, occurrence, orig_hash, orig_text, page, text, keywords, questions, "
                    "available, orphaned, created_by, updated_by, created_at, updated_at) "
                    "SELECT :new, kind, chunk, occurrence, orig_hash, orig_text, page, text, keywords, questions, available, orphaned, "
                    "created_by, updated_by, created_at, updated_at FROM chunk_edits WHERE document_id = :old ON CONFLICT DO NOTHING"),
                {"new": to_document, "old": from_document}).rowcount
    except Exception:  # noqa: BLE001 -- never fail an ingest over the editor
        log.warning("could not carry chunk edits from %s to %s", from_document, to_document, exc_info=True)
        return 0


def apply_plan(pieces: list[Piece], plan: IndexingPlan, context_for_manual: str) -> list[Piece]:
    """Edits onto the extracted pieces, and the hand-written chunks after them."""
    for piece in pieces:
        r = plan.edits.get(piece.index)
        if r is None:
            continue
        if r.text is not None:
            piece.text, piece.kind = r.text, "edited"
        piece.user_keywords, piece.user_questions, piece.available = list(r.keywords), list(r.questions), r.available
        if piece.kind == "" and (r.keywords or r.questions or not r.available):
            piece.kind = "edited"
    for r in plan.manual:
        pieces.append(Piece(page=r.page or 1, index=r.chunk, text=r.text or "", context=context_for_manual, kind="manual",
                            user_keywords=list(r.keywords), user_questions=list(r.questions), available=r.available))
    return pieces


# ── the editor's operations ──


def _piece_for(ctx: DocContext, chunk: Chunk, row: EditRow | None, payload: dict) -> Piece:
    piece = Piece(page=chunk.page, index=chunk.index, text=chunk.text, context=chunk.context, start=chunk.start, end=chunk.end,
                  keywords=list(payload.get("kw_llm") or []), questions=list(payload.get("q_llm") or []))
    if row is not None:
        apply_plan([piece], IndexingPlan(edits={chunk.index: row}), "")
    return piece


def _log(conn, document_id: str, chunk: int | None, action: str, actor: str | None, before, after) -> None:
    conn.execute(
        sql("INSERT INTO chunk_edit_log (document_id, chunk, action, actor, before, after) "
            "VALUES (:d, :c, :a, :u, CAST(:b AS jsonb), CAST(:f AS jsonb))"),
        {"d": document_id, "c": chunk, "a": action, "u": actor,
         "b": json.dumps(before, ensure_ascii=False) if before is not None else None,
         "f": json.dumps(after, ensure_ascii=False) if after is not None else None},
    )


def _view(piece_payload: dict, hit_text: str, row: EditRow | None, chunk: int, page: int) -> dict:
    kind = piece_payload.get("kind") or "extracted"
    return {
        "chunk": chunk, "page": page, "kind": kind, "text": hit_text,
        "original_text": row.orig_text if (row is not None and row.kind == "edit" and row.text is not None) else None,
        "available": piece_payload.get("available", True) is not False,
        "keywords": list(piece_payload.get("kw_user") or []), "questions": list(piece_payload.get("q_user") or []),
        "generated_keywords": list(piece_payload.get("kw_llm") or []), "generated_questions": list(piece_payload.get("q_llm") or []),
    }


def list_chunks(document_id: str, *, offset: int = 0, limit: int = 50, query: str = "", kinds: set[str] | None = None,
                unavailable_only: bool = False) -> dict:
    """The chunks of a document as the index holds them (with their edits), a page at a time, plus edits whose chunk is gone."""
    from app.scale import search as search_mod

    points = ix.scroll_document(document_id)
    rows = rows_for(document_id)
    by_chunk = {r.chunk: r for r in rows if not r.orphaned}
    hits = search_mod.hydrate([ix._hit(p, score=0.0) for p in points])
    items = []
    q = query.casefold().strip()
    for p, h in zip(points, hits):
        v = _view(p.payload or {}, h.text, by_chunk.get(h.chunk), h.chunk, h.page)
        if kinds and v["kind"] not in kinds:
            continue
        if unavailable_only and v["available"]:
            continue
        if q and q not in " ".join([v["text"], *v["keywords"], *v["questions"], *v["generated_keywords"], *v["generated_questions"]]).casefold():
            continue
        items.append(v)
    orphans = [{"chunk": r.chunk, "original_text": r.orig_text, "text": r.text, "keywords": r.keywords, "questions": r.questions,
                "available": r.available} for r in rows if r.orphaned]
    return {"document_id": str(document_id), "total": len(items), "offset": offset, "limit": limit,
            "chunks": items[offset: offset + limit], "orphaned_edits": orphans}


def get_chunk(document_id: str, chunk: int) -> dict:
    from app.scale import search as search_mod

    points = ix.get_points(document_id, [chunk])
    if not points:
        raise ChunkEditError("not_found", "no such chunk")
    h = search_mod.hydrate([ix._hit(points[0], score=0.0)])[0]
    row = next((r for r in rows_for(document_id) if r.chunk == chunk and not r.orphaned), None)
    return _view(points[0].payload or {}, h.text, row, chunk, h.page)


def update_chunk(document_id: str, chunk: int, *, text=UNSET, keywords=UNSET, questions=UNSET, available=UNSET, actor: str | None = None) -> dict:
    """Change an extracted or hand-written chunk. Pass only what changes (UNSET = leave as it is; text=None = back to the original)."""
    ctx = context_for(document_id)
    if chunk >= MANUAL_BASE:
        return _update_manual(ctx, chunk, text, keywords, questions, available, actor)
    chunks = ctx.chunks()
    if not 0 <= chunk < len(chunks):
        raise ChunkEditError("not_found", "no such chunk")
    c = chunks[chunk]
    points = ix.get_points(document_id, [chunk])
    if not points:
        raise ChunkEditError("not_found", "that chunk is not in the search index")
    payload = points[0].payload or {}
    if (payload.get("page"), payload.get("start"), payload.get("end")) != (c.page, c.start, c.end):
        raise ChunkEditError("conflict", "this document was indexed with different chunking settings or a different version of the file; re-index it before editing")
    h, occ = occurrences(chunks)[chunk]
    row = next((r for r in rows_for(document_id) if r.kind == "edit" and r.orig_hash == h and r.occurrence == occ), None)
    old = row.snapshot() if row else {"text": None, "keywords": [], "questions": [], "available": True}
    new = dict(old)
    if text is not UNSET:
        value = _clean_text(text, required=False)
        new["text"] = None if (value is None or _norm(value) == _norm(c.text)) else value
        if value is not None and not value:
            raise ChunkEditError("invalid", "text must not be empty (to switch the chunk off, set available=false)")
    if keywords is not UNSET:
        new["keywords"] = _clean_terms(keywords, limit=MAX_TERMS, max_chars=80, what="keywords")
    if questions is not UNSET:
        new["questions"] = _clean_terms(questions, limit=MAX_TERMS, max_chars=300, what="questions")
    if available is not UNSET:
        new["available"] = bool(available)
    if new == old:
        return get_chunk(document_id, chunk)

    plain = new["text"] is None and not new["keywords"] and not new["questions"] and new["available"]
    with engine().begin() as conn:
        if plain:
            if row is not None:
                conn.execute(sql("DELETE FROM chunk_edits WHERE edit_id = :e"), {"e": row.edit_id})
        else:
            conn.execute(
                sql("INSERT INTO chunk_edits (document_id, kind, chunk, occurrence, orig_hash, orig_text, page, text, keywords, questions, available, created_by, updated_by) "
                    "VALUES (:d, 'edit', :c, :o, :h, :ot, :p, :t, :k, :q, :a, :u, :u) "
                    "ON CONFLICT (document_id, orig_hash, occurrence) WHERE kind = 'edit' DO UPDATE SET "
                    "chunk = EXCLUDED.chunk, text = EXCLUDED.text, keywords = EXCLUDED.keywords, questions = EXCLUDED.questions, "
                    "available = EXCLUDED.available, orphaned = false, updated_by = EXCLUDED.updated_by, updated_at = now()"),
                {"d": document_id, "c": chunk, "o": occ, "h": h, "ot": c.text, "p": c.page, "t": new["text"], "k": new["keywords"],
                 "q": new["questions"], "a": new["available"], "u": actor},
            )
        _log(conn, document_id, chunk, "revert" if plain else "update", actor, old, new)
    row_after = None if plain else EditRow("", document_id, "edit", chunk, occ, h, c.text, c.page, new["text"], new["keywords"], new["questions"], new["available"])
    content_changed = plain or any(new[k] != old[k] for k in ("text", "keywords", "questions"))
    _write(ctx, chunk, c, row_after, payload, content_changed=content_changed)
    return get_chunk(document_id, chunk)


def _write(ctx: DocContext, chunk: int, c: Chunk, row: EditRow | None, payload: dict, *, content_changed: bool) -> None:
    """Bring the index point in line with the saved edit. A text / keyword change (or going back to the original) rewrites and
    re-embeds that one chunk; switching a chunk on or off rewrites one payload field."""
    if content_changed:
        piece = _piece_for(ctx, c, row, payload)
        index_pieces(ctx.document_id, ctx.rag_doc_id, ctx.name, ctx.path, ctx.source, ctx.acl, [piece])
    else:
        ix.set_point_payload(ctx.document_id, chunk, {"available": bool(row.available) if row is not None else True, "kind": "edited"})


def _update_manual(ctx: DocContext, chunk: int, text, keywords, questions, available, actor) -> dict:
    rows = [r for r in rows_for(ctx.document_id) if r.kind == "manual" and r.chunk == chunk]
    if not rows:
        raise ChunkEditError("not_found", "no such chunk")
    r = rows[0]
    old = r.snapshot()
    new = dict(old)
    if text is not UNSET:
        new["text"] = _clean_text(text, required=True)
    if keywords is not UNSET:
        new["keywords"] = _clean_terms(keywords, limit=MAX_TERMS, max_chars=80, what="keywords")
    if questions is not UNSET:
        new["questions"] = _clean_terms(questions, limit=MAX_TERMS, max_chars=300, what="questions")
    if available is not UNSET:
        new["available"] = bool(available)
    if new == old:
        return get_chunk(ctx.document_id, chunk)
    with engine().begin() as conn:
        conn.execute(sql("UPDATE chunk_edits SET text = :t, keywords = :k, questions = :q, available = :a, updated_by = :u, updated_at = now() WHERE edit_id = :e"),
                     {"t": new["text"], "k": new["keywords"], "q": new["questions"], "a": new["available"], "u": actor, "e": r.edit_id})
        _log(conn, ctx.document_id, chunk, "update", actor, old, new)
    _write_manual(ctx, EditRow(r.edit_id, ctx.document_id, "manual", chunk, page=r.page, text=new["text"], keywords=new["keywords"],
                               questions=new["questions"], available=new["available"]))
    return get_chunk(ctx.document_id, chunk)


def _write_manual(ctx: DocContext, r: EditRow) -> None:
    from app.scale.chunking import context_prefix

    piece = Piece(page=r.page or 1, index=r.chunk, text=r.text or "", context=context_prefix(ctx.name, ctx.path, ""), kind="manual",
                  user_keywords=list(r.keywords), user_questions=list(r.questions), available=r.available)
    index_pieces(ctx.document_id, ctx.rag_doc_id, ctx.name, ctx.path, ctx.source, ctx.acl, [piece])


def add_chunk(document_id: str, text: str, *, page: int = 1, keywords=(), questions=(), actor: str | None = None) -> dict:
    ctx = context_for(document_id)
    body = _clean_text(text, required=True)
    kws = _clean_terms(list(keywords or []), limit=MAX_TERMS, max_chars=80, what="keywords")
    qs = _clean_terms(list(questions or []), limit=MAX_TERMS, max_chars=300, what="questions")
    pages = len(ctx.pages())
    if not 1 <= int(page) <= max(pages, 1):
        raise ChunkEditError("invalid", f"page must be between 1 and {max(pages, 1)}")
    with engine().begin() as conn:
        # the next free number, taken under a row lock on the document so two editors cannot pick the same one
        conn.execute(sql("SELECT 1 FROM documents WHERE document_id = :d FOR UPDATE"), {"d": document_id})
        last = conn.execute(sql("SELECT COALESCE(MAX(chunk), :b - 1) FROM chunk_edits WHERE document_id = :d AND kind = 'manual'"),
                            {"d": document_id, "b": MANUAL_BASE}).scalar_one()
        number = int(last) + 1
        edit_id = conn.execute(
            sql("INSERT INTO chunk_edits (document_id, kind, chunk, page, text, keywords, questions, created_by, updated_by) "
                "VALUES (:d, 'manual', :c, :p, :t, :k, :q, :u, :u) RETURNING edit_id"),
            {"d": document_id, "c": number, "p": int(page), "t": body, "k": kws, "q": qs, "u": actor}).scalar_one()
        _log(conn, document_id, number, "add", actor, None, {"text": body, "keywords": kws, "questions": qs, "page": int(page)})
    _write_manual(ctx, EditRow(str(edit_id), document_id, "manual", number, page=int(page), text=body, keywords=kws, questions=qs))
    return get_chunk(document_id, number)


def delete_chunk(document_id: str, chunk: int, *, actor: str | None = None) -> dict:
    if chunk < MANUAL_BASE:
        raise ChunkEditError("invalid", "only chunks added by hand can be deleted; switch an extracted chunk off with available=false")
    rows = [r for r in rows_for(document_id) if r.kind == "manual" and r.chunk == chunk]
    if not rows:
        raise ChunkEditError("not_found", "no such chunk")
    with engine().begin() as conn:
        conn.execute(sql("DELETE FROM chunk_edits WHERE edit_id = :e"), {"e": rows[0].edit_id})
        _log(conn, document_id, chunk, "delete", actor, rows[0].snapshot(), None)
    ix.delete_points(document_id, [chunk])
    return {"deleted": chunk}


def history(document_id: str, limit: int = 100) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            sql("SELECT log_id, chunk, action, actor, before, after, at FROM chunk_edit_log WHERE document_id = :d ORDER BY log_id DESC LIMIT :n"),
            {"d": document_id, "n": limit}).mappings().all()
    return [{"id": r["log_id"], "chunk": r["chunk"], "action": r["action"], "actor": r["actor"], "before": r["before"],
             "after": r["after"], "at": r["at"].isoformat()} for r in rows]


# ── what the agent's keyword search tool shows ──


def overlay_pages(texts: dict[tuple[str, int], str]) -> dict[tuple[str, int], str]:
    """(rag doc id, page) -> page text, with corrected chunks swapped in and hand-written chunks appended, so the agent's
    keyword-search results show what the editor changed. Read-only: nothing is written back to the stored pages."""
    if not texts or not get_scale_settings().chunk_edit_overlay:
        return texts
    docs = sorted({k[0] for k in texts})
    try:
        with engine().connect() as conn:
            rows = conn.execute(
                sql("SELECT d.rag_doc_id, e.kind, e.page, e.orig_text, e.text FROM chunk_edits e JOIN documents d USING (document_id) "
                    "WHERE d.rag_doc_id = ANY(:docs) AND e.text IS NOT NULL AND NOT e.orphaned AND e.available"), {"docs": docs}).all()
    except Exception:  # noqa: BLE001
        log.warning("could not read chunk edits for the page overlay", exc_info=True)
        return texts
    if not rows:
        return texts
    out = dict(texts)
    for rag_doc_id, kind, page, orig, new in rows:
        key = (rag_doc_id, page)
        if key not in out:
            continue
        if kind == "edit" and orig and orig in out[key]:
            out[key] = out[key].replace(orig, new, 1)
        elif kind == "manual":
            out[key] = f"{out[key]}\n\n{new}"
    return out
