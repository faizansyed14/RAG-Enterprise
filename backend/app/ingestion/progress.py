"""
In-process pub/sub for ingestion progress, one queue per document. The
upload/status endpoints subscribe an SSE stream to a document's queue;
router.py and diagram_pipeline.py publish phase/page events into it as
ingestion actually happens -- this is what lets the frontend show real
chunking/OCR/indexing progress instead of a fake progress bar.

Events reach subscribers in this process directly. The latest event is also saved on the document row (`documents.progress`), so
the progress stream served by ANY replica can follow a document that another replica is working on, and a late subscriber still
sees where it stands. Saving is best-effort: progress must never fail an ingest.
"""

import asyncio
import uuid

_queues: dict[uuid.UUID, list[asyncio.Queue]] = {}


def subscribe(document_id: uuid.UUID) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _queues.setdefault(document_id, []).append(queue)
    return queue


def unsubscribe(document_id: uuid.UUID, queue: asyncio.Queue) -> None:
    subscribers = _queues.get(document_id)
    if subscribers and queue in subscribers:
        subscribers.remove(queue)
        if not subscribers:
            _queues.pop(document_id, None)


def publish(document_id: uuid.UUID, event: dict) -> None:
    for queue in _queues.get(document_id, []):
        queue.put_nowait(event)
    _save(document_id, event)


def _save(document_id: uuid.UUID, event: dict) -> None:
    try:
        import json

        from sqlalchemy import text

        from app.scale.db import engine

        light = {k: v for k, v in event.items() if k != "tree"}        # the section tree can be large; it has its own endpoint
        with engine().begin() as conn:
            conn.execute(
                text("UPDATE documents SET progress = CAST(:p AS jsonb), "
                     "ingest_lease_until = CASE WHEN :live THEN now() + make_interval(secs => :lease) ELSE NULL END "
                     "WHERE document_id = :d"),
                {"p": json.dumps(light), "d": str(document_id), "live": light.get("phase") not in ("indexed", "failed"),
                 "lease": LEASE_SECONDS},
            )
    except Exception:  # noqa: BLE001
        pass


LEASE_SECONDS = 120.0
