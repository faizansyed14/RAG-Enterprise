from app.ingestion.markdown_ingest import extract_blocks


def _make_md(tmp_path, text):
    path = str(tmp_path / "readme.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_extract_blocks_parses_real_headings(tmp_path):
    path = _make_md(tmp_path, "# Title\n\nIntro text.\n\n## Section One\n\nBody one.\n\n## Section Two\n\nBody two.\n")
    blocks = extract_blocks(path)

    headings = [b["text"] for b in blocks if b["type"] == "heading"]
    assert headings == ["readme", "Title", "Section One", "Section Two"]
    assert {"type": "paragraph", "text": "Body one."} in blocks
    assert {"type": "paragraph", "text": "Body two."} in blocks


def test_extract_blocks_preserves_fenced_code_and_ignores_hash_inside_it(tmp_path):
    path = _make_md(tmp_path, "# Title\n\n```python\n# not a heading\ndef f():\n    return 1\n```\n")
    blocks = extract_blocks(path)

    assert {"type": "heading", "text": "Title"} in blocks
    code_blocks = [b for b in blocks if b["type"] == "code"]
    assert len(code_blocks) == 1
    assert code_blocks[0]["text"] == "# not a heading\ndef f():\n    return 1"
    assert not any(b["type"] == "heading" and b["text"] == "not a heading" for b in blocks)


def test_extract_blocks_tolerates_unclosed_fence(tmp_path):
    path = _make_md(tmp_path, "# Title\n\n```\nunterminated code\n")
    blocks = extract_blocks(path)

    assert {"type": "code", "text": "unterminated code"} in blocks


def test_extract_blocks_empty_file(tmp_path):
    path = _make_md(tmp_path, "")
    assert extract_blocks(path) == []


def test_large_markdown_with_no_headings_falls_back_to_chunking(tmp_path):
    """Regression: a markdown file that's just a large prose blob with no
    real headings of its own has the same flat-tree rejection risk a large
    JSON export does (see chunking.py) -- it must fall back to fixed-size
    chunking rather than render as one giant unheaded block."""
    text = "\n".join(f"This is line {i} of plain prose with no headings." for i in range(300))
    path = _make_md(tmp_path, text)

    blocks = extract_blocks(path)

    headings = [b for b in blocks if b["type"] == "heading"]
    assert len(headings) > 1
    assert headings[0]["text"].startswith("readme - Part 1/")


def test_small_markdown_with_no_headings_keeps_plain_paragraph_shape(tmp_path):
    """Below the chunking threshold, a heading-less file just becomes the
    document-title heading plus its paragraphs -- no need to chunk."""
    path = _make_md(tmp_path, "Just a short note with no headings at all.")
    blocks = extract_blocks(path)

    assert blocks == [
        {"type": "heading", "text": "readme"},
        {"type": "paragraph", "text": "Just a short note with no headings at all."},
    ]
