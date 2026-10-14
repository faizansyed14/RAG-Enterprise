import os
import re

from app.ingestion.text_to_pdf import _fix_rtl_extraction, render_blocks_as_pdf, render_ocr_pages_as_pdf


def test_render_blocks_as_pdf_with_table_produces_valid_file(tmp_path):
    blocks = [
        {"type": "heading", "text": "Drawing Register"},
        {"type": "paragraph", "text": "Some intro text."},
        {"type": "table", "rows": [["No", "Rev"], ["A-108", "A"]]},
    ]
    out = str(tmp_path / "out.pdf")
    result = render_blocks_as_pdf(blocks, out)

    assert result == out
    assert os.path.getsize(out) > 0
    with open(out, "rb") as f:
        assert f.read(5) == b"%PDF-"


def test_render_blocks_as_pdf_empty_input_still_produces_pdf(tmp_path):
    out = str(tmp_path / "empty.pdf")
    render_blocks_as_pdf([], out)
    assert os.path.getsize(out) > 0


def test_fix_rtl_extraction_leaves_latin_and_digits_untouched():
    # No Arabic in the token -- returned as-is.
    assert _fix_rtl_extraction("B27 progress 100%") == "B27 progress 100%"


def test_render_blocks_as_pdf_arabic_text_survives_round_trip(tmp_path):
    # Regression test for a real bug: reportlab's default fonts can't encode
    # Arabic at all -- unfixed, this text would render as box-glyph filler
    # and be permanently unrecoverable once re-extracted for indexing (see
    # text_to_pdf.py's _fix_rtl_extraction docstring/comment). pypdfium2 is
    # the same library the tree engine's own flash-mode parser uses to pull
    # page text back out, so this checks against the real extraction path,
    # not just visual rendering.
    import pypdfium2 as pdfium

    original = "تم رفض الصب في الفيلا B27 بسبب فاصل صب بارد"
    out = str(tmp_path / "arabic.pdf")
    render_blocks_as_pdf([{"type": "paragraph", "text": original}], out)

    pdf = pdfium.PdfDocument(out)
    try:
        extracted = pdf[0].get_textpage().get_text_range()
    finally:
        pdf.close()

    assert "■" not in extracted  # no box-glyph data loss
    assert re.sub(r"\s+", " ", extracted).strip() == original


def test_render_ocr_pages_as_pdf_one_page_per_entry():
    import fitz

    pages = [
        {"page_number": 1, "text": "First page text"},
        {"page_number": 2, "text": "Second page text"},
    ]
    path = render_ocr_pages_as_pdf(pages)
    try:
        doc = fitz.open(path)
        try:
            assert doc.page_count == 2
            assert "First page text" in doc[0].get_text()
            assert "Second page text" in doc[1].get_text()
        finally:
            doc.close()  # Windows holds the file open otherwise; os.unlink below would fail
    finally:
        os.unlink(path)
