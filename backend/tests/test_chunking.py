from app.ingestion.chunking import LINES_PER_CHUNK, chunk_as_code_blocks


def test_text_at_or_under_threshold_is_one_heading_and_code_block():
    text = "\n".join(f"line {i}" for i in range(LINES_PER_CHUNK))
    blocks = chunk_as_code_blocks("doc", text)

    assert blocks == [{"type": "heading", "text": "doc"}, {"type": "code", "text": text}]


def test_text_over_threshold_splits_into_multiple_headed_chunks():
    text = "\n".join(f"line {i}" for i in range(LINES_PER_CHUNK + 50))
    blocks = chunk_as_code_blocks("doc", text)

    headings = [b for b in blocks if b["type"] == "heading"]
    codes = [b for b in blocks if b["type"] == "code"]
    assert len(headings) == 2 and len(codes) == 2
    assert headings[0]["text"] == f"doc - Part 1/2 (lines 1-{LINES_PER_CHUNK})"
    assert headings[1]["text"] == f"doc - Part 2/2 (lines {LINES_PER_CHUNK + 1}-{LINES_PER_CHUNK + 50})"
    # every line survives, in order, across the chunk boundary
    assert codes[0]["text"] + "\n" + codes[1]["text"] == text


def test_custom_chunk_size_is_respected():
    text = "\n".join(f"line {i}" for i in range(25))
    blocks = chunk_as_code_blocks("doc", text, lines_per_chunk=10)

    headings = [b for b in blocks if b["type"] == "heading"]
    assert len(headings) == 3  # 10 + 10 + 5
