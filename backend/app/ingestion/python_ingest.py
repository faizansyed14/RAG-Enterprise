"""
Python source ingestion. The tree engine's local mode only accepts PDF input
(see app/rag_service.py's module docstring), so this renders the file as
preformatted "code" block(s) (preserving indentation -- essential for
Python) and renders them through the shared block pipeline
(blocks_ingest.py). Large files are split into chunks by chunking.py -- same
large-file safeguard as json_ingest.py, since an unbroken code blob has the
same flat-tree rejection risk a large JSON export does.
"""

from pathlib import Path

from app.ingestion.chunking import chunk_as_code_blocks


def extract_blocks(file_path: str) -> list[dict]:
    text = Path(file_path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    return chunk_as_code_blocks(Path(file_path).stem, text)


def parse_python(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
