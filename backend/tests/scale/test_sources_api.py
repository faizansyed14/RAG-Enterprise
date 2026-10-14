"""Admin HTTP surface for sources and access groups."""

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from app.scale.db import engine

_mounted = False


@pytest.fixture(autouse=True)
def mount_routers():
    global _mounted
    if not _mounted:
        from app.scale.api import router
        from app.scale.sources_api import router as sources

        app.include_router(router)
        app.include_router(sources)
        _mounted = True


@pytest.fixture
def source_dir(tmp_path, monkeypatch):
    from app.scale.settings import get_scale_settings

    root = tmp_path / "sources"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("hello world")
    monkeypatch.setenv("SCALE_API_LOCAL_ROOTS", f'["{root}"]')
    get_scale_settings.cache_clear()
    return root


def client(headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


async def test_everything_is_admin_only(make_user):
    _, headers, _ = await make_user("user")
    async with client(headers) as c:
        for method, url in [("get", "/api/scale/kinds"), ("get", "/api/scale/connections"), ("get", "/api/scale/principals")]:
            assert (await getattr(c, method)(url)).status_code == 403
        assert (await c.post("/api/scale/connections", json={"kind": "local", "name": "x", "config": {}})).status_code == 403


async def test_kinds_lists_templates(make_user):
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        kinds = (await c.get("/api/scale/kinds")).json()["kinds"]
    by_kind = {k["kind"]: k for k in kinds}
    assert {"local", "smb", "graph", "s3"} <= set(by_kind)
    assert by_kind["smb"]["template"]["password_env"] == "SMB_PASSWORD"


async def test_create_list_patch_scan_delete(make_user, source_dir):
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        cfg = {"root": str(source_dir), "acl_rules": [{"prefix": "", "principals": ["*"]}]}
        resp = await c.post("/api/scale/connections", json={"kind": "local", "name": "Shared docs", "config": cfg})
        assert resp.status_code == 201, resp.text
        cid = resp.json()["connection_id"]
        assert resp.json()["scan_job"]

        dup = await c.post("/api/scale/connections", json={"kind": "local", "name": "Shared docs", "config": cfg})
        assert dup.status_code == 409

        rows = (await c.get("/api/scale/connections")).json()["connections"]
        mine = next(r for r in rows if r["connection_id"] == cid)
        assert mine["enabled"] and mine["scan_queued"]

        assert (await c.patch(f"/api/scale/connections/{cid}", json={"enabled": False})).status_code == 200
        assert (await c.post(f"/api/scale/connections/{cid}/scan")).status_code == 409
        assert (await c.patch(f"/api/scale/connections/{cid}", json={"enabled": True, "name": "Renamed"})).status_code == 200
        assert (await c.post(f"/api/scale/connections/{cid}/scan")).status_code == 200

        assert (await c.get(f"/api/scale/connections/{cid}/problems")).json() == {"items": []}
        assert (await c.delete(f"/api/scale/connections/{cid}")).status_code == 200
        assert (await c.delete(f"/api/scale/connections/{cid}")).status_code == 404


async def test_delete_refused_while_files_are_indexed(make_user, source_dir):
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        cid = (await c.post("/api/scale/connections", json={
            "kind": "local", "name": "Has files", "config": {"root": str(source_dir)}, "scan_now": False})).json()["connection_id"]
        with engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO source_items (connection_id, external_id, path, name, state) VALUES (:c, 'e1', 'docs/a.txt', 'a.txt', 'indexed')"
            ), {"c": cid})
        assert (await c.delete(f"/api/scale/connections/{cid}")).status_code == 409


async def test_inline_secrets_are_refused(make_user):
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        resp = await c.post("/api/scale/connections", json={
            "kind": "smb", "name": "bad", "config": {"server": "s", "share": "d", "username": "u", "password": "hunter2"}})
    assert resp.status_code == 422 and "password" in resp.text and "environment" in resp.text


async def test_local_source_must_be_inside_the_allowed_roots(make_user, source_dir, tmp_path):
    _, headers, _ = await make_user("admin")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    async with client(headers) as c:
        resp = await c.post("/api/scale/connections", json={"kind": "local", "name": "escape", "config": {"root": str(outside)}})
        assert resp.status_code == 422 and "must be inside" in resp.text
        resp = await c.post("/api/scale/connections", json={
            "kind": "local", "name": "traversal", "config": {"root": f"{source_dir}/../elsewhere"}})
        assert resp.status_code == 422


async def test_unknown_kind_and_missing_config(make_user):
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        assert (await c.post("/api/scale/connections", json={"kind": "nope", "name": "x", "config": {}})).status_code == 422
        resp = await c.post("/api/scale/connections", json={"kind": "smb", "name": "x", "config": {}})
        assert resp.status_code == 422


async def test_principals_roundtrip(make_user):
    target, _, _ = await make_user("user")
    _, headers, _ = await make_user("admin")
    async with client(headers) as c:
        resp = await c.post("/api/scale/principals", json={"username": target.username, "principal": "Group:Finance"})
        assert resp.status_code == 201 and resp.json()["principal"] == "group:finance"
        rows = (await c.get("/api/scale/principals")).json()["principals"]
        assert any(r["username"] == target.username and r["principal"] == "group:finance" for r in rows)

        assert (await c.post("/api/scale/principals", json={"username": target.username, "principal": "*"})).status_code == 422
        assert (await c.post("/api/scale/principals", json={"username": target.username, "principal": "nonsense"})).status_code == 422
        assert (await c.post("/api/scale/principals", json={"username": "ghost-user", "principal": "group:x"})).status_code == 404

        gone = await c.request("DELETE", "/api/scale/principals", json={"username": target.username, "principal": "group:finance"})
        assert gone.status_code == 200
        again = await c.request("DELETE", "/api/scale/principals", json={"username": target.username, "principal": "group:finance"})
        assert again.status_code == 404
