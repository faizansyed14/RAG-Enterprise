"""Documents whose original file is not kept (STORE_ORIGINALS=false): the source is the only copy.

A document stored this way has storage_key "source:<item id>" and no preview object. Its pages, chunks and embeddings are
in Postgres and Qdrant as always. This module finds the source item behind a document so the app can

  * give a link that opens the file at its source (SharePoint / OneDrive web address), asked for at the moment someone clicks;
  * download the file again for the few jobs that need the bytes (building a tree later, reading a scan with the vision model).

The bytes are never written to the object store by this module.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.scale.connectors.base import SourceFile
from app.scale.db import engine

log = logging.getLogger(__name__)
PREFIX = "source:"


def is_ref(storage_key: str | None) -> bool:
    return bool(storage_key) and str(storage_key).startswith(PREFIX)


def describe(document_id: str) -> dict | None:
    """The live source item behind a document (newest first when several sources hold identical bytes), or None."""
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.item_id, i.connection_id, i.external_id, i.path, i.name, i.size_bytes, i.version, i.acl, "
                "       c.kind, c.name AS connection_name "
                "FROM source_items i JOIN source_connections c USING (connection_id) "
                "WHERE i.document_id = :d AND NOT i.deleted ORDER BY i.updated_at DESC LIMIT 1"
            ),
            {"d": str(document_id)},
        ).mappings().first()
    return dict(row) if row else None


def _source_file(info: dict, connector) -> SourceFile:
    sf = SourceFile(
        external_id=info["external_id"], path=info["path"], name=info["name"], size=info["size_bytes"] or 0,
        modified_at=None, version=info["version"], acl=list(info["acl"] or []),
    )
    if getattr(connector, "drive_based", False) and ":" in sf.external_id:   # Graph ids are "<drive>:<item>"
        drive_id, item_ref = sf.external_id.split(":", 1)
        sf.extra = {"drive_id": drive_id, "item_id": item_ref}
    return sf


def where_it_lives(document_id: str) -> dict:
    """What the screen needs when the original is not stored: where the file is, and a link to open it if the source gives one.
    Never raises: a source that is down simply yields no link."""
    from app.scale.pipeline import connector_for

    info = describe(document_id)
    if info is None:
        return {"available": False}
    out = {"available": True, "source": info["connection_name"], "kind": info["kind"], "path": info["path"], "url": None}
    try:
        connector = connector_for(str(info["connection_id"]))
        link = getattr(connector, "source_link", None)
        if link is not None:
            out["url"] = link(_source_file(info, connector))
    except Exception:  # noqa: BLE001 -- a link is a convenience; the page text is still shown
        log.warning("could not get a source link for %s", document_id, exc_info=True)
    return out


def download(document_id: str, max_bytes: int) -> bytes:
    """The file's current bytes, fetched from its source (for jobs that cannot work from the stored pages alone)."""
    from app.scale.pipeline import connector_for

    info = describe(document_id)
    if info is None:
        raise LookupError("the file is no longer at its source")
    connector = connector_for(str(info["connection_id"]))
    chunks, total = [], 0
    with connector.open(_source_file(info, connector)) as src:
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise ValueError("the file is larger than the allowed size")
            chunks.append(block)
    return b"".join(chunks)
