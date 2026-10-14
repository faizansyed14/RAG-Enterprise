"""A file deleted from object storage must read as "deleted or not found", not as a link that answers 404 and crashes the viewer."""

import hashlib
import uuid

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.object_store import ObjectStore
from app.main import app
from app.models.db import Document, async_session
from app.scale.acl import grant_everyone
from tests.scale.helpers import doc, ingest


def client(headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


def _store_with(error):
    store = object.__new__(ObjectStore)
    store._bucket = "b"

    class _Client:
        def head_object(self, **_):
            if error is not None:
                raise error

    store._client = _Client()
    return store


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadObject")


def test_exists_is_false_only_when_the_store_says_the_object_is_not_there():
    assert _store_with(None).exists("k") is True
    assert _store_with(_client_error("404")).exists("k") is False
    assert _store_with(_client_error("NoSuchKey")).exists("k") is False
    assert _store_with(_client_error("403")).exists("k") is True               # cannot tell: do not claim "deleted"
    assert _store_with(EndpointConnectionError(endpoint_url="http://minio:9000")).exists("k") is True


async def test_a_file_still_there_gets_a_link(tmp_path, object_store, make_user):
    ingest(tmp_path, {"public/a.txt": "Plain text kept in object storage by default."})
    _, headers, _ = await make_user()
    async with client(headers) as c:
        body = (await c.get(f"/api/documents/{doc('a.txt')['document_id']}/preview")).json()
    assert body["url"] and not body.get("missing")


async def test_a_deleted_copy_is_reported_and_the_source_is_offered_instead(tmp_path, object_store, make_user):
    ingest(tmp_path, {"public/a.txt": "Plain text kept in object storage by default."})
    object_store.objects.clear()                                                # someone emptied the bucket
    _, headers, _ = await make_user()
    async with client(headers) as c:
        resp = await c.get(f"/api/documents/{doc('a.txt')['document_id']}/preview")
    body = resp.json()
    assert resp.status_code == 200 and body["url"] is None and body["missing"] is True
    assert "deleted" in body["message"] and body["source"]["available"] is True and body["source"]["path"].endswith("a.txt")


async def test_a_deleted_upload_with_no_source_just_says_so(object_store, make_user):
    document_id = uuid.uuid4()
    async with async_session() as session:
        session.add(Document(
            document_id=document_id, filename="upload.pdf", doc_type="pdf", status="indexed",
            content_hash=hashlib.sha256(str(document_id).encode()).hexdigest(), storage_key=f"originals/{document_id}/upload.pdf",
        ))
        await session.commit()
    grant_everyone(document_id)
    try:
        _, headers, _ = await make_user()
        async with client(headers) as c:
            body = (await c.get(f"/api/documents/{document_id}/preview")).json()
        assert body["url"] is None and body["missing"] is True and body["source"] is None and "deleted" in body["message"]
    finally:
        async with async_session() as session:
            row = await session.get(Document, document_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
