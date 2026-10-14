import fitz  # PyMuPDF, used only to synthesize test fixtures

from app.ingestion.pdf_ingest import classify_pdf, has_text_layer, page_count


def _make_pdf(tmp_path, text: str | None):
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    path = str(tmp_path / "test.pdf")
    doc.save(path)
    doc.close()
    return path


def test_has_text_layer_true_for_text_pdf(tmp_path):
    path = _make_pdf(tmp_path, "This drawing set contains general arrangement plans " * 6)
    assert has_text_layer(path) is True


def test_has_text_layer_false_for_blank_pdf(tmp_path):
    path = _make_pdf(tmp_path, None)
    assert has_text_layer(path) is False


def test_classify_pdf_scanned_flag(tmp_path):
    scanned_path = _make_pdf(tmp_path, None)
    assert classify_pdf(scanned_path)["is_scanned"] is True

    text_path = _make_pdf(tmp_path, "readable text " * 25)
    assert classify_pdf(text_path)["is_scanned"] is False


def test_page_count(tmp_path):
    path = _make_pdf(tmp_path, "one page")
    assert page_count(path) == 1


def _make_multi_page_pdf(tmp_path, texts: list[str | None]) -> str:
    doc = fitz.open()
    for text in texts:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    path = str(tmp_path / "multi.pdf")
    doc.save(path)
    doc.close()
    return path


def test_has_text_layer_false_when_a_short_text_page_masks_mostly_blank_pages(tmp_path):
    """A short real page followed by fully blank pages should not average out to
    "has a text layer" -- each page must clear the floor on its own merits."""
    path = _make_multi_page_pdf(tmp_path, [
        "Integrating data from various sources into a central data store. " * 2,
        None,
    ])
    assert has_text_layer(path) is False


def test_has_text_layer_false_for_punctuation_noise_that_inflates_char_count(tmp_path):
    """Regression: live-verified failure. A design-heavy resume's pages had 192 and
    139 raw characters -- comfortably over a plain character-count threshold -- but
    almost all of it was decorative-element fragments (stray colons, blank lines):
    ~20 real word-like tokens on the "better" page, 0 on the other. A raw character
    count doesn't distinguish that from real prose; counting word-like tokens does."""
    path = _make_multi_page_pdf(tmp_path, [
        "Integrating data from various sources. ".ljust(200, ":") ,
        ": \n" * 60,
    ])
    assert has_text_layer(path) is False


def test_has_text_layer_true_when_only_one_of_several_pages_is_blank(tmp_path):
    """A normal document's occasional blank cover/divider page should not force the
    whole document into the (expensive) OCR pipeline -- only a majority-blank
    document should."""
    path = _make_multi_page_pdf(tmp_path, [
        "General arrangement plan for level nine of the tower. " * 5,
        None,
        "Schedule of finishes and material callouts for this level. " * 5,
        "Structural notes and load assumptions for this revision. " * 5,
    ])
    assert has_text_layer(path) is True
