"""Synthetic pages with known contents, for tests and for measuring the parser on YOUR install.

Real scans have no answer key. These pages do: a PDF is drawn from text we choose, at positions we choose, in the
layouts that break naive extraction (two columns, running header and footer, tables, captions, figures), and the
ground truth (reading order, table cells, furniture) comes back with it. `degrade` then turns a clean page into a
"scan": rotation, blur, sensor noise, uneven light, JPEG compression, lower resolution.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass, field

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

W, H = A4
MARGIN = 50.0


@dataclass
class PageSpec:
    title: str | None = None
    columns: list[list[str]] = field(default_factory=lambda: [[]])      # one list of paragraphs per column
    table: list[list[str]] | None = None                                # first row is the header
    table_caption: str | None = None
    figure_caption: str | None = None
    header_left: str | None = None
    header_right: str | None = None
    footer: str | None = None                                           # may contain {n} and {total}
    font: str = "Times-Roman"
    size: float = 10.5
    # False: the content stream holds column 1, then column 2 (what a word processor writes). True: line by line across
    # the page (what "print to PDF", browsers and OCR'd scans write) -- a text extractor that trusts stream order then
    # reads straight across both columns.
    interleave: bool = False


@dataclass
class PageTruth:
    title: str | None
    paragraphs: list[str]            # in reading order: column 1 top to bottom, then column 2
    table: list[list[str]] | None
    captions: list[str]
    furniture: list[str]             # header / footer / page-number strings that must NOT appear in the body text


def _lines(text: str, font: str, size: float, width: float) -> list[str]:
    return simpleSplit(text, font, size, width)


def draw_page(c: canvas.Canvas, spec: PageSpec, n: int, total: int) -> PageTruth:
    c.setFont("Helvetica", 8)
    furniture: list[str] = []
    if spec.header_left:
        c.drawString(MARGIN, H - 32, spec.header_left)
        furniture.append(spec.header_left)
    if spec.header_right:
        c.drawRightString(W - MARGIN, H - 32, spec.header_right)
        furniture.append(spec.header_right)
    if spec.footer:
        footer = spec.footer.format(n=n, total=total)
        c.drawCentredString(W / 2, 28, footer)
        furniture.append(footer)
    y = H - 62
    if spec.title:
        c.setFont("Helvetica-Bold", 18)
        c.drawString(MARGIN, y - 18, spec.title)
        y -= 46
    top = y
    ncol = len(spec.columns)
    gutter = 26.0
    col_w = (W - 2 * MARGIN - gutter * (ncol - 1)) / ncol
    # the table and figure live below the text block, so the text may use only the space above them
    reserve = 0.0
    if spec.table:
        reserve += 22.0 * len(spec.table) + 36
    if spec.figure_caption:
        reserve += 170
    floor = 60 + reserve
    truth_pars: list[str] = []
    leading = spec.size * 1.3
    draws: list[tuple[float, float, str]] = []
    for ci, paragraphs in enumerate(spec.columns):
        x = MARGIN + ci * (col_w + gutter)
        yy = top
        for text in paragraphs:
            lines = _lines(text, spec.font, spec.size, col_w)
            if yy - leading * len(lines) < floor:
                break
            for ln in lines:
                yy -= leading
                draws.append((x, yy, ln))
            yy -= leading * 0.7
            truth_pars.append(text)
    if spec.interleave:
        draws.sort(key=lambda d: (-round(d[1]), d[0]))
    c.setFont(spec.font, spec.size)
    for x, yy, ln in draws:
        c.drawString(x, yy, ln)
    captions: list[str] = []
    ylow = floor + reserve
    cursor = ylow
    if spec.table:
        rows = spec.table
        ncols = max(len(r) for r in rows)
        cw = (W - 2 * MARGIN - 20) / ncols
        ty = cursor - 6
        c.setFont("Helvetica", 9.5)
        for r, row in enumerate(rows):
            for k in range(ncols):
                x0 = MARGIN + 10 + k * cw
                c.rect(x0, ty - 22 * (r + 1), cw, 22)
                cell = row[k] if k < len(row) else ""
                if r == 0:
                    c.setFont("Helvetica-Bold", 9.5)
                c.drawString(x0 + 5, ty - 22 * (r + 1) + 7, cell)
                if r == 0:
                    c.setFont("Helvetica", 9.5)
        cursor = ty - 22 * len(rows) - 6
        if spec.table_caption:
            c.setFont("Helvetica-Oblique", 9)
            c.drawString(MARGIN + 10, cursor - 10, spec.table_caption)
            captions.append(spec.table_caption)
        cursor -= 26
    if spec.figure_caption:
        c.setFillGray(0.82)
        c.rect(MARGIN + 60, cursor - 130, W - 2 * MARGIN - 120, 120, fill=1, stroke=0)
        c.setFillGray(0)
        c.setStrokeGray(0.4)
        for k in range(5):                                    # a bar chart, no text in it
            c.setFillGray(0.35)
            c.rect(MARGIN + 90 + k * 60, cursor - 125, 34, 20 + 17 * ((k * 7) % 5), fill=1, stroke=0)
        c.setFillGray(0)
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(MARGIN + 60, cursor - 144, spec.figure_caption)
        captions.append(spec.figure_caption)
    c.showPage()
    return PageTruth(spec.title, truth_pars, spec.table, captions, furniture)


def build_pdf(pages: list[PageSpec], path: str | None = None) -> tuple[bytes, list[PageTruth]]:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    truth = [draw_page(c, spec, i + 1, len(pages)) for i, spec in enumerate(pages)]
    c.save()
    data = buf.getvalue()
    if path:
        with open(path, "wb") as fh:
            fh.write(data)
    return data, truth


# ------------------------------------------------------------------------------------------------------ scanning

LEVELS = {
    # dpi of the scan, then the damage: rotation in degrees, blur radius, sensor noise (grey levels), JPEG quality, shading
    "clean": dict(dpi=200, rotate=0.0, blur=0.0, noise=0.0, jpeg=92, shade=0.0),       # a good flatbed scan
    "scan": dict(dpi=200, rotate=0.5, blur=0.5, noise=6.0, jpeg=60, shade=0.08),       # an ordinary office scan
    "poor": dict(dpi=150, rotate=-1.2, blur=0.9, noise=14.0, jpeg=40, shade=0.15),     # a bad photocopy / fax-like page
}


def degrade(img, level: str, seed: int = 0):
    """PIL image -> PIL image that looks scanned."""
    import numpy as np
    from PIL import Image, ImageFilter

    p = LEVELS[level]
    rng = np.random.default_rng(seed)
    im = img.convert("L")
    if p["rotate"]:
        im = im.rotate(p["rotate"], resample=Image.BICUBIC, expand=False, fillcolor=255)
    if p["blur"]:
        im = im.filter(ImageFilter.GaussianBlur(p["blur"]))
    arr = np.asarray(im, dtype=np.float32)
    if p["shade"]:                                            # uneven illumination: a soft gradient across the page
        yy, xx = np.mgrid[0: arr.shape[0], 0: arr.shape[1]]
        arr = arr * (1.0 - p["shade"] * (0.5 * xx / arr.shape[1] + 0.5 * yy / arr.shape[0]))
    if p["noise"]:
        arr = arr + rng.normal(0.0, p["noise"], arr.shape)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=p["jpeg"])
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def scan_pdf(pdf_bytes: bytes, level: str, seed: int = 0) -> bytes:
    """An image-only PDF (no text layer) made from `pdf_bytes` with the degradation `level`."""
    from PIL import Image

    from .document import _fitz

    fitz = _fitz()

    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    dpi = LEVELS[level]["dpi"]
    for i, page in enumerate(src):
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        im = degrade(im, level, seed + i)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=LEVELS[level]["jpeg"])
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    data = out.tobytes(deflate=True)
    out.close()
    src.close()
    return data


# ---------------------------------------------------------------------------------------------------- measuring

def _norm(text: str) -> list[str]:
    import re

    return re.findall(r"\w+", text.lower())


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def word_error_rate(truth: str, got: str) -> float:
    t, g = _norm(truth), _norm(got)
    return edit_distance(t, g) / max(len(t), 1)


def char_error_rate(truth: str, got: str) -> float:
    t, g = "".join(_norm(truth)), "".join(_norm(got))        # spaces ignored: space errors are measured by the WER
    return edit_distance(list(t), list(g)) / max(len(t), 1)


def order_accuracy(paragraphs: list[str], got: str, probe_words: int = 6, min_match: int = 3) -> float:
    """Of the adjacent pairs of true paragraphs, how many appear in the right order in `got`? Each paragraph is found
    by its first few words, tolerating recognition errors: the window of `got` that shares most of those words (at
    least `min_match`). A paragraph that cannot be found counts against both of its pairs."""
    from collections import Counter

    words = _norm(got)
    pos = []
    for p in paragraphs:
        probe = _norm(p)[:probe_words]
        want = Counter(probe)
        best, best_i = 0, -1
        for i in range(0, max(len(words) - len(probe) + 1, 1)):
            score = sum((Counter(words[i: i + len(probe)]) & want).values())
            if score > best:
                best, best_i = score, i
        pos.append(best_i if best >= min(min_match, len(probe)) else -1)
    if len(pos) < 2:
        return 1.0
    ok = sum(1 for a, b in zip(pos, pos[1:]) if a >= 0 and b >= 0 and a < b)
    return ok / (len(pos) - 1)


def table_cell_accuracy(truth: list[list[str]], got: list[list[str]] | None) -> float:
    """Share of true cells that sit at the same (row, column) in the parsed table."""
    if not truth:
        return 1.0
    if not got:
        return 0.0
    total = hit = 0
    for r, row in enumerate(truth):
        for k, cell in enumerate(row):
            total += 1
            if r < len(got) and k < len(got[r]) and _norm(got[r][k]) == _norm(cell):
                hit += 1
    return hit / max(total, 1)


def furniture_leak(furniture: list[str], got: str) -> float:
    """Share of header / footer strings that still appear in the text (0 is perfect)."""
    if not furniture:
        return 0.0
    text = " ".join(_norm(got))
    # a page number alone is a poor probe ("1" occurs everywhere): look for the whole normalised string
    leaked = sum(1 for f in furniture if " ".join(_norm(f)) and " ".join(_norm(f)) in text)
    return leaked / len(furniture)


def lorem(rng: random.Random, words: int, vocab: list[str]) -> str:
    out = [rng.choice(vocab) for _ in range(words)]
    out[0] = out[0].capitalize()
    return " ".join(out) + "."
