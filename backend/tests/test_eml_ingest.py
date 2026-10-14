from email.message import EmailMessage

from app.ingestion.eml_ingest import extract_blocks


def _make_eml(tmp_path, with_attachment=False):
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Drawing revision A-108"
    msg["Date"] = "Mon, 22 Sep 2026 10:00:00 +0000"
    msg.set_content("First paragraph.\n\nSecond paragraph.")
    if with_attachment:
        msg.add_attachment(b"dummy-bytes", maintype="application", subtype="pdf", filename="A-108.pdf")

    path = str(tmp_path / "test.eml")
    with open(path, "wb") as f:
        f.write(bytes(msg))
    return path


def test_extract_blocks_headers_and_body(tmp_path):
    path = _make_eml(tmp_path)
    blocks = extract_blocks(path)

    assert blocks[0] == {"type": "heading", "text": "Message"}
    header_rows = dict(blocks[1]["rows"])
    assert header_rows["Subject"] == "Drawing revision A-108"
    assert header_rows["From"] == "alice@example.com"

    assert blocks[2] == {"type": "heading", "text": "Body"}
    assert blocks[3]["text"] == "First paragraph."
    assert blocks[4]["text"] == "Second paragraph."


def test_extract_blocks_lists_attachments(tmp_path):
    path = _make_eml(tmp_path, with_attachment=True)
    blocks = extract_blocks(path)

    attachment_heading = next(b for b in blocks if b["type"] == "heading" and b["text"] == "Attachments")
    idx = blocks.index(attachment_heading)
    rows = blocks[idx + 1]["rows"]
    assert rows[0] == ["Filename", "Content-Type", "Size (bytes)"]
    assert rows[1][0] == "A-108.pdf"
