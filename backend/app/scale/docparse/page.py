"""One page image (+ optional text layer) -> blocks in reading order.

The steps, in the order a person would take them:
  1. get text lines with positions: the PDF's own text layer when it has one, else OCR (then put missing spaces back);
  2. find the layout regions (title, text, table, figure, caption, header, footer ...);
  3. give every line to the region it lies in; lines outside every region are grouped into paragraphs of their own;
  4. turn regions into blocks: tables through the structure model (geometric fallback if it loses text), text
     regions into paragraphs, titles into headings, figures into a short figure block;
  5. order everything with the XY-cut.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from dataclasses import dataclass

import numpy as np

from . import tables as tbl
from .deskew import deskew
from .engines import Engines
from .geometry import area, covered, height, iou
from .order import reading_order
from .paragraphs import merge_lines, order_lines, paragraph_box, paragraph_text, join_text
from .spaces import restore_spaces
from .types import Block, Line, PageResult, Region

log = logging.getLogger(__name__)

FURNITURE = {"header", "footer"}
_CAPTION = re.compile(r"^\s*(figure|fig\.|table|chart|diagram|exhibit|source)\s*\d*[.:)\-\u2013]", re.I)


@dataclass
class ParseOptions:
    ocr: str = "auto"                  # auto: the PDF's text layer where a page has one, OCR otherwise | always | never
    layout: bool = True
    tables: bool = True
    drop_furniture: bool = True
    restore_spaces: bool = True
    min_native_words: int = 5          # a page with fewer words in its text layer is treated as scanned
    table_style: str = "labeled"
    dpi: int = 150
    max_side: int = 2200               # longest edge of the rendered page, pixels
    max_pages: int = 300
    min_layout_score: float = 0.4
    deskew: bool = True                # straighten tilted scans before OCR (pages read from a text layer are never touched)

    @classmethod
    def from_settings(cls, cfg) -> "ParseOptions":
        return cls(
            ocr=cfg.docparse_ocr, layout=cfg.docparse_layout, tables=cfg.docparse_tables,
            drop_furniture=cfg.docparse_drop_furniture, restore_spaces=cfg.docparse_restore_spaces,
            table_style=cfg.docparse_table_style, dpi=cfg.docparse_dpi, max_pages=cfg.docparse_max_pages,
            deskew=cfg.docparse_deskew,
        )


def dedupe_regions(regions: list[Region], threshold: float = 0.7) -> list[Region]:
    """The same area is sometimes reported under two labels; keep the more confident one."""
    kept: list[Region] = []
    for r in sorted(regions, key=lambda r: r.score, reverse=True):
        if all(iou(r.box, k.box) < threshold for k in kept):
            kept.append(r)
    return kept


def assign_lines(lines: list[Line], regions: list[Region]) -> list[Line]:
    """Put each line into the region that contains it (tables and figures win over text regions around them).
    Returns the lines no region contains."""
    orphans: list[Line] = []
    for ln in lines:
        best, best_key = None, None
        for r in regions:
            share = covered(ln.box, r.box)
            if share < 0.5:
                continue
            key = (1 if r.label in ("table", "figure") else 0, share, -area(r.box))
            if best_key is None or key > best_key:
                best, best_key = r, key
        if best is None:
            orphans.append(ln)
        else:
            best.lines.append(ln)
    return orphans


def heading_level(line_height: float, body_height: float) -> int:
    ratio = line_height / body_height if body_height > 0 else 1.0
    return 1 if ratio >= 1.6 else 2 if ratio >= 1.25 else 3


class PageParser:
    def __init__(self, engines: Engines, opts: ParseOptions | None = None) -> None:
        self.engines = engines
        self.opts = opts or ParseOptions()

    # ------------------------------------------------------------------------------------------------ lines
    def _lines(self, image: np.ndarray, native: list[Line] | None, result: PageResult) -> tuple[list[Line], np.ndarray]:
        """-> (text lines, the image they are positioned in). A scan may come back straightened."""
        o = self.opts
        words = sum(len(ln.text.split()) for ln in native or [])
        if native and o.ocr != "always" and words >= o.min_native_words:
            return native, image
        if o.ocr == "never":
            return native or [], image
        if o.deskew:
            try:
                image, angle = deskew(image)
                if angle:
                    result.notes.append(f"deskew:{angle:.1f}")
            except Exception as exc:  # noqa: BLE001 - a failed straightening must not lose the page
                log.warning("deskew failed on page %s: %s", result.number, exc)
        lines = self.engines.ocr(image)
        result.used_ocr = True
        if o.restore_spaces:
            for ln in lines:
                ln.text = restore_spaces(image, ln)
        return lines, image

    # ----------------------------------------------------------------------------------------------- regions
    def _regions(self, image: np.ndarray, result: PageResult) -> list[Region]:
        if not self.opts.layout:
            return []
        try:
            regions = [r for r in self.engines.layout(image) if r.score >= self.opts.min_layout_score]
        except Exception as exc:  # noqa: BLE001 - never lose a page because the layout model failed
            log.warning("layout model failed on page %s: %s", result.number, exc)
            result.notes.append(f"layout_failed:{type(exc).__name__}")
            return []
        return dedupe_regions(regions)

    # ------------------------------------------------------------------------------------------------ tables
    def _table(self, image: np.ndarray, region: Region, page: int) -> Block | None:
        lines = region.lines
        if not lines:
            return None
        h, w = image.shape[:2]
        pad = 6
        x0, y0 = max(int(region.box[0]) - pad, 0), max(int(region.box[1]) - pad, 0)
        x1, y1 = min(int(region.box[2]) + pad, w), min(int(region.box[3]) + pad, h)
        lh = statistics.median([height(ln.box) for ln in lines]) or 10.0
        cells: list[Line] = []
        for ln in lines:
            cells.extend(tbl.split_native_cells(ln, 0.7 * lh) if ln.native else [ln])
        rel = [Line(box=(c.box[0] - x0, c.box[1] - y0, c.box[2] - x0, c.box[3] - y0), text=c.text, score=c.score) for c in cells]
        rows: list[list[str]] = []
        html = ""
        if self.opts.tables and x1 - x0 > 20 and y1 - y0 > 20:
            try:
                html = self.engines.table(image[y0:y1, x0:x1], rel) or ""
                rows = tbl.html_to_rows(html) if html else []
            except Exception as exc:  # noqa: BLE001
                log.warning("table model failed on page %s: %s", page, exc)
        if not rows or tbl.recovered_share(rows, cells) < 0.7:
            fallback = tbl.rows_from_lines(cells)
            if fallback and (not rows or tbl.recovered_share(fallback, cells) > tbl.recovered_share(rows, cells)):
                rows, html = fallback, ""
        if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
            return None                          # not really a table: the caller treats the lines as text
        return Block(type="table", rows=rows, html=html, box=region.box, page=page, source="table", score=region.score)

    # ------------------------------------------------------------------------------------------------ parse
    def parse(self, image: np.ndarray, number: int = 1, native: list[Line] | None = None) -> PageResult:
        t0 = time.perf_counter()
        h, w = image.shape[:2]
        result = PageResult(number=number, width=w, height=h)
        lines, image = self._lines(image, native, result)
        result.ocr_lines = len(lines)
        regions = self._regions(image, result)
        orphans = assign_lines(lines, regions)
        body = [height(ln.box) for ln in lines if height(ln.box) > 0]
        body_h = statistics.median(body) if body else 10.0

        # lines no region claimed: group them like a text region would
        for group in merge_lines(order_lines(orphans)):
            regions.append(Region(box=paragraph_box(group), label="text", score=1.0, lines=group))

        blocks: list[Block] = []
        for r in regions:
            if not r.lines and r.label != "figure":
                continue
            if r.label == "figure" and tbl.looks_tabular(r.lines):
                r.label = "table"                 # the layout model took a table for a picture: its words are in a grid
                result.notes.append("figure_read_as_table")
            if r.label == "table":
                tb = self._table(image, r, number)
                if tb is not None:
                    blocks.append(tb)
                    continue
                r.label = "text"                  # fall through: its lines are read as prose
            if r.label == "figure":
                text = join_text([ln.text for ln in order_lines(r.lines)])[:400]
                if text.strip():                  # a picture with no readable text adds nothing to search
                    blocks.append(Block(type="figure", text=text, box=r.box, page=number, source="figure", score=r.score))
                continue
            ordered = order_lines(r.lines)
            if r.label == "title":
                lh = statistics.median([height(ln.box) for ln in ordered])
                blocks.append(Block(
                    type="heading", text=join_text([ln.text for ln in ordered]), box=paragraph_box(ordered), page=number,
                    level=heading_level(lh, body_h), source="title", score=r.score,
                ))
                continue
            kind = {"reference": "reference", "equation": "equation", "figure_caption": "caption", "table_caption": "caption"}.get(r.label, "paragraph")
            if _CAPTION.match(join_text([ln.text for ln in ordered])):
                kind = "caption"                  # "Figure 3: ..." / "Table 2. ..." whatever the layout model called it
            for group in merge_lines(ordered):
                text = paragraph_text(group)
                if not text.strip():
                    continue
                lh = statistics.median([height(ln.box) for ln in group])
                one_line_heading = (
                    r.label == "text" and len(group) == 1 and lh >= 1.35 * body_h and len(text.split()) <= 14
                    and not text.rstrip().endswith((".", ",", ";"))
                )
                blocks.append(Block(
                    type="heading" if one_line_heading else kind, text=text, box=paragraph_box(group), page=number,
                    level=heading_level(lh, body_h) if one_line_heading else 0, source=r.label, score=r.score,
                ))
        gutter = max(12.0, 0.012 * w)
        result.blocks = reading_order(blocks, lambda b: b.box, min_gutter=gutter)
        result.seconds = time.perf_counter() - t0
        return result
