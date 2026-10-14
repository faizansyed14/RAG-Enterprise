"""
JSON ingestion. The tree engine's local mode only accepts PDF input (see
app/rag_service.py's module docstring), so this pretty-prints the parsed
document into preformatted "code" block(s) (preserving structure -- see
text_to_pdf.py) and renders them through the shared block pipeline
(blocks_ingest.py). Large files are split into chunks by chunking.py -- see
its module docstring for why (generic by line count rather than JSON
structure, since that varies too much -- array, deeply nested object, ... --
to chunk semantically for every shape).
"""

import json
from pathlib import Path

from app.ingestion.chunking import chunk_as_code_blocks


def extract_blocks(file_path: str) -> list[dict]:
    raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        text = raw  # not valid JSON -- render the raw text rather than failing

    text = text.strip()
    if not text:
        return []
    return chunk_as_code_blocks(Path(file_path).stem, text)


def parse_json(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
