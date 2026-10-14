"""PDF / image files -> ParsedDocument.  Rendering, the PDF text layer, page loop and the cross-page clean-up."""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from typing import Callable

import numpy as np

from .engines import DocparseUnavailable, Engines
from .page import FURNITURE, PageParser, ParseOptions
from .types import Line, PageResult, ParsedDocument

log = logging.getLogger(__name__)

def _fitz():
    """PyMuPDF under its current module name, falling back to the older 'fitz'."""
    try:
        import pymupdf
        return pymupdf
    except ImportError:                                                       # pragma: no cover - old PyMuPDF
        import fitz
        return fitz


IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}


class ParseError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------------------------- rendering

def render_page(page, dpi: int, max_side: int) -> tuple[np.ndarray, float]:
    """The page as a BGR image and the zoom used (pixels per PDF point)."""
    fitz = _fitz()

    longest = max(page.rect.width, page.rect.height) or 1.0
    zoom = min(dpi / 72.0, max_side / longest)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        img = np.repeat(img, 3, axis=2)
    return np.ascontiguousarray(img[:, :, :3][:, :, ::-1]), zoom


def native_lines(page, zoom: float) -> list[Line]:
    """The text layer as positioned lines in image pixels (one Line per visual line, words kept)."""
    fitz = _fitz()

    try:
        words = page.get_text("words")
    except Exception:  # noqa: BLE001
        return []
    rot = page.rotation_matrix if page.rotation else fitz.Identity
    groups: dict[tuple[int, int], list[tuple[int, fitz.Rect, str]]] = {}
    for x0, y0, x1, y1, word, block, line, wno in words:
        if not word.strip():
            continue
        r = fitz.Rect(x0, y0, x1, y1) * rot * fitz.Matrix(zoom, zoom)
        groups.setdefault((block, line), []).append((wno, r, word))
    lines = []
    for items in groups.values():
        items.sort(key=lambda t: (t[1].x0, t[0]))
        ws = [(r.x0, r.x1, w) for _, r, w in items]
        box = (min(r.x0 for _, r, _ in items), min(r.y0 for _, r, _ in items), max(r.x1 for _, r, _ in items), max(r.y1 for _, r, _ in items))
        lines.append(Line(box=box, text=" ".join(w for _, _, w in ws), score=1.0, words=ws, native=True))
    return lines


# ------------------------------------------------------------------------------------------- cross-page clean-up

_PAGE_NUMBER = re.compile(r"^\W*(page\s*)?#+(\s*(of|/|\|)\s*#+)?\W*$|^\W*-\s*#+\s*-\W*$")


def _normal(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip(" .-|:–—")


_SENTENCE_BREAK = re.compile(r"[a-z0-9][.!?]\s+[A-Z]")


def _reads_like_prose(text: str) -> bool:
    """A running header is a label ("ACME Corp - Confidential"). Text that holds a sentence boundary, or is long, or is
    a clause ending in a full stop is the first lines of a paragraph that happens to sit at the top of a page."""
    words = text.split()
    return bool(_SENTENCE_BREAK.search(text)) or len(words) >= 12 or (len(words) >= 6 and text.rstrip().endswith((".", ";", ",")))


def remove_furniture(pages: list[PageResult], drop_labelled: bool = True) -> None:
    """Running headers, footers and page numbers: text in the top or bottom margin that repeats from page to page, or
    that the layout model called header / footer, or that is a page number. Removed from the blocks and recorded in
    `page.furniture`. A heading in the middle of a page is never touched."""
    n = len(pages)
    edge = []
    seen: Counter[str] = Counter()
    for page in pages:
        keys = []
        for b in page.blocks:
            cy = (b.box[1] + b.box[3]) / 2.0
            in_margin = cy < 0.12 * page.height or cy > 0.88 * page.height
            text = b.text if b.type != "table" else ""
            short = len(text.split()) <= 14
            keys.append((b, _normal(text) if (in_margin and short and text) else None))
        edge.append(keys)
        for key in {k for _, k in keys if k}:
            seen[key] += 1
    repeat_min = max(2, math.ceil(0.5 * n)) if n >= 2 else 99
    for page, keys in zip(pages, edge):
        kept = []
        for b, key in keys:
            drop = False
            if key:
                drop = (bool(_PAGE_NUMBER.match(key)) or seen[key] >= repeat_min
                        or (drop_labelled and b.source in FURNITURE and not _reads_like_prose(b.text)))
            if drop:
                page.furniture.append(b.text)
            else:
                kept.append(b)
        page.blocks = kept


# ------------------------------------------------------------------------------------------------ entry points

def _engines(opts: ParseOptions, engines: Engines | None, threads: int) -> Engines:
    return engines or Engines.default(threads=threads)


def parse_pdf(
    path: str, opts: ParseOptions | None = None, engines: Engines | None = None, *, threads: int = 2,
    on_page: Callable[[PageResult], None] | None = None,
) -> ParsedDocument:
    try:
        fitz = _fitz()
    except ImportError as exc:                                                # pragma: no cover - environment
        raise DocparseUnavailable("pymupdf is not installed") from exc
    opts = opts or ParseOptions()
    parser = PageParser(_engines(opts, engines, threads), opts)
    t0 = time.perf_counter()
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"cannot open pdf: {exc}") from exc
    if doc.needs_pass:
        raise ParseError("pdf is password protected")
    pages: list[PageResult] = []
    try:
        for i in range(min(len(doc), opts.max_pages)):
            page = doc[i]
            image, zoom = render_page(page, opts.dpi, opts.max_side)
            native = None if opts.ocr == "always" else native_lines(page, zoom)
            result = parser.parse(image, i + 1, native)
            pages.append(result)
            if on_page:
                on_page(result)
    finally:
        doc.close()
    if opts.drop_furniture:
        remove_furniture(pages, drop_labelled=True)
    return ParsedDocument(pages=pages, seconds=time.perf_counter() - t0)


def _frames(path: str, max_frames: int):
    from PIL import Image, ImageOps, ImageSequence

    with Image.open(path) as im:
        for k, frame in enumerate(ImageSequence.Iterator(im)):
            if k >= max_frames:
                break
            frame = ImageOps.exif_transpose(frame).convert("RGB")
            yield frame.copy()


def parse_image(
    path: str, opts: ParseOptions | None = None, engines: Engines | None = None, *, threads: int = 2,
) -> ParsedDocument:
    """A scan or photo of a page (PNG / JPEG / TIFF, multi-page TIFF included)."""
    opts = opts or ParseOptions()
    parser = PageParser(_engines(opts, engines, threads), opts)
    t0 = time.perf_counter()
    pages = []
    try:
        for k, frame in enumerate(_frames(path, opts.max_pages)):
            scale = min(1.0, opts.max_side / max(frame.size))
            if scale < 1.0:
                frame = frame.resize((int(frame.width * scale), int(frame.height * scale)))
            arr = np.ascontiguousarray(np.asarray(frame)[:, :, ::-1])
            pages.append(parser.parse(arr, k + 1, None if opts.ocr != "never" else []))
    except DocparseUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"cannot read image: {exc}") from exc
    if opts.drop_furniture and len(pages) > 1:
        remove_furniture(pages, drop_labelled=True)
    return ParsedDocument(pages=pages, seconds=time.perf_counter() - t0)


def image_to_pdf(path: str, max_frames: int = 300) -> bytes:
    """A PDF with the image(s) as pages: what the viewer shows next to the extracted text."""
    import io

    fitz = _fitz()

    pdf = fitz.open()
    for frame in _frames(path, max_frames):
        buf = io.BytesIO()
        frame.save(buf, "PNG")
        page = pdf.new_page(width=frame.width * 72 / 150, height=frame.height * 72 / 150)
        page.insert_image(page.rect, stream=buf.getvalue())
    out = pdf.tobytes(deflate=True)
    pdf.close()
    return out
