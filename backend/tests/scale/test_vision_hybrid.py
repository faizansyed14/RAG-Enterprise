"""A PDF is read by the ordinary local reader first; only the pages it cannot read (scans, photos, picture-only pages, CAD sheets drawn as lines)
go to the vision model. A page that already has its text is never sent, even with a picture on it. The model call itself is replaced here."""

from types import SimpleNamespace

import fitz
import numpy as np
import pytest

from app.scale import pipeline
from app.scale.settings import ScaleSettings

LONG_TEXT = "The supplier shall deliver the goods within thirty days of the purchase order and bear the cost of transport. " * 6


def _noise_png(width=600, height=800) -> bytes:
    rng = np.random.default_rng(1)
    pix = fitz.Pixmap(fitz.csGRAY, width, height, rng.integers(0, 255, width * height, dtype=np.uint8).tobytes(), False)
    return pix.tobytes("png")


def _mixed_pdf(tmp_path) -> str:
    doc = fitz.open()
    page = doc.new_page()                                   # 0: an ordinary text page
    page.insert_textbox(fitz.Rect(40, 40, 555, 800), LONG_TEXT, fontsize=10)
    page = doc.new_page()                                   # 1: a scan: one full-page picture, no text
    page.insert_image(page.rect, stream=_noise_png())
    page = doc.new_page()                                   # 2: a picture over most of the page with a short caption
    page.insert_image(fitz.Rect(30, 80, 565, 700), stream=_noise_png())
    page.insert_text((40, 40), "Floor plan, level 2", fontsize=12)
    page = doc.new_page()                                   # 3: a text page with a small figure on it: keeps its own text
    page.insert_textbox(fitz.Rect(40, 40, 555, 800), LONG_TEXT, fontsize=10)
    page.insert_image(fitz.Rect(450, 700, 550, 780), stream=_noise_png(100, 80))
    doc.new_page()                                          # 4: blank
    page = doc.new_page()                                   # 5: a drawing made of lines, no text
    for i in range(40):
        page.draw_line((50, 50 + i * 15), (500, 60 + i * 15))
    path = str(tmp_path / "mixed.pdf")
    doc.save(path)
    return path


def _cfg(**over) -> ScaleSettings:
    return ScaleSettings(docparse_mode="off", **over)


@pytest.fixture
def vision_on(monkeypatch):
    cfg = _cfg(ocr_enabled=True)
    monkeypatch.setattr(pipeline, "get_scale_settings", lambda: cfg)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(openrouter_api_key="test-key"))
    return cfg


def _native_pages(path: str) -> list[str]:
    return [page.get_text() for page in fitz.open(path)]


def test_only_the_pages_the_local_reader_cannot_read_are_picked(tmp_path):
    path = _mixed_pdf(tmp_path)
    assert pipeline.vision_pages(path, _native_pages(path), _cfg()) == [1, 2, 5]


def test_picked_pages_replace_their_text_and_the_others_keep_theirs(tmp_path, vision_on, monkeypatch):
    path = _mixed_pdf(tmp_path)
    asked = []
    monkeypatch.setattr(pipeline, "_vision_text", lambda p, idx: asked.append(list(idx)) or {i: f"VISION page {i}" for i in idx})
    pages, preview, needs_ocr = pipeline.extract_pages(path, "mixed.pdf", "pdf", str(tmp_path))
    assert asked == [[1, 2, 5]] and not needs_ocr and preview is None
    assert pages[1] == "VISION page 1" and pages[2] == "VISION page 2" and pages[5] == "VISION page 5"
    assert "thirty days" in pages[0] and "thirty days" in pages[3]        # text pages (the second with a small figure) untouched
    assert pages[4].strip() == ""                                          # a blank page stays blank, costs nothing


def test_at_most_vision_max_pages_are_read(tmp_path, monkeypatch):
    path = _mixed_pdf(tmp_path)
    cfg = _cfg(ocr_enabled=True, vision_max_pages=2)
    monkeypatch.setattr(pipeline, "get_scale_settings", lambda: cfg)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(openrouter_api_key="test-key"))
    asked = []
    monkeypatch.setattr(pipeline, "_vision_text", lambda p, idx: asked.append(list(idx)) or {i: "x" for i in idx})
    pipeline.extract_pages(path, "mixed.pdf", "pdf", str(tmp_path))
    assert asked == [[1, 2]]


def test_nothing_is_sent_when_vision_is_off_or_has_no_key(tmp_path, monkeypatch):
    path = _mixed_pdf(tmp_path)

    def boom(*a, **k):
        raise AssertionError("the vision model must not be called")

    monkeypatch.setattr(pipeline, "_vision_text", boom)
    for cfg, key in ((_cfg(ocr_enabled=False), "test-key"), (_cfg(ocr_enabled=True), "")):
        monkeypatch.setattr(pipeline, "get_scale_settings", lambda cfg=cfg: cfg)
        monkeypatch.setattr(pipeline, "get_settings", lambda key=key: SimpleNamespace(openrouter_api_key=key))
        pages, _, needs_ocr = pipeline.extract_pages(path, "mixed.pdf", "pdf", str(tmp_path))
        assert "thirty days" in pages[0] and not needs_ocr             # a mixed PDF is indexed from its text pages


def test_a_fully_scanned_pdf_is_still_skipped_without_vision(tmp_path, monkeypatch):
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=_noise_png())
    path = str(tmp_path / "scan.pdf")
    doc.save(path)
    cfg = _cfg(ocr_enabled=False)
    monkeypatch.setattr(pipeline, "get_scale_settings", lambda: cfg)
    assert pipeline.extract_pages(path, "scan.pdf", "pdf", str(tmp_path))[2] is True


def test_a_fully_scanned_pdf_is_read_when_vision_is_on(tmp_path, vision_on, monkeypatch):
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=_noise_png())
    path = str(tmp_path / "scan.pdf")
    doc.save(path)
    monkeypatch.setattr(pipeline, "_vision_text", lambda p, idx: {0: "NOTICE PERIOD: ninety days written notice " * 5})
    pages, _, needs_ocr = pipeline.extract_pages(path, "scan.pdf", "pdf", str(tmp_path))
    assert "ninety days" in pages[0] and not needs_ocr


def test_an_image_file_becomes_a_one_page_pdf_read_by_vision(tmp_path, vision_on, monkeypatch):
    image = tmp_path / "photo.png"
    image.write_bytes(_noise_png())
    monkeypatch.setattr(pipeline, "_vision_text", lambda p, idx: {0: "Invoices are payable within thirty days."})
    pages, preview, needs_ocr = pipeline.extract_pages(str(image), "photo.png", "image", str(tmp_path))
    assert pages == ["Invoices are payable within thirty days."] and preview.startswith(b"%PDF") and not needs_ocr


def test_an_image_the_model_finds_nothing_in_is_skipped(tmp_path, vision_on, monkeypatch):
    image = tmp_path / "blank.png"
    image.write_bytes(_noise_png())
    monkeypatch.setattr(pipeline, "_vision_text", lambda p, idx: {0: "  "})
    with pytest.raises(pipeline.Skipped, match="no_text_found_by_vision"):
        pipeline.extract_pages(str(image), "blank.png", "image", str(tmp_path))
