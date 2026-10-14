"""The connector path reads every kind of file the upload path does, and says why when it cannot."""

import subprocess

import pytest

from app.scale import legacy_office, pipeline


def _pages(tmp_path, name, body: bytes):
    path = tmp_path / name
    path.write_bytes(body)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return pipeline.extract_pages(str(path), name, "other", str(work))


@pytest.mark.parametrize("name,body,needle", [
    ("deploy.log", b"2026-01-02 ERROR gateway timeout order 88123", "88123"),
    ("conf.yaml", b"retention_days: 2555\nowner: legal", "2555"),
    ("report.rpt", b"unregistered extension, plain text, vendor ZX-4471", "ZX-4471"),
    ("README", b"no extension at all but readable: code word HELIOTROPE", "HELIOTROPE"),
    ("memo.rtf", rb"{\rtf1\ansi Board approved the merger on Friday.\par}", "merger"),
    ("page.html", b"<html><body><p>Refund window is 14 days.</p></body></html>", "14 days"),
])
def test_text_and_web_files_are_read(tmp_path, name, body, needle):
    pages, preview, needs_ocr = _pages(tmp_path, name, body)
    assert needle in " ".join(pages) and preview.startswith(b"%PDF") and not needs_ocr


def test_a_binary_file_is_skipped_with_a_reason(tmp_path):
    with pytest.raises(pipeline.Skipped, match="unsupported_type"):
        _pages(tmp_path, "blob.dat", bytes(range(256)) * 20)


@pytest.mark.skipif(not legacy_office.available(), reason="LibreOffice not installed")
def test_a_legacy_word_file_is_read_by_the_connector_path(tmp_path):
    import docx

    d = docx.Document()
    d.add_paragraph("Termination requires ninety days written notice.")
    d.save(tmp_path / "c.docx")
    subprocess.run([legacy_office.soffice_path(), f"-env:UserInstallation=file://{tmp_path}/p", "--headless",
                    "--convert-to", "doc", "--outdir", str(tmp_path / "old"), str(tmp_path / "c.docx")],
                   check=True, capture_output=True, timeout=180)
    pages, preview, _ = _pages(tmp_path, "c.doc", (tmp_path / "old" / "c.doc").read_bytes())
    assert "ninety days" in " ".join(pages)


def test_a_missing_converter_is_a_skip_with_a_reason_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy_office, "soffice_path", lambda: None)
    with pytest.raises(pipeline.Skipped, match="LibreOffice"):
        _pages(tmp_path, "old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
