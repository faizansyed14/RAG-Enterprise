"""Shared large-file chunking for ingestion modules that would otherwise render a
whole file as one preformatted "code" block (JSON, Python, and markdown files with
no real headings of their own). A big blob under a single heading gives the tree engine
nothing to detect a structure from -- confirmed live: a 107KB/3559-line JSON export
rendered to 52 pages with one heading for the whole document, so flash mode fell
back to "one node per page" and was rejected outright (FLAT_TREE_MAX_NODES=10 in
rag_core/flash/api.py). Splitting into fixed-size line chunks, each under its own
heading, gives real section breaks to detect instead.
"""

# Lines per section once a file is split into chunks.
LINES_PER_CHUNK = 200


def chunk_as_code_blocks(stem: str, text: str, lines_per_chunk: int = LINES_PER_CHUNK) -> list[dict]:
    """`text` rendered as one heading + one code block if it's small, or split into
    `{"type": "heading", ...}, {"type": "code", ...}` pairs per chunk if it's large
    enough that a single block would risk the flat-tree rejection above."""
    lines = text.splitlines()
    if len(lines) <= lines_per_chunk:
        return [{"type": "heading", "text": stem}, {"type": "code", "text": text}]

    blocks: list[dict] = []
    total_parts = (len(lines) + lines_per_chunk - 1) // lines_per_chunk
    for part, start in enumerate(range(0, len(lines), lines_per_chunk), start=1):
        chunk_lines = lines[start:start + lines_per_chunk]
        end = start + len(chunk_lines)
        blocks.append({"type": "heading",
                       "text": f"{stem} - Part {part}/{total_parts} (lines {start + 1}-{end})"})
        blocks.append({"type": "code", "text": "\n".join(chunk_lines)})
    return blocks
