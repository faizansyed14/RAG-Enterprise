"""
CSV ingestion. The tree engine's local mode only accepts PDF input (see
app/rag_service.py's module docstring), so this reads the sheet into grid
table block(s) and renders them through the shared block pipeline
(blocks_ingest.py), same as a DOCX table.

A large CSV rendered as one giant table under a single heading gives
the tree engine nothing to detect a structure from -- confirmed live: a 144-row
register rendered to 12 pages with only one heading for the whole document,
so flash mode fell back to "one node per page" and was rejected outright
(FLAT_TREE_MAX_NODES=10 in rag_core/flash/api.py). Splitting the data rows
into fixed-size chunks, each under its own heading, is necessary but not
sufficient -- also confirmed live: even with a heading per chunk, a
ReportLab Table flowable's grid/cell layout still defeated the tree engine's
heading detector every time (toc_source stayed "pages", still rejected,
regardless of chunk size). Flattening each chunk's rows to labeled text
lines instead of a real table fixed it (toc_source: "detected"). The agent
reads extracted text, not visual layout, so this costs nothing in practice
-- it only changes what the rendered PDF preview looks like for a large CSV.
"""

import csv
from pathlib import Path

# Rows per section once a CSV is split into chunks -- see module docstring.
_ROWS_PER_CHUNK = 25


def _sniff_dialect(sample: str) -> type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def extract_blocks(file_path: str) -> list[dict]:
    with open(file_path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = _sniff_dialect(sample)
        rows = [row for row in csv.reader(f, dialect) if any(cell.strip() for cell in row)]

    if not rows:
        return []
    stem = Path(file_path).stem
    header, data_rows = rows[0], rows[1:]
    chunks = [data_rows[i:i + _ROWS_PER_CHUNK] for i in range(0, len(data_rows), _ROWS_PER_CHUNK)]
    if len(chunks) <= 1:
        return [{"type": "heading", "text": stem}, {"type": "table", "rows": rows}]

    blocks: list[dict] = []
    start = 0
    for chunk in chunks:
        end = start + len(chunk)
        blocks.append({"type": "heading", "text": f"{stem} - Rows {start + 1}-{end}"})
        for row in chunk:
            blocks.append({"type": "paragraph",
                           "text": " | ".join(f"{h}: {v}" for h, v in zip(header, row))})
        start = end
    return blocks


def parse_csv(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
