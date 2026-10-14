"""Dropbox connector tests (MOCK fixtures, not a real server).

There is no pip-installable Dropbox emulator, so these tests run the connector against `FakeDropbox`: a stateful, in-memory
imitation of the Dropbox API v2 HTTP endpoints behind an httpx.MockTransport. It implements files/list_folder (recursive, `limit`,
`include_non_downloadable_files`, path errors), files/list_folder/continue (both for the pages of a listing and for the change feed
of a saved cursor, with the `reset` error and a plain-text 400 for malformed cursors), files/list_revisions (200 for files,
409 path/not_file for folders, 409 path/not_found for unknown paths), content.dropboxapi.com files/download (argument in the
Dropbox-API-Arg header, empty body), the oauth2/token refresh-token grant (Basic app key:secret, form body), Bearer authentication
and 429 bodies. Entries use the documented shapes (".tag": file | folder | deleted, path_lower, path_display, rev, content_hash,
is_downloadable, ...). Like the real service, the feed reports a deleted FOLDER as one `deleted` entry and says nothing about its
children. Passing here proves the connector's logic against these shapes; it does not prove Dropbox behaves identically.
"""

import base64
import hashlib
import itertools
import json
import time

import httpx
import pytest

from app.scale import sync
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.dropbox import DropboxConnector

TOKEN = "sl.ACCESS-TOKEN-MUST-NOT-LEAK"
REFRESH = "REFRESH-TOKEN-MUST-NOT-LEAK"
APP_KEY, APP_SECRET = "app-key-123", "APP-SECRET-MUST-NOT-LEAK"


def api_error(summary, tag):
    return {"error_summary": summary, "error": tag}


NOT_FOUND = api_error("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})
NOT_FILE = api_error("path/not_file/..", {".tag": "path", "path": {".tag": "not_file"}})
RESET = api_error("reset/..", {".tag": "reset"})


def too_many(retry_after=3):
    return api_error("too_many_requests/..", {"reason": {".tag": "too_many_requests"}, "retry_after": retry_after})


class FakeDropbox:
    def __init__(self):
        self.nodes: dict[str, dict] = {}            # path_lower -> node
        self.seen_kind: dict[str, str] = {}         # path_lower -> "file" | "folder", even after deletion
        self.last_display: dict[str, str] = {}
        self.journal: list[tuple[str, dict]] = []   # (path_lower, entry as the feed reports it)
        self.counter = itertools.count(1)
        self.requests: list[httpx.Request] = []
        self.script: list[tuple[str, int, dict, dict | str]] = []    # (route substring, status, headers, body), used once
        self.snapshots: dict[int, tuple[list[dict], int, int]] = {}
        self.dead_cursors: set[str] = set()
        self.feed_page = 1000
        self.expire_feed = False
        self.raw_listing: list[dict] = []           # hand-written entries appended to every full listing
        self.leak_non_downloadable = False
        self.valid_token = TOKEN
        self.token_calls: list[dict] = []
        self.token_lifetime = 14400
        self.token_reject = False

    # ── state ──

    def _entry(self, node):
        lower = node["display"].lower()
        base = {"name": node["display"].rsplit("/", 1)[-1], "path_lower": lower, "path_display": node["display"],
                "id": "id:" + hashlib.md5(lower.encode()).hexdigest()[:12]}
        if node["kind"] == "folder":
            return {".tag": "folder", **base}
        return {".tag": "file", **base, "client_modified": "2026-09-01T09:00:00Z", "server_modified": node["mtime"],
                "rev": node["rev"], "size": len(node["data"]), "is_downloadable": node["downloadable"],
                "content_hash": hashlib.sha256(node["data"]).hexdigest()}

    def _record(self, node):
        lower = node["display"].lower()
        self.seen_kind[lower] = node["kind"]
        self.last_display[lower] = node["display"]
        self.journal.append((lower, self._entry(node)))

    def mkdir(self, display):
        lower = display.lower()
        if lower in self.nodes:
            return
        parent = display.rsplit("/", 1)[0]
        if parent:
            self.mkdir(parent)
        self.nodes[lower] = {"kind": "folder", "display": display}
        self._record(self.nodes[lower])

    def put(self, display, data=b"", *, downloadable=True, mtime="2026-09-01T10:00:00Z", rev=None):
        self.mkdir(display.rsplit("/", 1)[0]) if display.rsplit("/", 1)[0] else None
        node = {"kind": "file", "display": display, "data": data, "downloadable": downloadable, "mtime": mtime,
                "rev": rev or f"{next(self.counter):012x}"}
        self.nodes[display.lower()] = node
        self._record(node)

    def remove(self, display):
        lower = display.lower()
        node = self.nodes[lower]
        for key in [k for k in self.nodes if k == lower or k.startswith(lower + "/")]:
            del self.nodes[key]
        # like the real feed: one `deleted` entry for the path that was deleted, nothing for the children of a folder
        self.journal.append((lower, {".tag": "deleted", "name": node["display"].rsplit("/", 1)[-1], "path_lower": lower,
                                     "path_display": node["display"]}))

    def routes(self, route):
        return [r for r in self.requests if r.url.path == f"/2/{route}"]

    def bodies(self, route):
        return [json.loads(r.content) for r in self.routes(route)]

    # ── cursors ──

    @staticmethod
    def cursor(**kw):
        return "cur-" + base64.urlsafe_b64encode(json.dumps(kw).encode()).decode()

    @staticmethod
    def parse(cursor):
        return json.loads(base64.urlsafe_b64decode(cursor[4:]))

    # ── protocol ──

    def respond(self, status, body, headers=None):
        return httpx.Response(status, json=body, headers=headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth2/token":
            return self.token_endpoint(request)
        for i, (needle, status, headers, body) in enumerate(self.script):
            if needle in path:
                self.script.pop(i)
                return httpx.Response(status, headers=headers, content=body if isinstance(body, str) else json.dumps(body))
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            return self.respond(401, api_error("invalid_access_token/..", {".tag": "invalid_access_token"}))
        if request.url.host == "content.dropboxapi.com":
            assert path == "/2/files/download" and request.method == "POST"
            return self.download(request)
        assert request.url.host == "api.dropboxapi.com" and request.method == "POST", request.url
        assert request.headers["content-type"].startswith("application/json")
        body = json.loads(request.content)
        route = path[len("/2/"):]
        return getattr(self, "r_" + route.replace("/", "_"))(body)

    def token_endpoint(self, request):
        scheme, _, credentials = request.headers["authorization"].partition(" ")
        assert scheme == "Basic"
        basic = base64.b64decode(credentials).decode()
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        self.token_calls.append({"basic": basic, **form})
        if self.token_reject or basic != f"{APP_KEY}:{APP_SECRET}" or form["refresh_token"] != REFRESH:
            return self.respond(400, {"error": "invalid_grant", "error_description": "refresh token is invalid or revoked"})
        self.valid_token = f"minted-{len(self.token_calls)}"
        return self.respond(200, {"access_token": self.valid_token, "token_type": "bearer", "expires_in": self.token_lifetime})

    def under(self, root, lower):
        return not root or lower == root or lower.startswith(root + "/")

    def r_files_list_folder(self, body):
        assert body["recursive"] is True and body["include_deleted"] is False
        root = body["path"].lower()
        if root and root not in self.nodes:
            return self.respond(409, NOT_FOUND)
        if root and self.nodes[root]["kind"] != "folder":
            return self.respond(409, api_error("path/not_folder/..", {".tag": "path", "path": {".tag": "not_folder"}}))
        limit = body["limit"]
        assert 1 <= limit <= 2000
        rows = [self._entry(n) for key, n in sorted(self.nodes.items()) if key != root and self.under(root, key)]
        if not body.get("include_non_downloadable_files", True) and not self.leak_non_downloadable:
            rows = [r for r in rows if r.get("is_downloadable", True)]
        rows += self.raw_listing
        snap = next(self.counter)
        self.snapshots[snap] = (rows, len(self.journal), root)
        return self.page_of_snapshot(snap, 0, limit)

    def page_of_snapshot(self, snap, offset, limit):
        rows, head, root = self.snapshots[snap]
        chunk = rows[offset: offset + limit]
        if offset + limit < len(rows):
            return self.respond(200, {"entries": chunk, "has_more": True,
                                      "cursor": self.cursor(k="snap", snap=snap, offset=offset + limit, limit=limit)})
        return self.respond(200, {"entries": chunk, "has_more": False, "cursor": self.cursor(k="feed", start=head, end=None, skip=0, root=root)})

    def r_files_list_folder_continue(self, body):
        cursor = body["cursor"]
        if not cursor.startswith("cur-"):
            return httpx.Response(400, text='Error in call to API function "files/list_folder/continue": request body: cursor: Invalid cursor')
        if cursor in self.dead_cursors:
            return self.respond(409, RESET)
        state = self.parse(cursor)
        if state["k"] == "snap":
            return self.page_of_snapshot(state["snap"], state["offset"], state["limit"])
        if self.expire_feed:
            return self.respond(409, RESET)
        end = len(self.journal) if state["end"] is None else state["end"]
        latest: dict[str, dict] = {}
        for lower, entry in self.journal[state["start"]: end]:
            latest.pop(lower, None)
            latest[lower] = entry
        rows = [e for lower, e in latest.items() if self.under(state["root"], lower) and lower != state["root"]]
        chunk = rows[state["skip"]: state["skip"] + self.feed_page]
        if state["skip"] + self.feed_page < len(rows):
            nxt = self.cursor(k="feed", start=state["start"], end=end, skip=state["skip"] + self.feed_page, root=state["root"])
            return self.respond(200, {"entries": chunk, "has_more": True, "cursor": nxt})
        return self.respond(200, {"entries": chunk, "has_more": False, "cursor": self.cursor(k="feed", start=end, end=None, skip=0, root=state["root"])})

    def r_files_list_revisions(self, body):
        lower = body["path"].lower()
        kind = self.seen_kind.get(lower)
        if kind is None:
            return self.respond(409, NOT_FOUND)
        if kind == "folder":
            return self.respond(409, NOT_FILE)
        return self.respond(200, {"is_deleted": lower not in self.nodes, "entries": [{"name": lower.rsplit("/", 1)[-1]}]})

    def download(self, request):
        assert request.content == b"", "download takes its argument in the Dropbox-API-Arg header, not in a body"
        arg = json.loads(request.headers["dropbox-api-arg"])
        node = self.nodes.get(arg["path"].lower())
        if node is None:
            return self.respond(409, NOT_FOUND)
        if node["kind"] != "file":
            return self.respond(409, NOT_FILE)
        if not node["downloadable"]:
            return self.respond(409, api_error("path/unsupported_file/..", {".tag": "path", "path": {".tag": "unsupported_file"}}))
        return httpx.Response(200, content=node["data"], headers={"dropbox-api-result": json.dumps(self._entry(node)),
                                                                   "content-type": "application/octet-stream"})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_DBX_TOKEN", TOKEN)


@pytest.fixture
def db():
    return FakeDropbox()


def make(db, **cfg):
    sleeps: list[float] = []
    clock = cfg.pop("clock", None) or Clock()
    config = {"access_token_env": "T_DBX_TOKEN", "default_acl": ["*"], **cfg}
    c = DropboxConnector(config, transport=httpx.MockTransport(db.handler), sleep=sleeps.append, clock=clock)
    c.sleeps, c.clock = sleeps, clock
    return c


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 200))
    assert len(batches) < 200, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def by_id(changes):
    return {c.external_id: c for c in changes}


def populate(db):
    db.put("/Docs/readme.txt", b"hello")
    db.put("/Docs/2026/q1.pdf", b"%PDF-1.4 q1")
    db.put("/Docs/2026/q2.pdf", b"%PDF-1.4 q2")
    db.put("/Other/x.md", b"# x")
    db.put("/top.txt", b"top")
    return db


ALL = {"/docs/readme.txt", "/docs/2026/q1.pdf", "/docs/2026/q2.pdf", "/other/x.md", "/top.txt"}


def finished_cursor(connector):
    return scan_all(connector)[1][-1].cursor


# ── listing and pagination ──


def test_full_scan_pages_through_the_listing_and_maps_entries(db):
    populate(db)
    for i in range(6):
        db.put(f"/Docs/extra{i}.txt", f"x{i}".encode())
    c = make(db, page_size=4)
    changes, batches = scan_all(c)
    got = {x.external_id: x.file for x in changes}
    assert set(got) == ALL | {f"/docs/extra{i}.txt" for i in range(6)}            # folders are not files
    assert all(x.kind == "upsert" for x in changes)
    f = got["/docs/2026/q1.pdf"]
    assert (f.path, f.name, f.size, f.mime) == ("Docs/2026/q1.pdf", "q1.pdf", 11, "application/pdf")
    assert f.modified_at.isoformat() == "2026-09-01T10:00:00+00:00"
    assert len(batches) >= 3 and [b.done for b in batches] == [False] * (len(batches) - 1) + [True]
    first = db.bodies("files/list_folder")
    assert len(first) == 1 and first[0]["path"] == "" and first[0]["limit"] == 4 and first[0]["include_non_downloadable_files"] is False
    conts = db.bodies("files/list_folder/continue")
    assert [b["cursor"] for b in conts] == [b.cursor["cursor"] for b in batches[:-1]]       # each page continued from the last cursor
    assert all(b.cursor["mode"] == "full" for b in batches[:-1])
    last = batches[-1]
    assert last.complete_scope == "" and all(b.complete_scope is None for b in batches[:-1])
    assert last.cursor == {"cursor": last.cursor["cursor"], "last_full": c.clock.now}
    json.dumps([b.cursor for b in batches])


def test_page_size_is_capped_at_the_api_maximum(db):
    populate(db)
    scan_all(make(db, page_size=999_999))
    assert db.bodies("files/list_folder")[0]["limit"] == 2000
    scan_all(make(db))
    assert db.bodies("files/list_folder")[1]["limit"] == 2000


def test_root_path_limits_the_scan_to_a_folder_and_paths_are_relative_to_it(db):
    db.put("/Team/Docs/a.txt", b"a")
    db.put("/Team/b.txt", b"b")
    db.put("/Elsewhere/c.txt", b"c")
    for root in ("Team", "/team/", "/Team"):
        c = make(db, root_path=root)
        changes, _ = scan_all(c)
        assert {x.external_id: x.file.path for x in changes} == {"/team/docs/a.txt": "Docs/a.txt", "/team/b.txt": "b.txt"}
    assert db.bodies("files/list_folder")[0]["path"] == "/Team"                       # leading slash, no trailing one


def test_a_missing_root_folder_aborts_the_scan_instead_of_looking_like_an_empty_source(db):
    populate(db)
    c = make(db, root_path="/gone")
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 409 and "not_found" in str(err.value)
    seen = []
    with pytest.raises(HttpError):
        for batch in c.scan({}):
            seen.append(batch)
    assert not any(b.done or b.complete_scope is not None for b in seen)               # nothing could delete the index


def test_unicode_and_case_are_handled_path_lower_is_the_id_display_is_the_path(db):
    db.put("/Docs/Übersicht ÄÖ 日本語.txt", "größe".encode())
    c = make(db)
    changes, _ = scan_all(c)
    (change,) = changes
    assert change.external_id == "/docs/übersicht äö 日本語.txt" and change.file.path == "Docs/Übersicht ÄÖ 日本語.txt"
    with c.open(change.file) as fh:
        assert fh.read() == "größe".encode()
    assert db.routes("files/download")[0].headers["dropbox-api-arg"].isascii()          # escaped, as the API requires


def test_page_failure_mid_scan_propagates_and_no_batch_claims_completion(db):
    for i in range(7):
        db.put(f"/f{i}.txt", b"x")
    c = make(db, page_size=2)
    db.script.append(("list_folder/continue", 500, {}, "boom"))
    stream = c.scan({})
    got = [next(stream)]
    with pytest.raises(HttpError) as err:
        got.append(next(stream))
    assert err.value.status == 500 and not any(b.done or b.complete_scope is not None for b in got)


def test_entries_with_missing_fields_or_unexpected_tags_do_not_break_the_scan(db):
    db.put("/ok.txt", b"ok")
    db.raw_listing = [
        {".tag": "file", "path_lower": "/bare.txt"},                                   # no name, size, rev, hash, display
        {".tag": "file", "path_display": "/Only/Display.TXT"},                         # no path_lower at all
        {".tag": "file", "name": "nopath.txt"},                                        # no path at all
        {".tag": "deleted", "path_lower": "/ghost.txt"},                               # not valid in a full listing
        {".tag": "something_new", "path_lower": "/new-kind.txt"},
        {"path_lower": "/untagged.txt"},
    ]
    c = make(db)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert set(got) == {"/ok.txt", "/bare.txt", "/only/display.txt"}
    assert (got["/only/display.txt"].path, got["/only/display.txt"].name) == ("Only/Display.TXT", "Display.TXT")
    assert (got["/bare.txt"].path, got["/bare.txt"].name, got["/bare.txt"].size) == ("bare.txt", "bare.txt", 0)
    assert got["/bare.txt"].modified_at is None and c.stats["malformed"] == 1


# ── versions ──


def test_version_changes_iff_the_content_changes(db):
    populate(db)
    c = make(db)
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}              # stable across scans
    db.put("/Docs/readme.txt", b"HELLO")                                                  # same size, different bytes
    db.put("/top.txt", b"top", mtime="2026-10-01T00:00:00Z")                              # saved again, same content
    db.put("/other/x.md", b"# x changed")                                                # different size
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["/docs/readme.txt"] != before["/docs/readme.txt"] and after["/other/x.md"] != before["/other/x.md"]
    assert after["/top.txt"] == before["/top.txt"] and after["/docs/2026/q1.pdf"] == before["/docs/2026/q1.pdf"]


def test_version_falls_back_to_the_revision_without_a_content_hash(db):
    db.raw_listing = [{".tag": "file", "name": "a.txt", "path_lower": "/a.txt", "path_display": "/a.txt", "rev": "r1", "size": 3},
                      {".tag": "file", "name": "b.txt", "path_lower": "/b.txt", "path_display": "/b.txt", "size": 3,
                       "server_modified": "2026-09-01T10:00:00Z"}]
    c = make(db)
    got = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert got["/a.txt"] == "r1:3" and got["/b.txt"] == "2026-09-01T10:00:00Z:3"
    db.raw_listing[0]["rev"] = "r2"
    assert {x.external_id: x.file.version for x in scan_all(c)[0]}["/a.txt"] == "r2:3"


# ── skipped items ──


def test_non_downloadable_junk_and_oversize_files_are_skipped(db):
    db.put("/ok.txt", b"ok")
    db.put("/paper.paper", b"p", downloadable=False)
    db.put("/Thumbs.db", b"j")
    db.put("/sub/.DS_Store", b"j")
    db.put("/~$lock.docx", b"j")
    db.put("/big.bin", b"b" * 50)
    db.leak_non_downloadable = True                                    # even if the server lists it, it must not be indexed
    c = make(db, max_file_bytes=20)
    changes, _ = scan_all(c)
    assert list(by_id(changes)) == ["/ok.txt"]
    assert c.stats == {"not_downloadable": 1, "skipped_name": 3, "oversize": 1}
    everything = make(db, skip_names=[], max_file_bytes=1000)
    assert set(by_id(scan_all(everything)[0])) == {"/ok.txt", "/thumbs.db", "/sub/.ds_store", "/~$lock.docx", "/big.bin"}
    custom = make(db, skip_names=["*.txt"], max_file_bytes=1000)
    assert "/ok.txt" not in by_id(scan_all(custom)[0]) and "/big.bin" in by_id(scan_all(custom)[0])


def test_a_file_exactly_at_max_file_bytes_is_still_indexed(db):
    db.put("/exact.bin", b"e" * 20)
    db.put("/over.bin", b"o" * 21)
    c = make(db, max_file_bytes=20)
    assert list(by_id(scan_all(c)[0])) == ["/exact.bin"] and c.stats == {"oversize": 1}


# ── deletions ──


def test_full_scan_is_authoritative_so_removed_files_are_simply_absent(db):
    populate(db)
    c = make(db)
    first = set(by_id(scan_all(c)[0]))
    db.remove("/Docs/2026/q1.pdf")
    db.remove("/Other")
    changes, batches = scan_all(c)
    assert first - set(by_id(changes)) == {"/docs/2026/q1.pdf", "/other/x.md"} and batches[-1].complete_scope == ""


# ── resume ──


def test_a_crashed_full_scan_resumes_at_the_saved_cursor_and_is_not_authoritative(db):
    for i in range(9):
        db.put(f"/f{i}.txt", b"x")
    c = make(db, page_size=3)
    stream = c.scan({})
    first = next(stream)
    stream.close()                                                                       # crash after one batch
    c2 = make(db, page_size=3, clock=Clock(1_800_000_999.0))
    json_cursor = json.loads(json.dumps(first.cursor))                                   # it went through the database
    changes, batches = scan_all(c2, json_cursor)
    seen = list(by_id(first.changes)) + list(by_id(changes))
    assert sorted(seen) == sorted(f"/f{i}.txt" for i in range(9)) and len(seen) == 9     # no repeats, no gaps
    assert len(db.routes("files/list_folder")) == 1                                      # the listing was not restarted
    assert batches[-1].done and batches[-1].complete_scope is None
    assert batches[-1].cursor["last_full"] == first.cursor["started"]


# ── incremental feed ──


def test_a_finished_cursor_makes_the_next_scan_a_feed_read_of_only_the_changes(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.put("/Docs/new.txt", b"n")
    db.put("/Docs/readme.txt", b"HELLO")
    db.remove("/Docs/2026/q2.pdf")
    before = len(db.routes("files/list_folder"))
    changes, batches = scan_all(c, cur)
    assert [(x.kind, x.external_id) for x in changes] == [("upsert", "/docs/new.txt"), ("upsert", "/docs/readme.txt"),
                                                         ("delete", "/docs/2026/q2.pdf")]
    assert by_id(changes)["/docs/readme.txt"].file.size == 5
    assert len(db.routes("files/list_folder")) == before                                 # no new full listing
    assert db.bodies("files/list_folder/continue")[-1]["cursor"] == cur["cursor"]
    assert len(batches) == 1 and batches[0].done and batches[0].complete_scope is None
    assert batches[0].cursor["cursor"] != cur["cursor"] and batches[0].cursor["last_full"] == cur["last_full"]
    assert scan_all(c, batches[0].cursor)[0] == []                                       # nothing new: nothing reported
    json.dumps(batches[0].cursor)


def test_the_feed_is_paged_and_resumable_and_reports_every_change_once(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    for i in range(7):
        db.put(f"/n{i}.txt", f"v{i}".encode())
    db.feed_page = 2
    changes, batches = scan_all(c, cur)
    assert sorted(by_id(changes)) == [f"/n{i}.txt" for i in range(7)] and len(changes) == 7
    assert len(batches) == 4 and [b.done for b in batches] == [False] * 3 + [True]
    assert all(b.cursor["mode"] == "inc" for b in batches[:-1]) and all(b.complete_scope is None for b in batches)
    stream = c.scan(cur)
    first = next(stream)
    stream.close()                                                                       # crash in the middle of the feed
    rest, rest_batches = scan_all(make(db), json.loads(json.dumps(first.cursor)))
    assert sorted(list(by_id(first.changes)) + list(by_id(rest))) == [f"/n{i}.txt" for i in range(7)]
    assert len(first.changes) + len(rest) == 7 and rest_batches[-1].done
    assert "last_full" in rest_batches[-1].cursor and rest_batches[-1].cursor["cursor"] == batches[-1].cursor["cursor"]


def test_a_cursor_from_the_middle_of_a_feed_is_resumed_even_when_a_full_scan_would_otherwise_be_due(db):
    populate(db)
    c = make(db, full_resync_hours=1)
    cur = finished_cursor(c)
    for i in range(4):
        db.put(f"/n{i}.txt", b"x")
    db.feed_page = 2
    stream = c.scan(cur)
    first = next(stream)
    stream.close()
    c.clock.now += 100 * 3600                                                            # the crash lasted days
    before = len(db.routes("files/list_folder"))
    rest, batches = scan_all(c, first.cursor)
    assert len(db.routes("files/list_folder")) == before and batches[-1].complete_scope is None
    assert sorted(list(by_id(first.changes)) + list(by_id(rest))) == [f"/n{i}.txt" for i in range(4)]
    assert scan_all(c, batches[-1].cursor)[1][-1].complete_scope == ""                  # ... and the next scan is the overdue full one


def test_several_events_for_one_path_in_a_page_collapse_to_the_last_one(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    state = db.parse(cur["cursor"])
    db.put("/top.txt", b"v2")
    db.remove("/top.txt")
    db.put("/gone.txt", b"g")
    db.remove("/gone.txt")
    db.put("/back.txt", b"1")
    db.remove("/back.txt")
    db.put("/back.txt", b"2")
    # hand the feed through unmerged, the way a server that does not compact might
    db.r_files_list_folder_continue = lambda body, orig=db.r_files_list_folder_continue: db.respond(200, {
        "entries": [e for _, e in db.journal[state["start"]:] if e["path_lower"] in ("/top.txt", "/gone.txt", "/back.txt")],
        "has_more": False, "cursor": db.cursor(k="feed", start=len(db.journal), end=None, skip=0, root="")})
    changes, _ = scan_all(c, cur)
    assert [(x.kind, x.external_id) for x in changes] == [("delete", "/top.txt"), ("delete", "/gone.txt"), ("upsert", "/back.txt")]


def test_feed_events_for_folders_are_ignored(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.mkdir("/Docs/brand-new-folder")
    changes, batches = scan_all(c, cur)
    assert changes == [] and batches[-1].done


def test_a_file_that_becomes_unindexable_is_deleted_by_the_feed(db):
    populate(db)
    c = make(db, max_file_bytes=100)
    cur = finished_cursor(c)
    db.put("/Docs/readme.txt", b"x" * 500)                                              # grew past max_file_bytes
    db.put("/top.txt", b"top", downloadable=False)
    changes, _ = scan_all(c, cur)
    assert [(x.kind, x.external_id) for x in changes] == [("delete", "/docs/readme.txt"), ("delete", "/top.txt")]


def test_changes_outside_the_root_path_never_appear_in_the_feed(db):
    db.put("/Team/a.txt", b"a")
    db.put("/Elsewhere/b.txt", b"b")
    c = make(db, root_path="/Team")
    cur = finished_cursor(c)
    db.put("/Team/new.txt", b"n")
    db.put("/Elsewhere/new.txt", b"n")
    changes, _ = scan_all(c, cur)
    assert [(x.kind, x.external_id, x.file.path) for x in changes] == [("upsert", "/team/new.txt", "new.txt")]


# ── deleted folders ──


def test_a_deleted_folder_is_classified_and_its_prefix_is_declared_complete(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.remove("/Docs/2026")                                                              # feed: one entry, no children
    db.remove("/top.txt")
    changes, batches = scan_all(c, cur)
    assert [b.complete_scope for b in batches] == ["/docs/2026/", None] and batches[-1].done
    assert batches[0].changes == [] and not batches[0].done
    assert batches[0].cursor == {"mode": "inc", "cursor": cur["cursor"], "last_full": cur["last_full"]}   # a crash repeats the page
    assert sorted((x.kind, x.external_id) for x in changes) == [("delete", "/docs/2026"), ("delete", "/top.txt")]
    assert sorted(b["path"] for b in db.bodies("files/list_revisions")) == ["/docs/2026", "/top.txt"]
    assert all(b["mode"] == "path" for b in db.bodies("files/list_revisions"))


def test_the_folder_batches_are_idempotent_if_the_scan_is_repeated_after_a_crash(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.remove("/Docs/2026")
    stream = c.scan(cur)
    first = next(stream)
    stream.close()
    again_changes, again = scan_all(make(db), first.cursor)
    assert [b.complete_scope for b in again] == ["/docs/2026/", None] and again[-1].done
    assert [(x.kind, x.external_id) for x in again_changes] == [("delete", "/docs/2026")]


def test_a_delete_whose_kind_cannot_be_told_is_treated_as_a_possible_folder(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.journal.append(("/never-seen", {".tag": "deleted", "name": "never-seen", "path_lower": "/never-seen",
                                       "path_display": "/Never-Seen"}))
    changes, batches = scan_all(c, cur)                                                  # list_revisions says path/not_found
    assert [b.complete_scope for b in batches] == ["/never-seen/", None]
    assert [(x.kind, x.external_id) for x in changes] == [("delete", "/never-seen")]


def test_too_many_deleted_paths_force_the_next_scan_to_be_a_full_one(db):
    for i in range(5):
        db.put(f"/f{i}.txt", b"x")
    c = make(db, deleted_lookup_limit=2)
    cur = finished_cursor(c)
    for i in range(4):
        db.remove(f"/f{i}.txt")
    changes, batches = scan_all(c, cur)
    assert sorted(x.external_id for x in changes if x.kind == "delete") == [f"/f{i}.txt" for i in range(4)]
    assert len(db.routes("files/list_revisions")) == 2                                  # the limit held
    assert batches[-1].cursor["force_full"] is True
    before = len(db.routes("files/list_folder"))
    changes, batches = scan_all(c, batches[-1].cursor)
    assert len(db.routes("files/list_folder")) == before + 1 and batches[-1].complete_scope == ""
    assert "force_full" not in batches[-1].cursor and list(by_id(changes)) == ["/f4.txt"]


# ── cursors that stop working ──


def test_a_reset_feed_cursor_falls_back_to_an_authoritative_full_scan(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    db.put("/new.txt", b"n")
    db.expire_feed = True
    changes, batches = scan_all(c, cur)
    assert set(by_id(changes)) == ALL | {"/new.txt"} and batches[-1].complete_scope == ""
    assert len(db.routes("files/list_folder")) == 2


def test_a_malformed_cursor_is_treated_like_a_reset_one(db):
    populate(db)
    c = make(db)
    changes, batches = scan_all(c, {"cursor": "garbage", "last_full": c.clock.now})
    assert set(by_id(changes)) == ALL and batches[-1].complete_scope == ""


def test_a_reset_in_the_middle_of_a_resumed_full_scan_restarts_it(db):
    for i in range(6):
        db.put(f"/f{i}.txt", b"x")
    c = make(db, page_size=2)
    stream = c.scan({})
    first = next(stream)
    stream.close()
    db.dead_cursors.add(first.cursor["cursor"])
    changes, batches = scan_all(c, first.cursor)
    assert sorted(by_id(changes)) == [f"/f{i}.txt" for i in range(6)] and batches[-1].complete_scope == ""


def test_a_reset_after_some_feed_pages_were_delivered_restarts_with_a_full_scan(db):
    populate(db)
    c = make(db)
    cur = finished_cursor(c)
    for i in range(4):
        db.put(f"/n{i}.txt", b"x")
    db.feed_page = 2
    stream = c.scan(cur)
    first = next(stream)
    db.dead_cursors.add(first.cursor["cursor"])
    rest = list(stream)
    assert rest[-1].complete_scope == "" and rest[-1].done
    assert set(by_id([c for b in rest for c in b.changes])) == ALL | {f"/n{i}.txt" for i in range(4)}


def test_full_resync_hours_forces_a_periodic_full_scan(db):
    populate(db)
    c = make(db, full_resync_hours=24)
    cur = finished_cursor(c)
    c.clock.now += 23 * 3600
    _, batches = scan_all(c, cur)
    assert batches[-1].complete_scope is None and len(db.routes("files/list_folder")) == 1
    c.clock.now += 2 * 3600
    changes, batches = scan_all(c, cur)
    assert batches[-1].complete_scope == "" and len(db.routes("files/list_folder")) == 2 and set(by_id(changes)) == ALL
    assert batches[-1].cursor["last_full"] == c.clock.now
    never = make(db, full_resync_hours=0, clock=Clock(c.clock.now + 10 * 365 * 86400))
    assert scan_all(never, cur)[1][-1].complete_scope is None
    assert scan_all(make(db, full_resync_hours=24), {"cursor": cur["cursor"]})[1][-1].complete_scope == ""   # unknown age


# ── sync layer ──


class Replay:
    """Runs the connector from a given saved cursor, whatever the sync layer stored (it keeps none of a finished scan)."""

    kind = "dropbox"

    def __init__(self, inner, cursor):
        self.inner, self.cursor = inner, cursor

    def scan(self, _stored):
        return self.inner.scan(self.cursor)


def rows_of(cid):
    from sqlalchemy import text
    from app.scale.db import engine
    with engine().connect() as conn:
        return dict(conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c"), {"c": cid}).all())


def test_sync_layer_deletes_via_complete_scope_on_repeated_full_scans(db):
    populate(db)
    c = make(db)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it; run_scan only
    # uses the kind to build a connector when none is passed
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["upserts"] == 5 and stats["queued"] == 5
    db.remove("/top.txt")
    stats = sync.run_scan(cid, connector=c)
    rows = rows_of(cid)
    assert stats["deletes"] == 1 and rows["/top.txt"] is True and rows["/docs/readme.txt"] is False


def test_sync_layer_applies_feed_deletions_and_folder_scopes_but_not_to_sibling_prefixes(db):
    populate(db)
    db.put("/Docs/2026-archive/keep.txt", b"keep")                                       # shares the text prefix "/docs/2026"
    c = make(db)
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    sync.run_scan(cid, connector=c)
    cur = finished_cursor(c)
    db.remove("/Docs/2026")                                                              # q1 and q2 vanish with the folder
    db.remove("/top.txt")
    db.put("/new.txt", b"n")
    stats = sync.run_scan(cid, connector=Replay(c, cur))
    rows = rows_of(cid)
    assert rows["/docs/2026/q1.pdf"] is True and rows["/docs/2026/q2.pdf"] is True and rows["/top.txt"] is True
    assert rows["/docs/2026-archive/keep.txt"] is False and rows["/docs/readme.txt"] is False and rows["/new.txt"] is False
    assert stats["deletes"] == 3 and stats["upserts"] == 1


# ── permissions ──


def test_acl_rules_apply_by_path_and_fail_closed(db):
    populate(db)
    rules = [{"prefix": "Docs", "principals": ["group:Staff"]}, {"prefix": "Docs/2026", "principals": ["group:finance"]}]
    c = make(db, acl_rules=rules, default_acl=None)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert got["/docs/readme.txt"].acl == ["group:staff"] and got["/docs/2026/q1.pdf"].acl == ["group:finance"]
    assert got["/top.txt"].acl == [] and got["/other/x.md"].acl == []
    assert c.refresh_acl(got["/docs/2026/q1.pdf"]) == ["group:finance"] and c.refresh_acl(got["/top.txt"]) == []
    rebuilt = SourceFile("/docs/2026/q1.pdf", "Docs/2026/q1.pdf", "q1.pdf", 11, None, "v", [])        # as the sync layer rebuilds it
    assert c.refresh_acl(rebuilt) == ["group:finance"]
    assert [x.file.acl for x in scan_all(make(db))[0]] == [["*"]] * 5                    # default_acl from the fixture


def test_acl_paths_are_relative_to_root_path(db):
    db.put("/Team/Finance/pay.csv", b"1")
    db.put("/Team/Open/readme.txt", b"1")
    c = make(db, root_path="/Team", acl_rules=[{"prefix": "Finance", "principals": ["group:finance"]}], default_acl=["group:all"])
    got = {x.external_id: x.file.acl for x in scan_all(c)[0]}
    assert got == {"/team/finance/pay.csv": ["group:finance"], "/team/open/readme.txt": ["group:all"]}


# ── download ──


def test_open_downloads_exact_bytes_with_the_path_in_the_api_arg_header(db):
    payload = bytes(range(256)) * 40_000                                                 # ~10 MB, every byte value
    db.put("/Docs/blob.bin", payload)
    db.put("/Docs/Spaced Name (1).txt", b"text")
    c = make(db)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    with c.open(got["/docs/blob.bin"]) as fh:
        assert fh.read() == payload
    request = db.routes("files/download")[0]
    assert request.method == "POST" and request.url.host == "content.dropboxapi.com"
    assert json.loads(request.headers["dropbox-api-arg"]) == {"path": "/docs/blob.bin"}
    assert request.headers["authorization"] == f"Bearer {TOKEN}" and request.content == b""
    rebuilt = SourceFile("/docs/spaced name (1).txt", "Docs/Spaced Name (1).txt", "Spaced Name (1).txt", 4, None, "v", [])
    with c.open(rebuilt) as fh:
        assert fh.read() == b"text"


def test_open_reports_missing_and_unsupported_files_as_a_plain_error_not_an_auth_failure(db):
    db.put("/gone-soon.txt", b"x")
    db.put("/paper.paper", b"p", downloadable=False)
    c = make(db)
    sf = {x.external_id: x.file for x in scan_all(c)[0]}["/gone-soon.txt"]
    db.remove("/gone-soon.txt")
    with pytest.raises(HttpError) as err:
        c.open(sf)
    assert not isinstance(err.value, AuthError) and err.value.status == 409 and "/gone-soon.txt" in str(err.value) and "not_found" in str(err.value)
    with pytest.raises(HttpError, match="unsupported_file"):
        c.open(SourceFile("/paper.paper", "paper.paper", "paper.paper", 1, None, "v", []))


def test_open_refuses_a_body_larger_than_max_file_bytes_even_if_the_listing_said_small(db):
    db.put("/grew.bin", b"s" * 10)
    c = make(db, max_file_bytes=100)
    sf = {x.external_id: x.file for x in scan_all(c)[0]}["/grew.bin"]
    db.put("/grew.bin", b"b" * 5000)
    with pytest.raises(TooLarge):
        c.open(sf)


# ── rate limits and errors ──


@pytest.mark.parametrize("status,headers,body", [
    (429, {"Retry-After": "5"}, too_many(5)), (429, {}, too_many(1)), (503, {"Retry-After": "2"}, "busy"), (503, {}, "busy"),
])
def test_rate_limits_are_retried_on_listing_feed_and_download(db, status, headers, body):
    populate(db)
    c = make(db)
    db.script.extend([("files/list_folder", status, headers, body)] * 2)
    changes, batches = scan_all(c)
    assert len(changes) == 5 and len(c.sleeps) == 2
    assert c.sleeps == ([float(headers["Retry-After"])] * 2 if headers else [1, 2])
    db.put("/new.txt", b"n")
    db.script.append(("list_folder/continue", status, headers, body))
    assert [x.external_id for x in scan_all(c, batches[-1].cursor)[0]] == ["/new.txt"]
    sf = by_id(changes)["/top.txt"].file
    db.script.append(("files/download", status, headers, body))
    with c.open(sf) as fh:
        assert fh.read() == b"top"


def test_persistent_rate_limiting_gives_up_with_a_clear_non_auth_error(db):
    c = make(db, max_retries=2)
    db.script.extend([("files/list_folder", 429, {"Retry-After": "7"}, too_many(7))] * 5)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert not isinstance(err.value, AuthError) and err.value.status == 429 and c.sleeps == [7.0, 7.0]


def test_invalid_token_is_an_auth_error_that_does_not_leak_it(db, monkeypatch):
    monkeypatch.setenv("T_DBX_TOKEN", "WRONG-" + TOKEN)
    c = make(db)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert err.value.status == 401 and TOKEN not in blob and "WRONG-" not in blob and "invalid_access_token" in blob and c.sleeps == []


def test_a_missing_scope_is_an_auth_error_and_is_not_retried(db):
    c = make(db)
    db.script.append(("files/list_folder", 403, {}, api_error("missing_scope/..", {".tag": "missing_scope", "required_scope": "files.metadata.read"})))
    with pytest.raises(AuthError, match="missing_scope") as err:
        scan_all(c)
    assert err.value.status == 403 and c.sleeps == []


def test_unset_token_variable_is_named_and_nothing_is_sent(db, monkeypatch):
    monkeypatch.delenv("T_DBX_TOKEN")
    with pytest.raises(HttpError) as err:
        scan_all(make(db))
    assert "T_DBX_TOKEN" in str(err.value) and db.requests == []
    with pytest.raises(HttpError, match="no environment variable name"):
        scan_all(make(db, access_token_env=None))


def test_secrets_never_reach_cursors_source_files_or_the_stored_config(db):
    populate(db)
    c = make(db, page_size=2)
    changes, batches = scan_all(c)
    assert TOKEN not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)


# ── headers for team spaces ──


def test_team_space_headers_are_sent_on_every_request_including_downloads(db):
    populate(db)
    c = make(db, path_root_namespace_id="4003", select_user="dbmid:AAAA")
    changes, batches = scan_all(c)
    db.remove("/top.txt")
    scan_all(c, batches[-1].cursor)
    with c.open(by_id(changes)["/docs/readme.txt"].file):
        pass
    assert [r.url.path for r in db.requests] == ["/2/files/list_folder", "/2/files/list_folder/continue", "/2/files/list_revisions",
                                                 "/2/files/download"]
    for r in db.requests:
        assert json.loads(r.headers["dropbox-api-path-root"]) == {".tag": "root", "root": "4003"}
        assert r.headers["dropbox-api-select-user"] == "dbmid:AAAA"
    assert "dropbox-api-path-root" not in make(db).http.client.headers


# ── refresh-token authentication ──


@pytest.fixture
def refresh_env(monkeypatch, db):
    monkeypatch.delenv("T_DBX_TOKEN", raising=False)
    monkeypatch.setenv("T_DBX_REFRESH", REFRESH)
    monkeypatch.setenv("T_DBX_KEY", APP_KEY)
    monkeypatch.setenv("T_DBX_SECRET", APP_SECRET)
    db.valid_token = "none-yet"


REFRESH_CFG = {"access_token_env": None, "refresh_token_env": "T_DBX_REFRESH", "app_key_env": "T_DBX_KEY", "app_secret_env": "T_DBX_SECRET"}


def test_refresh_token_flow_mints_one_access_token_and_caches_it(db, refresh_env):
    populate(db)
    c = make(db, **REFRESH_CFG)
    changes, _ = scan_all(c)
    assert len(changes) == 5 and len(db.token_calls) == 1
    call = db.token_calls[0]
    assert call["basic"] == f"{APP_KEY}:{APP_SECRET}" and call["grant_type"] == "refresh_token" and call["refresh_token"] == REFRESH
    assert db.requests[-1].headers["authorization"] == "Bearer minted-1"
    scan_all(c)
    assert len(db.token_calls) == 1                                                      # still valid: no new token request


def test_the_access_token_is_renewed_shortly_before_it_expires(db, refresh_env):
    populate(db)
    db.token_lifetime = 3600
    c = make(db, **REFRESH_CFG)
    scan_all(c)
    c.clock.now += 3600 - 400
    scan_all(c)
    assert len(db.token_calls) == 1
    c.clock.now += 200                                                                   # inside the 300 s safety margin
    scan_all(c)
    assert len(db.token_calls) == 2 and db.requests[-1].headers["authorization"] == "Bearer minted-2"


def test_a_401_with_a_refresh_token_refreshes_once_and_retries(db, refresh_env):
    populate(db)
    c = make(db, **REFRESH_CFG)
    scan_all(c)
    assert len(db.token_calls) == 1
    db.valid_token = "minted-2"                                                          # the server dropped minted-1 early
    changes, _ = scan_all(c)                                                             # same clock: not an expiry refresh
    assert len(changes) == 5 and len(db.token_calls) == 2
    assert db.requests[-1].headers["authorization"] == "Bearer minted-2"


def test_a_persistent_401_with_a_refresh_token_refreshes_once_then_fails_instead_of_looping(db, refresh_env):
    populate(db)
    c = make(db, **REFRESH_CFG)
    db.script.extend([("files/list_folder", 401, {}, api_error("expired_access_token/..", {".tag": "expired_access_token"}))] * 3)
    with pytest.raises(AuthError, match="expired_access_token") as err:
        scan_all(c)
    assert err.value.status == 401 and len(db.token_calls) == 2 and len(db.routes("files/list_folder")) == 2


def test_a_rejected_refresh_token_is_a_clear_auth_error_without_the_secrets(db, refresh_env, monkeypatch):
    monkeypatch.setenv("T_DBX_REFRESH", "REVOKED-" + REFRESH)
    c = make(db, **REFRESH_CFG)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value) + repr(err.value.__cause__)
    assert "invalid_grant" in blob and "refresh token" in blob.lower()
    for secret in (REFRESH, "REVOKED-", APP_SECRET, APP_KEY):
        assert secret not in blob
    assert c.sleeps == [] and not db.routes("files/list_folder")


def test_missing_refresh_settings_are_reported_by_variable_name(db, refresh_env, monkeypatch):
    monkeypatch.delenv("T_DBX_SECRET")
    with pytest.raises(HttpError, match="T_DBX_SECRET"):
        scan_all(make(db, **REFRESH_CFG))
    assert db.token_calls == []
    with pytest.raises(HttpError, match="app key"):
        scan_all(make(db, **{**REFRESH_CFG, "app_key_env": None}))
