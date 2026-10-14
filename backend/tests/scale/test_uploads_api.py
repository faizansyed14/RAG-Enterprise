"""Uploading files into a source from the admin screen: where they land, who may read them, and that nothing escapes."""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from app.scale import search, sync
from app.scale.db import engine
from app.scale.settings import get_scale_settings

from .helpers import drain


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path, object_store):
    monkeypatch.setenv("SCALE_UPLOAD_DIR", str(tmp_path / "uploads"))
    get_scale_settings.cache_clear()
    if not any(getattr(r, "path", "") == "/api/scale/uploads" for r in app.routes):
        from app.scale.api import router
        from app.scale.sources_api import router as sources
        from app.scale.uploads_api import router as uploads

        for r in (router, sources, uploads):
            app.include_router(r)
    yield
    get_scale_settings.cache_clear()


def api(headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers, timeout=60)


def unique():
    return f"Upload {uuid.uuid4().hex[:8]}"


DEFAULT_BODY = b"The supplier shall keep the retention at five percent until practical completion."


def f(name, body=DEFAULT_BODY):
    return ("files", (name, body, "application/octet-stream"))


def connection(cid):
    with engine().connect() as conn:
        return conn.execute(text("SELECT kind, config FROM source_connections WHERE connection_id = :c"), {"c": cid}).one()


async def test_upload_is_admin_only(make_user):
    _, headers, _ = await make_user("user")
    async with api(headers) as c:
        r = await c.post("/api/scale/uploads", data={"name": unique(), "access": "everyone"}, files=[f("a.txt")])
    assert r.status_code == 403


async def test_a_new_source_is_created_with_the_chosen_readers_and_the_files_are_saved(make_user, tmp_path):
    _, headers, _ = await make_user("admin")
    name = unique()
    async with api(headers) as c:
        r = await c.post("/api/scale/uploads", data={"name": name, "access": "principals", "principals": "group:Finance, user:Ana"},
                         files=[f("budget.txt"), f("notes.txt")])
    body = r.json()
    assert r.status_code == 201 and body["created"] and [s["name"] for s in body["saved"]] == ["budget.txt", "notes.txt"] and body["scan_queued"]
    kind, config = connection(body["connection_id"])
    assert kind == "local" and config["managed_upload"] is True
    assert config["acl_rules"] == [{"prefix": "", "principals": ["group:finance", "user:ana"]}]
    assert (tmp_path / "uploads").resolve() in __import__("pathlib").Path(config["root"]).resolve().parents
    assert sorted(p.name for p in __import__("pathlib").Path(config["root"]).iterdir()) == ["budget.txt", "notes.txt"]


async def test_nothing_is_stored_until_someone_chooses_who_may_read(make_user, tmp_path):
    _, headers, _ = await make_user("admin")
    async with api(headers) as c:
        for data in ({"name": unique()}, {"name": unique(), "access": "open"}, {"name": unique(), "access": "principals", "principals": ""},
                     {"name": unique(), "access": "principals", "principals": "finance"}):
            r = await c.post("/api/scale/uploads", data=data, files=[f("a.txt")])
            assert r.status_code == 422, data
    assert not (tmp_path / "uploads").exists() or not any((tmp_path / "uploads").iterdir())


async def test_file_names_cannot_escape_the_folder_and_odd_names_are_refused(make_user):
    _, headers, _ = await make_user("admin")
    async with api(headers) as c:
        r = await c.post("/api/scale/uploads", data={"name": unique(), "access": "everyone"},
                         files=[f("../../etc/passwd"), f("..\\..\\boot.ini"), f(".hidden"), f("~$lock.docx"), f("ok.txt")])
    body = r.json()
    assert r.status_code == 201
    assert sorted(s["name"] for s in body["saved"]) == ["boot.ini", "ok.txt", "passwd"]
    assert {x["name"] for x in body["rejected"]} == {".hidden", "~$lock.docx"}
    _, config = connection(body["connection_id"])
    import pathlib
    assert not (pathlib.Path(config["root"]).parent / "passwd").exists()


async def test_a_file_over_the_size_limit_is_refused_and_the_others_are_kept(make_user, monkeypatch):
    monkeypatch.setenv("SCALE_MAX_FILE_MB", "1")
    get_scale_settings.cache_clear()
    _, headers, _ = await make_user("admin")
    async with api(headers) as c:
        r = await c.post("/api/scale/uploads", data={"name": unique(), "access": "everyone"},
                         files=[f("big.txt", b"x" * (1024 * 1024 + 5)), f("small.txt")])
    body = r.json()
    assert [s["name"] for s in body["saved"]] == ["small.txt"] and body["rejected"][0]["name"] == "big.txt" and "limit" in body["rejected"][0]["reason"]


async def test_adding_to_an_existing_source_keeps_its_readers_and_replaces_a_file_of_the_same_name(make_user):
    _, headers, _ = await make_user("admin")
    name = unique()
    async with api(headers) as c:
        first = (await c.post("/api/scale/uploads", data={"name": name, "access": "principals", "principals": "group:legal"}, files=[f("a.txt", b"one")])).json()
        second = await c.post("/api/scale/uploads", data={"name": name, "access": "everyone"}, files=[f("a.txt", b"two two"), f("b.txt")])
        assert second.status_code == 201 and not second.json()["created"] and second.json()["connection_id"] == first["connection_id"]
        listing = (await c.get(f"/api/scale/uploads/{first['connection_id']}/files")).json()
    assert [(x["name"], x["size"]) for x in listing["files"]] == [("a.txt", 7), ("b.txt", len(DEFAULT_BODY))]
    assert connection(first["connection_id"])[1]["acl_rules"][0]["principals"] == ["group:legal"]     # "everyone" on the second call changed nothing


async def test_a_name_that_belongs_to_another_kind_of_source_is_not_taken_over(make_user, tmp_path):
    _, headers, _ = await make_user("admin")
    name = unique()
    (tmp_path / "plain").mkdir()
    sync.create_connection("local", name, {"root": str(tmp_path / "plain")})
    async with api(headers) as c:
        r = await c.post("/api/scale/uploads", data={"name": name, "access": "everyone"}, files=[f("a.txt")])
    assert r.status_code == 409


async def test_a_file_can_be_removed_but_only_from_an_uploaded_source(make_user, tmp_path):
    _, headers, _ = await make_user("admin")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "keep.txt").write_text("x")
    plain = sync.create_connection("local", unique(), {"root": str(tmp_path / "plain")})
    async with api(headers) as c:
        made = (await c.post("/api/scale/uploads", data={"name": unique(), "access": "everyone"}, files=[f("a.txt"), f("b.txt")])).json()
        cid = made["connection_id"]
        assert (await c.delete(f"/api/scale/uploads/{cid}/files", params={"name": "a.txt"})).json()["removed"] == "a.txt"
        assert (await c.delete(f"/api/scale/uploads/{cid}/files", params={"name": "a.txt"})).status_code == 404
        assert (await c.delete(f"/api/scale/uploads/{cid}/files", params={"name": "../../x"})).status_code == 404
        assert (await c.get(f"/api/scale/uploads/{plain}/files")).status_code == 404
        assert (await c.delete(f"/api/scale/uploads/{plain}/files", params={"name": "keep.txt"})).status_code == 404
        names = [x["name"] for x in (await c.get(f"/api/scale/uploads/{cid}/files")).json()["files"]]
    assert names == ["b.txt"] and (tmp_path / "plain" / "keep.txt").exists()


async def test_uploaded_files_are_indexed_locally_and_only_the_chosen_readers_find_them(make_user):
    _, headers, _ = await make_user("admin")
    async with api(headers) as c:
        made = (await c.post("/api/scale/uploads", data={"name": unique(), "access": "principals", "principals": "group:finance"},
                             files=[f("retention.txt", b"The retention held back from each interim payment is five percent of the certified value.")])).json()
    cid = made["connection_id"]
    sync.run_scan(cid)
    drain()
    with engine().connect() as conn:
        assert conn.execute(text("SELECT state FROM source_items WHERE connection_id = :c"), {"c": cid}).scalar_one() == "indexed"
    mine = search.search_chunks("retention held back interim payment", principals={"group:finance", "*"}, limit=5)
    other = search.search_chunks("retention held back interim payment", principals={"group:legal", "*"}, limit=5)
    assert any(h.name == "retention.txt" for h in mine) and not any(h.name == "retention.txt" for h in other)
