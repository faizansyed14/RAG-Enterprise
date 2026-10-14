from app.ingestion.txt_ingest import extract_blocks


def _make_txt(tmp_path, text):
    path = str(tmp_path / "notes.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_extract_blocks_splits_on_blank_lines(tmp_path):
    path = _make_txt(tmp_path, "First paragraph.\n\nSecond paragraph.\n\n\nThird paragraph.")
    blocks = extract_blocks(path)

    assert blocks[0] == {"type": "heading", "text": "notes"}
    assert [b["text"] for b in blocks[1:]] == ["First paragraph.", "Second paragraph.", "Third paragraph."]


def test_extract_blocks_empty_file(tmp_path):
    path = _make_txt(tmp_path, "   \n\n  ")
    assert extract_blocks(path) == []
