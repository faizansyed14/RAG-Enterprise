"""docparse: layout-aware document parsing with OCR, built for retrieval.

    from app.scale.docparse import parse_pdf, parse_image, ParseOptions
    doc = parse_pdf("scan.pdf", ParseOptions(ocr="auto"))
    pages = doc.texts()          # one string per page: headings, paragraphs, tables as self-describing rows

What it does for a page (see page.py): text from the PDF's text layer or from OCR (with word gaps restored), page
layout regions (title / text / table / figure / caption / header / footer), tables rebuilt as rows and columns,
header / footer / page-number removal across pages, paragraphs joined across lines, and reading order that follows
the columns. The models run on CPU through onnxruntime; install them with requirements-docparse.txt.
"""

from .document import IMAGE_EXTENSIONS, ParseError, image_to_pdf, parse_image, parse_pdf, remove_furniture
from .engines import DocparseUnavailable, Engines, available
from .page import PageParser, ParseOptions
from .render import page_text, table_text
from .types import Block, Line, PageResult, ParsedDocument, Region

__all__ = [
    "Block", "DocparseUnavailable", "Engines", "IMAGE_EXTENSIONS", "Line", "PageParser", "PageResult", "ParseError",
    "ParseOptions", "ParsedDocument", "Region", "available", "image_to_pdf", "page_text", "parse_image", "parse_pdf",
    "remove_furniture", "table_text",
]
