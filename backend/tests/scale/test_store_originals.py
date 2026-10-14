"""STORE_ORIGINALS=false: the source is the only copy of the file. Pages and the index are kept; the original and its rendered
preview are not written to object storage, and a citation opens the file at its source."""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.main import app
from app.scale import pipeline, sourceref
from app.scale.db import engine
from tests.scale.helpers import doc, ingest

FILES = {
    "legal/contract.txt": "Termination for convenience requires 30 days written notice. Reference SUB-084.",
    "finance/budget.txt": "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.",
    "public/handbook.txt": "Vacation policy: employees accrue two days of leave per month.",
}


def client(headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


def grant(user_id, *principals):
    with engine().begin() as conn:
        for p in principals:
            conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, :p)"), {"u": user_id, "p": p})


def row(filename):
    with engine().connect() as conn:
        return conn.execute(text("SELECT storage_key, preview_storage_key, status, tier FROM documents WHERE filename = :f"), {"f": filename}).one()


@pytest.fixture
def no_copy(tmp_path, object_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "store_originals", False)
    ingest(tmp_path, FILES)
    return tmp_path


def test_nothing_is_written_to_object_storage(no_copy, object_store):
    assert object_store.objects == {}
    storage_key, preview_key, status, tier = row("contract.txt")
    assert storage_key.startswith("source:") and preview_key is None and status == "indexed" and tier == "lite"


async def test_search_and_page_text_still_work_and_keep_the_permissions(no_copy, make_user):
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    contract, budget = doc("contract.txt"), doc("budget.txt")
    async with client(headers) as c:
        hits = {r["name"] for r in (await c.get("/api/scale/search", params={"q": "termination notice SUB-084"})).json()["results"]}
        page = await c.get(f"/api/documents/{contract['document_id']}/pages/1")
        forbidden = await c.get(f"/api/documents/{budget['document_id']}/pages/1")
        no_such_page = await c.get(f"/api/documents/{contract['document_id']}/pages/9")
    assert "contract.txt" in hits and "budget.txt" not in hits
    assert page.status_code == 200 and "Termination for convenience" in page.json()["text"]
    assert forbidden.status_code == 404 and no_such_page.status_code == 404           # a forbidden file looks like a missing one


async def test_the_preview_says_where_the_file_lives(no_copy, make_user):
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    contract = doc("contract.txt")
    async with client(headers) as c:
        body = (await c.get(f"/api/documents/{contract['document_id']}/preview")).json()
    assert body["url"] is None and body["stored"] is False
    assert body["source"]["available"] is True and body["source"]["path"].endswith("contract.txt") and body["source"]["url"] is None


async def test_a_source_that_can_give_a_link_has_it_in_the_preview(no_copy, make_user, monkeypatch):
    from app.scale.connectors.local_folder import LocalFolderConnector

    monkeypatch.setattr(LocalFolderConnector, "source_link", lambda self, sf: f"https://contoso.sharepoint.com/{sf.name}", raising=False)
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    contract = doc("contract.txt")
    async with client(headers) as c:
        body = (await c.get(f"/api/documents/{contract['document_id']}/preview")).json()
    assert body["source"]["url"] == "https://contoso.sharepoint.com/contract.txt"


async def test_a_source_that_is_down_still_gives_the_page_text_not_an_error(no_copy, make_user, monkeypatch):
    from app.scale.connectors.local_folder import LocalFolderConnector

    def boom(self, sf):
        raise RuntimeError("the source is unreachable")

    monkeypatch.setattr(LocalFolderConnector, "source_link", boom, raising=False)
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    contract = doc("contract.txt")
    async with client(headers) as c:
        preview = await c.get(f"/api/documents/{contract['document_id']}/preview")
        page = await c.get(f"/api/documents/{contract['document_id']}/pages/1")
    assert preview.status_code == 200 and preview.json()["source"]["url"] is None and page.status_code == 200


def test_the_bytes_can_be_fetched_again_from_the_source_when_a_job_needs_them(no_copy):
    contract = doc("contract.txt")
    data = sourceref.download(contract["document_id"], 10_000_000)
    assert data == FILES["legal/contract.txt"].encode()


def test_removing_a_source_only_document_leaves_no_error_and_no_rows(no_copy, object_store):
    contract = doc("contract.txt")
    pipeline.delete_document_everywhere(contract["document_id"])
    with engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM documents WHERE filename = 'contract.txt'")).scalar_one() == 0
    assert object_store.objects == {}


def test_the_default_still_keeps_a_copy_for_previews(tmp_path, object_store):
    ingest(tmp_path, {"public/a.txt": "Plain text that is kept in object storage by default."})
    storage_key, _, _, _ = row("a.txt")
    assert storage_key.startswith("originals/") and storage_key in object_store.objects


async def test_deleting_a_source_only_document_through_the_api_works(no_copy, make_user):
    _, headers, _ = await make_user(role="admin")
    contract = doc("contract.txt")
    async with client(headers) as c:
        assert (await c.delete(f"/api/documents/{contract['document_id']}")).status_code == 200
