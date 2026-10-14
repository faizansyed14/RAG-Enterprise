import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import get_settings
from app.scale import acl as acl_mod
from app.scale import index as scale_index
from app.scale import sourceref
from app.scale.settings import get_scale_settings
from app.core.object_store import get_object_store
from app.core.upload_validation import CONTENT_TYPES, extension_of, read_capped, sanitize_filename, validate_content
from app.core.security import require_admin, require_user
from app.ingestion import local_reader
from app.ingestion.progress import subscribe, unsubscribe
from app.ingestion.router import prepare_retry, schedule_ingest
from app.models.db import DiagramPage, Document, DocumentFolder, Folder, async_session, get_session
from app.models.schemas import DocumentOut, DocumentTreeResponse, SetDocumentFoldersRequest, UploadResponse
from app.rag_service import delete_document as rag_delete_document, get_index_dump, get_tree
from app.retrieval.vector_store import delete_by_document, diagram_collection_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])



async def _folder_ids_by_document(session: AsyncSession, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """One grouped query for however many documents are being returned,
    not one query per document."""
    if not document_ids:
        return {}
    rows = (
        await session.execute(
            select(DocumentFolder.document_id, DocumentFolder.folder_id).where(
                DocumentFolder.document_id.in_(document_ids)
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for document_id, folder_id in rows:
        grouped[document_id].append(folder_id)
    return grouped


def _document_out(doc: Document, folder_ids: list[uuid.UUID]) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.folder_ids = folder_ids
    return out


async def _add_to_folder(session: AsyncSession, document_id: uuid.UUID, folder_id: uuid.UUID) -> None:
    existing = await session.get(DocumentFolder, {"document_id": document_id, "folder_id": folder_id})
    if existing is not None:
        return
    session.add(DocumentFolder(document_id=document_id, folder_id=folder_id))
    await session.commit()


@router.post("", response_model=UploadResponse)
async def upload_document(
    file: UploadFile,
    folder_id: uuid.UUID | None = Form(None),
    admin=Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    filename = sanitize_filename(file.filename)
    ext = extension_of(filename)
    allowed = local_reader.accepted_types()
    if ext not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported file type: .{ext}. Supported: {', '.join(sorted(allowed))}",
        )
    doc_type = allowed[ext]

    if folder_id is not None and await session.get(Folder, folder_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")

    data = await read_capped(file, get_settings().max_upload_mb * 1024 * 1024)
    validate_content(ext, data)
    content_hash = hashlib.sha256(data).hexdigest()

    existing = await session.execute(select(Document).where(Document.content_hash == content_hash))
    existing_doc = existing.scalar_one_or_none()
    if existing_doc is not None:
        # Re-uploading (possibly identical) content into a folder still
        # files it there -- e.g. dragging an already-indexed file into a
        # second folder via the OS file picker should work like "copy",
        # not silently do nothing.
        if folder_id is not None:
            await _add_to_folder(session, existing_doc.document_id, folder_id)
        if existing_doc.status == "failed":
            # Uploading the same file again is how you retry a failed one.
            await prepare_retry(session, existing_doc)
            schedule_ingest(existing_doc.document_id, existing_doc.storage_key, existing_doc.doc_type)
            return UploadResponse(document_id=existing_doc.document_id, status="queued")
        return UploadResponse(document_id=existing_doc.document_id, status=existing_doc.status)

    store = get_object_store()
    storage_key = f"originals/{uuid.uuid4()}/{filename}"
    await asyncio.to_thread(store.put_object, storage_key, data, content_type=CONTENT_TYPES.get(ext, "text/plain"))

    doc = Document(filename=filename, doc_type=doc_type, content_hash=content_hash,
                    storage_key=storage_key, status="queued",
                    ingest_lease_until=datetime.now(timezone.utc) + timedelta(seconds=120))   # this replica holds it; see ingestion/router.py
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    # A UI upload is readable by every signed-in user; without this row the fail-closed access check would hide it from
    # everyone, its uploader included. (To restrict who may read files, add them through Sources > Files instead.)
    await asyncio.to_thread(acl_mod.grant_everyone, doc.document_id)

    if folder_id is not None:
        await _add_to_folder(session, doc.document_id, folder_id)

    audit("document_uploaded", by=admin.username, document=doc.document_id, type=doc_type, bytes=len(data))
    # Hand the connection back to the pool before the (long) indexing job starts.
    await session.close()
    schedule_ingest(doc.document_id, storage_key, doc_type)
    return UploadResponse(document_id=doc.document_id, status="queued")


def _parse_cursor(cursor: str | None):
    """"<created_at iso>|<document id>" from the X-Next-Cursor header of the previous page, or None."""
    if not cursor:
        return None
    try:
        stamp, ident = cursor.split("|", 1)
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "cursor is not one this server returned") from None


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    folder_id: uuid.UUID | None = None,
    unfiled: bool = False,
    limit: int | None = None,
    offset: int = 0,
    cursor: str | None = None,
    q: str | None = None,
    response: Response = None,  # type: ignore[assignment]
    _admin=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentOut]:
    query = select(Document).order_by(Document.created_at.desc(), Document.document_id.desc())
    scale = get_scale_settings()
    after = _parse_cursor(cursor)
    # Only documents this user may read, newest first. Clients that send no `limit` (the current UI) get up to
    # SCALE_LEGACY_LIST_LIMIT; paging clients send ?cursor= (or ?limit=&offset=).
    if not (scale.admin_bypass_acl and _admin.role == "admin"):
        principals = await asyncio.to_thread(acl_mod.principals_for_user, _admin.user_id, _admin.username)
        da = acl_mod.document_access_table
        query = query.where(
            Document.document_id.in_(select(da.c.document_id).where(da.c.principal.in_(sorted(principals))))
        )
    if q:
        query = query.where(Document.filename.ilike(f"%{q}%"))
    limit = scale.legacy_list_limit if limit is None else min(max(limit, 1), 1000)
    if after is not None:
        # Keyset paging: continue after the last row of the previous page. Cost does not grow with the page number
        # (an OFFSET of a million rows reads and discards a million rows).
        query = query.where(tuple_(Document.created_at, Document.document_id) < tuple_(after[0], after[1]))
    else:
        query = query.offset(max(offset, 0))
    query = query.limit(limit)
    if folder_id is not None:
        query = query.join(DocumentFolder, DocumentFolder.document_id == Document.document_id).where(
            DocumentFolder.folder_id == folder_id
        )
    elif unfiled:
        query = query.outerjoin(DocumentFolder, DocumentFolder.document_id == Document.document_id).where(
            DocumentFolder.folder_id.is_(None)
        )
    docs = (await session.execute(query)).scalars().all()
    if response is not None and docs and len(docs) == limit and docs[-1].created_at is not None:
        response.headers["X-Next-Cursor"] = f"{docs[-1].created_at.isoformat()}|{docs[-1].document_id}"
    folder_map = await _folder_ids_by_document(session, [d.document_id for d in docs])
    return [_document_out(d, folder_map.get(d.document_id, [])) for d in docs]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    folder_map = await _folder_ids_by_document(session, [document_id])
    return _document_out(doc, folder_map.get(document_id, []))


@router.put("/{document_id}/folders", response_model=DocumentOut)
async def set_document_folders(
    document_id: uuid.UUID,
    body: SetDocumentFoldersRequest,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    """Replaces a document's complete folder membership in one call -- the
    one primitive that drives move (new set = [target]), copy (new set =
    current + [target]), and remove-from-folder (new set = current minus
    one) from the frontend, which computes the desired final set itself."""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    folder_ids = list(dict.fromkeys(body.folder_ids))  # de-dupe, keep order
    if folder_ids:
        found = (
            await session.execute(select(Folder.folder_id).where(Folder.folder_id.in_(folder_ids)))
        ).scalars().all()
        missing = set(folder_ids) - set(found)
        if missing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Folder(s) not found: {', '.join(str(m) for m in missing)}")

    await session.execute(delete(DocumentFolder).where(DocumentFolder.document_id == document_id))
    for folder_id in folder_ids:
        session.add(DocumentFolder(document_id=document_id, folder_id=folder_id))
    await session.commit()

    return _document_out(doc, folder_ids)


@router.get("/{document_id}/tree", response_model=DocumentTreeResponse)
async def get_document_tree(
    document_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DocumentTreeResponse:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if doc.rag_doc_id is None:
        return DocumentTreeResponse(document_id=document_id, tree=[])
    tree = await asyncio.to_thread(get_tree, doc.rag_doc_id)
    return DocumentTreeResponse(document_id=document_id, tree=tree.get("result", []))


@router.get("/{document_id}/progress")
async def document_progress(
    document_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE stream of ingestion phases as they happen -- see ingestion/progress.py.

    progress.py's pub/sub has no history: publish() only reaches queues that
    already exist at that exact moment, so subscribing after the terminal
    event already fired means waiting forever on one that will never repeat.
    That's not just a narrow race -- it's guaranteed whenever upload_document's
    content-hash dedup path returns an already-indexed document without
    scheduling a new ingest_document() task at all (no background task means
    no publish() will ever fire for this document_id again). Subscribing
    before checking status (not after) closes the narrow race; checking
    status at all closes the dedup case.
    """
    queue = subscribe(document_id)
    doc = await session.get(Document, document_id)
    already_done = doc is not None and doc.status in ("indexed", "failed")

    async def event_stream():
        try:
            if already_done:
                event: dict = {"phase": doc.status}
                if doc.status == "failed" and doc.error:
                    event["error"] = doc.error
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
                return
            last = None
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Another replica may be doing the work: follow the progress it saved on the document row.
                    async with async_session() as fresh:
                        row = await fresh.get(Document, document_id)
                    if row is None:
                        break
                    event = dict(row.progress or {"phase": row.status})
                    if row.status in ("indexed", "failed"):
                        event = {"phase": row.status, **({"error": row.error} if row.status == "failed" and row.error else {})}
                    if event == last:
                        continue
                last = event
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
                if event.get("phase") in ("indexed", "failed"):
                    break
        finally:
            unsubscribe(document_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{document_id}/raw")
async def get_document_raw(
    document_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Full stored index dump: Postgres row, object-store keys, tree-engine
    meta/pages/tree, and any diagram OCR rows — so the UI can show exactly
    what was persisted after upload."""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    diagrams = (
        await session.execute(
            select(DiagramPage)
            .where(DiagramPage.document_id == document_id)
            .order_by(DiagramPage.page_number)
        )
    ).scalars().all()

    index: dict = {
        "rag_doc_id": doc.rag_doc_id,
        "meta": None,
        "pages": [],
        "node_chunks": [],
        "tree": [],
    }
    if doc.rag_doc_id:
        try:
            index = await asyncio.to_thread(get_index_dump, doc.rag_doc_id)
        except Exception as exc:  # noqa: BLE001 -- surface to UI, don't 500 the dump
            index = {
                "rag_doc_id": doc.rag_doc_id,
                "meta": None,
                "pages": [],
                "node_chunks": [],
                "tree": [],
                "error": str(exc),
            }

    return {
        "document": {
            "document_id": str(doc.document_id),
            "filename": doc.filename,
            "doc_type": doc.doc_type,
            "status": doc.status,
            "status_detail": doc.status_detail,
            "error": doc.error,
            "is_scanned": doc.is_scanned,
            "page_count": doc.page_count,
            "content_hash": doc.content_hash,
            "storage_key": doc.storage_key,
            "preview_storage_key": doc.preview_storage_key,
            "rag_doc_id": doc.rag_doc_id,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
        },
        "index": index,
        "diagrams": [
            {
                "page_number": d.page_number,
                "figure_id": d.figure_id,
                "caption": d.caption,
                "description": d.description,
                "ocr_text": d.ocr_text,
                "image_key": d.image_key,
                "qdrant_point_id": str(d.qdrant_point_id),
                "embedding_model": d.embedding_model,
                "callouts": d.callouts,
            }
            for d in diagrams
        ],
    }


async def _require_readable(user, document_id: uuid.UUID) -> None:
    """Scale mode: a presigned file URL is a read of the file, so it needs the same access check as a
    search. Answers 404 (not 403) so a guessed id reveals nothing about whether the document exists."""
    scale = get_scale_settings()
    if scale.admin_bypass_acl and user.role == "admin":
        return
    principals = await asyncio.to_thread(acl_mod.principals_for_user, user.user_id, user.username)
    if str(document_id) not in await asyncio.to_thread(acl_mod.visible_document_ids, [document_id], principals):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")


FILE_MISSING_MESSAGE = "This file was deleted from storage or could not be found."


@router.get("/{document_id}/preview")
async def preview_document(
    document_id: uuid.UUID,
    _admin=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _require_readable(_admin, document_id)
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    # DOCX/CSV/etc: preview the rendered PDF actually fed to the tree engine
    # (the raw original can't be rendered by the frontend's PDF viewer) --
    # see ingestion/blocks_ingest.py. PDF: the original is already a PDF, so
    # it's fine to fall back to it -- but only for doc_type "pdf": for every
    # other format, preview_storage_key is only ever set *after* ingestion
    # succeeds (blocks_ingest.py uploads it only once submit_pdf doesn't
    # raise), so a failed non-PDF upload has no preview_storage_key and no
    # valid PDF to fall back to either -- falling back to storage_key there
    # used to hand the frontend's PDF viewer the raw .csv/.docx bytes,
    # crashing it with "Invalid PDF structure" instead of a clear "no
    # preview yet" response.
    if sourceref.is_ref(doc.storage_key):
        # STORE_ORIGINALS=false: there is no stored copy. Say where the file lives (and how to open it there); the screen shows
        # the stored text of the cited page next to that.
        where = await asyncio.to_thread(sourceref.where_it_lives, str(document_id))
        return {"url": None, "stored": False, "name": doc.filename, "page_count": doc.page_count, "source": where}
    if doc.preview_storage_key:
        key = doc.preview_storage_key
    elif doc.doc_type == "pdf":
        key = doc.storage_key
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No preview available for this document")
    store = get_object_store()
    if not await asyncio.to_thread(store.exists, key):
        # The stored copy was deleted (or never made it). Say so plainly instead of handing the viewer a link that answers 404.
        # If the file also lives at a source, the screen can still show where and the stored page text.
        where = await asyncio.to_thread(sourceref.where_it_lives, str(document_id))
        logger.warning("stored file %s for document %s is missing from object storage", key, document_id)
        return {"url": None, "stored": bool(not where.get("available")), "missing": True, "name": doc.filename,
                "page_count": doc.page_count, "message": FILE_MISSING_MESSAGE, "source": where if where.get("available") else None}
    url = store.presigned_url(key)
    return {"url": url}


@router.get("/{document_id}/pages/{page_number}")
async def page_text(
    document_id: uuid.UUID,
    page_number: int,
    _admin=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The stored text of one page: what a citation shows when the original file is not kept. Same access check as a search."""
    await _require_readable(_admin, document_id)
    doc = await session.get(Document, document_id)
    if doc is None or not doc.rag_doc_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    row = (await session.execute(
        text("SELECT text FROM document_pages WHERE doc_id = :r AND page_number = :p"), {"r": doc.rag_doc_id, "p": page_number}
    )).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return {"page": page_number, "page_count": doc.page_count, "text": row[0]}


@router.get("/{document_id}/diagrams/{page_number}")
async def preview_diagram_page(
    document_id: uuid.UUID,
    page_number: int,
    _admin=Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _require_readable(_admin, document_id)
    result = await session.execute(
        select(DiagramPage).where(
            DiagramPage.document_id == document_id, DiagramPage.page_number == page_number
        )
    )
    diagram = result.scalar_one_or_none()
    if diagram is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Diagram page not found")
    url = get_object_store().presigned_url(diagram.image_key)
    return {"url": url, "caption": diagram.caption, "description": diagram.description,
            "figure_id": diagram.figure_id, "ocr_text": diagram.ocr_text}


def _delete_object_logged(key: str, what: str, document_id: uuid.UUID) -> None:
    try:
        get_object_store().delete_object(key)
    except Exception:
        logger.exception("Failed to delete %s (%s) for document %s", what, key, document_id)


@router.delete("/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    # Diagram rows carry the only pointers to their page-crop PNGs -- read
    # them before the DELETE below removes those pointers, or the objects
    # become permanently unreachable orphans in object storage.
    diagram_keys = (
        await session.execute(
            select(DiagramPage.image_key).where(DiagramPage.document_id == document_id)
        )
    ).scalars().all()

    if doc.rag_doc_id:
        try:
            await asyncio.to_thread(rag_delete_document, doc.rag_doc_id)
        except Exception:
            logger.exception("Failed to delete tree-engine document %s for document %s", doc.rag_doc_id, document_id)

    if not sourceref.is_ref(doc.storage_key):                    # a source-only document has no stored copy to delete
        _delete_object_logged(doc.storage_key, "original upload", document_id)
    if doc.preview_storage_key:
        _delete_object_logged(doc.preview_storage_key, "preview", document_id)
    for image_key in diagram_keys:
        _delete_object_logged(image_key, "diagram page image", document_id)

    await delete_by_document(diagram_collection_name(), document_id)
    try:
        await asyncio.to_thread(scale_index.delete_document_chunks, str(document_id))
    except Exception:  # noqa: BLE001 -- search hides it already (every hit is re-checked in Postgres); the orphan sweep reclaims the space
        logger.warning("could not remove the search chunks of %s now; the orphan sweep will", document_id, exc_info=True)
    await session.execute(delete(DiagramPage).where(DiagramPage.document_id == document_id))
    await session.delete(doc)
    await session.commit()
    return {"document_id": str(document_id), "deleted": True}
