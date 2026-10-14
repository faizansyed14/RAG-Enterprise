"""
PDF classification. A PDF with a real text layer submits straight to the
tree engine. A scanned or drawing-heavy PDF has no text layer for the
engine's local mode to read at all (it refuses a PDF whose pages are all
blank), so it can't be submitted directly -- ingestion/router.py runs the
diagram pipeline first (our own pinned vision model OCRs every page) and
only then builds a tree, from that OCR text, via
ingestion/text_to_pdf.render_ocr_pages_as_pdf + app.rag_service.submit_pdf.
"""

import re

from pypdf import PdfReader

# Word-like tokens, not raw characters. Confirmed live: a design-heavy resume's page
# had 192-139 raw characters per page -- comfortably over a character threshold -- but
# almost all of it was decorative-element fragments (stray colons, blank lines), only
# ~20 real word-like tokens on the "better" page and 0 on the other. Raw character
# count doesn't distinguish real prose from that kind of noise; word-like token count
# does.
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_MIN_WORDS_PER_PAGE = 40


def has_text_layer(file_path: str) -> bool:
    """Per-page, not averaged, and counting real words, not raw characters. A
    document averaging enough characters per page can still be almost entirely
    unusable if that's one text-bearing page masking several blank ones (or, as
    above, decorative noise rather than prose) -- requiring a majority of sampled
    pages to individually clear a word-count floor catches both, while still
    tolerating a normal document's occasional blank cover/divider page (which a
    strict "every page must pass" rule would not)."""
    reader = PdfReader(file_path)
    if not reader.pages:
        return False
    sampled = reader.pages[: min(5, len(reader.pages))]
    passing = sum(1 for p in sampled
                  if len(_WORD_RE.findall(p.extract_text() or "")) >= _MIN_WORDS_PER_PAGE)
    return passing > len(sampled) / 2


def page_count(file_path: str) -> int:
    return len(PdfReader(file_path).pages)


def classify_pdf(file_path: str) -> dict:
    scanned = not has_text_layer(file_path)
    return {"is_scanned": scanned, "page_count": page_count(file_path)}
