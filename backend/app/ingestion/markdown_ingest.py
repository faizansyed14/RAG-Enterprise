"""
Markdown ingestion. The tree engine's local mode only accepts PDF input (see
app/rag_service.py's module docstring). Unlike plain text/JSON/Python,
markdown has real heading syntax (# / ## / ...), so headings are parsed
directly into heading blocks instead of relying on fixed-size chunking --
gives the tree engine genuine section titles instead of generic "Part N" labels.
Fenced code blocks (```...```) are preserved as preformatted "code" blocks
so indentation/formatting inside them survives, and a "#" inside a fence is
never treated as a heading.

A markdown file with no real headings of its own is exactly the same flat
blob a large JSON/Python file would be, so it falls back to the same
fixed-size line chunking (chunking.py) rather than risk the flat-tree
rejection a large unheaded blob would hit (confirmed live for JSON/CSV --
see chunking.py's module docstring).
"""

import re
from pathlib import Path

from app.ingestion.chunking import LINES_PER_CHUNK, chunk_as_code_blocks

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^```")


def _parse_markdown(stem: str, text: str) -> list[dict]:
    blocks: list[dict] = [{"type": "heading", "text": stem}]
    in_fence = False
    fence_lines: list[str] = []
    para_lines: list[str] = []

    def flush_para() -> None:
        if para_lines:
            blocks.append({"type": "paragraph", "text": " ".join(para_lines).strip()})
            para_lines.clear()

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            if in_fence:
                blocks.append({"type": "code", "text": "\n".join(fence_lines)})
                fence_lines = []
            else:
                flush_para()
            in_fence = not in_fence
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_para()
            blocks.append({"type": "heading", "text": heading_match.group(2).strip()})
            continue
        if not line.strip():
            flush_para()
            continue
        para_lines.append(line.strip())

    flush_para()
    if in_fence and fence_lines:  # tolerate an unclosed fence
        blocks.append({"type": "code", "text": "\n".join(fence_lines)})
    return blocks


def extract_blocks(file_path: str) -> list[dict]:
    text = Path(file_path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    stem = Path(file_path).stem
    blocks = _parse_markdown(stem, text)

    real_headings = sum(1 for b in blocks if b["type"] == "heading") - 1  # minus the doc-title heading
    if real_headings < 1 and len(text.splitlines()) > LINES_PER_CHUNK:
        return chunk_as_code_blocks(stem, text)
    return blocks


def parse_markdown(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
