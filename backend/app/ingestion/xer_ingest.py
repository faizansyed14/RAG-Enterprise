"""
XER (Primavera P6 schedule export) ingestion. The tree engine's local mode
only accepts PDF input (see app/rag_service.py's module docstring), so this
parses XER's tab-delimited %T/%F/%R table sections (TASK, PROJWBS,
TASKPRED, RSRC, ...) into one heading + grid table block per table --
mirroring how a multi-sheet XLSX workbook is handled (xlsx_ingest.py) --
and renders them through the shared block pipeline (blocks_ingest.py).
"""

from pathlib import Path


def _normalize_row(row: list[str], width: int) -> list[str]:
    if len(row) == width:
        return row
    if len(row) < width:
        return row + [""] * (width - len(row))
    return row[:width]


def extract_blocks(file_path: str) -> list[dict]:
    text = Path(file_path).read_text(encoding="utf-8", errors="replace")

    blocks: list[dict] = []
    table_name: str | None = None
    fields: list[str] = []
    rows: list[list[str]] = []

    def flush() -> None:
        if not table_name or not rows:
            return
        data = [_normalize_row(r, len(fields)) for r in rows] if fields else rows
        blocks.append({"type": "heading", "text": table_name})
        blocks.append({"type": "table", "rows": ([fields] if fields else []) + data})

    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            flush()
            table_name = parts[1] if len(parts) > 1 else "Table"
            fields, rows = [], []
        elif tag == "%F":
            fields = parts[1:]
        elif tag == "%R":
            rows.append(parts[1:])
        elif tag == "%E":
            flush()
            table_name = None
        # ERMHDR and any other control tags carry no tabular data -- skipped

    flush()  # tolerate a file missing its trailing %E
    return blocks


def parse_xer(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
