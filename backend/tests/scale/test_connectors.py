import json
import os

import httpx
import pytest

from app.scale.connectors.base import AclRules
from app.scale.connectors.crawl import CrawlConnector, FsEntry
from app.scale.connectors.graph import GraphConnector, GraphError, permissions_to_acl
from app.scale.connectors.local_folder import LocalFolderConnector


def all_changes(connector, cursor=None):
    out, batches = [], []
    for b in connector.scan(cursor or {}):
        batches.append(b)
        out.extend(b.changes)
    return out, batches


# ── AclRules ──


def test_longest_prefix_wins_and_default_is_fail_closed():
    rules = AclRules([{"prefix": "finance", "principals": ["group:finance"]},
                      {"prefix": "finance/public", "principals": ["*"]}])
    assert rules.for_path("finance/q1.xlsx") == ["group:finance"]
    assert rules.for_path("finance/public/notice.pdf") == ["*"]
    assert rules.for_path("hr/salaries.xlsx") == []
    assert rules.for_path("financeX/q1.xlsx") == []          # prefix must match a whole path segment


# ── local folder / crawl ──


@pytest.fixture
def tree(tmp_path):
    for rel, body in {"a/one.txt": "1", "a/two.txt": "22", "b/three.txt": "333", ".hidden/x.txt": "x",
                      "top.txt": "t", "Thumbs.db": "junk"}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def test_local_scan_lists_sorted_skips_hidden_and_junk(tree):
    c = LocalFolderConnector({"root": str(tree), "default_acl": ["*"]})
    changes, batches = all_changes(c)
    assert [ch.external_id for ch in changes] == ["a/one.txt", "a/two.txt", "b/three.txt", "top.txt"]
    assert batches[-1].done and batches[-1].complete_scope == ""
    assert all(ch.file.acl == ["*"] for ch in changes)
    assert changes[0].file.version.endswith(":1")


def test_resumed_scan_continues_after_cursor_and_is_not_authoritative(tree):
    c = LocalFolderConnector({"root": str(tree), "default_acl": ["*"], "batch_size": 1})
    first = next(iter(c.scan({})))
    assert [x.external_id for x in first.changes] == ["a/one.txt"] and first.cursor == {"after": "a/one.txt"}
    changes, batches = all_changes(c, first.cursor)
    assert [x.external_id for x in changes] == ["a/two.txt", "b/three.txt", "top.txt"]
    assert batches[-1].complete_scope is None             # a partial view must never delete the rest


def test_local_connector_cannot_read_outside_its_root(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, tree / "link.txt")
    c = LocalFolderConnector({"root": str(tree), "default_acl": ["*"]})
    changes, _ = all_changes(c)
    assert "link.txt" not in [x.external_id for x in changes]   # symlinks are not followed
    from app.scale.connectors.base import SourceFile
    with pytest.raises(PermissionError):
        c.open(SourceFile("../" + outside.name, "x", "x", 0, None, "v", []))


def test_smb_style_fake_filesystem_goes_through_the_same_engine():
    class FakeFS:
        files = {"": [FsEntry("Finance", True), FsEntry("readme.txt", False, 5, 10)],
                 "Finance": [FsEntry("budget.xlsx", False, 100, 20)]}

        def scandir(self, rel):
            return self.files[rel]

        def open(self, rel):
            raise NotImplementedError

    c = CrawlConnector(FakeFS(), {"acl_rules": [{"prefix": "Finance", "principals": ["group:finance"]}]})
    changes, _ = all_changes(c)
    by = {x.external_id: x.file for x in changes}
    assert by["Finance/budget.xlsx"].acl == ["group:finance"]
    assert by["readme.txt"].acl == []


def test_smb_unc_paths():
    from app.scale.connectors.smb import SmbFS

    class Fake:
        def register_session(self, *a, **k):
            self.reg = (a, k)

        def scandir(self, path):
            self.last = path
            return []

    fake = Fake()
    fs = SmbFS("fs01", "Shared", "Projects", "svc", "pw", smbclient_module=fake)
    fs.scandir("Falcon/2023")
    assert fake.last == "\\\\fs01\\Shared\\Projects\\Falcon\\2023"
    assert fake.reg[1]["username"] == "svc"


# ── Microsoft Graph ──

DRIVE = "b!drive1"


def item(i, name="f.docx", **kw):
    base = {"id": i, "name": name, "size": 10, "eTag": f'"{i}-v1"', "file": {"mimeType": "x"},
            "lastModifiedDateTime": "2026-09-01T10:00:00Z", "webUrl": "https://sp/x",
            "parentReference": {"path": "/drives/b!drive1/root:/Projects/Falcon"}}
    base.update(kw)
    return base


def perm(**kw):
    return {"roles": ["read"], **kw}


class GraphFake:
    """Records requests and plays back scripted responses."""

    def __init__(self, delta_pages, permissions, delta_status=None):
        self.delta_pages = list(delta_pages)
        self.permissions = permissions
        self.requests = []
        self.delta_status = list(delta_status or [])
        self.sleeps = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "/$batch" in url:
            body = json.loads(request.content)
            responses = []
            for r in body["requests"]:
                item_id = r["url"].split("/items/")[1].split("/")[0]
                spec = self.permissions.get(item_id, (200, []))
                status, value = spec
                responses.append({"id": r["id"], "status": status, "body": {"value": value}})
            return httpx.Response(200, json={"responses": responses})
        if "/delta" in url or "skiptoken" in url:
            if self.delta_status:
                status = self.delta_status.pop(0)
                if status == 410:
                    return httpx.Response(410, json={"error": {"code": "resyncRequired"}})
                if status == 429:
                    return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json=self.delta_pages.pop(0))
        return httpx.Response(404)


def graph(fake, **cfg):
    return GraphConnector({"drive_ids": [DRIVE], **cfg}, token_provider=lambda: "tok",
                          transport=httpx.MockTransport(fake.handler), sleep=fake.sleeps.append)


def test_graph_delta_paging_last_occurrence_wins_and_deleted_facet():
    pages = [
        {"value": [item("1"), item("2", "g.pdf")], "@odata.nextLink": f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/root/delta?skiptoken=a"},
        {"value": [item("1", eTag='"1-v2"'), {"id": "3", "name": "old.txt", "deleted": {}}, {"id": "9", "folder": {}, "name": "dir"}],
         "@odata.deltaLink": f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/root/delta?token=T2"},
    ]
    fake = GraphFake(pages, {"1": (200, [perm(grantedToV2={"group": {"id": "G1"}})]),
                             "2": (200, [perm(grantedToV2={"user": {"id": "U1", "email": "a@corp.com"}})])})
    changes, batches = all_changes(graph(fake))
    kinds = [(c.kind, c.external_id) for c in changes]
    assert (("delete", f"{DRIVE}:3")) in kinds
    assert not any("9" == c.external_id.split(":")[1] for c in changes)       # folders are not files
    one = [c for c in changes if c.external_id.endswith(":1")]
    assert one[-1].file.version == '"1-v2"'                                    # last occurrence wins
    assert one[-1].file.acl == ["group:g1"]
    assert batches[-1].done and batches[-1].cursor["drives"][DRIVE].endswith("token=T2")
    assert batches[-1].complete_scope is None                                  # a delta is not authoritative
    assert changes[0].file.path.endswith("Projects/Falcon/f.docx")


def test_graph_resumes_from_saved_delta_link():
    link = f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/root/delta?token=SAVED"
    fake = GraphFake([{"value": [], "@odata.deltaLink": link + "2"}], {})
    all_changes(graph(fake), {"drives": {DRIVE: link}})
    assert "token=SAVED" in str(fake.requests[0].url)


def test_graph_410_triggers_full_resync_that_is_authoritative_for_that_drive_only():
    link = f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/root/delta?token=STALE"
    fake = GraphFake([{"value": [item("1")], "@odata.deltaLink": link + "NEW"}],
                     {"1": (200, [perm(grantedToV2={"group": {"id": "G"}})])}, delta_status=[410])
    changes, batches = all_changes(graph(fake), {"drives": {DRIVE: link}})
    assert batches[-1].complete_scope == f"{DRIVE}:"
    assert [c.external_id for c in changes] == [f"{DRIVE}:1"]


def test_graph_honours_retry_after():
    fake = GraphFake([{"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?token=1"}], {},
                     delta_status=[429])
    all_changes(graph(fake))
    assert fake.sleeps == [7.0]


def test_graph_permission_failure_fails_closed():
    fake = GraphFake([{"value": [item("1")], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?token=1"}],
                     {"1": (403, [])})
    changes, _ = all_changes(graph(fake))
    assert changes[0].file.acl == []


def test_graph_batches_permission_requests_in_twenties():
    items = [item(str(i)) for i in range(45)]
    fake = GraphFake([{"value": items, "@odata.deltaLink": "https://graph.microsoft.com/v1.0/x?token=1"}], {})
    all_changes(graph(fake))
    batch_calls = [r for r in fake.requests if "/$batch" in str(r.url)]
    assert [len(json.loads(r.content)["requests"]) for r in batch_calls] == [20, 20, 5]


def test_graph_sends_sharing_headers_only_when_asked():
    link = "https://graph.microsoft.com/v1.0/x?token=1"
    fake = GraphFake([{"value": [], "@odata.deltaLink": link}], {})
    all_changes(graph(fake))
    assert "prefer" not in fake.requests[0].headers
    fake = GraphFake([{"value": [], "@odata.deltaLink": link}], {})
    all_changes(graph(fake, use_sharing_headers=True))
    assert "deltashowsharingchanges" in fake.requests[0].headers["prefer"]


def test_permission_mapping():
    perms = [
        perm(grantedToV2={"user": {"id": "U1", "email": "A@Corp.com"}}),
        perm(grantedToV2={"group": {"id": "G1"}}),
        perm(link={"scope": "users"}, grantedToIdentitiesV2=[{"user": {"email": "b@corp.com"}}]),
        perm(link={"scope": "organization"}),
        perm(link={"scope": "anonymous"}),
        perm(grantedToV2={"siteGroup": {"id": "5", "displayName": "Falcon Members"}}),
        {"roles": ["restricted-view"], "grantedToV2": {"user": {"id": "U9"}}},
    ]
    acl = permissions_to_acl(perms)
    assert "user:a@corp.com" in acl and "user:u1" in acl and "group:g1" in acl and "user:b@corp.com" in acl
    assert "*" not in acl                                   # links do not widen access by default
    assert not any("u9" in a for a in acl)                  # roles that grant nothing readable are ignored
    assert not any("5" in a or "falcon" in a for a in acl)  # unmapped site group grants nothing
    mapped = permissions_to_acl(perms, site_group_map={"Falcon Members": ["group:entra-falcon"]},
                                link_scopes_as_access=["organization"])
    assert "group:entra-falcon" in mapped and "*" in mapped


def test_graph_requires_a_secret_env_var():
    c = GraphConnector({"drive_ids": [DRIVE], "tenant_id": "t", "client_id": "c", "client_secret_env": "NOPE_NOT_SET"})
    with pytest.raises(GraphError):
        c._client_credentials_token()
