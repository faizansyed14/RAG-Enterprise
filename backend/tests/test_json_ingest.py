import json

from app.ingestion.json_ingest import extract_blocks


def _make_json(tmp_path, content):
    path = str(tmp_path / "data.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_extract_blocks_pretty_prints_valid_json(tmp_path):
    path = _make_json(tmp_path, json.dumps({"drawing_no": "A-108", "revision": "A"}))
    blocks = extract_blocks(path)

    assert blocks[0] == {"type": "heading", "text": "data"}
    assert blocks[1]["type"] == "code"
    assert json.loads(blocks[1]["text"]) == {"drawing_no": "A-108", "revision": "A"}
    assert "\n" in blocks[1]["text"]  # actually pretty-printed, not minified


def test_extract_blocks_falls_back_to_raw_text_on_invalid_json(tmp_path):
    path = _make_json(tmp_path, "{not valid json,,,")
    blocks = extract_blocks(path)

    assert blocks[1]["text"] == "{not valid json,,,"


def test_extract_blocks_empty_file(tmp_path):
    path = _make_json(tmp_path, "")
    assert extract_blocks(path) == []


def test_large_json_splits_into_headed_chunks(tmp_path):
    """Regression: live-verified failure. A 3559-line/107KB export rendered
    as one heading + one giant code blob hit 52 pages with zero real
    structure and was rejected outright by flash mode's flat-tree cap."""
    data = {"items": [{"id": i, "note": f"finding number {i} needs review"} for i in range(400)]}
    path = _make_json(tmp_path, json.dumps(data))

    blocks = extract_blocks(path)

    headings = [b for b in blocks if b["type"] == "heading"]
    assert len(headings) > 1, "a large JSON file must produce more than one heading block"
    assert headings[0]["text"].startswith("data - Part 1/")
    # the full content still round-trips across the chunk boundaries
    combined = "\n".join(b["text"] for b in blocks if b["type"] == "code")
    assert json.loads(combined) == data


def test_small_json_below_chunk_threshold_is_unaffected(tmp_path):
    """A JSON file small enough to not need splitting should keep the
    original single heading + code block shape."""
    path = _make_json(tmp_path, json.dumps({"drawing_no": "A-108", "revision": "A"}))
    blocks = extract_blocks(path)

    assert blocks[0] == {"type": "heading", "text": "data"}
    assert blocks[1]["type"] == "code"
