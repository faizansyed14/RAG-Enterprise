"""GET /api/documents/{id}/preview -- real Postgres, real ASGI app (see
test_hardening.py's _client pattern), MinIO's presigned_url() is a pure local
signature computation so it works without the object actually existing.

Regression: a failed non-PDF upload has no preview_storage_key (blocks_ingest.py
only uploads the rendered preview PDF *after* submit_pdf succeeds -- see its
module docstring) and no valid PDF to fall back to either. The endpoint used to
fall back to storage_key (the raw, non-PDF original) unconditionally, handing
the frontend's PDF viewer a CSV/DOCX/JSON file and crashing it with
"InvalidPDFException: Invalid PDF structure" instead of a clear 404.
"""

import hashlib
import uuid

import httpx
import pytest

from app.main import app
from app.models.db import Document, async_session
from app.scale.acl import grant_everyone


def _client(headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {})


@pytest.fixture(autouse=True)
def object_is_present(monkeypatch):
    """These tests are about which key is previewed, not whether MinIO holds it: pretend the object exists."""
    from app.core.object_store import ObjectStore

    monkeypatch.setattr(ObjectStore, "exists", lambda self, key: True)


@pytest.fixture
async def make_doc():
    created: list[uuid.UUID] = []

    async def _make(doc_type: str, status: str, preview_storage_key: str | None) -> uuid.UUID:
        document_id = uuid.uuid4()
        async with async_session() as session:
            session.add(Document(
                document_id=document_id, filename=f"test.{doc_type}", doc_type=doc_type,
                content_hash=hashlib.sha256(str(document_id).encode()).hexdigest(),
                storage_key=f"originals/{document_id}/test.{doc_type}",
                preview_storage_key=preview_storage_key,
                status=status, error="boom" if status == "failed" else None,
            ))
            await session.commit()
        grant_everyone(document_id)
        created.append(document_id)
        return document_id

    yield _make

    async with async_session() as session:
        for document_id in created:
            doc = await session.get(Document, document_id)
            if doc is not None:
                await session.delete(doc)
        await session.commit()


async def test_failed_non_pdf_upload_returns_404_not_a_broken_preview(make_user, make_doc):
    _, headers, _ = await make_user(role="user")
    document_id = await make_doc(doc_type="csv", status="failed", preview_storage_key=None)

    async with _client(headers) as client:
        resp = await client.get(f"/api/documents/{document_id}/preview")

    assert resp.status_code == 404


async def test_failed_pdf_upload_still_falls_back_to_the_original(make_user, make_doc):
    """Unchanged behavior: a PDF's original file genuinely is a valid PDF,
    so falling back to storage_key when there's no rendered preview is
    correct for doc_type == "pdf" specifically (scanned or not)."""
    _, headers, _ = await make_user(role="user")
    document_id = await make_doc(doc_type="pdf", status="failed", preview_storage_key=None)

    async with _client(headers) as client:
        resp = await client.get(f"/api/documents/{document_id}/preview")

    assert resp.status_code == 200
    assert "url" in resp.json()


async def test_indexed_non_pdf_document_uses_its_rendered_preview(make_user, make_doc):
    _, headers, _ = await make_user(role="user")
    document_id = await make_doc(
        doc_type="csv", status="indexed", preview_storage_key=f"previews/{uuid.uuid4()}/rendered.pdf",
    )

    async with _client(headers) as client:
        resp = await client.get(f"/api/documents/{document_id}/preview")

    assert resp.status_code == 200
    assert "url" in resp.json()
