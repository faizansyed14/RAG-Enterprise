"""
Plain-text ingestion. The tree engine's local mode only accepts PDF input
(see app/rag_service.py's module docstring), so this splits the file into
paragraph blocks (blank-line separated, same convention as an EML body --
see eml_ingest.py) and renders them through the shared block pipeline
(blocks_ingest.py).
"""

from pathlib import Path


def extract_blocks(file_path: str) -> list[dict]:
    text = Path(file_path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []

    blocks: list[dict] = [{"type": "heading", "text": Path(file_path).stem}]
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            blocks.append({"type": "paragraph", "text": para})
    return blocks


def parse_txt(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
