"""Self-healing chunk index.

Postgres is the source of truth (pages, trees, access); Qdrant holds a derived copy (one point per chunk). A derived copy
can fall out of step in ways nobody notices until a question goes unanswered: the vector volume was restored from an old
backup, a collection was dropped, a point or only its vector was deleted by hand, an embedding job died half way, the
embedding model or the chunking settings were changed, or Postgres was restored and the index still holds documents that
no longer exist. None of these raise an error at query time; the chunks are simply not there.

This job compares the two and repairs the index from Postgres (no source access needed: the pages are stored):

  missing     the document has no chunks in the index                              -> re-embed
  partial     fewer chunks than the last indexing wrote (plus hand-written ones)   -> re-embed
  extra       more chunks than expected (leftovers of an interrupted rebuild)      -> re-embed, leftovers removed
  no_vector   a chunk whose dense vector is gone                                   -> re-embed
  signature   built with another embedding model / text analysis / chunk size      -> re-embed into the current collection
  orphan      chunks whose document is not in Postgres                             -> deleted

Re-embedding is the normal `index_document` job; it writes deterministic point ids and only then removes leftovers, so
the document stays searchable while it is rebuilt. Every document is compared at least every
`reconcile_interval_hours`; a run spends at most `reconcile_seconds` and carries on at the next scheduler tick.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.scale import index as ix
from app.scale import queue
from app.scale.db import engine
from app.scale.settings import ScaleSettings, get_scale_settings

log = logging.getLogger("scale.reconcile")


def index_signature(cfg: ScaleSettings | None = None) -> str:
    """What a document's chunks were built with. The collection name already carries the embedding model, the text
    analysis and the index format; the chunk size settings are added because they change what a chunk is."""
    cfg = cfg or get_scale_settings()
    return f"{ix.collection_name(cfg)}|w{cfg.chunk_words}/{cfg.chunk_overlap_words}/{cfg.min_chunk_words}"


# ── state ──


def _get_state(key: str) -> str | None:
    with engine().connect() as conn:
        return conn.execute(text("SELECT value FROM scale_state WHERE key = :k"), {"k": key}).scalar()


def _set_state(key: str, value: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("INSERT INTO scale_state (key, value) VALUES (:k, :v) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": key, "v": value},
        )


def last_result() -> dict | None:
    raw = _get_state("reconcile_last")
    return json.loads(raw) if raw else None


# ── what is due ──

_DUE = (
    "FROM documents d WHERE d.status = 'indexed' AND d.rag_doc_id IS NOT NULL AND d.chunks_indexed_at IS NOT NULL "
    "AND d.chunks_indexed_at < now() - make_interval(secs => :grace) "
    "AND (d.reconciled_at IS NULL OR d.reconciled_at < :cutoff) "
)


def _params(cfg: ScaleSettings, cutoff: datetime) -> dict:
    return {"grace": float(cfg.reconcile_grace_minutes) * 60.0, "cutoff": cutoff}


def _default_cutoff(cfg: ScaleSettings) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=cfg.reconcile_interval_hours)


def has_work(cfg: ScaleSettings | None = None) -> bool:
    """Cheap check for the scheduler: is any document due for a comparison, or is the orphan sweep due?"""
    cfg = cfg or get_scale_settings()
    if not cfg.reconcile_enabled:
        return False
    if index_looks_short(cfg):
        return True
    with engine().connect() as conn:
        if conn.execute(text("SELECT 1 " + _DUE + "LIMIT 1"), _params(cfg, _default_cutoff(cfg))).first():
            return True
    return _orphans_due(cfg)


_SHORT: dict[str, float] = {}      # when the whole-index check last ran in this process


def index_looks_short(cfg: ScaleSettings | None = None, *, fresh: bool = False) -> bool:
    """The cheap whole-index check: far fewer chunks in Qdrant than Postgres says were written (a wiped or old volume, a dropped
    collection). Two cheap calls, no per-document work. Without this a lost index would wait for each document's daily turn."""
    cfg = cfg or get_scale_settings()
    now = time.monotonic()
    if not fresh and now - _SHORT.get("at", -1e9) < cfg.reconcile_quick_check_seconds:
        return _SHORT.get("short", 0.0) > 0
    with engine().connect() as conn:
        expected = int(conn.execute(
            text("SELECT COALESCE(sum(chunks_expected), 0) FROM documents WHERE chunks_indexed_at IS NOT NULL "
                 "AND index_signature = :s AND chunks_indexed_at < now() - make_interval(secs => :g)"),
            {"s": index_signature(cfg), "g": float(cfg.reconcile_grace_minutes) * 60.0}).scalar_one())
    short = False
    if expected > 0:
        try:
            actual = ix.count_chunks(cfg)
        except Exception:  # noqa: BLE001 -- the index is unreachable: not a verdict about its contents
            actual = expected
        short = actual < expected * cfg.reconcile_short_ratio
    _SHORT.update(at=now, short=1.0 if short else 0.0)
    return short


def _orphans_due(cfg: ScaleSettings) -> bool:
    if _get_state("orphans_cursor"):
        return True                         # a pass is half done: keep going
    last = _get_state("orphans_last")
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - then > timedelta(hours=cfg.reconcile_orphans_every_hours)


# ── the comparison ──


def judge(count: int, expected: int | None, manual: int, stored_signature: str | None, signature: str,
          vector_missing: bool) -> str:
    """The verdict for one document. `count` is what the index holds, `expected` what the last indexing wrote
    (None for a document indexed before this job existed), `manual` the hand-written chunks it should also have."""
    if expected is None:
        return "missing" if count == 0 else ("no_vector" if vector_missing else "adopt")
    if stored_signature != signature:
        return "signature"
    want = expected + manual
    if count == 0 and want > 0:
        return "missing"
    if count < want:
        return "partial"
    if count > want:
        return "extra"
    if vector_missing:
        return "no_vector"
    return "ok"


def _batch(cfg: ScaleSettings, cutoff: datetime, skip: int) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT d.document_id::text AS document_id, d.tier, d.chunks_expected, d.index_signature, "
                "(SELECT count(*) FROM chunk_edits e WHERE e.document_id = d.document_id AND e.kind = 'manual') AS manual "
                + _DUE + "ORDER BY d.reconciled_at NULLS FIRST, d.document_id LIMIT :n OFFSET :o"
            ),
            {**_params(cfg, cutoff), "n": cfg.reconcile_batch, "o": skip},
        ).mappings().all()
    return [dict(r) for r in rows]


def _backlog(cap: int) -> bool:
    with engine().connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM scale_jobs WHERE kind = 'index_document' AND status = 'queued'")).scalar_one()
    return int(n) >= cap


def run(*, dry_run: bool = False, force: bool = False, orphans: bool | None = None, cfg: ScaleSettings | None = None,
        budget_s: float | None = None) -> dict:
    """Compare documents with the index and repair the difference. `force` compares every document now whatever its last
    comparison; `dry_run` reports without changing anything; `orphans` None = only when due."""
    cfg = cfg or get_scale_settings()
    started = time.monotonic()
    budget = cfg.reconcile_seconds if budget_s is None else budget_s
    run_start = datetime.now(timezone.utc)
    if not force and index_looks_short(cfg, fresh=True):
        force = True                       # the index as a whole is far smaller than it should be: compare everything now
        log.warning("the chunk index holds far fewer chunks than were written; comparing every document")
    cutoff = run_start if force else _default_cutoff(cfg)
    signature = index_signature(cfg)
    verdicts: Counter = Counter()
    requeued = examined = 0
    collection_missing = False
    skip = 0
    while (time.monotonic() - started) < budget or examined == 0:
        if not dry_run and _backlog(cfg.reconcile_requeue_cap):
            verdicts["deferred_backlog"] += 1
            break
        rows = _batch(cfg, cutoff, skip)
        if not rows:
            break
        ids = [r["document_id"] for r in rows]
        counts = ix.document_chunk_counts(ids, cfg)
        if counts is None:
            collection_missing = True
            counts = {d: 0 for d in ids}
        try:
            no_vector = ix.documents_without_vector(ids, cfg) if not collection_missing else set()
        except Exception:  # noqa: BLE001 -- an older Qdrant without the condition: the count check still runs
            log.warning("vector check unavailable", exc_info=True)
            no_vector = set()
        redo: list[str] = []
        adopt: list[tuple[str, int]] = []
        for r in rows:
            v = judge(counts[r["document_id"]], r["chunks_expected"], int(r["manual"]), r["index_signature"], signature,
                      r["document_id"] in no_vector)
            verdicts[v] += 1
            if v == "adopt":
                adopt.append((r["document_id"], max(counts[r["document_id"]] - int(r["manual"]), 0)))
            elif v not in ("ok",):
                redo.append(r["document_id"])
        examined += len(rows)
        if dry_run:
            skip += len(rows)
        else:
            _apply(ids, redo, adopt, signature)
            requeued += len(redo)
        if requeued >= cfg.reconcile_requeue_cap:
            break
    result = {"examined": examined, "verdicts": dict(verdicts), "requeued": requeued, "dry_run": dry_run,
              "collection_missing": collection_missing, "seconds": round(time.monotonic() - started, 2)}
    if orphans or (orphans is None and not dry_run and _orphans_due(cfg)):
        result["orphans_removed"] = sweep_orphans(cfg, dry_run=dry_run)
    if not dry_run:
        _set_state("reconcile_last", json.dumps({**result, "at": datetime.now(timezone.utc).isoformat()}))
    if requeued or result.get("orphans_removed"):
        log.warning("reconcile repaired the index: %s", result)
    return result


def _apply(examined: list[str], redo: list[str], adopt: list[tuple[str, int]], signature: str) -> None:
    with engine().begin() as conn:
        conn.execute(text("UPDATE documents SET reconciled_at = now() WHERE document_id = ANY(CAST(:ids AS uuid[]))"), {"ids": examined})
        for document_id, expected in adopt:
            conn.execute(
                text("UPDATE documents SET chunks_expected = :n, index_signature = :s WHERE document_id = :d"),
                {"n": expected, "s": signature, "d": document_id},
            )
        if redo:
            conn.execute(text("UPDATE documents SET chunks_indexed_at = NULL WHERE document_id = ANY(CAST(:ids AS uuid[]))"), {"ids": redo})
    for document_id in redo:
        queue.enqueue("index_document", {"document_id": document_id}, dedupe_key=f"idx:{document_id}")


# ── chunks of documents that no longer exist ──


def sweep_orphans(cfg: ScaleSettings | None = None, *, dry_run: bool = False) -> int:
    """Delete chunks whose document is not in Postgres (a restored database, a manual delete). Search already hides them
    (every hit is re-checked in Postgres); this reclaims the space. The sweep walks the whole index in bounded runs
    (`reconcile_orphan_scan_points` chunks each) and remembers where it stopped, so any size of index is covered, not just the
    documents with the most chunks. A dry run looks at the first run's worth and changes nothing, cursor included."""
    cfg = cfg or get_scale_settings()
    cursor = None if dry_run else _get_state("orphans_cursor")
    known, next_cursor = ix.scan_document_ids(cursor or None, cfg.reconcile_orphan_scan_points, cfg)
    known_list = sorted(known)
    removed = 0
    for start in range(0, len(known_list), 1000):
        chunk = [d for d in known_list[start: start + 1000] if d != "None"]
        if not chunk:
            continue
        try:
            with engine().connect() as conn:
                present = {r[0] for r in conn.execute(
                    text("SELECT document_id::text FROM documents WHERE document_id = ANY(CAST(:ids AS uuid[]))"), {"ids": chunk})}
        except Exception:  # noqa: BLE001 -- a payload that is not a uuid cannot be a document of ours: leave it alone
            log.warning("orphan sweep skipped a batch with malformed ids", exc_info=True)
            continue
        for document_id in chunk:
            if document_id not in present:
                removed += 1
                if not dry_run:
                    ix.delete_document_chunks(document_id, cfg)
    if not dry_run:
        if next_cursor is None:
            _set_state("orphans_cursor", "")
            _set_state("orphans_last", datetime.now(timezone.utc).isoformat())      # one whole pass finished
        else:
            _set_state("orphans_cursor", next_cursor)                               # the next run continues from here
    return removed


# ── for status screens ──


def summary(cfg: ScaleSettings | None = None) -> dict:
    cfg = cfg or get_scale_settings()
    signature = index_signature(cfg)
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE chunks_indexed_at IS NOT NULL) AS indexed, "
                "count(*) FILTER (WHERE chunks_indexed_at IS NULL AND status = 'indexed' AND rag_doc_id IS NOT NULL) AS pending, "
                "count(*) FILTER (WHERE chunks_indexed_at IS NOT NULL AND index_signature IS DISTINCT FROM :s "
                "                 AND index_signature IS NOT NULL) AS other_build, "
                "count(*) FILTER (WHERE chunks_indexed_at IS NOT NULL AND reconciled_at IS NULL) AS never_compared "
                "FROM documents"
            ),
            {"s": signature},
        ).mappings().one()
    return {**dict(row), "last_run": last_result()}
