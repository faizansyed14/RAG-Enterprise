"""
EML ingestion. The tree engine's local mode only accepts PDF input (see
app/rag_service.py's module docstring), so this reads the message headers
into a table, the body into paragraph blocks, and any attachments into a
listing table (metadata only -- attachment content isn't extracted), and
renders them through the shared block pipeline (blocks_ingest.py).
"""

import re
from email import message_from_bytes, policy
from email.message import EmailMessage

_HEADERS = ("From", "To", "Cc", "Subject", "Date")


def _body_text(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    content = body.get_content()
    if body.get_content_type() == "text/html":
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"[ \t]+", " ", content).strip()
    return content


def extract_blocks(file_path: str) -> list[dict]:
    with open(file_path, "rb") as f:
        msg = message_from_bytes(f.read(), policy=policy.default)

    blocks: list[dict] = []

    header_rows = [[name, msg.get(name)] for name in _HEADERS if msg.get(name)]
    if header_rows:
        blocks.append({"type": "heading", "text": "Message"})
        blocks.append({"type": "table", "rows": header_rows})

    body_text = _body_text(msg)
    if body_text:
        blocks.append({"type": "heading", "text": "Body"})
        for para in body_text.split("\n\n"):
            para = para.strip()
            if para:
                blocks.append({"type": "paragraph", "text": para})

    attachments = list(msg.iter_attachments())
    if attachments:
        rows = [["Filename", "Content-Type", "Size (bytes)"]]
        for part in attachments:
            payload = part.get_payload(decode=True) or b""
            rows.append([part.get_filename() or "(unnamed)", part.get_content_type(), str(len(payload))])
        blocks.append({"type": "heading", "text": "Attachments"})
        blocks.append({"type": "table", "rows": rows})

    return blocks


def parse_eml(file_path: str, document_id) -> dict:
    from app.ingestion.blocks_ingest import submit_blocks

    blocks = extract_blocks(file_path)
    return submit_blocks(blocks, file_path, document_id)
