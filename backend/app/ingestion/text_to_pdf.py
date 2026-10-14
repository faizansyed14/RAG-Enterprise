"""
Renders a list of content blocks to a real PDF. The tree engine's local client
only accepts PDF input (see app/rag_service.py's module docstring), so
DOCX (headings, paragraphs, and *tables* -- kept as real grid tables, not
flattened text, so the tree/summary pass sees actual rows/columns)
and scanned-page OCR text both go through here before ever reaching
the tree engine.

Block shapes:
    {"type": "heading", "text": str}
    {"type": "paragraph", "text": str}
    {"type": "table", "rows": list[list[str]]}
    {"type": "code", "text": str}   -- preformatted/monospace, whitespace and
                                        newlines preserved (long lines are
                                        soft-wrapped); used for JSON/XER-style
                                        structured text where flowing it as
                                        prose would lose meaning.
    {"type": "page_break"}   -- hard break; used only for OCR page synthesis,
                                 where page N of the synthetic PDF must stay
                                 page N of the original scanned document.
"""

import re
import tempfile
import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

# reportlab's default fonts (Helvetica etc.) only cover Latin-1/WinAnsi --
# a codepoint they can't encode (e.g. Arabic) isn't skipped, it's silently
# replaced by a box glyph *in the PDF itself*, permanently destroying the
# original text before it's ever extracted for indexing. Noto Sans Arabic
# covers Latin + Arabic, so it's used for every block, not just Arabic ones.
_ARABIC_FONT_NAME = "NotoSansArabic"
pdfmetrics.registerFont(TTFont(_ARABIC_FONT_NAME, str(Path(__file__).parent / "fonts" / "NotoSansArabic.ttf")))

_styles = getSampleStyleSheet()
_heading_style = ParagraphStyle("Heading", parent=_styles["Heading1"], spaceAfter=10, fontName=_ARABIC_FONT_NAME)
_body_style = ParagraphStyle("Body", parent=_styles["BodyText"], fontName=_ARABIC_FONT_NAME)
_code_style = ParagraphStyle("Code", parent=_styles["Code"], fontSize=7.5, leading=9, fontName=_ARABIC_FONT_NAME)

_CODE_WRAP_WIDTH = 100

# rlbidi (reportlab's optional bidi engine) isn't installed, so reportlab
# draws each RTL run's glyphs in a fixed reversed order without knowing it
# -- confirmed empirically, not assumed: extracting text back out of a
# rendered PDF (pypdfium2, the tree engine's own extractor) returns each
# Arabic word's letters reversed while word order and embedded non-Arabic
# runs (digits, "B27"-style codes) stay correct. Pre-reversing each Arabic
# letter run here before rendering cancels that out, since the transform
# is its own inverse -- round-trip-verified character-for-character
# against pypdfium2 extraction, not just visually.
_ARABIC_LETTER_RUN_RE = re.compile(r"[ء-غف-يٮ-ۓەﭐ-﷿ﹰ-﻿]+")


def _fix_rtl_extraction(text: str) -> str:
    def fix_token(token: str) -> str:
        if not _ARABIC_LETTER_RUN_RE.search(token):
            return token
        return _ARABIC_LETTER_RUN_RE.sub(lambda m: m.group(0)[::-1], token)

    return " ".join(fix_token(tok) for tok in text.split(" "))


def _wrap_preformatted(text: str, width: int = _CODE_WRAP_WIDTH) -> str:
    """Preformatted has no auto word-wrap -- a long unwrapped line just runs
    off the page. Soft-wraps long lines while keeping indentation, so the
    text stays fully visible instead of being cut off at the page edge."""
    out: list[str] = []
    for line in text.splitlines():
        if len(line) <= width:
            out.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        out.extend(
            textwrap.wrap(
                line, width=width, initial_indent=indent, subsequent_indent=indent + "  ",
                break_long_words=True, break_on_hyphens=False,
            )
            or [line]
        )
    return "\n".join(out)

_TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
])


def _escape(text: str) -> str:
    text = _fix_rtl_extraction(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _flowable(block: dict) -> list:
    kind = block.get("type")
    if kind == "page_break":
        return [PageBreak()]
    if kind == "heading":
        return [Paragraph(_escape(block.get("text", "")), _heading_style)]
    if kind == "table":
        rows = block.get("rows") or []
        if not rows:
            return []
        wrapped = [[Paragraph(_escape(str(cell)), _body_style) for cell in row] for row in rows]
        table = Table(wrapped, repeatRows=1)
        table.setStyle(_TABLE_STYLE)
        return [table, Spacer(1, 10)]
    if kind == "code":
        text = (block.get("text") or "").strip()
        if not text:
            return []
        return [Preformatted(_wrap_preformatted(_fix_rtl_extraction(text)), _code_style), Spacer(1, 10)]
    # paragraph (default)
    text = (block.get("text") or "").strip()
    if not text:
        return [Spacer(1, 6)]
    return [Paragraph(_escape(text), _body_style)]


def render_blocks_as_pdf(blocks: list[dict], output_path: str | None = None) -> str:
    """Writes the PDF and returns its path (a fresh temp file if output_path is None)."""
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        output_path = tmp.name

    doc = SimpleDocTemplate(
        output_path,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    flowables: list = []
    for block in blocks:
        flowables.extend(_flowable(block))
    if not flowables:
        flowables = [Paragraph("(empty document)", _body_style)]

    doc.build(flowables)
    return output_path


def render_ocr_pages_as_pdf(pages: list[dict], output_path: str | None = None) -> str:
    """One synthetic PDF page per OCR'd page (each {"page_number", "text"}),
    a hard PageBreak between them so page N in the synthetic PDF is always
    page N of the original scanned document -- see the diagram_pipeline
    docstring for why this exists. Pass output_path to control the file's
    name (it becomes the tree engine's citation document name -- see
    ingestion/router.py's _named_pdf_path); omitted, a random temp path is used."""
    blocks: list[dict] = []
    for i, page in enumerate(sorted(pages, key=lambda p: p["page_number"])):
        if i > 0:
            blocks.append({"type": "page_break"})
        blocks.append({"type": "heading", "text": f"Page {page['page_number']}"})
        text = (page.get("text") or "").strip()
        blocks.append({"type": "paragraph", "text": text or "(no legible text on this page)"})
    return render_blocks_as_pdf(blocks, output_path)
