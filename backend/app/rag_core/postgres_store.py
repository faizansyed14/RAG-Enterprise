"""Where the document section trees and page text are kept: PostgreSQL (tables `document_trees` and friends).

The tree and page JSON are small (a few KB to low MB per document), so keeping them in the database gives them the same backup,
replication and access control as everything else, and lets any number of API replicas and workers share them.

Interface used by local_api.py: get_meta, get_tree, get_pages, list_metas, save_document, delete_document, lock.

Synchronous, not async: local_api.py runs off the event loop via asyncio.to_thread() (see rag_service.py), so this needs a
blocking driver -- psycopg2, via its own small SQLAlchemy engine, separate from models/db.py's async one.
"""

from __future__ import annotations

import json
import logging
import zlib
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text

from app.core.config import get_settings

log = logging.getLogger(__name__)

# Exact match first, then -- only if that found nothing -- any-term match. plainto_tsquery ANDs
# every word, which is what you want for "SUB-084 review status" but returns nothing for a
# sentence-style question; measured on a 1,011-document library: AND found nothing for all 8
# project-named questions and 2 of 11 identifier questions, and the any-term fallback is what
# rescued those 2 (see docs/ARCHITECTURE.md, "Retrieval at scale").
_TSQUERY_ALL = "plainto_tsquery('english', :query)"
_TSQUERY_ANY = "replace(plainto_tsquery('english', :query)::text, '&', '|')::tsquery"
_MATCH_MODES = (("all terms", _TSQUERY_ALL), ("any term", _TSQUERY_ANY))

# One fixed key for the single global mutex DocStore.lock() provided (confirmed: one call
# site, local_api.py's submit_document, guarding the check-then-write name-uniqueness race --
# not a per-document lock). Any 64-bit int works; this is just a stable, arbitrary constant.
_LOCK_KEY = zlib.crc32(b"rag_core.postgres_store.submit_document") & 0x7FFFFFFF


def _strip_nul(value):
    """Postgres's jsonb type rejects the NUL codepoint (U+0000) even escaped inside a JSON
    string -- confirmed live: a real invoice PDF extracted a stray \\u0000 in place of a
    hyphen ("KPFUIKY8\\u000005" for "KPFUIKY8-0005"), and psycopg2 raised
    UntranslatableCharacter on insert. NUL in extracted document text is always an
    extraction artifact, never meaningful content, so it's dropped -- same spirit as
    local_api.py's own _scrub_surrogates() for lone surrogates."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    return value


def _with_hybrid(query: str, limit: int, doc_ids, keyword: list[dict], mode: str) -> tuple[list[dict], str]:
    """Add semantic / sparse hits from the hybrid chunk index to the keyword results
    (never replacing them unless SCALE_MERGE_MODE=hybrid). Any failure leaves the keyword results as they were."""
    try:
        from app.scale.search import merge_page_results, page_search_hook

        return merge_page_results(keyword, mode, page_search_hook(query, limit, doc_ids), limit)
    except Exception:  # noqa: BLE001
        log.warning("hybrid page search unavailable; returning keyword results", exc_info=True)
        return keyword, mode


def _sync_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+psycopg2://", 1)


@lru_cache
def _engine():
    return create_engine(_sync_dsn(get_settings().postgres_dsn), pool_pre_ping=True)


class PostgresDocStore:
    def save_document(self, doc_id: str, meta: dict, tree: list, pages: list) -> None:
        with _engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO document_trees (doc_id, meta, tree, pages) "
                    "VALUES (:doc_id, CAST(:meta AS jsonb), CAST(:tree AS jsonb), CAST(:pages AS jsonb)) "
                    "ON CONFLICT (doc_id) DO UPDATE SET "
                    "meta = EXCLUDED.meta, tree = EXCLUDED.tree, pages = EXCLUDED.pages, updated_at = now()"
                ),
                {
                    "doc_id": doc_id,
                    "meta": json.dumps(_strip_nul(meta)),
                    "tree": json.dumps(_strip_nul(tree)),
                    "pages": json.dumps(_strip_nul(pages)),
                },
            )
            # The page-level search index is derived data: inside a SAVEPOINT, so a failure here
            # rolls back only the index rows and never the document itself -- an ingest must not
            # fail because search_content's index could not be refreshed. The document stays
            # readable either way; scripts/eval_retrieval.py reports any document left unindexed.
            try:
                with conn.begin_nested():
                    self._index_pages(conn, doc_id, meta, pages)
            except Exception:  # noqa: BLE001
                log.warning("page search index not updated for %s", doc_id, exc_info=True)

    @staticmethod
    def _index_pages(conn, doc_id: str, meta: dict, pages: list) -> None:
        conn.execute(text("DELETE FROM document_pages WHERE doc_id = :doc_id"), {"doc_id": doc_id})
        name = _strip_nul(str(meta.get("name") or ""))
        description = _strip_nul(str(meta.get("description") or ""))
        rows = []
        for position, page in enumerate(pages or [], start=1):
            if not isinstance(page, dict):
                continue
            rows.append({
                "doc_id": doc_id,
                "page_number": int(page.get("page_number") or position),
                "doc_name": name,
                "doc_description": description,
                "text": _strip_nul(str(page.get("markdown") or "")),
            })
        if rows:
            conn.execute(
                text(
                    "INSERT INTO document_pages (doc_id, page_number, doc_name, doc_description, text) "
                    "VALUES (:doc_id, :page_number, :doc_name, :doc_description, :text) "
                    "ON CONFLICT (doc_id, page_number) DO UPDATE SET doc_name = EXCLUDED.doc_name, "
                    "doc_description = EXCLUDED.doc_description, text = EXCLUDED.text"
                ),
                rows,
            )

    def get_meta(self, doc_id: str) -> dict | None:
        return self._get_column(doc_id, "meta")

    def get_tree(self, doc_id: str) -> list | None:
        return self._get_column(doc_id, "tree")

    def get_pages(self, doc_id: str) -> list | None:
        return self._get_column(doc_id, "pages")

    def _get_column(self, doc_id: str, column: str):
        # column is always one of the three literal strings above, never user input.
        query = {"meta": "SELECT meta FROM document_trees WHERE doc_id = :doc_id",
                 "tree": "SELECT tree FROM document_trees WHERE doc_id = :doc_id",
                 "pages": "SELECT pages FROM document_trees WHERE doc_id = :doc_id"}[column]
        with _engine().connect() as conn:
            row = conn.execute(text(query), {"doc_id": doc_id}).first()
        return row[0] if row else None

    def list_metas(self) -> list[dict]:
        with _engine().connect() as conn:
            rows = conn.execute(text("SELECT meta FROM document_trees")).all()
        return [row[0] for row in rows]

    def find_metas_by_name(self, name: str, doc_ids=None, limit: int = 50) -> list[dict]:
        """Documents with exactly this name -- an index lookup (document_trees_name_idx, migration 0012),
        not a scan of every document. `doc_ids`, when given, scopes it like chat's selected-document scope."""
        params: dict = {"name": name, "limit": limit}
        scope = ""
        if doc_ids is not None:
            scope = " AND doc_id = ANY(:doc_ids)"
            params["doc_ids"] = list(doc_ids)
        with _engine().connect() as conn:
            rows = conn.execute(
                text(f"SELECT meta FROM document_trees WHERE meta ->> 'name' = :name{scope} LIMIT :limit"), params
            ).all()
        return [row[0] for row in rows]

    def names_like(self, prefix: str, limit: int = 10000) -> set[str]:
        """Existing names that start with `prefix` (used to pick a free _N suffix without loading every document)."""
        with _engine().connect() as conn:
            # LIKE 'prefix%' (not starts_with()) so the text_pattern_ops index on the name can serve it.
            pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(
                text("SELECT meta ->> 'name' FROM document_trees WHERE meta ->> 'name' LIKE :p ESCAPE '\\' LIMIT :limit"),
                {"p": pattern, "limit": limit},
            ).all()
        return {row[0] for row in rows}

    def search_metas(self, query: str, limit: int, offset: int,
                      doc_ids=None) -> tuple[list[dict], int]:
        """Keyword (tsvector) ranked search over name/description/top-level section titles --
        see alembic/versions/0008_document_trees_search.py for how search_vector is maintained.
        Lexical, not semantic: no embeddings involved. `doc_ids`, when given, scopes the search
        the same way chat's selected-document scope does (see agent_tools.py's _allowed_ids)."""
        params: dict = {"query": query, "limit": limit, "offset": offset}
        scope = ""
        if doc_ids is not None:
            scope = " AND doc_id = ANY(:doc_ids)"
            params["doc_ids"] = list(doc_ids)

        with _engine().connect() as conn:
            # All-terms match first; the any-term fallback only runs when that finds nothing, so a
            # precise query is never diluted but a sentence-style one still finds its documents.
            for _mode, tsquery in _MATCH_MODES:
                where = f"search_vector @@ {tsquery}{scope}"
                total = conn.execute(
                    text(f"SELECT count(*) FROM document_trees WHERE {where}"), params
                ).scalar_one()
                if total:
                    rows = conn.execute(
                        text(
                            f"SELECT meta FROM document_trees WHERE {where} "
                            f"ORDER BY ts_rank(search_vector, {tsquery}) DESC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    ).all()
                    return [row[0] for row in rows], total
        return [], 0

    def search_pages(self, query: str, limit: int = 8, doc_ids=None) -> tuple[list[dict], str]:
        """Full-text search over the *pages* of every document (document_pages -- see
        alembic/versions/0011_document_pages.py): the one search that can find a fact by what a page
        says. Same all-terms-then-any-term cascade as search_metas. `doc_ids`, when given, scopes it
        the way chat's selected-document scope does (see agent_tools.py's _allowed_ids). Returns
        ([{doc_id, name, page, text}], match_mode) with match_mode "all terms", "any term" or
        "no match"; lexical, not semantic -- no embeddings involved."""
        params: dict = {"query": query, "limit": limit}
        scope = ""
        if doc_ids is not None:
            scope = " AND dp.doc_id = ANY(:doc_ids)"
            params["doc_ids"] = list(doc_ids)
        with _engine().connect() as conn:
            keyword: list[dict] = []
            keyword_mode = "no match"
            for mode, tsquery in _MATCH_MODES:
                rows = conn.execute(
                    text(
                        "SELECT dp.doc_id, dp.doc_name, dp.page_number, dp.text "
                        f"FROM document_pages dp WHERE dp.text_tsv @@ {tsquery}{scope} "
                        f"ORDER BY ts_rank_cd(dp.text_tsv, {tsquery}) DESC, dp.doc_id, dp.page_number "
                        "LIMIT :limit"
                    ),
                    params,
                ).all()
                if rows:
                    keyword = [{"doc_id": r[0], "name": r[1], "page": r[2], "text": r[3]} for r in rows]
                    keyword_mode = mode
                    break
        return _with_hybrid(query, limit, doc_ids, keyword, keyword_mode)

    def delete_document(self, doc_id: str) -> bool:
        with _engine().begin() as conn:
            result = conn.execute(
                text("DELETE FROM document_trees WHERE doc_id = :doc_id RETURNING 1"), {"doc_id": doc_id}
            )
            return result.first() is not None

    @contextmanager
    def lock(self):
        with _engine().connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _LOCK_KEY})
            conn.commit()
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
                conn.commit()
