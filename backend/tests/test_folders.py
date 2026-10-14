"""Real integration tests against the live app + Postgres (no mocking --
same ASGITransport pattern as test_app_health.py). Auth: a throwaway admin
user is created per test (conftest's make_user) and its token is minted
directly via create_token(), rather than going through /api/auth/login.

Document fixtures are inserted directly into Postgres, not created via
POST /api/documents -- that endpoint schedules a real background
ingestion task (real LLM calls against the configured model), which
these tests have no reason to pay for; they're testing folder membership,
not ingestion. The upload endpoint's own folder_id form field is verified
separately, live, against the running dev stack.
"""

import hashlib
import uuid

import httpx
import pytest
import pytest_asyncio

from app.main import app
from app.models.db import Document, async_session
from app.scale.acl import grant_everyone


_ADMIN_HEADERS: dict = {}


@pytest_asyncio.fixture(autouse=True)
async def _admin_headers(make_user):
    _, headers, _ = await make_user(role="admin")
    _ADMIN_HEADERS.clear()
    _ADMIN_HEADERS.update(headers)
    yield


def _headers() -> dict:
    return dict(_ADMIN_HEADERS)


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", headers=_headers())


async def _make_document(name: str) -> str:
    document_id = uuid.uuid4()
    async with async_session() as session:
        session.add(Document(
            document_id=document_id,
            filename=name,
            doc_type="txt",
            content_hash=hashlib.sha256(f"{name}-{document_id}".encode()).hexdigest(),
            storage_key=f"originals/{document_id}/{name}",
            status="indexed",
        ))
        await session.commit()
    grant_everyone(document_id)
    return str(document_id)


@pytest.mark.asyncio
async def test_create_rename_delete_folder():
    async with _client() as client:
        created = await client.post("/api/folders", json={"name": "  Project Alpha  "})
        assert created.status_code == 200
        folder = created.json()
        assert folder["name"] == "Project Alpha"  # trimmed
        assert folder["document_count"] == 0
        folder_id = folder["folder_id"]

        listed = await client.get("/api/folders")
        assert any(f["folder_id"] == folder_id for f in listed.json())

        renamed = await client.patch(f"/api/folders/{folder_id}", json={"name": "Project Beta"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Project Beta"

        deleted = await client.delete(f"/api/folders/{folder_id}")
        assert deleted.status_code == 200

        listed_after = await client.get("/api/folders")
        assert all(f["folder_id"] != folder_id for f in listed_after.json())


@pytest.mark.asyncio
async def test_set_document_folders_move_copy_and_unfiled_after_folder_delete():
    document_id = await _make_document("movecopy.txt")
    async with _client() as client:
        folder_a = (await client.post("/api/folders", json={"name": "Folder A"})).json()
        folder_b = (await client.post("/api/folders", json={"name": "Folder B"})).json()

        # Move: doc starts unfiled, "move" into A.
        moved = await client.put(f"/api/documents/{document_id}/folders", json={"folder_ids": [folder_a["folder_id"]]})
        assert moved.status_code == 200
        assert moved.json()["folder_ids"] == [folder_a["folder_id"]]

        # Copy: A stays, B is added -- same document, both folders.
        copied = await client.put(
            f"/api/documents/{document_id}/folders",
            json={"folder_ids": [folder_a["folder_id"], folder_b["folder_id"]]},
        )
        assert copied.status_code == 200
        assert set(copied.json()["folder_ids"]) == {folder_a["folder_id"], folder_b["folder_id"]}

        in_a = await client.get("/api/documents", params={"folder_id": folder_a["folder_id"]})
        in_b = await client.get("/api/documents", params={"folder_id": folder_b["folder_id"]})
        assert any(d["document_id"] == document_id for d in in_a.json())
        assert any(d["document_id"] == document_id for d in in_b.json())

        folder_count = (await client.get("/api/folders")).json()
        counts = {f["folder_id"]: f["document_count"] for f in folder_count}
        assert counts[folder_a["folder_id"]] == 1
        assert counts[folder_b["folder_id"]] == 1

        # Deleting a folder doesn't touch the document -- it just loses
        # that one membership.
        await client.delete(f"/api/folders/{folder_a['folder_id']}")
        doc_after = (await client.get(f"/api/documents/{document_id}")).json()
        assert doc_after["folder_ids"] == [folder_b["folder_id"]]

        # Removing the last membership makes it show up under ?unfiled=true.
        await client.put(f"/api/documents/{document_id}/folders", json={"folder_ids": []})
        unfiled = await client.get("/api/documents", params={"unfiled": "true"})
        assert any(d["document_id"] == document_id for d in unfiled.json())

        # cleanup
        await client.delete(f"/api/documents/{document_id}")
        await client.delete(f"/api/folders/{folder_b['folder_id']}")


@pytest.mark.asyncio
async def test_set_document_folders_rejects_unknown_folder():
    document_id = await _make_document("badref.txt")
    async with _client() as client:
        resp = await client.put(f"/api/documents/{document_id}/folders", json={"folder_ids": [str(uuid.uuid4())]})
        assert resp.status_code == 404
        await client.delete(f"/api/documents/{document_id}")
