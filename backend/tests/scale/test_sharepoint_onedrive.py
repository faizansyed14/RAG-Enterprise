"""SharePoint and OneDrive sources: finding the libraries, scanning them, and above all never writing.

A simulated Graph server stands in for Microsoft (httpx.MockTransport). Nothing here talks to a real tenant."""

import base64
import json

import httpx
import pytest

from app.main import app
from app.scale.connectors import build_connector
from app.scale.connectors.graph_common import (
    GRAPH, GraphError, ReadOnlyViolation, assert_read_only, site_path_from_url, token_roles,
)
from app.scale.connectors.onedrive import OneDriveConnector
from app.scale.connectors.sharepoint import SharePointConnector

T = "11111111-2222-3333-4444-555555555555"
C = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CREDS = {"tenant_id": T, "client_id": C, "client_secret_env": "SP_SECRET"}
SITE = "https://contoso.sharepoint.com/sites/Finance"


def jwt(roles):
    payload = base64.urlsafe_b64encode(json.dumps({"roles": roles}).encode()).decode().rstrip("=")
    return f"x.{payload}.y"


def file_item(i, name="Budget.xlsx", path="/drives/D1/root:/Q3", modified="2026-09-01T10:00:00Z"):
    return {"id": i, "name": name, "size": 10, "eTag": f'"{i}"', "file": {"mimeType": "x"}, "lastModifiedDateTime": modified,
            "webUrl": "https://x", "parentReference": {"path": path}}


class Tenant:
    """A tiny Microsoft 365 tenant. Records every request so tests can check what was sent."""

    def __init__(self):
        self.sites = {"contoso.sharepoint.com:/sites/Finance": {"id": "s1", "displayName": "Finance", "webUrl": SITE}}
        self.site_list = [{"id": "s1", "displayName": "Finance", "webUrl": SITE},
                          {"id": "s2", "displayName": "Ana", "webUrl": "https://contoso-my.sharepoint.com/personal/ana"}]
        self.site_drives = {"s1": [{"id": "D1", "name": "Documents", "driveType": "documentLibrary"},
                                   {"id": "D2", "name": "Contracts", "driveType": "documentLibrary"},
                                   {"id": "D3", "name": "Hold", "driveType": "documentLibrary", "system": {}},
                                   {"id": "D4", "name": "Odd", "driveType": "business"}]}
        self.users = {"ana@contoso.com": {"id": "U1", "name": "OneDrive"}}
        self.user_list = [{"id": "1", "userPrincipalName": "ana@contoso.com", "accountEnabled": True, "userType": "Member"},
                          {"id": "2", "userPrincipalName": "off@contoso.com", "accountEnabled": False, "userType": "Member"},
                          {"id": "3", "userPrincipalName": "ext@gmail.com#EXT#", "accountEnabled": True, "userType": "Guest"},
                          {"id": "4", "userPrincipalName": "bob@contoso.com", "accountEnabled": True, "userType": "Member"}]
        self.delta = {"D1": [{"value": [file_item("1"), file_item("2", "old.pdf"), {"id": "9", "folder": {}, "name": "dir"}],
                              "@odata.deltaLink": f"{GRAPH}/drives/D1/root/delta?token=T"}]}
        self.perms = {}                       # item id -> (status, value)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.next_link_host = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url, path = str(req.url), req.url.path
        if path.endswith("/$batch"):
            subs = json.loads(req.content)["requests"]
            out = []
            for s in subs:
                item = s["url"].split("/items/")[1].split("/")[0]
                status, value = self.perms.get(item, (200, [{"roles": ["read"], "grantedToV2": {"user": {"email": "ana@contoso.com"}}}]))
                out.append({"id": s["id"], "status": status, "body": {"value": value}})
            return httpx.Response(200, json={"responses": out})
        if path == "/v1.0/sites" and "search" in url:
            if self.next_link_host:
                return httpx.Response(200, json={"value": self.site_list[:1], "@odata.nextLink": f"https://{self.next_link_host}/v1.0/sites?page=2"})
            return httpx.Response(200, json={"value": self.site_list})
        if path.startswith("/v1.0/sites/") and path.endswith("/drives"):
            sid = path.split("/")[3]
            return httpx.Response(200, json={"value": self.site_drives.get(sid, [])})
        if path.startswith("/v1.0/sites/"):
            ref = path[len("/v1.0/sites/"):]
            site = self.sites.get(ref)
            return httpx.Response(200, json=site) if site else httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        if path == "/v1.0/users":
            return httpx.Response(200, json={"value": self.user_list})
        if path.startswith("/v1.0/users/") and path.endswith("/drive"):
            upn = path.split("/")[3]
            u = self.users.get(upn)
            return httpx.Response(200, json={"id": u["id"], "name": "OneDrive", "driveType": "business", "webUrl": "https://x-my"}) if u \
                else httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        if path.endswith("/root/children"):
            did = path.split("/")[3]
            return httpx.Response(200, json={"value": [file_item("1"), {"id": "f", "name": "dir", "folder": {}}]}) if did in ("D1", "D2", "U1") \
                else httpx.Response(403, json={"error": {"code": "accessDenied"}})
        if path.endswith("/root/delta"):
            did = path.split("/")[3]
            pages = self.delta.get(did)
            return httpx.Response(200, json=pages.pop(0)) if pages else httpx.Response(200, json={"value": [], "@odata.deltaLink": f"{GRAPH}/drives/{did}/root/delta?token=E"})
        if path.endswith("/content"):
            return httpx.Response(302, headers={"Location": "https://download.example.net/blob?sig=abc"})
        if req.url.host == "download.example.net":
            return httpx.Response(200, content=b"file-bytes")
        return httpx.Response(404)

    @property
    def methods(self):
        return {r.method for r in self.requests}


def make(cls, tenant, token=None, **cfg):
    return cls({**CREDS, **cfg}, token_provider=lambda: token or jwt(["Sites.Read.All", "Files.Read.All"]),
               transport=httpx.MockTransport(tenant.handler), sleep=tenant.sleeps.append)


def scan_all(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


# ── the read-only guard ──

@pytest.mark.parametrize("method,url,body", [
    ("PUT", f"{GRAPH}/drives/D1/items/1/content", None),
    ("PATCH", f"{GRAPH}/drives/D1/items/1", {"name": "x"}),
    ("DELETE", f"{GRAPH}/drives/D1/items/1", None),
    ("POST", f"{GRAPH}/drives/D1/items/1/createLink", {"type": "view"}),
    ("POST", f"{GRAPH}/drives/D1/items/1/copy", {}),
    ("GET", "https://evil.example.com/v1.0/me", None),
    ("GET", "http://graph.microsoft.com/v1.0/me", None),
    ("POST", f"{GRAPH}/$batch", {"requests": [{"id": "1", "method": "DELETE", "url": "/drives/D1/items/1"}]}),
    ("POST", f"{GRAPH}/$batch", {"requests": [{"id": "1", "method": "GET", "url": "/me", "body": {"a": 1}}]}),
    ("POST", f"{GRAPH}/$batch", {"requests": []}),
    ("POST", "https://login.microsoftonline.com.evil.com/x/oauth2/v2.0/token", None),
])
def test_anything_that_could_write_or_leak_the_token_is_stopped_before_it_is_sent(method, url, body):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(method, url, body)


def test_the_requests_a_scan_needs_are_allowed():
    assert_read_only("GET", f"{GRAPH}/drives/D1/root/delta")
    assert_read_only("POST", "https://login.microsoftonline.com/t/oauth2/v2.0/token")
    assert_read_only("POST", f"{GRAPH}/$batch", {"requests": [{"id": "0", "method": "GET", "url": "/drives/D1/items/1/permissions"}]})


def test_a_whole_scan_a_test_and_a_download_send_only_gets_and_read_only_batches():
    t = Tenant()
    sp = make(SharePointConnector, t, sites=[SITE])
    scan_all(sp)
    sp.test()
    file = scan_all(make(SharePointConnector, Tenant(), sites=[SITE]))[0][0].file
    sp.open(file).read()
    assert t.methods <= {"GET", "POST"}
    for r in t.requests:
        if r.method == "POST":
            assert r.url.path.endswith("/$batch") and all(s["method"] == "GET" for s in json.loads(r.content)["requests"])


def test_the_download_link_does_not_receive_the_bearer_token():
    t = Tenant()
    sp = make(SharePointConnector, t, sites=[SITE])
    file = scan_all(sp)[0][0].file
    assert sp.open(file).read() == b"file-bytes"
    download = [r for r in t.requests if r.url.host == "download.example.net"][0]
    assert "authorization" not in {k.lower() for k in download.headers}


# ── SharePoint ──

def test_sharepoint_finds_the_document_libraries_of_a_site_and_skips_system_and_other_drives():
    t = Tenant()
    changes, batches = scan_all(make(SharePointConnector, t, sites=[SITE]))
    files = [c.file for c in changes if c.kind == "upsert"]
    assert [f.name for f in files] == ["Budget.xlsx", "old.pdf"]
    assert files[0].path == "Finance / Documents/Q3/Budget.xlsx"          # the path carries site and library
    assert files[0].external_id == "D1:1"
    scanned = {r.url.path.split("/")[3] for r in t.requests if r.url.path.endswith("/root/delta")}
    assert scanned == {"D1", "D2"}                                         # D3 is a system library, D4 is not a document library
    assert batches[-1].done


def test_sharepoint_can_be_limited_to_named_libraries():
    t = Tenant()
    make(SharePointConnector, t, sites=[SITE], libraries=["contracts"]) and scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["contracts"]))
    assert {r.url.path.split("/")[3] for r in t.requests if r.url.path.endswith("/root/delta")} == {"D2"}


def test_no_sites_listed_means_every_site_but_not_personal_onedrive_sites():
    t = Tenant()
    scan_all(make(SharePointConnector, t))
    assert any(r.url.path == "/v1.0/sites" for r in t.requests)
    assert not any(r.url.path.startswith("/v1.0/sites/s2") for r in t.requests)


def test_a_site_that_is_not_there_is_reported_and_the_others_still_scan():
    t = Tenant()
    sp = make(SharePointConnector, t, sites=[SITE, "https://contoso.sharepoint.com/sites/Gone"])
    changes, _ = scan_all(sp)
    assert any(c.kind == "upsert" for c in changes)
    assert any("Gone" in n and "not found" in n for n in sp.notes)


@pytest.mark.parametrize("bad", [
    "http://contoso.sharepoint.com/sites/Finance", "https://evil.example.com/sites/Finance",
    "https://contoso-my.sharepoint.com/personal/ana", "file:///etc/passwd", "contoso.sharepoint.com",
])
def test_site_addresses_must_be_https_sharepoint_sites(bad):
    with pytest.raises(ValueError):
        site_path_from_url(bad)


def test_a_site_address_pasted_from_the_browser_is_trimmed_to_the_site():
    assert site_path_from_url("https://contoso.sharepoint.com/sites/Finance/Shared Documents/Forms/AllItems.aspx") == ("contoso.sharepoint.com", "/sites/Finance")
    assert site_path_from_url("https://contoso.sharepoint.com/") == ("contoso.sharepoint.com", "")


def test_credentials_are_checked_for_shape_before_anything_is_stored(monkeypatch):
    monkeypatch.setenv("SP_SECRET", "x")
    for bad in ({"tenant_id": "nope"}, {"client_id": "nope"}, {"client_secret_env": "has space"}, {"client_secret_env": "s3cr3t-value!"}):
        with pytest.raises(ValueError):
            build_connector("sharepoint", {**CREDS, "sites": [SITE], **bad})
    assert build_connector("sharepoint", {**CREDS, "sites": [SITE]}).kind == "sharepoint"
    assert build_connector("sharepoint", {**CREDS, "tenant_id": "contoso.onmicrosoft.com"}).kind == "sharepoint"


# ── OneDrive ──

def test_onedrive_scans_the_named_users_and_reports_those_without_a_drive():
    t = Tenant()
    t.delta["U1"] = [{"value": [file_item("1", path="/drives/U1/root:/Docs")], "@odata.deltaLink": f"{GRAPH}/drives/U1/root/delta?token=T"}]
    od = make(OneDriveConnector, t, users=["ana@contoso.com", "new@contoso.com"])
    changes, _ = scan_all(od)
    f = [c.file for c in changes if c.kind == "upsert"][0]
    assert f.path.startswith("OneDrive - ana@contoso.com/")
    assert any("new@contoso.com" in n and "no OneDrive" in n for n in od.notes)


def test_all_users_skips_disabled_accounts_and_guests():
    t = Tenant()
    t.users["bob@contoso.com"] = {"id": "U2"}
    od = make(OneDriveConnector, t, all_users=True)
    drives = od._discover_drives()
    assert [d.owner for d in drives] == ["ana@contoso.com", "bob@contoso.com"]


def test_onedrive_needs_users_or_all_users_and_rejects_odd_addresses():
    with pytest.raises(ValueError):
        OneDriveConnector({**CREDS}, token_provider=lambda: "t")
    for bad in ("../x", "a b@c.com", "ana", "ana@contoso.com/drive?x=1"):
        with pytest.raises(ValueError):
            OneDriveConnector({**CREDS, "users": [bad]}, token_provider=lambda: "t")


# ── who may read what ──

def test_by_default_each_file_keeps_its_own_sharing():
    t = Tenant()
    t.perms["1"] = (200, [{"roles": ["read"], "grantedToV2": {"group": {"id": "G1"}}}])
    changes, _ = scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["documents"]))
    assert {c.file.external_id: c.file.acl for c in changes if c.kind == "upsert"}["D1:1"] == ["group:g1"]


def test_without_permission_rights_files_get_no_readers_rather_than_everyone():
    t = Tenant()
    t.perms["1"] = (403, [])
    changes, _ = scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["documents"]))
    assert [c.file.acl for c in changes if c.file.external_id == "D1:1"] == [[]]


def test_everyone_mode_reads_no_permissions_at_all_and_marks_files_readable_for_all_users():
    t = Tenant()
    changes, _ = scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["documents"], access={"mode": "everyone"}))
    assert all(c.file.acl == ["*"] for c in changes if c.kind == "upsert")
    assert not any(r.url.path.endswith("/$batch") for r in t.requests)


def test_principals_mode_uses_the_listed_principals():
    t = Tenant()
    changes, _ = scan_all(make(OneDriveConnector, t, users=["ana@contoso.com"], access={"mode": "principals", "principals": ["group:finance"]}))
    assert all(c.file.acl == ["group:finance"] for c in changes if c.kind == "upsert")


def test_bad_access_settings_are_refused():
    for access in ({"mode": "open"}, {"mode": "principals", "principals": []}):
        with pytest.raises(ValueError):
            SharePointConnector({**CREDS, "sites": [SITE], "access": access}, token_provider=lambda: "t")


# ── filters ──

def test_filters_by_extension_folder_and_date_and_a_filtered_file_becomes_a_delete():
    t = Tenant()
    t.delta["D1"] = [{"value": [file_item("1", "a.pdf", "/drives/D1/root:/Q3"), file_item("2", "b.docx", "/drives/D1/root:/Q3"),
                                file_item("3", "c.pdf", "/drives/D1/root:/Archive"), file_item("4", "d.pdf", modified="2019-01-01T00:00:00Z")],
                      "@odata.deltaLink": f"{GRAPH}/drives/D1/root/delta?token=T"}]
    cfg = dict(sites=[SITE], libraries=["documents"], include_extensions=["PDF"], exclude_paths=["*/Archive"], modified_after="2024-01-01")
    changes, _ = scan_all(make(SharePointConnector, t, **cfg))
    assert [c.external_id for c in changes if c.kind == "upsert"] == ["D1:1"]
    assert {c.external_id for c in changes if c.kind == "delete"} == {"D1:2", "D1:3", "D1:4"}


# ── safety of links and cursors ──

def test_a_next_link_to_another_server_is_never_followed():
    t = Tenant()
    t.next_link_host = "evil.example.com"
    with pytest.raises(GraphError):
        scan_all(make(SharePointConnector, t))
    assert not any(r.url.host == "evil.example.com" for r in t.requests)


def test_a_stored_link_that_points_elsewhere_starts_a_fresh_scan_instead_of_being_used():
    t = Tenant()
    scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["documents"]),
             {"drives": {"D1": "https://evil.example.com/steal?token=1"}})
    assert not any(r.url.host == "evil.example.com" for r in t.requests)


def test_a_library_that_disappears_from_discovery_keeps_its_cursor_and_nothing_is_deleted():
    t = Tenant()
    cursor = {"drives": {"D2": f"{GRAPH}/drives/D2/root/delta?token=OLD"}}
    changes, batches = scan_all(make(SharePointConnector, t, sites=[SITE], libraries=["documents"]), cursor)
    assert not any(c.kind == "delete" for c in changes) and all(b.complete_scope is None for b in batches)
    assert "D2" in batches[-1].cursor["drives"]


def test_a_big_download_is_refused():
    t = Tenant()
    sp = make(SharePointConnector, t, sites=[SITE], libraries=["documents"], max_file_mb=0)
    file = scan_all(sp)[0][0].file
    with pytest.raises(GraphError):
        sp.open(file)


# ── "Test connection" ──

def test_the_test_report_lists_libraries_roles_and_whether_permissions_can_be_read():
    t = Tenant()
    report = make(SharePointConnector, t, sites=[SITE], token=jwt(["Sites.Read.All", "Files.Read.All"])).test()
    assert report["ok"] and report["drive_count"] == 2 and report["permissions_readable"] is True
    assert report["roles"] == ["Files.Read.All", "Sites.Read.All"] and report["write_roles"] == []
    assert [d["label"] for d in report["drives"]] == ["Finance / Documents", "Finance / Contracts"]


def test_the_test_report_warns_about_write_permissions_and_unreadable_permission_data():
    t = Tenant()
    t.perms["1"] = (403, [])
    report = make(SharePointConnector, t, sites=[SITE], token=jwt(["Sites.ReadWrite.All", "Files.Read.All"])).test()
    assert report["write_roles"] == ["Sites.ReadWrite.All"] and report["permissions_readable"] is False


def test_the_test_report_says_so_when_nothing_is_found_and_when_sign_in_fails():
    t = Tenant()
    report = make(SharePointConnector, t, sites=["https://contoso.sharepoint.com/sites/Gone"]).test()
    assert not report["ok"] and "No document libraries" in report["error"]

    def refuse():
        raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://login.microsoftonline.com/t/oauth2/v2.0/token"),
                                    response=httpx.Response(401, json={"error_description": "AADSTS7000215: Invalid client secret provided."}))
    sp = SharePointConnector({**CREDS, "sites": [SITE]}, token_provider=refuse, transport=httpx.MockTransport(t.handler))
    out = sp.test()
    assert not out["ok"] and "AADSTS7000215" in out["error"]


def test_token_roles_reads_a_token_without_trusting_it():
    assert token_roles(jwt(["B", "A"])) == ["A", "B"] and token_roles("garbage") == []


# ── the HTTP surface ──

@pytest.fixture
def mounted():
    from app.scale.api import router
    from app.scale.sources_api import router as sources
    if not any(getattr(r, "path", "") == "/api/scale/connections/test" for r in app.routes):
        app.include_router(router)
        app.include_router(sources)


def api(headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


async def test_the_test_endpoint_runs_the_connector_test_and_is_admin_only(make_user, mounted, monkeypatch):
    from app.scale import sources_api
    t = Tenant()
    monkeypatch.setattr(sources_api, "build_connector", lambda kind, cfg: make(SharePointConnector, t, **cfg))
    _, user_headers, _ = await make_user("user")
    _, headers, _ = await make_user("admin")
    body = {"kind": "sharepoint", "config": {"sites": [SITE]}}
    async with api(user_headers) as c:
        assert (await c.post("/api/scale/connections/test", json=body)).status_code == 403
    async with api(headers) as c:
        out = (await c.post("/api/scale/connections/test", json=body)).json()
        assert out["ok"] and out["drive_count"] == 2
        assert (await c.post("/api/scale/connections/test", json={"kind": "sharepoint", "config": {"client_secret": "oops"}})).status_code == 422
        assert (await c.post("/api/scale/connections/test", json={"kind": "nope", "config": {}})).status_code == 422
    monkeypatch.setattr(sources_api, "build_connector", lambda kind, cfg: object())       # a source type that has no test yet
    async with api(headers) as c:
        r = await c.post("/api/scale/connections/test", json={"kind": "local", "config": {}})
        assert r.status_code == 422 and "no connection test" in r.json()["detail"]


async def test_a_sharepoint_source_with_a_bad_tenant_is_refused_and_a_good_one_is_saved(make_user, mounted):
    _, headers, _ = await make_user("admin")
    good = {"kind": "sharepoint", "name": "Finance site", "scan_now": False, "config": {**CREDS, "sites": [SITE]}}
    async with api(headers) as c:
        assert (await c.post("/api/scale/connections", json={**good, "config": {**good["config"], "tenant_id": "x"}})).status_code == 422
        assert (await c.post("/api/scale/connections", json=good)).status_code == 201
        kinds = {k["kind"]: k for k in (await c.get("/api/scale/kinds")).json()["kinds"]}
        assert {"sharepoint", "onedrive"} <= set(kinds) and kinds["sharepoint"]["template"]["access"] == {"mode": "source"}


async def test_env_check_reports_yes_or_no_never_the_value(make_user, mounted, monkeypatch):
    monkeypatch.setenv("SP_TEST_SECRET", "super-secret-value")
    _, headers, _ = await make_user("admin")
    async with api(headers) as c:
        r = await c.get("/api/scale/env-check", params={"names": "SP_TEST_SECRET,NOT_THERE_X"})
        assert r.json() == {"set": {"SP_TEST_SECRET": True, "NOT_THERE_X": False}}
        assert "super-secret-value" not in r.text
        assert (await c.get("/api/scale/env-check", params={"names": "bad name"})).status_code == 422
