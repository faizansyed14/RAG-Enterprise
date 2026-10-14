"""Readers that need no model server and send nothing off the machine, for the formats the original ingesters did
not cover: scanned PDFs and images (local OCR + layout + tables, `app/scale/docparse`), PowerPoint / OpenDocument / RTF /
HTML (`app/scale/formats`), legacy Office files through LibreOffice (`app/scale/legacy_office`) and text / code of any
kind (`app/scale/readers`).

These are the same readers the connector pipeline uses, so a file reads identically whether it arrives through an
upload or through a source. Scanned pages go through `SCALE_DOCPARSE_MODE`: `scanned` (the default) reads them locally,
`off` sends each page to the vision model instead (one model call per page, needed for drawings whose diagrams should be
captioned and searched visually).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.upload_validation import IMAGE_TYPES
from app.scale import readers
from app.scale.formats import UnreadableFile, labeled_rows
from app.scale.settings import get_scale_settings


class CannotRead(Exception):
    """The file is valid but nothing could be read from it. The message is shown to the administrator."""


def docparse_ready() -> bool:
    """Local OCR is switched on and its packages are installed."""
    if get_scale_settings().docparse_mode == "off":
        return False
    from app.scale import docparse

    return docparse.available()


def accepted_types() -> dict[str, str]:
    """extension -> doc_type stored on the document row (the types the original ingesters know keep their own name)."""
    out = {t: t for t in ("pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer", "py", "md")}
    out.update({t: "other" for t in readers.OTHER_TYPES})
    if docparse_ready():
        out.update({t: "other" for t in IMAGE_TYPES})
    return out


def office_blocks(path: str) -> list[dict]:
    """Blocks for any accepted non-PDF type that the original ingesters do not own (see `readers.blocks_for`)."""
    ext = Path(path).suffix.lstrip(".").lower()
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            blocks = readers.blocks_for(path, ext, tmp_dir)
        except UnreadableFile as exc:
            raise CannotRead(f"The file could not be read: {exc}") from exc
    if not blocks:
        raise CannotRead("The file has no readable text")
    return labeled_rows(blocks)


def scanned_pages(path: str, kind: str) -> list[dict]:
    """OCR + layout + tables for a PDF without a text layer (kind 'pdf') or an image (kind 'image').
    -> [{"page_number": 1, "text": ...}, ...]"""
    from app.scale import docparse

    cfg = get_scale_settings()
    opts = docparse.ParseOptions.from_settings(cfg)
    try:
        doc = docparse.parse_image(path, opts, threads=cfg.docparse_threads) if kind == "image" \
            else docparse.parse_pdf(path, opts, threads=cfg.docparse_threads)
    except docparse.ParseError as exc:
        raise CannotRead(f"The file could not be read: {exc}") from exc
    texts = doc.texts(cfg.docparse_table_style)
    if not any(t.strip() for t in texts):
        raise CannotRead("No text was found on the pages (blank, or too faint to read)")
    return [{"page_number": i + 1, "text": t} for i, t in enumerate(texts)]


def image_preview_pdf(path: str) -> bytes:
    from app.scale import docparse

    return docparse.image_to_pdf(path)
