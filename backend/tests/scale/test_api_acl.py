"""HTTP surface in scale mode: list, preview and search are filtered by the caller's principals, and a
forbidden document is indistinguishable from a missing one."""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from app.scale.db import engine
from tests.scale.helpers import doc, ingest

FILES = {
    "finance/budget.txt": "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.",
    "legal/contract.txt": "Termination for convenience requires 30 days written notice. Reference SUB-084.",
    "public/handbook.txt": "Vacation policy: employees accrue two days of leave per month.",
}


@pytest.fixture
def corpus(tmp_path, object_store):
    ingest(tmp_path, FILES)


def client(headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


def grant(user_id, *principals):
    with engine().begin() as conn:
        for p in principals:
            conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, :p)"), {"u": user_id, "p": p})


async def test_search_returns_only_what_the_caller_may_read(corpus, make_user):
    finance, h_fin, _ = await make_user()
    nobody, h_none, _ = await make_user()
    grant(finance.user_id, "group:finance")

    async with client(h_fin) as c:
        names = {r["name"] for r in (await c.get("/api/scale/search", params={"q": "budget Falcon vacation termination"})).json()["results"]}
    assert "budget.txt" in names and "contract.txt" not in names

    async with client(h_none) as c:
        resp = await c.get("/api/scale/search", params={"q": "budget Falcon vacation termination"})
    assert resp.status_code == 200
    assert {r["name"] for r in resp.json()["results"]} == {"handbook.txt"}          # only the '*' document


async def test_search_requires_a_login(corpus):
    async with client({}) as c:
        assert (await c.get("/api/scale/search", params={"q": "budget"})).status_code in (401, 403)


async def test_document_list_is_filtered_and_paged(corpus, make_user):
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    async with client(headers) as c:
        names = {d["filename"] for d in (await c.get("/api/documents")).json()}
        paged = (await c.get("/api/documents", params={"limit": 1})).json()
        searched = (await c.get("/api/documents", params={"q": "contract"})).json()
    assert {"contract.txt", "handbook.txt"} <= names and "budget.txt" not in names
    assert len(paged) == 1
    assert [d["filename"] for d in searched] == ["contract.txt"]


async def test_preview_of_a_forbidden_document_is_a_plain_404(corpus, make_user):
    user, headers, _ = await make_user()
    grant(user.user_id, "group:legal")
    budget, contract = doc("budget.txt"), doc("contract.txt")
    async with client(headers) as c:
        forbidden = await c.get(f"/api/documents/{budget['document_id']}/preview")
        missing = await c.get(f"/api/documents/{uuid.uuid4()}/preview")
        allowed = await c.get(f"/api/documents/{contract['document_id']}/preview")
        diagram = await c.get(f"/api/documents/{budget['document_id']}/diagrams/1")
    assert forbidden.status_code == 404 and missing.status_code == 404
    assert forbidden.json() == missing.json()                       # a guessed id reveals nothing
    assert diagram.status_code == 404
    assert allowed.status_code == 200 and allowed.json()["url"]


async def test_admin_sees_only_what_their_principals_allow_by_default(corpus, make_user):
    admin, headers, _ = await make_user(role="admin")
    async with client(headers) as c:
        names = {d["filename"] for d in (await c.get("/api/documents")).json()}
        status = await c.get("/api/scale/status")
    assert "budget.txt" not in names and "contract.txt" not in names
    assert status.status_code == 200 and "jobs" in status.json()    # the operator view still works
    assert "indexed" in status.json()["index_health"]                # ... including whether the search index matches the documents
    assert isinstance(status.json()["workers"], list)                # ... and which workers have reported in


async def test_status_and_promote_are_admin_only(corpus, make_user):
    user, headers, _ = await make_user()
    async with client(headers) as c:
        assert (await c.get("/api/scale/status")).status_code in (401, 403)
        assert (await c.post(f"/api/scale/promote/{uuid.uuid4()}")).status_code in (401, 403)
        assert (await c.post("/api/scale/reconcile")).status_code in (401, 403)


async def test_the_document_list_without_a_limit_is_bounded_but_not_tiny(corpus, make_user, monkeypatch):
    """The current UI sends no limit. It must still see its whole library (up to the configured bound), and a
    paging client can ask for less."""
    from app.scale.settings import get_scale_settings

    user, headers, _ = await make_user()
    grant(user.user_id, "group:finance", "group:legal")
    monkeypatch.setenv("SCALE_LEGACY_LIST_LIMIT", "2")
    get_scale_settings.cache_clear()
    async with client(headers) as c:
        default = (await c.get("/api/documents")).json()
        paged = (await c.get("/api/documents", params={"limit": 1, "offset": 1})).json()
    assert len(default) == 2                                         # the configured bound, newest first
    assert len(paged) == 1
    monkeypatch.setenv("SCALE_LEGACY_LIST_LIMIT", "5000")
    get_scale_settings.cache_clear()
    async with client(headers) as c:
        everything = (await c.get("/api/documents")).json()
        explicit = (await c.get("/api/documents", params={"limit": 1000})).json()
    assert {d["document_id"] for d in everything} == {d["document_id"] for d in explicit}
    assert {"budget.txt", "contract.txt", "handbook.txt"} <= {d["filename"] for d in everything}
