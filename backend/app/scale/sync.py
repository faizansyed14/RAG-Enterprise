"""connector scan -> source_items -> jobs.

The rules that make a sync trustworthy, each one enforced here and covered by a test:

  1. Deletes are first-class. A file removed at the source becomes unreadable immediately (its
     principals leave document_access in the same transaction) and is physically removed by a job.
  2. Permission changes are first-class. A changed ACL with an unchanged file rewrites access at once
     and queues only the cheap index-payload update; nothing is re-parsed or re-embedded.
  3. Unchanged files cost nothing: the connector's `version` is compared and no job is queued.
  4. Backpressure: scanning pauses when the queue is already long, and resumes from its cursor.
  5. Every skipped or failed file stays visible in source_items (state + error). Nothing is silently dropped.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.scale import acl as acl_mod
from app.scale.connectors import Connector, build_connector
from app.scale.connectors.base import Change, ScanBatch, SourceFile
from app.scale.db import engine
from app.scale.queue import enqueue
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)


def create_connection(kind: str, name: str, config: dict) -> str:
    from app.scale.connectors import CONNECTORS

    if kind not in CONNECTORS:
        raise ValueError(f"unknown connector kind {kind!r}; available: {', '.join(sorted(CONNECTORS))}")
    with engine().begin() as conn:
        connection_id = conn.execute(
            text(
                "INSERT INTO source_connections (kind, name, config) VALUES (:k, :n, CAST(:c AS jsonb)) "
                "RETURNING connection_id"
            ),
            {"k": kind, "n": name, "c": json.dumps(config)},
        ).scalar_one()
        conn.execute(text("INSERT INTO connector_state (connection_id) VALUES (:c)"), {"c": connection_id})
    return str(connection_id)


def _queued_count() -> int:
    with engine().connect() as conn:
        return conn.execute(text("SELECT count(*) FROM scale_jobs WHERE status = 'queued'")).scalar_one()


def run_scan(connection_id: str, *, connector: Connector | None = None) -> dict[str, Any]:
    cfg = get_scale_settings()
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.kind, c.config, s.cursor FROM source_connections c "
                "LEFT JOIN connector_state s USING (connection_id) WHERE c.connection_id = :c AND c.enabled"
            ),
            {"c": connection_id},
        ).first()
    if row is None:
        return {"status": "disabled_or_missing"}
    connector = connector or build_connector(row[0], row[1])
    cursor: dict = row[2] or {}
    scan_id = cursor.pop("_scan_id", None) or uuid.uuid4().hex[:12]
    stats = {"upserts": 0, "queued": 0, "acl_changes": 0, "deletes": 0, "batches": 0}

    with engine().begin() as conn:
        conn.execute(
            text("UPDATE connector_state SET last_scan_started = now(), last_error = NULL WHERE connection_id = :c"),
            {"c": connection_id},
        )
    try:
        for batch in connector.scan(cursor):
            if _queued_count() > cfg.max_queued_jobs:
                _save_cursor(connection_id, {**cursor, "_scan_id": scan_id})
                return {"status": "paused_backpressure", **stats}
            _apply_batch(connection_id, batch, scan_id, stats)
            cursor = batch.cursor
            if batch.complete_scope is not None:
                _apply_completion(connection_id, batch.complete_scope, scan_id, stats)
            if batch.done:
                # The last batch's cursor is the starting point of the NEXT scan (a delta link, a change token, a
                # history id). Crawl-style connectors end with {} and so start from scratch next time. Dropping it
                # here would turn every delta source into a full enumeration that can never see a deletion.
                _save_cursor(connection_id, batch.cursor or {}, finished=True)
            else:
                _save_cursor(connection_id, {**cursor, "_scan_id": scan_id})
            stats["batches"] += 1
    except Exception as exc:  # noqa: BLE001
        with engine().begin() as conn:
            conn.execute(
                text("UPDATE connector_state SET last_error = :e WHERE connection_id = :c"),
                {"e": str(exc)[:2000], "c": connection_id},
            )
        raise
    return {"status": "done", **stats}


def _save_cursor(connection_id: str, cursor: dict, finished: bool = False) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE connector_state SET cursor = CAST(:cur AS jsonb)"
                + (", last_scan_finished = now()" if finished else "")
                + " WHERE connection_id = :c"
            ),
            {"cur": json.dumps(cursor), "c": connection_id},
        )


def _apply_batch(connection_id: str, batch: ScanBatch, scan_id: str, stats: dict) -> None:
    changes = batch.changes
    if not changes:
        return
    ids = [c.external_id for c in changes]
    with engine().begin() as conn:
        existing = {
            r["external_id"]: r
            for r in conn.execute(
                text(
                    "SELECT item_id, external_id, version, acl, deleted, document_id, state, indexed_version, "
                    "(updated_at < now() - make_interval(secs => CAST(:retry_s AS double precision))) AS retry_due "
                    "FROM source_items WHERE connection_id = :c AND external_id = ANY(:ids)"
                ),
                {"c": connection_id, "ids": ids, "retry_s": float(get_scale_settings().failed_retry_hours) * 3600.0},
            ).mappings()
        }
        for change in changes:
            old = existing.get(change.external_id)
            if change.kind == "delete":
                _apply_delete(conn, connection_id, change, old, stats)
            else:
                _apply_upsert(conn, connection_id, change, old, scan_id, stats)


def _apply_delete(conn, connection_id: str, change: Change, old, stats: dict) -> None:
    if old is None or old["deleted"]:
        return
    conn.execute(
        text("UPDATE source_items SET deleted = true, updated_at = now() WHERE item_id = :i"), {"i": old["item_id"]}
    )
    if old["document_id"]:
        acl_mod.recompute_document_access(conn, old["document_id"])   # unreadable from this moment
    enqueue("delete_item", {"item_id": str(old["item_id"])}, dedupe_key=f"delete:{old['item_id']}", conn=conn)
    stats["deletes"] += 1


def _apply_upsert(conn, connection_id: str, change: Change, old, scan_id: str, stats: dict) -> None:
    sf: SourceFile = change.file  # type: ignore[assignment]
    acl = acl_mod.normalize_acl(sf.acl)
    params = {
        "c": connection_id, "e": sf.external_id, "p": sf.path, "n": sf.name, "s": sf.size,
        "m": sf.modified_at, "v": sf.version, "a": acl, "scan": scan_id,
    }
    stats["upserts"] += 1
    if old is None:
        item_id = conn.execute(
            text(
                "INSERT INTO source_items (connection_id, external_id, path, name, size_bytes, modified_at, version, "
                "acl, acl_synced_at, last_seen_scan, state) "
                "VALUES (:c, :e, :p, :n, :s, :m, :v, :a, now(), :scan, 'pending') RETURNING item_id"
            ),
            params,
        ).scalar_one()
        enqueue("ingest_item", {"item_id": str(item_id)}, dedupe_key=f"ingest:{item_id}", conn=conn)
        stats["queued"] += 1
        return

    # A skipped file (unsupported type, scanned PDF without OCR, too large) is recorded with the version
    # it was skipped at and is looked at again only when the file changes -- otherwise every scan would
    # re-queue every unsupported file forever. A failed file is retried once per failed_retry_hours;
    # `scale jobs requeue-dead` retries sooner.
    state = old["state"]
    changed_content = (
        old["version"] != sf.version
        or old["deleted"]
        or state == "pending"
        or (state == "failed" and bool(old["retry_due"]))
        or (state == "indexed" and old["indexed_version"] != sf.version)
    )
    changed_acl = sorted(old["acl"] or []) != acl
    conn.execute(
        text(
            "UPDATE source_items SET path = :p, name = :n, size_bytes = :s, modified_at = :m, version = :v, "
            "acl = :a, acl_synced_at = now(), last_seen_scan = :scan, deleted = false, "
            "state = CASE WHEN :changed THEN 'pending' ELSE state END, updated_at = now() "
            "WHERE item_id = :i"
        ),
        {**params, "i": old["item_id"], "changed": changed_content},
    )
    if changed_acl and old["document_id"]:
        # Access changes take effect immediately in Postgres; the index payload follows via a job.
        acl_mod.recompute_document_access(conn, old["document_id"])
        enqueue("index_acl", {"document_id": str(old["document_id"])},
                dedupe_key=f"acl:{old['document_id']}", conn=conn)
        stats["acl_changes"] += 1
    if changed_content:
        enqueue("ingest_item", {"item_id": str(old["item_id"])}, dedupe_key=f"ingest:{old['item_id']}", conn=conn)
        stats["queued"] += 1


def _apply_completion(connection_id: str, scope: str, scan_id: str, stats: dict) -> None:
    """The scan enumerated everything under `scope`; whatever it did not see is gone."""
    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "UPDATE source_items SET deleted = true, updated_at = now() "
                "WHERE connection_id = :c AND starts_with(external_id, :scope) AND NOT deleted "
                "AND last_seen_scan IS DISTINCT FROM :scan RETURNING item_id, document_id"
            ),
            {"c": connection_id, "scope": scope, "scan": scan_id},
        ).all()
        for item_id, document_id in rows:
            if document_id:
                acl_mod.recompute_document_access(conn, document_id)
            enqueue("delete_item", {"item_id": str(item_id)}, dedupe_key=f"delete:{item_id}", conn=conn)
            stats["deletes"] += 1


def run_acl_sweep(connection_id: str, *, older_than_hours: float, limit: int = 200,
                  connector: Connector | None = None) -> dict[str, Any]:
    """Re-read permissions for the files whose ACL was last checked longest ago.

    Delta feeds do not reliably announce every permission change; this bounds how stale an ACL can be.
    """
    with engine().connect() as conn:
        conn_row = conn.execute(
            text("SELECT kind, config FROM source_connections WHERE connection_id = :c AND enabled"),
            {"c": connection_id},
        ).first()
        if conn_row is None:
            return {"status": "disabled_or_missing"}
        items = conn.execute(
            text(
                "SELECT item_id, external_id, path, name, size_bytes, version, acl, document_id FROM source_items "
                "WHERE connection_id = :c AND NOT deleted AND document_id IS NOT NULL "
                "AND (acl_synced_at IS NULL OR acl_synced_at < now() - make_interval(secs => CAST(:h AS double precision))) "
                "ORDER BY acl_synced_at NULLS FIRST LIMIT :n"
            ),
            {"c": connection_id, "h": float(older_than_hours) * 3600.0, "n": limit},
        ).mappings().all()
    connector = connector or build_connector(conn_row[0], conn_row[1])
    changed = 0
    for item in items:
        sf = SourceFile(item["external_id"], item["path"], item["name"], item["size_bytes"] or 0, None,
                        item["version"], list(item["acl"] or []))
        if getattr(connector, "drive_based", False) and ":" in sf.external_id:
            drive_id, item_ref = sf.external_id.split(":", 1)
            sf.extra = {"drive_id": drive_id, "item_id": item_ref}
        new_acl = acl_mod.normalize_acl(connector.refresh_acl(sf))
        with engine().begin() as conn:
            conn.execute(
                text("UPDATE source_items SET acl = :a, acl_synced_at = now() WHERE item_id = :i"),
                {"a": new_acl, "i": item["item_id"]},
            )
            if sorted(item["acl"] or []) != new_acl:
                acl_mod.recompute_document_access(conn, item["document_id"])
                enqueue("index_acl", {"document_id": str(item["document_id"])},
                        dedupe_key=f"acl:{item['document_id']}", conn=conn)
                changed += 1
    return {"status": "done", "checked": len(items), "changed": changed}
