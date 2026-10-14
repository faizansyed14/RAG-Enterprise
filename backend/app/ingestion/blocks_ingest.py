"""
Shared submit path for every format that reduces to the block-rendering
pipeline (text_to_pdf.py) -- DOCX, CSV, XLSX, and EML all become a list of
heading/paragraph/table blocks, get rendered to one PDF, and submitted to
the tree engine the same way a real PDF would be. Each format module only
needs its own extract_blocks(); this handles render + submit + preview
persistence identically for all of them.
"""

import tempfile
from pathlib import Path

from app.core.object_store import get_object_store
from app.ingestion.text_to_pdf import render_blocks_as_pdf
from app.rag_service import submit_pdf


def submit_blocks(blocks: list[dict], file_path: str, document_id) -> dict:
    # A sibling directory so the rendered PDF can be named after the source
    # file's stem -- the tree engine names a document after whatever
    # basename it's given (rag_core/local_api.py), and a random tempfile
    # name would otherwise surface as the citation's document name.
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = str(Path(tmp_dir) / f"{Path(file_path).stem}.pdf")
        render_blocks_as_pdf(blocks, pdf_path)
        result = submit_pdf(pdf_path, mode="flash")

        # Persisted so /preview can show exactly what was chunked -- the
        # source format can't be rendered by the frontend's PDF viewer, but
        # this can.
        preview_storage_key = f"previews/{document_id}/rendered.pdf"
        get_object_store().put_object(preview_storage_key, Path(pdf_path).read_bytes(), "application/pdf")

    return {"rag_doc_id": result["doc_id"], "is_scanned": False, "preview_storage_key": preview_storage_key}
