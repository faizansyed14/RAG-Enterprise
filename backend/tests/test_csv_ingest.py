from app.ingestion.csv_ingest import extract_blocks


def _make_csv(tmp_path, text):
    path = str(tmp_path / "test.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    return path


def test_extract_blocks_comma(tmp_path):
    path = _make_csv(tmp_path, "Drawing No,Revision\nA-108,A\nA-109,B\n")
    blocks = extract_blocks(path)

    assert [b["type"] for b in blocks] == ["heading", "table"]
    assert blocks[0]["text"] == "test"
    assert blocks[1]["rows"] == [["Drawing No", "Revision"], ["A-108", "A"], ["A-109", "B"]]


def test_extract_blocks_semicolon_delimiter(tmp_path):
    path = _make_csv(tmp_path, "Drawing No;Revision\nA-108;A\n")
    blocks = extract_blocks(path)

    assert blocks[1]["rows"] == [["Drawing No", "Revision"], ["A-108", "A"]]


def test_extract_blocks_empty_file(tmp_path):
    path = _make_csv(tmp_path, "")
    assert extract_blocks(path) == []


def test_large_csv_splits_into_headed_chunks_not_one_table(tmp_path):
    """Regression: live-verified failure. A 144-row register rendered as one
    heading + one giant table hit 12 pages with zero real structure and was
    rejected outright by flash mode's flat-tree cap. Large CSVs must split
    into multiple heading blocks -- and (a second live-verified failure) as
    paragraph lines, not per-chunk tables, since a ReportLab Table flowable
    defeated the tree engine's heading detector even with headings between chunks."""
    rows = "\n".join(f"A-{i:03d},A,SHEET {i}" for i in range(1, 101))
    path = _make_csv(tmp_path, f"Drawing No,Revision,Title\n{rows}\n")

    blocks = extract_blocks(path)

    headings = [b for b in blocks if b["type"] == "heading"]
    assert len(headings) > 1, "a large CSV must produce more than one heading block"
    assert all(b["type"] != "table" for b in blocks), "chunks must not use the Table flowable"
    assert headings[0]["text"] == "test - Rows 1-25"
    # every data row survives, each labeled with its column header
    assert any(b.get("text") == "Drawing No: A-001 | Revision: A | Title: SHEET 1" for b in blocks)
    assert any(b.get("text") == "Drawing No: A-100 | Revision: A | Title: SHEET 100" for b in blocks)


def test_small_csv_below_chunk_threshold_is_unaffected(tmp_path):
    """A CSV with only a handful of rows should keep the original single
    heading + table shape -- chunking only kicks in once it's actually large
    enough to need it."""
    path = _make_csv(tmp_path, "Drawing No,Revision\nA-108,A\nA-109,B\n")
    blocks = extract_blocks(path)

    assert [b["type"] for b in blocks] == ["heading", "table"]
    assert blocks[0]["text"] == "test"
