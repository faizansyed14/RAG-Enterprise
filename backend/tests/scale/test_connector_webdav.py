"""WebDAV connector (and the shared crawl engine in webdav.py) tests.

Most tests run against a REAL WebDAV server: wsgidav served by cheroot on a local port, mounted at /files, with
real HTTP Basic authentication. Failure injection (429 / 503, a forbidden collection, bearer checks) is done by a
tiny WSGI wrapper in front of that real server. The "vendor shape" tests at the bottom feed hand-written
207 Multi-Status documents (Nextcloud / IIS / Apache style) through an httpx.MockTransport.
"""

import base64
import itertools
import json
import os
import threading

import httpx
import pytest
from cheroot import wsgi
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.webdav import (
    DirUnreadable, NetCrawlConnector, NetEntry, WebDavConnector, parse_xml,
)

PASSWORD = "dav-pw-MUST-NOT-LEAK-42"
TOKEN = "bearer-tok-MUST-NOT-LEAK-77"


class Chaos:
    """WSGI wrapper: answer the next N requests with a canned status, forbid some paths, or demand a bearer token."""

    def __init__(self, app):
        self.app, self.script, self.forbidden, self.missing, self.bearer, self.log = app, [], set(), set(), None, []

    def __call__(self, environ, start_response):
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
        self.log.append((method, path))
        if self.bearer is not None:
            if environ.get("HTTP_AUTHORIZATION") != f"Bearer {self.bearer}":
                start_response("401 Unauthorized", [("Content-Type", "text/plain"), ("WWW-Authenticate", "Bearer")])
                return [b"token rejected"]
            environ["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(f"alice:{PASSWORD}".encode()).decode()
        if method == "PROPFIND" and path.rstrip("/") in self.forbidden:
            start_response("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"no"]
        if method == "PROPFIND" and path.rstrip("/") in self.missing:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"gone"]
        if self.script:
            status, headers = self.script.pop(0)
            start_response(status, [("Content-Type", "text/plain"), *headers])
            return [b"busy"]
        return self.app(environ, start_response)


class Dav:
    def __init__(self, root, chaos, port):
        self.root, self.chaos, self.port = root, chaos, port
        self.url = f"http://127.0.0.1:{port}/files"

    def write(self, rel, data=b"x", mtime=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def connector(self, **cfg):
        sleeps: list[float] = []
        config = {"base_url": self.url, "username": "alice", "password_env": "T_DAV_PW", "default_acl": ["*"], **cfg}
        return WebDavConnector(config, sleep=sleeps.append), sleeps


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_DAV_PW", PASSWORD)
    monkeypatch.setenv("T_DAV_TOKEN", TOKEN)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def dav(tmp_path):
    root = tmp_path / "dav"
    root.mkdir()
    app = WsgiDAVApp({
        "host": "127.0.0.1", "port": 0, "provider_mapping": {"/files": FilesystemProvider(str(root))},
        "simple_dc": {"user_mapping": {"*": {"alice": {"password": PASSWORD}}}},
        "http_authenticator": {"domain_controller": None, "accept_basic": True, "accept_digest": False,
                               "default_to_digest": False},
        "verbose": 0, "logging": {"enable": False}, "dir_browser": {"enable": False},
    })
    chaos = Chaos(app)
    server = wsgi.Server(("127.0.0.1", 0), chaos)
    server.prepare()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    yield Dav(root, chaos, server.bind_addr[1])
    server.stop()
    thread.join(5)


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def ids(changes):
    return [c.external_id for c in changes]


# ── listing ──


def test_walks_nested_collections_with_awkward_names_in_path_order(dav):
    files = {"readme.txt": "r", "docs/a b.txt": "ab", "docs/sub dir/é ü.txt": "u", "docs/sub dir/日本.md": "j",
             "100%.txt": "p", "a#b?c.txt": "q", "docs/z.txt": "z"}
    for rel, body in files.items():
        dav.write(rel, body)
    (dav.root / "empty-dir").mkdir()
    c, _ = dav.connector()
    changes, batches = scan_all(c)
    assert sorted(ids(changes)) == sorted(files)
    assert ids(changes) == sorted(ids(changes))                    # visit order == plain string order of the paths
    first = next(x.file for x in changes if x.external_id == "docs/a b.txt")
    assert (first.path, first.name, first.size, first.mime) == ("docs/a b.txt", "a b.txt", 2, "text/plain")
    assert first.modified_at is not None and first.version.endswith(":2")
    assert batches[-1].done and batches[-1].complete_scope == ""


def test_batches_follow_batch_size_since_webdav_has_no_pagination(dav):
    for i in range(7):
        dav.write(f"d{i % 2}/f{i}.txt", f"{i}")
    c, _ = dav.connector(batch_size=3)
    changes, batches = scan_all(c)
    assert [len(b.changes) for b in batches] == [3, 3, 1]
    assert [b.done for b in batches] == [False, False, True]
    assert batches[0].cursor == {"after": ids(changes)[2]} and batches[-1].cursor == {}
    json.dumps([b.cursor for b in batches])


def test_hidden_junk_and_lock_files_are_skipped(dav):
    for rel in ("keep.txt", ".hidden.txt", ".git/config", "Thumbs.db", "~$lock.docx", "sub/.DS_Store", "sub/ok.txt"):
        dav.write(rel, "x")
    c, _ = dav.connector()
    assert ids(scan_all(c)[0]) == ["keep.txt", "sub/ok.txt"]
    c2, _ = dav.connector(skip_hidden=False, skip_names=[])
    assert ".hidden.txt" in ids(scan_all(c2)[0]) and "Thumbs.db" in ids(scan_all(c2)[0])


# ── versions, deletes, resume ──


def test_version_changes_when_content_changes_even_at_the_same_size(dav):
    dav.write("a.txt", "hello", mtime=1_700_000_000)
    dav.write("b.txt", "stay", mtime=1_700_000_000)
    c, _ = dav.connector()
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}
    dav.write("a.txt", "HELLO", mtime=1_700_000_100)
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["a.txt"] != before["a.txt"] and after["b.txt"] == before["b.txt"]


def test_deleted_files_are_missing_from_an_authoritative_listing(dav):
    dav.write("keep.txt")
    gone = dav.write("sub/gone.txt")
    c, _ = dav.connector()
    indexed = set(ids(scan_all(c)[0]))
    gone.unlink()
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope == "" and indexed - set(ids(changes)) == {"sub/gone.txt"}


def test_resume_from_every_batch_boundary_is_exact_and_never_authoritative(dav):
    # names chosen so that a naive name-sort differs from path-sort: "report" the directory vs "report-2020.txt"
    for rel in ("report/z.txt", "report/a.txt", "report-2020.txt", "report.txt", "a/b/c.txt", "a/b.txt", "a b.txt",
                "a+b.txt", "a.txt", "b/x.txt", "bb.txt", "B.txt", "é.txt"):
        dav.write(rel)
    c, _ = dav.connector(batch_size=2)
    everything, batches = scan_all(c)
    assert ids(everything) == sorted(ids(everything)) and len(everything) == 13
    for k in range(len(batches) - 1):
        before = [x for b in batches[: k + 1] for x in ids(b.changes)]
        resumed, rb = scan_all(c, batches[k].cursor)
        assert before + ids(resumed) == ids(everything), f"resume after batch {k}"
        assert rb[-1].complete_scope is None


def test_a_collection_the_server_forbids_is_skipped_and_the_scan_stops_being_authoritative(dav):
    dav.write("ok.txt")
    dav.write("secret/s.txt")
    dav.write("secret/deep/d.txt")
    dav.chaos.forbidden.add("/files/secret")
    c, sleeps = dav.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["ok.txt"] and batches[-1].complete_scope is None and sleeps == []
    assert c.stats["unreadable_dir"] == 1


def test_a_collection_that_vanishes_between_listing_and_descending_counts_as_empty(dav):
    dav.write("ok.txt")
    dav.write("vanishing/v.txt")
    dav.chaos.missing.add("/files/vanishing")
    c, _ = dav.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["ok.txt"] and batches[-1].complete_scope == ""     # its files really are gone


def test_forbidden_root_is_an_auth_error_not_an_empty_share(dav):
    dav.chaos.forbidden.add("/files")
    c, _ = dav.connector()
    with pytest.raises(AuthError) as err:
        scan_all(c)
    assert err.value.status == 403


def test_missing_root_is_a_clear_error(dav):
    c, _ = dav.connector(base_url=dav.url + "/does-not-exist")
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 404


# ── permissions ──


def test_acl_rules_longest_prefix_and_fail_closed(dav):
    for rel in ("finance/q1.xlsx", "finance/public/notice.pdf", "hr/pay.csv", "readme.txt"):
        dav.write(rel)
    rules = [{"prefix": "finance", "principals": ["group:Finance"]}, {"prefix": "finance/public", "principals": ["*"]}]
    c, _ = dav.connector(default_acl=None, acl_rules=rules)
    by = {x.external_id: x.file for x in scan_all(c)[0]}
    assert by["finance/q1.xlsx"].acl == ["group:finance"] and by["finance/public/notice.pdf"].acl == ["*"]
    assert by["hr/pay.csv"].acl == [] and by["readme.txt"].acl == []
    assert c.refresh_acl(by["finance/q1.xlsx"]) == ["group:finance"] and c.refresh_acl(by["hr/pay.csv"]) == []


# ── bytes ──


def test_open_returns_exact_bytes_for_awkward_names_and_big_files(dav):
    payloads = {"a b.txt": b"hello\n", "dir/100% #1 ?.bin": bytes(range(256)) * 5 + b"\x00", "é/日本.bin": os.urandom(2_600_000)}
    for rel, data in payloads.items():
        dav.write(rel, data)
    c, _ = dav.connector()
    for change in scan_all(c)[0]:
        with c.open(change.file) as fh:
            assert fh.read() == payloads[change.external_id]
    with c.open(SourceFile("a b.txt", "a b.txt", "a b.txt", 6, None, "v", [])) as fh:    # only what the sync layer persists
        assert fh.read() == b"hello\n"


def test_open_of_a_deleted_file_is_a_404_error(dav):
    c, _ = dav.connector()
    with pytest.raises(HttpError) as err:
        c.open(SourceFile("gone.txt", "gone.txt", "gone.txt", 1, None, "v", []))
    assert err.value.status == 404


def test_oversize_files_are_skipped_in_the_scan_and_refused_by_open(dav):
    dav.write("small.txt", b"s" * 100)
    dav.write("big.bin", b"b" * 5000)
    c, _ = dav.connector(max_file_bytes=1000)
    changes, batches = scan_all(c)
    assert ids(changes) == ["small.txt"] and batches[-1].done and c.stats["oversize"] == 1
    with pytest.raises(TooLarge):
        c.open(SourceFile("big.bin", "big.bin", "big.bin", 100, None, "v", []))


# ── rate limits and errors ──


@pytest.mark.parametrize("status", ["429 Too Many Requests", "503 Service Unavailable"])
def test_rate_limit_is_retried_honouring_retry_after_for_listing_and_download(dav, status):
    dav.write("a.txt", b"payload")
    c, sleeps = dav.connector()
    dav.chaos.script = [(status, [("Retry-After", "3")])] * 2
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"] and sleeps == [3.0, 3.0]
    dav.chaos.script = [(status, [])]
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"payload"
    assert sleeps == [3.0, 3.0, 1.0]


def test_persistent_503_gives_up_with_a_clear_error(dav):
    c, sleeps = dav.connector(max_retries=2)
    dav.chaos.script = [("503 Service Unavailable", [])] * 10
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 503 and not isinstance(err.value, AuthError) and sleeps == [1, 2]


def test_wrong_password_is_an_auth_error_that_does_not_leak_it(dav, monkeypatch):
    monkeypatch.setenv("T_DAV_PW", "WRONG-" + PASSWORD)
    c, sleeps = dav.connector()
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert err.value.status == 401 and PASSWORD not in blob and "WRONG-" not in blob and sleeps == []


def test_unset_password_variable_is_named_but_no_request_is_made(dav, monkeypatch):
    monkeypatch.delenv("T_DAV_PW")
    c, _ = dav.connector()
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "T_DAV_PW" in str(err.value) and dav.chaos.log == []


def test_bearer_token_auth(dav, monkeypatch):
    dav.write("a.txt", b"bytes")
    dav.chaos.bearer = TOKEN
    c = WebDavConnector({"base_url": dav.url, "token_env": "T_DAV_TOKEN", "default_acl": ["*"]})
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"]
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"bytes"
    monkeypatch.setenv("T_DAV_TOKEN", "stale-" + TOKEN)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    assert TOKEN not in str(err.value)                           # read per request: the rotated value is picked up


def test_unreachable_server_is_retried_then_reported_without_a_traceback_of_secrets():
    sleeps: list[float] = []
    c = WebDavConnector({"base_url": "http://127.0.0.1:1/dav", "username": "alice", "password_env": "T_DAV_PW",
                         "max_retries": 2}, sleep=sleeps.append)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "ConnectError" in str(err.value) and PASSWORD not in str(err.value) and sleeps == [1, 2]


def test_secrets_never_reach_cursors_or_source_files(dav):
    dav.write("a.txt")
    dav.write("b.txt")
    c, _ = dav.connector(batch_size=1)
    changes, batches = scan_all(c)
    blob = repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)
    assert PASSWORD not in blob and TOKEN not in blob


# ── the shared engine, with a fake file system ──


class FakeFS:
    def __init__(self, tree):
        self.tree, self.listed = tree, []

    def listdir(self, rel):
        self.listed.append(rel)
        node = self.tree[rel]
        if isinstance(node, Exception):
            raise node
        return node

    def open(self, rel, max_bytes):
        raise NotImplementedError


def test_engine_aborts_instead_of_deleting_when_a_directory_listing_fails():
    fs = FakeFS({"": [NetEntry("a", True), NetEntry("f.txt", False, 1, 1)], "a": RuntimeError("connection reset")})
    with pytest.raises(RuntimeError):
        list(NetCrawlConnector(fs, {}).scan({}))        # NOT swallowed as "empty directory"


def test_engine_max_depth_marks_the_scan_partial_and_unreadable_dirs_too():
    tree = {"": [NetEntry("d1", True), NetEntry("top.txt", False, 1, 1)], "d1": [NetEntry("d2", True), NetEntry("x.txt", False, 1, 1)],
            "d1/d2": [NetEntry("deep.txt", False, 1, 1)]}
    c = NetCrawlConnector(FakeFS(tree), {"max_depth": 1, "default_acl": ["*"]})
    batches = list(c.scan({}))
    assert [x.external_id for b in batches for x in b.changes] == ["d1/x.txt", "top.txt"]
    assert batches[-1].complete_scope is None and c.stats["too_deep"] == 1
    tree["d1"] = DirUnreadable("d1")
    c = NetCrawlConnector(FakeFS(tree), {"default_acl": ["*"]})
    batches = list(c.scan({}))
    assert [x.external_id for b in batches for x in b.changes] == ["top.txt"] and batches[-1].complete_scope is None


# ── vendor-shaped Multi-Status documents through a mock transport ──

NEXTCLOUD = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
 <d:response><d:href>/remote.php/dav/files/alice/Docs/</d:href><d:propstat><d:prop>
   <d:getlastmodified>Mon, 05 Oct 2026 10:00:00 GMT</d:getlastmodified><d:resourcetype><d:collection/></d:resourcetype>
   <d:getetag>&quot;5f1c0aa1&quot;</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/files/alice/Docs/Q3%20report.docx</d:href><d:propstat><d:prop>
   <d:getlastmodified>Fri, 02 Oct 2026 09:30:00 GMT</d:getlastmodified><d:getcontentlength>48213</d:getcontentlength><d:resourcetype/>
   <d:getetag>&quot;a8b7c6d5e4f3&quot;</d:getetag>
   <d:getcontenttype>application/vnd.openxmlformats-officedocument.wordprocessingml.document</d:getcontenttype></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat><d:propstat><d:prop><oc:checksums/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/files/alice/Docs/Archive/</d:href><d:propstat><d:prop>
   <d:getlastmodified>Fri, 02 Oct 2026 09:30:00 GMT</d:getlastmodified><d:resourcetype><d:collection/></d:resourcetype><d:getetag>&quot;c0ffee&quot;</d:getetag></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat><d:propstat><d:prop><d:getcontentlength/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>
</d:multistatus>"""

IIS = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
<D:response><D:href>http://iis.corp.local/share/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>http://iis.corp.local/share/plan.pdf</D:href><D:propstat><D:prop>
 <D:getcontentlength>1024</D:getcontentlength><D:getetag>W/"0x8DCAB"</D:getetag><D:getlastmodified>Tue, 01 Sep 2026 08:00:00 GMT</D:getlastmodified>
 </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>http://evil.example.net/share/stolen.txt</D:href><D:propstat><D:prop><D:getcontentlength>5</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/share/../../etc/passwd</D:href><D:propstat><D:prop><D:getcontentlength>5</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/share/sub/nested.txt</D:href><D:propstat><D:prop><D:getcontentlength>5</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/other/outside.txt</D:href><D:propstat><D:prop><D:getcontentlength>5</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/share/gone.txt</D:href><D:status>HTTP/1.1 404 Not Found</D:status></D:response>
<D:response><D:href>/share/no-etag.txt</D:href><D:propstat><D:prop><D:getcontentlength>7</D:getcontentlength><D:getlastmodified>Tue, 01 Sep 2026 08:00:00 GMT</D:getlastmodified></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
</D:multistatus>"""


APACHE = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:ns0="DAV:">
<D:response><D:href>/dav/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
<D:response><D:href>/dav/notes.txt</D:href>
 <D:propstat><D:prop><D:getetag/><D:getcontenttype/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>
 <D:propstat><D:prop><D:getcontentlength>12</D:getcontentlength><D:getetag>"9c-5f3a1b2c4d5e6"</D:getetag><D:getcontenttype>text/plain</D:getcontenttype>
   <D:getlastmodified>Wed, 07 Oct 2026 12:00:00 GMT</D:getlastmodified><D:resourcetype/></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>
</D:multistatus>"""


def mock_connector(body, base_url, **cfg):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers.get("depth")))
        return httpx.Response(207, content=body, headers={"Content-Type": "application/xml"})

    c = WebDavConnector({"base_url": base_url, "default_acl": ["*"], **cfg}, transport=httpx.MockTransport(handler))
    return c, seen


def test_nextcloud_shaped_response_parses_etag_size_type_and_skips_the_collection_itself():
    c, seen = mock_connector(NEXTCLOUD, "https://cloud.corp.example/remote.php/dav/files/alice/Docs")
    # the walk would descend into Archive/; answer it with the same document -> it lists "Docs" again, which is
    # not a child of Archive, so nothing comes back (and nothing loops)
    changes, _ = scan_all(c)
    assert ids(changes) == ["Q3 report.docx"]
    f = changes[0].file
    assert f.version == "a8b7c6d5e4f3:48213" and f.size == 48213 and f.mime.endswith("wordprocessingml.document")
    assert f.modified_at.year == 2026 and f.modified_at.tzinfo is not None
    assert seen[0][0] == "PROPFIND" and seen[0][2] == "1" and seen[0][1].endswith("/Docs/")
    assert seen[1][1].endswith("/Docs/Archive/")


def test_iis_shaped_response_ignores_foreign_hosts_traversal_nested_and_error_entries():
    c, _ = mock_connector(IIS, "http://iis.corp.local/share")
    changes, _ = scan_all(c)
    by = {x.external_id: x.file for x in changes}
    assert sorted(by) == ["no-etag.txt", "plan.pdf"]                      # nothing foreign, nothing outside, no 404 entry
    assert by["plan.pdf"].version == '0x8DCAB:1024'                        # weak marker and quotes stripped
    assert by["no-etag.txt"].version == f"{int(by['no-etag.txt'].modified_at.timestamp() * 1000)}:7"   # last-modified fallback


def test_apache_shaped_response_takes_values_from_the_2xx_propstat_not_the_empty_404_one():
    c, _ = mock_connector(APACHE, "http://h/dav")
    f = scan_all(c)[0][0].file
    assert (f.external_id, f.size, f.mime, f.version) == ("notes.txt", 12, "text/plain", "9c-5f3a1b2c4d5e6:12")


def test_xml_with_a_doctype_or_entities_is_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><d:multistatus xmlns:d="DAV:">&a;</d:multistatus>'
    with pytest.raises(HttpError, match="DOCTYPE"):
        parse_xml(evil)
    with pytest.raises(HttpError, match="malformed"):
        parse_xml(b"<d:multistatus")
    c, _ = mock_connector(evil, "http://h/dav")
    with pytest.raises(HttpError):
        scan_all(c)
