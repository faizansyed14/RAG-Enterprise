"""source item -> object store -> pages -> chunk index.   (the 'lite' tier)

The lite tier gives every file a searchable, citeable, page-readable presence for the cost of parsing,
embedding and storage -- no LLM call per document. A document is exactly as readable by the agent as
a fully indexed one (get_page_content works, citations resolve); what it lacks is the full section tree
section tree, which can be built later for the documents that deserve it ('promote_tree' job).

Idempotence and failure
  * Every step can be re-run: chunk point ids are deterministic, the tree row is replaced, the
    object store key is overwritten.
  * One advisory lock per content hash serialises workers that meet identical bytes.
  * A crash leaves the document in 'extracting'; the retried job resumes it (it never loops on it).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.scale import acl as acl_mod
from app.scale import sourceref
from app.scale import index as ix
from app.scale.chunking import chunk_document
from app.scale.connectors import Connector, build_connector
from app.scale.connectors.base import SourceFile
from app.scale.db import engine
from app.scale import readers
from app.scale.formats import UnreadableFile, labeled_rows
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)

_BLOCK_TYPES = {"docx", "csv", "xlsx", "eml", "txt", "json", "xer", "py", "md"}
_KNOWN_TYPES = _BLOCK_TYPES | {"pdf"}
_IMAGE_TYPES = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}


class RetryLater(Exception):
    """Not a failure: another worker holds this content hash. Re-queue shortly."""


class Skipped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def extension_of(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


# ── connectors (one per connection per process) ──

_connectors: dict[str, Connector] = {}


def connector_for(connection_id: str) -> Connector:
    if connection_id not in _connectors:
        with engine().connect() as conn:
            row = conn.execute(
                text("SELECT kind, config FROM source_connections WHERE connection_id = :c"), {"c": connection_id}
            ).first()
        if row is None:
            raise LookupError(f"connection {connection_id} no longer exists")
        _connectors[connection_id] = build_connector(row[0], row[1])
    return _connectors[connection_id]


def forget_connectors() -> None:
    _connectors.clear()


# ── text extraction ──


def _pdf_pages(path: str) -> list[str]:
    from app.rag_core.local_api import LocalAPI

    return LocalAPI._extract_page_texts(path)


def _render_blocks_to_pages(blocks: list[dict], stem: str, tmp_dir: str) -> tuple[list[str], bytes]:
    from app.ingestion.text_to_pdf import render_blocks_as_pdf

    pdf_path = str(Path(tmp_dir) / f"{stem}.pdf")
    render_blocks_as_pdf(blocks, pdf_path)
    return _pdf_pages(pdf_path), Path(pdf_path).read_bytes()


def _docparse_ready(cfg) -> bool:
    """Is local layout + OCR parsing switched on AND installed? Off or missing -> the earlier behaviour, unchanged."""
    if cfg.docparse_mode == "off":
        return False
    from app.scale import docparse

    if docparse.available():
        return True
    log.warning("SCALE_DOCPARSE_MODE=%s but the docparse packages are not installed (requirements-docparse.txt)", cfg.docparse_mode)
    return False


def _docparse_pages(path: str, kind: str, cfg) -> tuple[list[str], bytes | None]:
    """-> (page texts, preview pdf or None). kind: 'pdf' | 'image'. Raises Skipped for files that cannot be read."""
    from app.scale import docparse

    opts = docparse.ParseOptions.from_settings(cfg)
    try:
        if kind == "image":
            doc = docparse.parse_image(path, opts, threads=cfg.docparse_threads)
        else:
            doc = docparse.parse_pdf(path, opts, threads=cfg.docparse_threads)
    except docparse.ParseError as exc:
        raise Skipped(f"unreadable:{exc}") from exc
    texts = doc.texts(cfg.docparse_table_style)
    if not any(t.strip() for t in texts):
        raise Skipped("no_text_found_by_ocr")
    return texts, (docparse.image_to_pdf(path) if kind == "image" else None)


def _vision_ready(cfg) -> bool:
    """Is vision reading of scanned / image pages switched on AND usable (an API key is set)?"""
    return bool(cfg.ocr_enabled and get_settings().openrouter_api_key)


def vision_pages(path: str, pages: list[str], cfg) -> list[int]:
    """The pages (0-based) of a PDF that the local text reader could not read and the vision model should:
    - no usable text (under vision_min_chars) but something on the page: images over it, or many drawn lines (a scan, a CAD sheet
      exported as lines). A blank page is left alone.
    - mostly one picture with little text: images cover vision_image_ratio of the page and it has under vision_max_text_chars.
    A page that already has its text is never sent, even with a picture on it."""
    import fitz

    picked: list[int] = []
    with fitz.open(path) as doc:
        for index, text_ in enumerate(pages):
            if index >= len(doc):
                break
            chars = len(text_.strip())
            if chars >= cfg.vision_max_text_chars:
                continue
            page = doc[index]
            area = (page.rect.width * page.rect.height) or 1.0
            covered = min(1.0, sum((fitz.Rect(info["bbox"]) & page.rect).get_area() for info in page.get_image_info()) / area)
            if chars < cfg.vision_min_chars:
                if covered >= 0.05 or len(page.get_cdrawings()) > 20:
                    picked.append(index)
            elif covered >= cfg.vision_image_ratio:
                picked.append(index)
    return picked


def _vision_text(path: str, indexes: list[int]) -> dict[int, str]:
    import asyncio

    from app.ingestion.diagram_pipeline import read_pages

    return asyncio.run(read_pages(path, indexes))


def _read_image_pages(path: str, pages: list[str], cfg) -> list[str]:
    """The text-layer pages, with the pages the local reader could not read replaced by what the vision model read."""
    wanted = vision_pages(path, pages, cfg)
    if not wanted:
        return pages
    if len(wanted) > cfg.vision_max_pages:
        log.warning("%s: %d pages need the vision model; reading the first %d (SCALE_VISION_MAX_PAGES), the rest keep their own text",
                    Path(path).name, len(wanted), cfg.vision_max_pages)
        wanted = wanted[: cfg.vision_max_pages]
    merged = list(pages)
    for index, text_ in _vision_text(path, wanted).items():
        if text_.strip():
            merged[index] = text_
    return merged


def _image_via_vision(path: str, stem: str, tmp_dir: str) -> tuple[list[str], bytes, bool]:
    """A photo or scan as a file of its own: wrap it as a one-page PDF (also the preview), read that page with the vision model."""
    import fitz

    with fitz.open(path) as image:
        pdf_bytes = image.convert_to_pdf()
    pdf_path = str(Path(tmp_dir) / f"{stem}.pdf")
    Path(pdf_path).write_bytes(pdf_bytes)
    text_ = _vision_text(pdf_path, [0]).get(0, "")
    if not text_.strip():
        raise Skipped("no_text_found_by_vision")
    return [text_], pdf_bytes, False


def extract_pages(path: str, filename: str, doc_type: str, tmp_dir: str) -> tuple[list[str], bytes | None, bool]:
    """-> (page texts, rendered preview pdf or None for native pdf, needs_ocr)."""
    stem = Path(filename).stem or "document"
    cfg = get_scale_settings()
    if doc_type == "image":
        if _docparse_ready(cfg):
            pages, preview = _docparse_pages(path, "image", cfg)
            return pages, preview, False
        return _image_via_vision(path, stem, tmp_dir)
    if doc_type == "pdf":
        # docparse "all": every PDF gets layout analysis (columns, tables as rows, running headers removed)
        if cfg.docparse_mode == "all" and _docparse_ready(cfg):
            pages, _ = _docparse_pages(path, "pdf", cfg)
            return pages, None, False
        pages = _pdf_pages(path)
        if _vision_ready(cfg) and not (cfg.docparse_mode == "scanned" and _docparse_ready(cfg)):
            pages = _read_image_pages(path, pages, cfg)           # scans, photos and picture-only pages go to the vision model
        words = [len(t.split()) for t in pages]
        # The legacy classifier (pdf_ingest.classify_pdf) wants >= 40 words on most of the first five pages,
        # which sends a short text-only PDF (a one-page letter) to vision OCR. For the lite tier a PDF counts
        # as scanned only when its text layer is essentially empty.
        if pages and (sum(words) / len(pages) >= cfg.min_words_per_page or sum(words) >= 200):
            return pages, None, False
        if cfg.docparse_mode == "scanned" and _docparse_ready(cfg):
            pages, _ = _docparse_pages(path, "pdf", cfg)
            return pages, None, False
        return [], None, True
    if doc_type in _BLOCK_TYPES:
        import importlib

        module = importlib.import_module(
            {"md": "app.ingestion.markdown_ingest", "py": "app.ingestion.python_ingest"}.get(
                doc_type, f"app.ingestion.{doc_type}_ingest"
            )
        )
        blocks = module.extract_blocks(path)
        if not blocks:
            raise Skipped("empty_document")
        pages, pdf = _render_blocks_to_pages(labeled_rows(blocks), stem, tmp_dir)
        return pages, pdf, False
    # Everything else (pptx odt rtf html, legacy Office through LibreOffice, text and code, unregistered text): blocks.
    ext = extension_of(filename)
    try:
        blocks = readers.blocks_for(path, ext, tmp_dir)
    except readers.Unsupported as exc:
        raise Skipped(str(exc)) from exc
    except UnreadableFile as exc:
        raise Skipped(f"unreadable:{exc}") from exc
    if not blocks:
        raise Skipped("empty_document")
    pages, pdf = _render_blocks_to_pages(labeled_rows(blocks), stem, tmp_dir)
    return pages, pdf, False


# ── names ──


@contextmanager
def _name_lock(base: str):
    """Serialise the 'is this name free?' check with the write that claims it. The agent addresses
    documents by name, so two documents must never end up with the same one."""
    key = int.from_bytes(hashlib.blake2b(f"name:{base}".encode(), digest_size=8).digest(), "little") >> 1
    with engine().connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            conn.commit()


def _suffixed(base: str, document_id: str) -> str:
    from app.rag_core.naming import truncate_filename

    return truncate_filename(base, suffix=f" [{document_id[:8]}]")


def _unique_name(conn, filename: str, document_id: str) -> str:
    from app.rag_core.naming import sanitize_filename

    base = sanitize_filename(filename)
    taken = conn.execute(
        text("SELECT 1 FROM document_trees WHERE meta ->> 'name' = :n LIMIT 1"), {"n": base}
    ).first()
    return _suffixed(base, document_id) if taken else base


def _restore_plain_name(document_id: str, filename: str) -> None:
    """A re-indexed file is saved while its previous version still holds the plain name, so it first gets
    a disambiguating suffix. Once the old version is gone, give the plain name back."""
    from app.rag_core.naming import sanitize_filename

    base = sanitize_filename(filename)
    with _name_lock(base) as conn:
        row = conn.execute(
            text(
                "SELECT d.rag_doc_id, t.meta ->> 'name' FROM documents d "
                "JOIN document_trees t ON t.doc_id = d.rag_doc_id WHERE d.document_id = :d"
            ),
            {"d": document_id},
        ).first()
        if row is None or row[1] == base or row[1] != _suffixed(base, document_id):
            return
        if conn.execute(
            text("SELECT 1 FROM document_trees WHERE meta ->> 'name' = :n AND doc_id <> :r LIMIT 1"),
            {"n": base, "r": row[0]},
        ).first():
            return
        conn.execute(
            text("UPDATE document_trees SET meta = jsonb_set(meta, '{name}', to_jsonb(CAST(:n AS text))), "
                 "updated_at = now() WHERE doc_id = :r"),
            {"n": base, "r": row[0]},
        )
        conn.execute(text("UPDATE document_pages SET doc_name = :n WHERE doc_id = :r"), {"n": base, "r": row[0]})
        conn.commit()
    ix.set_document_name(document_id, base)


# ── the pipeline ──


def ingest_item(item_id: str) -> dict:
    cfg = get_scale_settings()
    with engine().connect() as conn:
        item = conn.execute(
            text(
                "SELECT item_id, connection_id, external_id, path, name, size_bytes, version, acl, deleted, "
                "document_id, indexed_version FROM source_items WHERE item_id = :i"
            ),
            {"i": item_id},
        ).mappings().first()
    if item is None:
        return {"status": "gone"}
    if item["deleted"]:
        return delete_item(item_id)
    if item["indexed_version"] == item["version"] and item["document_id"]:
        return {"status": "unchanged"}
    if item["size_bytes"] and item["size_bytes"] > cfg.max_file_mb * 1024 * 1024:
        _mark(item_id, "skipped", f"too_large:{item['size_bytes']}")
        return {"status": "skipped", "reason": "too_large"}

    connector = connector_for(str(item["connection_id"]))
    sf = SourceFile(
        external_id=item["external_id"], path=item["path"], name=item["name"], size=item["size_bytes"] or 0,
        modified_at=None, version=item["version"], acl=list(item["acl"] or []),
    )
    # Graph needs drive/item ids; they are recoverable from the composite external id.
    if ":" in sf.external_id and getattr(connector, "drive_based", False):
        drive_id, item_ref = sf.external_id.split(":", 1)
        sf.extra = {"drive_id": drive_id, "item_id": item_ref}

    ext = extension_of(item["name"])
    doc_type = ext if ext in _KNOWN_TYPES else "other"
    if ext in _IMAGE_TYPES and (_docparse_ready(cfg) or _vision_ready(cfg)):
        doc_type = "image"                                  # read locally (OCR + layout), or by the vision model
    if doc_type == "other" and (ext in readers.BINARY_TYPES or ext in _IMAGE_TYPES):
        # known binary kinds are not even downloaded; images land here when the OCR packages are missing
        reason = "needs_ocr_packages" if ext in _IMAGE_TYPES else f"unsupported_type:{ext}"
        _mark(item_id, "skipped", reason)
        return {"status": "skipped", "reason": "unsupported_type"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = str(Path(tmp_dir) / (Path(item["name"]).name or "file"))
        digest = hashlib.sha256()
        total = 0
        with connector.open(sf) as src, open(local_path, "wb") as dst:
            while True:
                block = src.read(1 << 20)
                if not block:
                    break
                total += len(block)
                if total > cfg.max_file_mb * 1024 * 1024:
                    _mark(item_id, "skipped", "too_large")
                    return {"status": "skipped", "reason": "too_large"}
                digest.update(block)
                dst.write(block)
        content_hash = digest.hexdigest()

        # One worker at a time per distinct content.
        lock_key = int.from_bytes(hashlib.blake2b(content_hash.encode(), digest_size=8).digest(), "little") >> 1
        with engine().connect() as lock_conn:
            if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar():
                raise RetryLater(content_hash)
            try:
                return _ingest_locked(item, sf, local_path, doc_type, content_hash, tmp_dir)
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                lock_conn.commit()


def _ingest_locked(item, sf: SourceFile, local_path: str, doc_type: str, content_hash: str, tmp_dir: str) -> dict:
    from app.core.object_store import get_object_store

    cfg = get_scale_settings()
    item_id = str(item["item_id"])
    previous_document = str(item["document_id"]) if item["document_id"] else None

    with engine().begin() as conn:
        existing = conn.execute(
            text("SELECT document_id, status, storage_key, rag_doc_id FROM documents WHERE content_hash = :h"),
            {"h": content_hash},
        ).mappings().first()

    if existing and existing["status"] == "indexed":
        document_id = str(existing["document_id"])
        _link(item_id, document_id, item["version"])
        _refresh_access(document_id)
        _drop_orphan(previous_document, keep=document_id)
        return {"status": "linked", "document_id": document_id}

    # --- build a new document (or resume an interrupted one) ---
    try:
        pages, preview_pdf, needs_ocr = extract_pages(local_path, item["name"], doc_type, tmp_dir)
    except Skipped as exc:                               # a known, permanent reason: recorded on the item, never retried
        _mark(item_id, "skipped", exc.reason)
        return {"status": "skipped", "reason": exc.reason}
    if needs_ocr:
        if _vision_ready(cfg):                          # the vision model was asked and still found nothing on any page
            _mark(item_id, "skipped", "no_text_found_by_vision")
            return {"status": "skipped", "reason": "no_text"}
        _mark(item_id, "skipped", "needs_ocr (set SCALE_OCR_ENABLED=true, with OPENROUTER_API_KEY and MODEL_VISION, to read scanned pages with the vision model)")
        return {"status": "skipped", "reason": "needs_ocr"}
    if not needs_ocr and not any(p.strip() for p in pages):
        _mark(item_id, "skipped", "no_text")
        return {"status": "skipped", "reason": "no_text"}

    keep_copy = get_settings().store_originals
    store = get_object_store()
    data = Path(local_path).read_bytes()
    if existing:
        document_id = str(existing["document_id"])
        storage_key = existing["storage_key"]
        _purge_derived(document_id, existing["rag_doc_id"])
    else:
        document_id = str(uuid.uuid4())
        storage_key = f"originals/{uuid.uuid4()}/{item['name']}" if keep_copy else f"{sourceref.PREFIX}{item_id}"
    preview_key = None
    if keep_copy:
        store.put_object(storage_key, data, "application/octet-stream")
        if preview_pdf is not None:
            preview_key = f"previews/{document_id}/rendered.pdf"
            store.put_object(preview_key, preview_pdf, "application/pdf")

    with engine().begin() as conn:
        if not existing:
            conn.execute(
                text(
                    "INSERT INTO documents (document_id, filename, doc_type, content_hash, storage_key, status, tier) "
                    "VALUES (:d, :f, :t, :h, :k, 'extracting', 'lite')"
                ),
                {"d": document_id, "f": item["name"], "t": "other" if doc_type == "image" else doc_type, "h": content_hash, "k": storage_key},
            )

    if previous_document and previous_document != document_id:
        from app.scale import chunk_edit

        chunk_edit.carry_over(previous_document, document_id)       # the editor's work follows the file to its new version
    rag_doc_id = _write_tree_row(document_id, item, pages)
    chunk_count = _index_chunks(document_id, rag_doc_id, item, pages, sf.acl)

    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET rag_doc_id = :r, status = 'indexed', status_detail = NULL, error = NULL, "
                "page_count = :n, preview_storage_key = :p, tier = 'lite' WHERE document_id = :d"
            ),
            {"r": rag_doc_id, "n": len(pages), "p": preview_key, "d": document_id},
        )
    _link(item_id, document_id, item["version"])
    _refresh_access(document_id)
    _drop_orphan(previous_document, keep=document_id)
    _restore_plain_name(document_id, item["name"])

    if get_settings().tree_generation and len(pages) >= max(1, cfg.auto_promote_min_pages):
        from app.scale.queue import enqueue

        enqueue("promote_tree", {"document_id": document_id}, dedupe_key=f"promote:{document_id}")
    return {"status": "indexed", "document_id": document_id, "pages": len(pages), "chunks": chunk_count}


def _write_tree_row(document_id: str, item, pages: list[str]) -> str:
    from app.rag_core.naming import sanitize_filename

    rag_doc_id = "doc-" + uuid.uuid4().hex
    with _name_lock(sanitize_filename(item["name"])) as conn:
        name = _unique_name(conn, item["name"], document_id)
        return _save_tree_row(rag_doc_id, name, item, pages)


def _save_tree_row(rag_doc_id: str, name: str, item, pages: list[str]) -> str:
    from app.rag_core.postgres_store import PostgresDocStore

    meta = {
        "id": rag_doc_id,
        "name": name,
        # The folder path is indexed as the description, so words in it ("Project Falcon", "2023") are
        # searchable and visible to the agent when it lists documents.
        "description": f"Source: {item['path']}",
        "status": "completed",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "pageNum": len(pages),
        "folderId": None,
        "metadata": {"source_item": str(item["item_id"]), "tier": "lite",
                     "pages_without_text": sum(1 for t in pages if not t.strip())},
        "mode": "lite",
    }
    page_rows = [{"page_number": i, "markdown": t} for i, t in enumerate(pages, start=1)]
    PostgresDocStore().save_document(rag_doc_id, meta, [], page_rows)
    return rag_doc_id


def _index_chunks(document_id: str, rag_doc_id: str, item, pages: list[str], acl: list[str]) -> int:
    return _index_pages(document_id, rag_doc_id, item["path"], str(item["connection_id"]), pages, acl)


def _index_pages(document_id: str, rag_doc_id: str, path: str, source: str, pages: list[str], acl: list[str]) -> int:
    """Chunk the pages and write them to the index: the extracted chunks, with the chunk editor's saved changes applied and
    its hand-written chunks added, and (if switched on) the enrichment model's keywords and questions."""
    from app.scale import chunk_edit, enrich
    from app.scale.chunking import context_prefix
    from app.scale.pieces import Piece, index_pieces

    cfg = get_scale_settings()
    with engine().connect() as conn:
        name = conn.execute(
            text("SELECT meta ->> 'name' FROM document_trees WHERE doc_id = :r"), {"r": rag_doc_id}
        ).scalar_one()
    chunks = chunk_document(pages, title=name, path=path)
    plan = chunk_edit.plan_for_indexing(document_id, chunks)
    pieces = [Piece(page=c.page, index=c.index, text=c.text, context=c.context, start=c.start, end=c.end) for c in chunks]
    chunk_edit.apply_plan(pieces, plan, context_prefix(name, path, ""))
    if cfg.enrich_keywords > 0 or cfg.enrich_questions > 0:
        for piece, extra in zip(pieces, enrich.enrich_texts([p.text for p in pieces], name, cfg)):
            piece.keywords, piece.questions = extra.keywords, extra.questions
    # Write first, then remove what is no longer wanted. (Deleting first left the document unsearchable while it was
    # re-embedded, and left it with no chunks at all if the embedder failed half way.)
    before = ix.chunk_numbers(document_id)
    count = index_pieces(document_id, rag_doc_id, name, path, source, acl, pieces, cfg)
    stale = before - {p.index for p in pieces}
    if stale:
        ix.delete_points(document_id, sorted(stale))
    from app.scale import reconcile

    with engine().begin() as conn:
        conn.execute(
            text("UPDATE documents SET chunks_indexed_at = now(), chunks_expected = :n, index_signature = :s, "
                 "reconciled_at = now() WHERE document_id = :d"),
            {"d": document_id, "n": sum(1 for p in pieces if p.kind != "manual"), "s": reconcile.index_signature(cfg)},
        )
    return count


# ── documents that were not ingested by the scale pipeline (UI uploads, the existing library) ──


def index_document(document_id: str) -> dict:
    """Put a document that already has pages in Postgres into the hybrid chunk index.

    These are the documents uploaded through the UI and everything that existed before the scale layer was
    switched on. They keep their full section tree; this only adds them to the chunk index so that
    retrieval can find them. Who may read them is whatever `document_access` says; a document that has no
    access rows and no source item can only be a manual upload and keeps today's behaviour (every signed-in
    user), recorded explicitly.
    """
    from app.rag_core.postgres_store import PostgresDocStore

    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT rag_doc_id, filename, status, tier FROM documents WHERE document_id = :d"),
            {"d": document_id},
        ).first()
    if row is None:
        return {"status": "gone"}
    rag_doc_id, filename, status, tier = row
    if status != "indexed" or not rag_doc_id:
        return {"status": "not_ready"}
    with engine().begin() as conn:
        has_source = conn.execute(
            text("SELECT 1 FROM source_items WHERE document_id = :d LIMIT 1"), {"d": document_id}
        ).first()
        acl = [r[0] for r in conn.execute(
            text("SELECT principal FROM document_access WHERE document_id = :d"), {"d": document_id})]
        if not acl and not has_source:
            conn.execute(
                text("INSERT INTO document_access (document_id, principal) VALUES (:d, '*') ON CONFLICT DO NOTHING"),
                {"d": document_id},
            )
            acl = [acl_mod.EVERYONE]
    pages = [p.get("markdown", "") for p in (PostgresDocStore().get_pages(rag_doc_id) or []) if isinstance(p, dict)]
    path, source = filename, ""
    with engine().connect() as conn:       # a document that came from a source keeps its folder path and source in the index
        item = conn.execute(
            text("SELECT path, connection_id FROM source_items WHERE document_id = :d AND NOT deleted ORDER BY updated_at DESC LIMIT 1"),
            {"d": document_id},
        ).first()
    if item is not None:
        path, source = item[0], str(item[1])
    count = _index_pages(document_id, rag_doc_id, path, source, pages, acl)
    return {"status": "indexed", "document_id": document_id, "chunks": count, "tier": tier}


def enqueue_unindexed(limit: int = 500) -> int:
    """Queue chunk-indexing for documents that are indexed in Postgres but not yet in the chunk index.
    Safe to call repeatedly (dedupe keys); the worker calls it on a timer, and `cli index-existing` calls it
    right after the scale layer is first switched on."""
    from app.scale.queue import enqueue

    with engine().connect() as conn:
        ids = [str(r[0]) for r in conn.execute(
            text(
                "SELECT d.document_id FROM documents d WHERE d.chunks_indexed_at IS NULL AND d.status = 'indexed' "
                "AND d.rag_doc_id IS NOT NULL "
                "AND (d.tier <> 'lite' OR d.created_at < now() - interval '1 hour') "
                "AND NOT EXISTS (SELECT 1 FROM scale_jobs j WHERE j.dedupe_key = 'idx:' || d.document_id::text "
                "                AND j.status IN ('queued', 'running')) "
                "ORDER BY d.created_at LIMIT :n"
            ),
            {"n": limit},
        )]
    queued = 0
    for document_id in ids:
        if enqueue("index_document", {"document_id": document_id}, dedupe_key=f"idx:{document_id}") is not None:
            queued += 1
    return queued


# ── bookkeeping ──


def _mark(item_id: str, state: str, error: str | None = None) -> None:
    with engine().begin() as conn:
        conn.execute(
            text("UPDATE source_items SET state = :s, error = :e, updated_at = now() WHERE item_id = :i"),
            {"s": state, "e": error, "i": item_id},
        )


def _link(item_id: str, document_id: str, version: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE source_items SET document_id = :d, indexed_version = :v, state = 'indexed', error = NULL, "
                "updated_at = now() WHERE item_id = :i"
            ),
            {"d": document_id, "v": version, "i": item_id},
        )


def _refresh_access(document_id: str) -> list[str]:
    """Recompute document_access from live source items and push the result to every chunk."""
    with engine().begin() as conn:
        union = acl_mod.recompute_document_access(conn, document_id)
    ix.set_document_acl(document_id, union)
    return union


def _purge_derived(document_id: str, rag_doc_id: str | None) -> None:
    ix.delete_document_chunks(document_id)
    if rag_doc_id:
        from app.rag_core.postgres_store import PostgresDocStore

        PostgresDocStore().delete_document(rag_doc_id)


def _drop_orphan(document_id: str | None, keep: str | None = None) -> None:
    """Remove a document that no live source item points to any more."""
    if not document_id or document_id == keep:
        return
    with engine().connect() as conn:
        live = conn.execute(
            text("SELECT 1 FROM source_items WHERE document_id = :d AND NOT deleted LIMIT 1"), {"d": document_id}
        ).first()
    if live:
        _refresh_access(document_id)
        return
    delete_document_everywhere(document_id)


def _discard_working_copies(document_id: str, *keys: str) -> None:
    """STORE_ORIGINALS=false: remove the temporary original and any rendered preview an engine path left in the object store."""
    from app.core.object_store import get_object_store

    store = get_object_store()
    with engine().begin() as conn:
        preview = conn.execute(text("SELECT preview_storage_key FROM documents WHERE document_id = :d"), {"d": document_id}).scalar()
        conn.execute(text("UPDATE documents SET preview_storage_key = NULL WHERE document_id = :d"), {"d": document_id})
    for key in [*keys, preview]:
        if key:
            try:
                store.delete_object(key)
            except Exception:  # noqa: BLE001
                log.warning("could not delete working copy %s", key, exc_info=True)


def delete_document_everywhere(document_id: str) -> None:
    from app.core.object_store import get_object_store

    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT rag_doc_id, storage_key, preview_storage_key FROM documents WHERE document_id = :d"),
            {"d": document_id},
        ).first()
    if row is None:
        return
    _purge_derived(document_id, row[0])
    store = get_object_store()
    for key in (row[1], row[2]):
        if key and not sourceref.is_ref(key):
            try:
                store.delete_object(key)
            except Exception:  # noqa: BLE001 -- an already-missing object is not an error
                log.warning("could not delete object %s", key, exc_info=True)
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM documents WHERE document_id = :d"), {"d": document_id})


def delete_item(item_id: str) -> dict:
    """The source says this file is gone. Remove its access first (so it is unreadable immediately), then
    remove the document if nothing else still points to it."""
    with engine().connect() as conn:
        row = conn.execute(text("SELECT document_id FROM source_items WHERE item_id = :i"), {"i": item_id}).first()
    if row is None or row[0] is None:
        return {"status": "gone"}
    document_id = str(row[0])
    _refresh_access(document_id)  # deleted items no longer contribute principals
    _drop_orphan(document_id)
    return {"status": "deleted", "document_id": document_id}


def refresh_item_acl(item_id: str, new_acl: list[str]) -> dict:
    """Permission change only: rewrite the ACL, no parsing, no embedding."""
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "UPDATE source_items SET acl = :a, acl_synced_at = now(), updated_at = now() "
                "WHERE item_id = :i RETURNING document_id"
            ),
            {"a": acl_mod.normalize_acl(new_acl), "i": item_id},
        ).first()
    if row is None or row[0] is None:
        return {"status": "no_document"}
    union = _refresh_access(str(row[0]))
    return {"status": "acl_updated", "document_id": str(row[0]), "principals": len(union)}


def promote_to_tree(document_id: str) -> dict:
    """Build the full section tree for a document (LLM calls) and swap it in. The chunk index
    keeps working throughout; only the document's tree row is replaced."""
    import asyncio

    from app.ingestion.router import ingest_document

    if not get_settings().tree_generation:
        return {"status": "skipped", "reason": "TREE_GENERATION is off"}
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT storage_key, doc_type, rag_doc_id, tier FROM documents WHERE document_id = :d"),
            {"d": document_id},
        ).first()
    if row is None:
        return {"status": "gone"}
    if row[3] == "tree":
        return {"status": "already_tree"}
    old_rag_doc_id = row[2]
    if row[1] == "other":
        return {"status": "skipped", "reason": "no tree path for converted formats"}
    work_key = row[0]
    if sourceref.is_ref(row[0]):                    # the original was not kept: fetch it from the source for this job only
        from app.core.object_store import get_object_store

        work_key = f"tmp/{document_id}/original.{row[1]}"
        get_object_store().put_object(work_key, sourceref.download(document_id, get_scale_settings().max_file_mb * 1024 * 1024),
                                      "application/octet-stream")
    try:
        asyncio.run(ingest_document(uuid.UUID(document_id), work_key, row[1]))
    finally:
        if work_key != row[0]:
            _discard_working_copies(document_id, work_key)
    with engine().connect() as conn:
        new = conn.execute(
            text("SELECT status, rag_doc_id FROM documents WHERE document_id = :d"), {"d": document_id}
        ).first()
    if new is None or new[0] != "indexed" or not new[1]:
        raise RuntimeError("tree build failed; the lite version remains searchable")
    with engine().begin() as conn:
        conn.execute(text("UPDATE documents SET tier = 'tree' WHERE document_id = :d"), {"d": document_id})
    # Point every chunk at the new tree id and name, then drop the old tree row.
    client, name = ix.get_client(), ix.collection_name()
    with engine().connect() as conn:
        new_name = conn.execute(
            text("SELECT meta ->> 'name' FROM document_trees WHERE doc_id = :r"), {"r": new[1]}
        ).scalar_one()
    if client.collection_exists(name):
        client.set_payload(
            name, payload={"doc_id": new[1], "name": new_name}, points=ix._doc_filter(document_id), wait=True
        )
    if old_rag_doc_id and old_rag_doc_id != new[1]:
        from app.rag_core.postgres_store import PostgresDocStore

        PostgresDocStore().delete_document(old_rag_doc_id)
    return {"status": "promoted", "document_id": document_id, "rag_doc_id": new[1]}
