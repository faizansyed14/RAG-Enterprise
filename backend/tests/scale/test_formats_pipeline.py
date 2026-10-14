"""The built-in readers inside the real pipeline: scan -> worker -> pages -> index -> permission-checked search."""

import uuid

import pytest

from app.scale import search, sync, worker
from app.scale.db import engine
from sqlalchemy import text


def drain():
    assert worker.main(["--once", "--no-schedule", "--concurrency", "1"]) == 0


def states():
    with engine().connect() as conn:
        return {n: (s, e) for n, s, e in conn.execute(text("SELECT name, state, error FROM source_items")).all()}


def test_presentations_web_pages_rtf_and_long_spreadsheets_are_searchable(tmp_path, object_store):
    pptx = pytest.importorskip("pptx")
    openpyxl = pytest.importorskip("openpyxl")

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Harbour Expansion Plan"
    slide.placeholders[1].text = "Phase two dredging begins in the spring and is led by Orion Marine."
    (tmp_path / "board").mkdir()
    prs.save(tmp_path / "board" / "expansion.pptx")
    (tmp_path / "board" / "visitors.html").write_text(
        "<html><head><title>Visitor Policy</title></head><body><h1>Visitor Policy</h1><p>Guests must wear a green lanyard at the Sharjah depot.</p></body></html>")
    (tmp_path / "board" / "memo.rtf").write_text(r"{\rtf1\ansi Memo\par The Zephyr tender closes on Thursday.\par}")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Receivables"
    ws.append(["Invoice", "Customer", "Amount", "Status"])
    for i in range(1, 91):
        ws.append([f"INV-{i:04d}", f"Customer {i % 7}", i * 125, "overdue" if i == 70 else "paid"])
    wb.save(tmp_path / "board" / "ledger.xlsx")

    cid = sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": str(tmp_path), "acl_rules": [{"prefix": "board", "principals": ["group:board"]}]})
    sync.run_scan(cid)
    drain()
    st = states()
    assert {k: v[0] for k, v in st.items()} == {"expansion.pptx": "indexed", "visitors.html": "indexed", "memo.rtf": "indexed", "ledger.xlsx": "indexed"}, st

    def top(query, *principals):
        cands = search.candidate_documents(query, set(principals))
        return cands[0].name if cands else None

    assert top("who leads the dredging in phase two", "group:board") == "expansion.pptx"
    assert top("green lanyard Sharjah depot", "group:board") == "visitors.html"
    assert top("when does the Zephyr tender close", "group:board") == "memo.rtf"
    assert top("which invoice is overdue", "group:board") == "ledger.xlsx"
    assert top("green lanyard Sharjah depot", "group:other") is None                     # permissions apply to the new types too
    # a chunk from deep inside the long spreadsheet still says what its numbers are
    hits = search.search_chunks("INV-0070 overdue", principals={"group:board"}, limit=3, rerank=False)
    assert hits and "Invoice: INV-0070" in hits[0].text and "Status: overdue" in hits[0].text


def test_a_corrupt_office_file_is_recorded_as_skipped_not_lost(tmp_path, object_store):
    (tmp_path / "broken.pptx").write_text("not a zip at all")
    (tmp_path / "ok.txt").write_text("An ordinary text file about quarterly forecasts.")
    cid = sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": str(tmp_path), "acl_rules": [{"prefix": "", "principals": ["*"]}]})
    sync.run_scan(cid)
    drain()
    st = states()
    assert st["ok.txt"][0] == "indexed"
    assert st["broken.pptx"][0] == "skipped" and st["broken.pptx"][1].startswith("unreadable:")
