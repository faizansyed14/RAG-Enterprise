from app.ingestion.python_ingest import extract_blocks


def _make_py(tmp_path, text):
    path = str(tmp_path / "script.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_extract_blocks_small_file_is_one_heading_and_code_block(tmp_path):
    path = _make_py(tmp_path, "def foo():\n    return 1\n")
    blocks = extract_blocks(path)

    assert blocks == [
        {"type": "heading", "text": "script"},
        {"type": "code", "text": "def foo():\n    return 1"},
    ]


def test_extract_blocks_preserves_indentation(tmp_path):
    path = _make_py(tmp_path, "class Foo:\n    def bar(self):\n        return 1\n")
    blocks = extract_blocks(path)

    assert "    def bar(self):\n        return 1" in blocks[1]["text"]


def test_extract_blocks_empty_file(tmp_path):
    path = _make_py(tmp_path, "")
    assert extract_blocks(path) == []


def test_large_python_file_splits_into_headed_chunks(tmp_path):
    """Regression: an unbroken code blob has the same flat-tree rejection
    risk a large JSON export does (see chunking.py) -- a large .py file must
    split into multiple heading blocks, not render as one giant code block."""
    text = "\n".join(f"def func_{i}():\n    return {i}\n" for i in range(100))
    path = _make_py(tmp_path, text)

    blocks = extract_blocks(path)

    headings = [b for b in blocks if b["type"] == "heading"]
    assert len(headings) > 1
    assert headings[0]["text"].startswith("script - Part 1/")
    combined = "".join(b["text"] for b in blocks if b["type"] == "code")
    assert "def func_0():" in combined
    assert "def func_99():" in combined
