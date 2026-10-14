"""Changing the embedding model (or anything else that renames the collection) without a search outage.

Every collection name is derived from the settings that built it, so changing a setting points search at a NEW, empty
collection. Without help that means "nothing found" until the whole corpus is re-embedded. This module keeps the previous
collection readable until the new one has caught up:

* each collection's settings are recorded when it first becomes current (the manifest);
* when the current collection changes, the previous one becomes *legacy* and stays in place;
* search also queries every legacy collection, using the embedder recorded in its manifest, and keeps a legacy hit only for a
  document that has not been rebuilt yet (a rebuilt document is answered from the new collection);
* permission changes and deletions are applied to the legacy collections too, so an old index never serves what was revoked;
* when no document is left on the old build, `retire` deletes the legacy collections.

Rolling back is putting the old settings back: the new collection becomes the legacy one and documents re-embed as they are due.
Legacy reads only ever add recall. A legacy collection that cannot be queried (its model is gone) is skipped with a warning.
"""

from __future__ import annotations

import json
import logging
import re
import time

from sqlalchemy import text

from app.scale import index as ix
from app.scale.db import engine
from app.scale.settings import ScaleSettings, get_scale_settings

log = logging.getLogger("scale.migration")

# The settings that decide what a collection's vectors and terms mean. Secrets and infrastructure addresses are not recorded:
# a legacy read uses today's Qdrant and today's API key.
MANIFEST_FIELDS = (
    "collection", "embed_backend", "embed_model", "embed_dim", "embed_base_url", "embed_query_prefix", "embed_doc_prefix",
    "embed_batch", "embed_timeout_s", "onnx_model_dir", "onnx_max_tokens", "onnx_pooling", "stem_langs", "sparse_avgdl",
    "ctx_dense", "ctx_sparse",
)
_SECRET = re.compile(r"(key|secret|password|token)$", re.I)

_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 10.0


def _get(key: str) -> str | None:
    with engine().connect() as conn:
        return conn.execute(text("SELECT value FROM scale_state WHERE key = :k"), {"k": key}).scalar()


def _set(key: str, value: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("INSERT INTO scale_state (key, value) VALUES (:k, :v) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": key, "v": value},
        )
    _CACHE.clear()


def manifest_of(cfg: ScaleSettings) -> dict:
    dump = cfg.model_dump()
    return {k: dump[k] for k in MANIFEST_FIELDS if k in dump and not _SECRET.search(k)}


def _legacy_list() -> list[dict]:
    raw = _get("legacy_collections")
    return json.loads(raw) if raw else []


def note_current(cfg: ScaleSettings | None = None) -> None:
    """Called when a collection is opened for writing. Records its manifest; if the current collection changed, the previous
    one becomes legacy. Never raises: bookkeeping must not stop ingestion."""
    cfg = cfg or get_scale_settings()
    try:
        name = ix.collection_name(cfg)
        current = _get("current_collection")
        if current == name:
            return
        if current:
            old_raw = _get(f"manifest:{current}")
            legacy = [e for e in _legacy_list() if e["name"] not in (current, name)]
            if old_raw:
                legacy.append({"name": current, "manifest": json.loads(old_raw)})
            else:
                log.warning("collection %s has no recorded settings (built before migrations were tracked), so it cannot be "
                            "read after the switch", current)
            _set("legacy_collections", json.dumps([e for e in legacy if e["name"] != name]))
        else:
            _set("legacy_collections", json.dumps([e for e in _legacy_list() if e["name"] != name]))
        _set(f"manifest:{name}", json.dumps(manifest_of(cfg)))
        _set("current_collection", name)
        if current:
            log.info("index build changed: %s is now current, %s is read as legacy until every document is rebuilt", name, current)
    except Exception:  # noqa: BLE001
        log.warning("could not record the index build", exc_info=True)


def _cfg_from(manifest: dict, current: ScaleSettings) -> ScaleSettings:
    return ScaleSettings(**{**current.model_dump(), **manifest})


def legacy_configs(cfg: ScaleSettings | None = None) -> list[tuple[str, ScaleSettings]]:
    """(collection name, the settings that built it) for each legacy collection that still exists and is worth reading."""
    cfg = cfg or get_scale_settings()
    hit = _CACHE.get("legacy")
    now = time.monotonic()
    if hit and now - hit[0] < _TTL and _CACHE.get("legacy_for") == (cfg.qdrant_location, ix.collection_name(cfg)):
        return hit[1]  # type: ignore[return-value]
    out: list[tuple[str, ScaleSettings]] = []
    try:
        current = ix.collection_name(cfg)
        client = ix.get_client(cfg)
        for entry in _legacy_list():
            if entry["name"] == current or not client.collection_exists(entry["name"]):
                continue
            legacy_cfg = _cfg_from(entry["manifest"], cfg)
            if ix.collection_name(legacy_cfg) != entry["name"]:
                log.warning("legacy collection %s cannot be reproduced from its recorded settings; skipping it", entry["name"])
                continue
            out.append((entry["name"], legacy_cfg))
    except Exception:  # noqa: BLE001 -- search must keep working without the old index
        log.warning("could not read the legacy index list", exc_info=True)
    _CACHE["legacy"] = (now, out)
    _CACHE["legacy_for"] = (cfg.qdrant_location, ix.collection_name(cfg))  # type: ignore[assignment]
    return out


def all_names(cfg: ScaleSettings | None = None) -> list[str]:
    """The current collection plus legacy ones that exist: where a permission change or a deletion must be applied."""
    cfg = cfg or get_scale_settings()
    return [ix.collection_name(cfg)] + [n for n, _ in legacy_configs(cfg)]


def migrated_ids(document_ids: list[str], cfg: ScaleSettings | None = None) -> set[str]:
    """Which of these documents are already built with the current settings (so a legacy hit for them is dropped)."""
    if not document_ids:
        return set()
    from app.scale.reconcile import index_signature

    with engine().connect() as conn:
        rows = conn.execute(
            text("SELECT document_id::text FROM documents WHERE document_id = ANY(CAST(:ids AS uuid[])) "
                 "AND chunks_indexed_at IS NOT NULL AND index_signature = :s"),
            {"ids": list(document_ids), "s": index_signature(cfg)},
        )
        return {r[0] for r in rows}


def status(cfg: ScaleSettings | None = None) -> dict:
    cfg = cfg or get_scale_settings()
    from app.scale.reconcile import index_signature

    legacy = legacy_configs(cfg)
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT count(*) FILTER (WHERE chunks_indexed_at IS NOT NULL AND index_signature = :s) AS rebuilt, "
                 "       count(*) FILTER (WHERE status = 'indexed' AND rag_doc_id IS NOT NULL "
                 "                        AND (chunks_indexed_at IS NULL OR index_signature IS DISTINCT FROM :s)) AS remaining "
                 "FROM documents"),
            {"s": index_signature(cfg)},
        ).mappings().one()
    total = row["rebuilt"] + row["remaining"]
    return {
        "current": ix.collection_name(cfg), "legacy": [n for n, _ in legacy], "migrating": bool(legacy) and row["remaining"] > 0,
        "documents_rebuilt": row["rebuilt"], "documents_remaining": row["remaining"],
        "percent": round(100.0 * row["rebuilt"] / total, 1) if total else 100.0,
    }


def retire(cfg: ScaleSettings | None = None, *, force: bool = False) -> list[str]:
    """Delete the legacy collections. Refused while any document is still on the old build, unless forced."""
    cfg = cfg or get_scale_settings()
    st = status(cfg)
    if st["documents_remaining"] and not force:
        raise RuntimeError(f"{st['documents_remaining']} documents are not rebuilt yet ({st['percent']}% done); retiring now "
                           "would make them unsearchable. Wait, or pass force to delete anyway.")
    client = ix.get_client(cfg)
    removed = []
    for entry in _legacy_list():
        if entry["name"] != ix.collection_name(cfg) and client.collection_exists(entry["name"]):
            client.delete_collection(entry["name"])
            removed.append(entry["name"])
    _set("legacy_collections", "[]")
    return removed
