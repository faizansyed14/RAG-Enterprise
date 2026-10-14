"""
Ingestion dispatch, run as a background task after documents.py has
already written the `documents` row and confirmed content_hash is new.
Publishes progress events throughout (ingestion/progress.py) -- this is
what the frontend's real-time ingestion view actually watches.

Tree-engine calls (submit_pdf, get_tree) and DOCX parsing are synchronous,
blocking, LLM-calling operations (see app/rag_service.py) -- run via
asyncio.to_thread so a slow index build never blocks the event loop other
requests (including this same document's own progress SSE stream) depend on.
"""

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from app.core.object_store import get_object_store
from app.core.upload_validation import IMAGE_TYPES
from app.ingestion import (
    csv_ingest,
    local_reader,
    docx_ingest,
    eml_ingest,
    json_ingest,
    markdown_ingest,
    pdf_ingest,
    python_ingest,
    txt_ingest,
    xer_ingest,
    xlsx_ingest,
)
from app.ingestion.diagram_pipeline import process_pdf_pages
from app.ingestion.progress import publish
from app.ingestion.text_to_pdf import render_ocr_pages_as_pdf
from sqlalchemy import delete

from app.core.config import get_settings
from app.models.db import DiagramPage, Document, async_session
from app.rag_service import get_tree, submit_pdf
from app.retrieval.vector_store import delete_by_document, diagram_collection_name

logger = logging.getLogger(__name__)

# Every non-PDF format reduces to the same block-rendering pipeline
# (ingestion/blocks_ingest.py) -- only the extraction differs per format.
_BLOCK_PARSERS = {
    "docx": docx_ingest.parse_docx,
    "csv": csv_ingest.parse_csv,
    "xlsx": xlsx_ingest.parse_xlsx,
    "eml": eml_ingest.parse_eml,
    "txt": txt_ingest.parse_txt,
    "json": json_ingest.parse_json,
    "xer": xer_ingest.parse_xer,
    "py": python_ingest.parse_python,
    "md": markdown_ingest.parse_markdown,
}
_STATUS_DETAIL = {
    "docx": "Extracting paragraphs and tables",
    "csv": "Extracting rows",
    "xlsx": "Extracting sheets",
    "eml": "Extracting message",
    "txt": "Extracting text",
    "json": "Extracting document",
    "xer": "Extracting schedule tables",
    "py": "Extracting source",
    "md": "Extracting sections",
}


def _extension(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


def _parse_office(path: str, document_id) -> dict:
    """pptx / odt / rtf / html: the built-in readers, then the same render-and-index path as a Word file."""
    from app.ingestion.blocks_ingest import submit_blocks

    return submit_blocks(local_reader.office_blocks(path), path, document_id)


def _parse_image(path: str, document_id) -> dict:
    """A scan or photo: local OCR + layout, then a tree over the recognised text. The original image is the preview."""
    pages = local_reader.scanned_pages(path, "image")
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = str(Path(tmp_dir) / f"{Path(path).stem}.pdf")
        render_ocr_pages_as_pdf(pages, output_path=pdf_path)
        result = submit_pdf(pdf_path, "flash")
    preview_key = f"previews/{document_id}/rendered.pdf"
    get_object_store().put_object(preview_key, local_reader.image_preview_pdf(path), "application/pdf")
    return {"rag_doc_id": result["doc_id"], "is_scanned": True, "preview_storage_key": preview_key}


def _named_pdf_path(tmp_dir: str, original_filename: str) -> str:
    """The tree engine's local mode names a document after the basename of
    the path it's given (rag_core/local_api.py) -- a random tempfile name
    would surface as the citation's document name instead of the real
    uploaded filename. Writing into a fresh per-document temp *directory*
    under the real (extension-swapped-to-.pdf) name keeps citations
    readable without colliding with any other upload's temp file."""
    stem = Path(original_filename).stem or "document"
    return str(Path(tmp_dir) / f"{stem}.pdf")


async def _ingest_pdf(document_id: uuid.UUID, tmp_path: str, session) -> tuple[str, bool, int]:
    classification = await asyncio.to_thread(pdf_ingest.classify_pdf, tmp_path)
    if not classification["is_scanned"]:
        publish(document_id, {"phase": "indexing"})
        result = await asyncio.to_thread(submit_pdf, tmp_path, "flash")
        return result["doc_id"], False, classification["page_count"]

    if await asyncio.to_thread(local_reader.docparse_ready):
        # Default: the local OCR / layout / table models. Nothing leaves the machine and there is no per-page model cost.
        publish(document_id, {"phase": "ocr", "page": 0, "total": classification["page_count"]})
        ocr_pages = await asyncio.to_thread(local_reader.scanned_pages, tmp_path, "pdf")
    else:
        # SCALE_DOCPARSE_MODE=off (or the OCR packages are missing): the vision model reads every page, and also
        # captions the diagrams so that visual questions can find them.
        ocr_pages = await process_pdf_pages(document_id, tmp_path, session)
    publish(document_id, {"phase": "indexing"})
    # A sibling directory so the synthetic PDF can reuse tmp_path's exact
    # basename (same original-derived name -- see _named_pdf_path) without
    # colliding with tmp_path itself, which still exists at this point.
    with tempfile.TemporaryDirectory() as synth_dir:
        synthetic_pdf = str(Path(synth_dir) / Path(tmp_path).name)
        render_ocr_pages_as_pdf(ocr_pages, output_path=synthetic_pdf)
        result = await asyncio.to_thread(submit_pdf, synthetic_pdf, "flash")
    return result["doc_id"], True, classification["page_count"]


# At most `ingest_concurrency` documents are indexed at a time; the rest wait as "queued".
# One semaphore per event loop (a semaphore must not be shared across loops).
_gates: dict[int, asyncio.Semaphore] = {}
_recovery_tasks: set[asyncio.Task] = set()
_UNFINISHED = ("queued", "extracting", "ocr", "indexing")
LEASE_SECONDS = 120.0


def _gate() -> asyncio.Semaphore:
    key = id(asyncio.get_running_loop())
    if key not in _gates:
        _gates[key] = asyncio.Semaphore(max(1, get_settings().ingest_concurrency))
    return _gates[key]


def _renew_lease(document_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app.scale.db import engine

    with engine().begin() as conn:
        conn.execute(
            text("UPDATE documents SET ingest_lease_until = now() + make_interval(secs => :s) "
                 "WHERE document_id = :d AND status IN ('queued', 'extracting', 'ocr', 'indexing')"),
            {"s": LEASE_SECONDS, "d": str(document_id)},
        )


async def _lease_heartbeat(document_id: uuid.UUID) -> None:
    """While this process holds the document (waiting for a free slot or working on it) keep saying so. If the process dies the
    lease runs out and another replica resumes the document; while it lives no other replica touches it."""
    while True:
        try:
            await asyncio.to_thread(_renew_lease, document_id)
        except Exception:  # noqa: BLE001
            logger.warning("could not renew the ingest lease for %s", document_id, exc_info=True)
        await asyncio.sleep(LEASE_SECONDS / 4)


async def ingest_document(document_id: uuid.UUID, storage_key: str, doc_type: str) -> None:
    beat = asyncio.create_task(_lease_heartbeat(document_id))
    try:
        async with _gate():
            await _ingest_document_inner(document_id, storage_key, doc_type)
    finally:
        beat.cancel()


def schedule_ingest(document_id: uuid.UUID, storage_key: str, doc_type: str) -> None:
    """Start indexing in the background, independent of the HTTP request that asked for it.
    (Starlette BackgroundTasks keep the request -- and its database connection -- alive until
    the job finishes, which exhausts the connection pool as soon as many uploads queue up.)"""
    task = asyncio.create_task(ingest_document(document_id, storage_key, doc_type))
    _recovery_tasks.add(task)
    task.add_done_callback(_recovery_tasks.discard)


async def prepare_retry(session, doc: Document) -> None:
    """Put a failed/interrupted document back to `queued`, discarding the diagram rows and
    vectors a half-finished run may have written so a second run doesn't duplicate them."""
    await session.execute(delete(DiagramPage).where(DiagramPage.document_id == doc.document_id))
    try:
        await delete_by_document(diagram_collection_name(), doc.document_id)
    except Exception:  # noqa: BLE001 -- nothing to clean if the collection doesn't exist yet
        logger.warning("Could not clear diagram vectors for %s before retry", doc.document_id)
    doc.status = "queued"
    doc.status_detail = None
    doc.error = None
    await session.commit()


async def recover_unfinished_ingests(only: set[uuid.UUID] | None = None, limit: int = 50) -> int:
    """Resume documents that are queued/extracting/ocr/indexing but that no replica is working on. A document is claimed in
    one statement (FOR UPDATE SKIP LOCKED, lease set), so replicas that recover at the same moment never take the same one,
    and a document another replica is actively working on (lease still valid) is left alone. Runs at boot and every minute."""
    from sqlalchemy import text

    # 'lite' documents belong to the scale worker (which retries its own jobs); re-running the legacy
    # in-process ingestion on them would race the worker.
    sql = (
        "UPDATE documents SET ingest_lease_until = now() + make_interval(secs => :lease) WHERE document_id IN ("
        "  SELECT document_id FROM documents WHERE status = ANY(:st) AND tier <> 'lite' "
        "  AND (ingest_lease_until IS NULL OR ingest_lease_until < now()) "
        + ("  AND document_id = ANY(CAST(:only AS uuid[])) " if only is not None else "")
        + "  ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING document_id"
    )
    params: dict = {"lease": LEASE_SECONDS, "st": list(_UNFINISHED), "n": limit}
    if only is not None:
        params["only"] = [str(i) for i in only]
    async with async_session() as session:
        claimed = [r[0] for r in (await session.execute(text(sql), params)).all()]
        await session.commit()
        jobs = []
        for document_id in claimed:
            doc = await session.get(Document, document_id)
            if doc is None:
                continue
            await prepare_retry(session, doc)
            jobs.append((doc.document_id, doc.storage_key, doc.doc_type))
    for document_id, storage_key, doc_type in jobs:
        schedule_ingest(document_id, storage_key, doc_type)
    if jobs:
        logger.info("Resuming %d unfinished document(s) nobody is working on", len(jobs))
    return len(jobs)


async def _queue_chunk_indexing(document_id: uuid.UUID, tier: str) -> None:
    """Scale mode: a document finished by this (UI upload) path must also enter the hybrid chunk index, or
    automatic retrieval can never find it. 'lite' documents are indexed by their own pipeline. Never allowed
    to fail the ingest: the worker's timer picks up anything missed here."""
    if tier == "lite":
        return
    try:
        from app.scale.queue import enqueue

        await asyncio.to_thread(
            enqueue, "index_document", {"document_id": str(document_id)}, dedupe_key=f"idx:{document_id}"
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not queue chunk indexing for %s", document_id, exc_info=True)


async def _ingest_document_inner(document_id: uuid.UUID, storage_key: str, doc_type: str) -> None:
    async with async_session() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            logger.error("ingest_document called for missing document %s", document_id)
            return

        doc.status = "extracting"
        doc.status_detail = "Reading file"
        await session.commit()
        publish(document_id, {"phase": "extracting"})

        store = get_object_store()

        try:
            data = await asyncio.to_thread(store.get_object, storage_key)
            with tempfile.TemporaryDirectory() as tmp_dir:
                if doc_type == "pdf":
                    tmp_path = _named_pdf_path(tmp_dir, doc.filename)
                else:
                    tmp_path = str(Path(tmp_dir) / (Path(doc.filename).name or f"document.{doc_type}"))
                Path(tmp_path).write_bytes(data)

                ext = _extension(doc.filename)
                if doc_type == "pdf":
                    rag_doc_id, is_scanned, page_count = await _ingest_pdf(document_id, tmp_path, session)
                else:
                    doc.status = "extracting"
                    doc.status_detail = _STATUS_DETAIL.get(doc_type) or (
                        "Running OCR" if ext in IMAGE_TYPES else "Extracting content")
                    await session.commit()
                    parser = _BLOCK_PARSERS.get(doc_type)
                    if parser is None:                      # doc_type "other": the extension says how to read it
                        parser = _parse_image if ext in IMAGE_TYPES else _parse_office
                    result = await asyncio.to_thread(parser, tmp_path, document_id)
                    rag_doc_id, is_scanned = result["rag_doc_id"], bool(result.get("is_scanned"))
                    page_count = 1 if ext in IMAGE_TYPES else None
                    doc.preview_storage_key = result["preview_storage_key"]

                doc.rag_doc_id = rag_doc_id
                doc.is_scanned = is_scanned
                if page_count is not None:
                    doc.page_count = page_count
                doc.status = "indexed"
                doc.status_detail = None
                await session.commit()
                await _queue_chunk_indexing(document_id, doc.tier)

                tree = await asyncio.to_thread(get_tree, rag_doc_id)
                publish(document_id, {"phase": "indexed", "tree": tree.get("result", [])})

        except Exception as exc:
            doc.status = "failed"
            doc.error = str(exc)
            await session.commit()
            publish(document_id, {"phase": "failed", "error": str(exc)})
            logger.exception("Ingestion failed for document %s", document_id)
