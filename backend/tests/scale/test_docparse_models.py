"""docparse on the REAL models (PP-OCR, PP-Layout, SLANet-plus), on pages whose contents are known, and through the
real ingest pipeline. Skipped when the model packages are not installed (pip install -r requirements-docparse.txt)."""

import uuid

import pytest

from app.scale import docparse

pytestmark = pytest.mark.skipif(not docparse.available(), reason="docparse model packages not installed")

from app.scale.docparse import synth as S  # noqa: E402

PARAS = [
    "The notice period for terminating the supplier agreement is ninety days written notice. Either party may "
    "terminate for convenience after the initial twelve month term has expired.",
    "Invoices are payable within thirty days of receipt. Late payments accrue interest at one and a half percent "
    "per month. Disputed amounts must be raised in writing within ten business days.",
    "The supplier shall maintain insurance covering public liability of not less than five million dirhams for the "
    "duration of this agreement and shall provide a certificate on request.",
    "Goods remain the property of the supplier until payment is received in full. Risk passes to the buyer on "
    "delivery to the warehouse in Jebel Ali.",
]
TABLE = [["Item", "Unit", "Qty", "Price"], ["Bearing", "pack", "419", "699.23"], ["Relay", "each", "282", "730.08"], ["Valve", "box", "107", "509.87"]]


def spec(n_pages=2, interleave=True):
    common = dict(header_left="ACME Corp - Confidential", header_right="Annual Report 2025", footer="Page {n} of {total}", interleave=interleave)
    pages = [S.PageSpec(title="Supplier Terms", columns=[PARAS[:2], PARAS[2:]], table=TABLE, table_caption="Table 1: Price list", **common)]
    for _ in range(n_pages - 1):
        pages.append(S.PageSpec(columns=[PARAS[:3]], figure_caption="Figure 1: Quarterly volume", **common))
    return pages


@pytest.fixture(scope="module")
def built():
    return S.build_pdf(spec())


def write(tmp_path, data, name="doc.pdf"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def metrics(doc, truth):
    body = lambda p: "\n".join(b.text for b in p.blocks if b.type != "table" and b.text)      # noqa: E731
    out = {"wer": [], "order": [], "leak": [], "cells": []}
    for page, t in zip(doc.pages, truth):
        expect = " ".join(([t.title] if t.title else []) + t.paragraphs + t.captions)
        out["wer"].append(S.word_error_rate(expect, body(page)))
        full = docparse.page_text(page)
        out["order"].append(S.order_accuracy(([t.title] if t.title else []) + t.paragraphs, full))
        out["leak"].append(S.furniture_leak(t.furniture, full))
        if t.table:
            tab = next((b.rows for b in page.blocks if b.type == "table"), None)
            out["cells"].append(S.table_cell_accuracy(t.table, tab))
    return {k: sum(v) / len(v) for k, v in out.items() if v}


def test_a_text_layer_pdf_keeps_columns_in_order_drops_running_text_and_rebuilds_the_table(built, tmp_path):
    data, truth = built
    doc = docparse.parse_pdf(write(tmp_path, data), docparse.ParseOptions(ocr="auto"))
    m = metrics(doc, truth)
    assert not any(p.used_ocr for p in doc.pages)                # the text layer was used, no OCR
    assert m["wer"] < 0.01 and m["order"] == 1.0 and m["leak"] == 0.0 and m["cells"] == 1.0, m
    text = "\n".join(doc.texts())
    assert "Item: Relay | Unit: each | Qty: 282 | Price: 730.08" in text
    assert "ACME Corp" not in text and "Page 2 of 2" not in text


def test_a_scanned_pdf_with_no_text_layer_is_read_by_ocr(built, tmp_path):
    data, truth = built
    scan = S.scan_pdf(data, "scan", seed=3)
    path = write(tmp_path, scan, "scan.pdf")
    assert all(not p.get_text() for p in __import__("pymupdf").open(path))       # really no text layer
    doc = docparse.parse_pdf(path, docparse.ParseOptions(ocr="auto"))
    assert all(p.used_ocr for p in doc.pages)
    m = metrics(doc, truth)
    assert m["wer"] < 0.12 and m["order"] >= 0.9 and m["leak"] == 0.0 and m["cells"] >= 0.8, m


def test_word_gap_restoration_is_what_makes_ocr_text_searchable_by_word(built, tmp_path):
    data, truth = built
    path = write(tmp_path, S.scan_pdf(data, "clean"), "clean.pdf")
    raw = docparse.parse_pdf(path, docparse.ParseOptions(ocr="always", restore_spaces=False))
    fixed = docparse.parse_pdf(path, docparse.ParseOptions(ocr="always", restore_spaces=True))
    assert metrics(fixed, truth)["wer"] < metrics(raw, truth)["wer"]
    assert metrics(fixed, truth)["wer"] < 0.05


def test_an_image_file_is_parsed_and_gets_a_pdf_preview(built, tmp_path):
    import pymupdf

    data, truth = built
    page = pymupdf.open(stream=S.scan_pdf(data, "clean"), filetype="pdf")[0]
    png = tmp_path / "page.png"
    png.write_bytes(page.get_pixmap(dpi=130).tobytes("png"))
    doc = docparse.parse_image(str(png), docparse.ParseOptions())
    assert len(doc.pages) == 1 and doc.pages[0].used_ocr
    assert "notice period" in doc.texts()[0].lower()
    preview = docparse.image_to_pdf(str(png))
    assert preview.startswith(b"%PDF") and len(pymupdf.open(stream=preview, filetype="pdf")) == 1


def test_a_multi_page_tiff_becomes_one_page_per_frame(built, tmp_path):
    from PIL import Image

    import pymupdf

    data, _ = built
    pdf = pymupdf.open(stream=S.scan_pdf(data, "clean"), filetype="pdf")
    frames = [Image.frombytes("RGB", (p.get_pixmap(dpi=100).width, p.get_pixmap(dpi=100).height), p.get_pixmap(dpi=100).samples) for p in pdf]
    path = tmp_path / "multi.tiff"
    frames[0].save(path, save_all=True, append_images=frames[1:])
    doc = docparse.parse_image(str(path), docparse.ParseOptions())
    assert len(doc.pages) == 2


def test_an_unreadable_file_raises_parse_error_not_a_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(docparse.ParseError):
        docparse.parse_pdf(str(bad))
    bad_img = tmp_path / "bad.png"
    bad_img.write_bytes(b"not an image")
    with pytest.raises(docparse.ParseError):
        docparse.parse_image(str(bad_img))


def test_a_blank_page_gives_no_blocks_and_no_error(tmp_path):
    import pymupdf

    pdf = pymupdf.open()
    pdf.new_page()
    doc = docparse.parse_pdf(write(tmp_path, pdf.tobytes()), docparse.ParseOptions(ocr="auto"))
    assert doc.pages[0].blocks == []


# ---------------------------------------------------------------------------------------- the ingest pipeline

def drain():
    from app.scale import worker

    assert worker.main(["--once", "--no-schedule", "--concurrency", "1"]) == 0


def items():
    from sqlalchemy import text

    from app.scale.db import engine

    with engine().connect() as conn:
        return {n: (s, e) for n, s, e in conn.execute(text("SELECT name, state, error FROM source_items")).all()}


def make_share(tmp_path, built):
    import pymupdf

    data, _ = built
    (tmp_path / "scans").mkdir()
    (tmp_path / "scans" / "terms_scan.pdf").write_bytes(S.scan_pdf(data, "scan", seed=5))
    (tmp_path / "scans" / "terms_text.pdf").write_bytes(data)
    page = pymupdf.open(stream=S.scan_pdf(data, "clean"), filetype="pdf")[0]
    (tmp_path / "scans" / "photo_of_page.png").write_bytes(page.get_pixmap(dpi=130).tobytes("png"))
    return tmp_path


def connect(tmp_path):
    from app.scale import sync

    return sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": str(tmp_path), "acl_rules": [{"prefix": "scans", "principals": ["group:ops"]}]})


def test_with_docparse_off_scanned_files_are_skipped_as_before(built, tmp_path, object_store, monkeypatch):
    from app.scale import sync
    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "off")
    get_scale_settings.cache_clear()
    share = make_share(tmp_path, built)
    sync.run_scan(connect(share))
    drain()
    st = items()
    assert st["terms_text.pdf"][0] == "indexed"
    assert st["terms_scan.pdf"][0] == "skipped" and "needs_ocr" in st["terms_scan.pdf"][1]


def test_with_docparse_scanned_the_scans_and_the_photo_are_indexed_and_found(built, tmp_path, object_store, monkeypatch):
    from app.scale import search, sync
    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "scanned")
    get_scale_settings.cache_clear()
    share = make_share(tmp_path, built)
    sync.run_scan(connect(share))
    drain()
    st = items()
    assert {k: v[0] for k, v in st.items()} == {"terms_scan.pdf": "indexed", "terms_text.pdf": "indexed", "photo_of_page.png": "indexed"}, st

    def top_names(query, *principals):
        return [c.name for c in search.candidate_documents(query, set(principals))]

    names = top_names("how much public liability insurance must the supplier maintain", "group:ops")
    assert {"terms_scan.pdf", "photo_of_page.png"} <= set(names)                       # the scan and the photo answer it
    assert "photo_of_page.png" in top_names("Jebel Ali warehouse delivery risk passes", "group:ops")
    assert top_names("Jebel Ali warehouse delivery risk passes", "group:other") == []
    hits = search.search_chunks("price of Relay", principals={"group:ops"}, limit=5, rerank=False)
    assert any("Item: Relay" in h.text and "730.08" in h.text for h in hits)           # table rows searched as rows
    # a header that repeats on the pages of a scan is removed; a single page cannot prove its first line is a running header, so it keeps it
    assert not [(h.name, h.text[:120]) for h in hits if "ACME Corp" in h.text and h.name == "terms_scan.pdf"]


def test_docparse_all_reads_text_pdfs_with_layout_too(built, tmp_path, object_store, monkeypatch):
    from app.scale import pipeline
    from app.scale.settings import get_scale_settings

    data, _ = built
    path = write(tmp_path, data)
    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "scanned")
    get_scale_settings.cache_clear()
    plain, _, ocr = pipeline.extract_pages(path, "doc.pdf", "pdf", str(tmp_path))
    assert not ocr and "ACME Corp - Confidential" in plain[0]                            # a text PDF keeps the old path
    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "all")
    get_scale_settings.cache_clear()
    laid_out, _, ocr = pipeline.extract_pages(path, "doc.pdf", "pdf", str(tmp_path))
    assert not ocr and "ACME Corp - Confidential" not in laid_out[0] and "Item: Relay" in laid_out[0]


def test_missing_packages_fall_back_to_the_old_behaviour_with_a_warning(tmp_path, monkeypatch, caplog):
    from app.scale import docparse as dp
    from app.scale import pipeline
    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "scanned")
    get_scale_settings.cache_clear()
    monkeypatch.setattr(dp, "available", lambda: False)
    assert pipeline._docparse_ready(get_scale_settings()) is False
    assert "not installed" in caplog.text
