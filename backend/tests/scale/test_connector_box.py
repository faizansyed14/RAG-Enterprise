"""Box connector tests (MOCK fixtures, not a real server).

There is no pip-installable Box emulator, so these tests run the connector against `FakeBox`: a stateful, in-memory imitation of
the Box API v2 behind an httpx.MockTransport. It implements GET folders/{id}/items (usemarker=true, `limit`, opaque `marker` /
`next_marker`, `fields` filtering, folders listed before files before web links, 400 for an unknown marker), GET files/{id}/content
(a 302 to a pre-signed URL on another host, optionally preceded by 202 + Retry-After answers), the client-credentials grant on
oauth2/token, Bearer authentication and the documented error bodies (`{"type": "error", "status": ..., "code": ...}`). Passing here
proves the connector's logic against these shapes; it does not prove Box behaves identically.
"""

import hashlib
import itertools
import json
import time

import httpx
import pytest

from app.scale import sync
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.box import BoxConnector

TOKEN = "box-ACCESS-TOKEN-MUST-NOT-LEAK"
CLIENT_ID, CLIENT_SECRET = "client-id-123", "CLIENT-SECRET-MUST-NOT-LEAK"


def box_error(status, code, message="boom"):
    return {"type": "error", "status": status, "code": code, "message": message, "help_url": "http://developers.box.com/docs/#errors",
            "request_id": "abc123"}


class FakeBox:
    def __init__(self):
        self.folders: dict[str, dict] = {"0": {"name": "All Files", "parent": None}}
        self.files: dict[str, dict] = {}
        self.web_links: dict[str, dict] = {}
        self.raw_entries: dict[str, list[dict]] = {}          # folder id -> hand-written entries appended to its listing
        self.counter = itertools.count(1000)
        self.requests: list[httpx.Request] = []
        self.script: list[tuple[str, int, dict, dict | str]] = []      # (path substring, status, headers, body), used once each
        self.forbidden: dict[str, int] = {}                             # folder id -> status its listing answers with
        self.not_ready = 0                                              # number of 202 answers before a download works
        self.valid_token = TOKEN
        self.token_calls: list[dict] = []
        self.token_lifetime = 4000

    # ── state ──

    def folder(self, name, parent="0"):
        fid = str(next(self.counter))
        self.folders[fid] = {"name": name, "parent": parent}
        return fid

    def file(self, name, data=b"", parent="0", **extra):
        fid = str(next(self.counter))
        self.files[fid] = {"name": name, "parent": parent, "data": data, "etag_n": 0, "ver_n": 1, "mtime": "2026-09-01T10:00:00-07:00",
                           "extra": extra}
        return fid

    def upload(self, fid, data):
        f = self.files[fid]
        f["data"], f["ver_n"], f["etag_n"] = data, f["ver_n"] + 1, f["etag_n"] + 1
        f["mtime"] = "2026-10-01T10:00:00-07:00"

    def rename(self, fid, name):
        self.files[fid]["name"] = name
        self.files[fid]["etag_n"] += 1

    def move(self, fid, parent):
        self.files[fid]["parent"] = parent
        self.files[fid]["etag_n"] += 1

    def delete(self, fid):
        del self.files[fid]

    def delete_folder(self, fid):
        del self.folders[fid]

    def routes(self, fragment):
        return [r for r in self.requests if fragment in r.url.path]

    def listings(self):
        return [r for r in self.requests if r.url.path.endswith("/items")]

    def _file_entry(self, fid, f):
        sha1 = hashlib.sha1(f["data"]).hexdigest()
        entry = {"type": "file", "id": fid, "etag": str(f["etag_n"]), "name": f["name"], "size": len(f["data"]), "sha1": sha1,
                 "modified_at": f["mtime"], "file_version": {"type": "file_version", "id": f"v{fid}-{f['ver_n']}", "sha1": sha1}}
        entry.update(f["extra"])
        return {k: v for k, v in entry.items() if v is not None}

    # ── protocol ──

    def respond(self, status, body, headers=None):
        return httpx.Response(status, json=body, headers=headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "dl.boxcloud.com":
            assert "authorization" not in request.headers, "the API token must not be sent to the download host"
            fid = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.files[fid]["data"], headers={"content-type": "application/octet-stream"})
        path = request.url.path
        if path == "/oauth2/token":
            return self.token_endpoint(request)
        for i, (needle, status, headers, body) in enumerate(self.script):
            if needle in path:
                self.script.pop(i)
                return httpx.Response(status, headers=headers, content=body if isinstance(body, str) else json.dumps(body))
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            return self.respond(401, box_error(401, "unauthorized", "Unauthorized"),
                                {"WWW-Authenticate": 'Bearer realm="Service", error="invalid_token"'})
        assert request.method == "GET" and path.startswith("/2.0/"), (request.method, path)
        parts = path[len("/2.0/"):].split("/")
        if parts[0] == "folders" and parts[2:] == ["items"]:
            return self.list_items(parts[1], request.url.params)
        if parts[0] == "files" and parts[2:] == ["content"]:
            return self.content(parts[1])
        raise AssertionError(f"unexpected request {request.method} {path}")

    def list_items(self, folder_id, p):
        if folder_id in self.forbidden:
            status = self.forbidden[folder_id]
            return self.respond(status, box_error(status, "access_denied_insufficient_permissions" if status == 403 else "not_found"))
        if folder_id not in self.folders:
            return self.respond(404, box_error(404, "not_found", "Not Found"))
        assert p.get("usemarker") == "true"
        limit = int(p["limit"])
        assert 1 <= limit <= 1000, limit
        rows = [("folder", fid, f) for fid, f in sorted(self.folders.items(), key=lambda kv: kv[1]["name"]) if f["parent"] == folder_id]
        rows += [("file", fid, f) for fid, f in sorted(self.files.items(), key=lambda kv: kv[1]["name"]) if f["parent"] == folder_id]
        rows += [("web_link", fid, f) for fid, f in sorted(self.web_links.items()) if f["parent"] == folder_id]
        entries = []
        for kind, fid, f in rows:
            if kind == "folder":
                entries.append({"type": "folder", "id": fid, "etag": "0", "name": f["name"]})
            elif kind == "file":
                entries.append(self._file_entry(fid, f))
            else:
                entries.append({"type": "web_link", "id": fid, "name": f["name"], "etag": "0"})
        entries += self.raw_entries.get(folder_id, [])
        wanted = set(p["fields"].split(",")) | {"type", "id", "etag"} if p.get("fields") else None
        if wanted:
            entries = [{k: v for k, v in e.items() if k in wanted} for e in entries]
        offset = 0
        if p.get("marker"):
            if not p["marker"].startswith("mk:"):
                return self.respond(400, box_error(400, "bad_request", "Invalid marker"))
            offset = int(p["marker"][3:])
        page = entries[offset: offset + limit]
        more = offset + limit < len(entries)
        return self.respond(200, {"total_count": len(entries), "entries": page, "limit": limit,
                                  "order": [{"by": "type", "direction": "ASC"}, {"by": "name", "direction": "ASC"}],
                                  "next_marker": f"mk:{offset + limit}" if more else None})

    def content(self, fid):
        if fid not in self.files:
            return self.respond(404, box_error(404, "not_found", "Not Found"))
        if self.not_ready > 0:
            self.not_ready -= 1
            return httpx.Response(202, headers={"Retry-After": "1"})
        return httpx.Response(302, headers={"Location": f"https://dl.boxcloud.com/d/1/{fid}?signature=abc"})

    def token_endpoint(self, request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        self.token_calls.append(form)
        if form["grant_type"] != "client_credentials" or form["client_id"] != CLIENT_ID or form["client_secret"] != CLIENT_SECRET:
            return self.respond(400, {"error": "invalid_client", "error_description": "The client credentials are invalid"})
        self.valid_token = f"minted-{len(self.token_calls)}"
        return self.respond(200, {"access_token": self.valid_token, "expires_in": self.token_lifetime, "token_type": "bearer",
                                  "restricted_to": []})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_BOX_TOKEN", TOKEN)


@pytest.fixture
def box():
    return FakeBox()


def make(box, **cfg):
    sleeps: list[float] = []
    clock = cfg.pop("clock", None) or Clock()
    config = {"access_token_env": "T_BOX_TOKEN", "default_acl": ["*"], **cfg}
    c = BoxConnector(config, transport=httpx.MockTransport(box.handler), sleep=sleeps.append, clock=clock)
    c.sleeps, c.clock = sleeps, clock
    return c


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def by_path(changes):
    return {c.file.path: c for c in changes}


def paths(changes):
    return sorted(c.file.path for c in changes)


def populate(box):
    ids = {}
    ids["docs"] = box.folder("Docs")
    ids["y26"] = box.folder("2026", parent=ids["docs"])
    ids["readme"] = box.file("readme.txt", b"hello", parent=ids["docs"])
    ids["q1"] = box.file("q1.pdf", b"%PDF-1.4 q1", parent=ids["y26"])
    ids["q2"] = box.file("q2.pdf", b"%PDF-1.4 q2", parent=ids["y26"])
    ids["other"] = box.folder("Other")
    ids["x"] = box.file("x.md", b"# x", parent=ids["other"])
    ids["top"] = box.file("top.txt", b"top")
    return ids


ALL = ["Docs/2026/q1.pdf", "Docs/2026/q2.pdf", "Docs/readme.txt", "Other/x.md", "top.txt"]


# ── listing and pagination ──


def test_full_scan_walks_nested_folders_follows_markers_and_maps_entries(box):
    ids = populate(box)
    for i in range(7):
        box.file(f"extra{i}.txt", f"x{i}".encode(), parent=ids["docs"])
    c = make(box, page_size=3)
    changes, batches = scan_all(c)
    assert paths(changes) == sorted(ALL + [f"Docs/extra{i}.txt" for i in range(7)])
    got = by_path(changes)
    f = got["Docs/2026/q1.pdf"].file
    assert (f.external_id, f.name, f.size, f.mime) == (ids["q1"], "q1.pdf", 11, "application/pdf")
    assert f.version == hashlib.sha1(b"%PDF-1.4 q1").hexdigest() + ":11"
    assert f.modified_at.isoformat() == "2026-09-01T10:00:00-07:00"
    assert all(x.kind == "upsert" for x in changes)
    docs_calls = [r for r in box.listings() if r.url.path == f"/2.0/folders/{ids['docs']}/items"]
    assert [r.url.params.get("marker") for r in docs_calls] == [None, "mk:3", "mk:6"]       # 9 entries, 3 per page
    assert all(r.url.params["usemarker"] == "true" and r.url.params["limit"] == "3" for r in box.listings())
    assert {"sha1", "etag", "size", "modified_at", "name"} <= set(box.listings()[0].url.params["fields"].split(","))
    assert batches[-1].done and batches[-1].complete_scope == "" and batches[-1].cursor == {}
    assert all(b.complete_scope is None and not b.done for b in batches[:-1])


def test_page_size_is_capped_at_the_api_maximum(box):
    populate(box)
    scan_all(make(box, page_size=50_000))
    assert {r.url.params["limit"] for r in box.listings()} == {"1000"}


def test_root_folder_id_limits_the_scan_to_a_subtree_with_relative_paths(box):
    ids = populate(box)
    c = make(box, root_folder_id=ids["docs"])
    changes, _ = scan_all(c)
    assert paths(changes) == ["2026/q1.pdf", "2026/q2.pdf", "readme.txt"]
    assert not [r for r in box.listings() if r.url.path.startswith("/2.0/folders/0/")]
    assert make(box, root_folder_id=int(ids["docs"])).root_id == ids["docs"]                # a numeric id from YAML / JSON is fine


def test_files_in_a_folder_hidden_behind_many_subfolders_are_all_found_depth_first(box):
    top = box.folder("top")
    expected = []
    for i in range(4):
        sub = box.folder(f"d{i}", parent=top)
        for j in range(3):
            box.file(f"f{j}.txt", b"x", parent=sub)
            expected.append(f"top/d{i}/f{j}.txt")
        deeper = box.folder("deeper", parent=sub)
        box.file("deep.txt", b"x", parent=deeper)
        expected.append(f"top/d{i}/deeper/deep.txt")
    changes, _ = scan_all(make(box, page_size=2))
    assert paths(changes) == sorted(expected) and len({c.external_id for c in changes}) == 16


def test_entries_with_missing_fields_web_links_and_unexpected_types_do_not_break_the_scan(box):
    ids = populate(box)
    box.web_links["9001"] = {"name": "A link", "parent": "0"}
    box.raw_entries["0"] = [
        {"type": "file", "name": "no-id.txt"},                                          # no id
        {"type": "file", "id": "7001"},                                                 # no name
        {"type": "folder", "id": "7002"},                                               # folder without name
        {"type": "mystery", "id": "7003", "name": "new thing"},
        {"id": "7004", "name": "untyped.txt"},
        {"type": "file", "id": "7005", "name": "bare.txt"},                             # nothing but id and name
    ]
    c = make(box)
    changes, _ = scan_all(c)
    assert paths(changes) == sorted(ALL + ["bare.txt"])
    bare = by_path(changes)["bare.txt"].file
    assert (bare.size, bare.version, bare.modified_at, bare.external_id) == (0, ":0", None, "7005")
    assert c.stats == {"malformed": 3, "not_a_file": 3}


def test_junk_oversize_and_skip_names_are_skipped_for_files_and_folders(box):
    ids = populate(box)
    box.file("Thumbs.db", b"j")
    box.file("~$draft.docx", b"j")
    junk = box.folder(".DS_Store")
    box.file("inside-skipped-folder.txt", b"x", parent=junk)
    box.file("big.bin", b"b" * 50)
    box.file("exact.bin", b"e" * 20)
    c = make(box, max_file_bytes=20)
    changes, _ = scan_all(c)
    assert paths(changes) == sorted(ALL + ["exact.bin"])
    assert c.stats == {"skipped_name": 3, "oversize": 1}
    assert not [r for r in box.listings() if junk in r.url.path]                              # a skipped folder is not read
    custom = make(box, skip_names=["*.txt"], max_file_bytes=1000)
    assert not [p for p in paths(scan_all(custom)[0]) if p.endswith(".txt")]
    everything = make(box, skip_names=[], max_file_bytes=1000)
    assert {"Thumbs.db", "~$draft.docx", ".DS_Store/inside-skipped-folder.txt"} <= set(paths(scan_all(everything)[0]))


# ── versions ──


def test_version_follows_the_content_not_the_metadata(box):
    ids = populate(box)
    c = make(box)
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}                 # stable across scans
    box.upload(ids["q1"], b"%PDF-1.4 q1")                                                    # same bytes uploaded again: new file_version
    box.rename(ids["q2"], "q2-final.pdf")
    box.move(ids["x"], ids["docs"])
    box.upload(ids["readme"], b"HELLO")                                                      # same size, different bytes
    box.upload(ids["top"], b"top plus")
    after_changes, _ = scan_all(c)
    after = {x.external_id: x.file.version for x in after_changes}
    assert after[ids["q1"]] == before[ids["q1"]] and after[ids["q2"]] == before[ids["q2"]] and after[ids["x"]] == before[ids["x"]]
    assert after[ids["readme"]] != before[ids["readme"]] and after[ids["top"]] != before[ids["top"]]
    moved = {x.external_id: x.file.path for x in after_changes}
    assert moved[ids["q2"]] == "Docs/2026/q2-final.pdf" and moved[ids["x"]] == "Docs/x.md"        # same id, new path


def test_version_falls_back_from_sha1_to_file_version_to_etag(box):
    box.raw_entries["0"] = [
        {"type": "file", "id": "1", "name": "a.txt", "size": 3, "file_version": {"id": "fv1"}},
        {"type": "file", "id": "2", "name": "b.txt", "size": 3, "etag": "7"},
        {"type": "file", "id": "3", "name": "c.txt", "size": 3, "sha1": None, "etag": "7", "file_version": {"id": "fv9"}},
    ]
    got = {x.external_id: x.file.version for x in scan_all(make(box))[0]}
    assert got == {"1": "fv1:3", "2": "7:3", "3": "fv9:3"}


# ── deletions ──


def test_full_scan_is_authoritative_so_deleted_files_and_folders_are_simply_absent(box):
    ids = populate(box)
    c = make(box)
    first = {x.external_id for x in scan_all(c)[0]}
    box.delete(ids["q1"])
    box.delete(ids["x"])
    box.delete_folder(ids["other"])
    changes, batches = scan_all(c)
    assert first - {x.external_id for x in changes} == {ids["q1"], ids["x"]} and batches[-1].complete_scope == ""


# ── batches and resume ──


def test_pages_are_combined_into_batches_by_batch_size(box):
    populate(box)
    _, batches = scan_all(make(box))
    assert len(batches) == 1 and batches[0].done                                             # default: one batch for a small tree
    changes, batches = scan_all(make(box, batch_size=2))
    assert len(changes) == 5 and len(batches) == 3 and [len(b.changes) for b in batches] == [2, 2, 1]


def test_listing_calls_per_batch_are_bounded_even_when_no_file_turns_up(box):
    root = box.folder("empties")
    for i in range(12):
        box.folder(f"e{i}", parent=root)
    box.file("only.txt", b"x", parent=box.folder("last", parent=root))
    changes, batches = scan_all(make(box, max_calls_per_batch=4))
    assert paths(changes) == ["empties/last/only.txt"] and len(batches) >= 3
    assert all(b.cursor["stack"] for b in batches[:-1])                                       # each carries a resumable work list


def test_a_crashed_scan_resumes_from_the_saved_work_list_without_repeating_or_skipping(box):
    ids = populate(box)
    for i in range(6):
        box.file(f"extra{i}.txt", b"x", parent=ids["docs"])
    expected = sorted(ALL + [f"Docs/extra{i}.txt" for i in range(6)])
    baseline = make(box, page_size=3, batch_size=1)
    scan_all(baseline)
    total_calls = len(box.listings())
    for crash_after in (1, 2, 3):
        box.requests.clear()
        stream = make(box, page_size=3, batch_size=1).scan({})
        got = [next(stream) for _ in range(crash_after)]
        stream.close()
        saved = json.loads(json.dumps(got[-1].cursor))                                        # it went through the database
        assert set(saved) == {"stack"} and saved["stack"]
        rest, rest_batches = scan_all(make(box, page_size=3, batch_size=1), saved)
        seen = [c.file.path for b in got for c in b.changes] + [c.file.path for c in rest]
        assert sorted(seen) == expected, crash_after                                          # nothing repeated, nothing missed
        assert len(box.listings()) == total_calls                                             # no folder page was read twice
        assert rest_batches[-1].done and rest_batches[-1].complete_scope is None             # a resumed scan cannot prove absence
        assert rest_batches[-1].cursor == {}


def test_batch_cursors_are_snapshots_that_later_progress_does_not_change(box):
    populate(box)
    c = make(box, batch_size=1)
    taken, kept = [], []
    for batch in c.scan({}):
        taken.append(json.dumps(batch.cursor))                                               # what the sync layer saves right away
        kept.append(batch)
    assert len(kept) >= 4 and taken == [json.dumps(b.cursor) for b in kept]


# ── failures while walking ──


@pytest.mark.parametrize("status", [403, 404])
def test_an_unreadable_or_vanished_subfolder_is_skipped_and_the_scan_stops_being_authoritative(box, status):
    ids = populate(box)
    box.forbidden[ids["other"]] = status
    c = make(box)
    changes, batches = scan_all(c)
    assert paths(changes) == [p for p in ALL if not p.startswith("Other/")]
    assert batches[-1].done and batches[-1].complete_scope is None and c.stats["folder_unreadable"] == 1


def test_a_folder_skipped_early_in_the_walk_still_makes_the_final_batch_non_authoritative(box):
    ids = populate(box)
    box.forbidden[ids["docs"]] = 403                                  # read first; the walk then ends normally in Other
    changes, batches = scan_all(make(box))
    assert paths(changes) == ["Other/x.md", "top.txt"] and batches[-1].done and batches[-1].complete_scope is None
    box.forbidden.clear()
    box.forbidden[ids["y26"]] = 404                                   # a nested one
    changes, batches = scan_all(make(box))
    assert "Docs/2026/q1.pdf" not in paths(changes) and "Docs/readme.txt" in paths(changes) and batches[-1].complete_scope is None


def test_a_skipped_last_folder_still_closes_the_scan(box):
    ids = populate(box)
    last = box.folder("zzz-last")
    box.forbidden[last] = 403
    changes, batches = scan_all(make(box))
    assert paths(changes) == ALL and batches[-1].done and batches[-1].complete_scope is None


def test_a_failing_root_listing_aborts_instead_of_looking_like_an_empty_source(box):
    populate(box)
    with pytest.raises(HttpError) as err:
        scan_all(make(box, root_folder_id="424242"))
    assert err.value.status == 404 and "not_found" in str(err.value)
    box.forbidden["0"] = 403
    with pytest.raises(AuthError) as err2:
        scan_all(make(box))
    assert err2.value.status == 403 and "access_denied_insufficient_permissions" in str(err2.value)


def test_server_errors_and_expired_tokens_mid_scan_abort_with_no_completion_claim(box):
    ids = populate(box)
    c = make(box, batch_size=1)
    box.script.append((f"/folders/{ids['y26']}/items", 500, {}, box_error(500, "internal_server_error")))
    seen = []
    with pytest.raises(HttpError) as err:
        for batch in c.scan({}):
            seen.append(batch)
    assert err.value.status == 500 and not any(b.done or b.complete_scope is not None for b in seen)
    c2 = make(box, batch_size=1)
    box.script.append((f"/folders/{ids['y26']}/items", 401, {}, box_error(401, "unauthorized")))
    with pytest.raises(AuthError) as err2:
        scan_all(c2)
    assert err2.value.status == 401                                                           # not mistaken for "folder unreadable"
    assert "folder_unreadable" not in c2.stats


def test_a_marker_box_no_longer_accepts_makes_the_folder_be_read_again_from_its_start(box):
    ids = populate(box)
    c = make(box)
    changes, batches = scan_all(c, {"stack": [[ids["docs"], "Docs", "garbage"]]})
    assert paths(changes) == [p for p in ALL if p.startswith("Docs/")] and c.stats == {"marker_reset": 1}
    assert batches[-1].complete_scope is None                                                 # resumed: not authoritative
    docs_calls = [r.url.params.get("marker") for r in box.listings() if r.url.path == f"/2.0/folders/{ids['docs']}/items"]
    assert docs_calls == ["garbage", None]
    box.script.append((f"/folders/{ids['docs']}/items", 400, {}, box_error(400, "bad_request", "nope")))
    with pytest.raises(HttpError) as err:                                                     # a 400 with no marker is a real error
        scan_all(make(box), {"stack": [[ids["docs"], "Docs", None]]})
    assert err.value.status == 400


# ── sync layer ──


def rows_of(cid):
    from sqlalchemy import text
    from app.scale.db import engine
    with engine().connect() as conn:
        return dict(conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c"), {"c": cid}).all())


def paths_of(cid):
    from sqlalchemy import text
    from app.scale.db import engine
    with engine().connect() as conn:
        return dict(conn.execute(text("SELECT external_id, path FROM source_items WHERE connection_id = :c"), {"c": cid}).all())


def test_sync_layer_deletes_via_complete_scope_and_keeps_moved_files_alive(box):
    ids = populate(box)
    c = make(box)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it; run_scan only
    # uses the kind to build a connector when none is passed
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["upserts"] == 5 and stats["queued"] == 5
    box.delete(ids["top"])
    box.move(ids["q1"], ids["other"])                                                         # a move is an update, not delete + add
    stats = sync.run_scan(cid, connector=c)
    rows = rows_of(cid)
    assert stats["deletes"] == 1 and rows[ids["top"]] is True and rows[ids["q1"]] is False and rows[ids["readme"]] is False
    assert paths_of(cid)[ids["q1"]] == "Other/q1.pdf"                                         # same item, new path


def test_sync_layer_resumes_an_interrupted_scan_and_does_not_delete_anything(box):
    ids = populate(box)
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    sync.run_scan(cid, connector=make(box))
    box.delete(ids["top"])
    c = make(box, batch_size=1)
    stream = c.scan({})
    first = next(stream)
    stream.close()
    from sqlalchemy import text
    from app.scale.db import engine
    with engine().begin() as conn:                                                            # what the sync layer would have saved
        conn.execute(text("UPDATE connector_state SET cursor = CAST(:c AS jsonb) WHERE connection_id = :i"),
                     {"c": json.dumps({**first.cursor, "_scan_id": "interrupted"}), "i": cid})
    stats = sync.run_scan(cid, connector=make(box, batch_size=1))
    assert stats["deletes"] == 0 and rows_of(cid)[ids["top"]] is False                      # not authoritative: the file stays until a full scan


# ── permissions ──


def test_acl_rules_apply_by_path_and_fail_closed(box):
    ids = populate(box)
    rules = [{"prefix": "Docs", "principals": ["group:Staff"]}, {"prefix": "Docs/2026", "principals": ["group:finance"]}]
    c = make(box, acl_rules=rules, default_acl=None)
    got = by_path(scan_all(c)[0])
    assert got["Docs/readme.txt"].file.acl == ["group:staff"] and got["Docs/2026/q1.pdf"].file.acl == ["group:finance"]
    assert got["top.txt"].file.acl == [] and got["Other/x.md"].file.acl == []
    assert c.refresh_acl(got["Docs/2026/q1.pdf"].file) == ["group:finance"] and c.refresh_acl(got["top.txt"].file) == []
    rebuilt = SourceFile(ids["q1"], "Docs/2026/q1.pdf", "q1.pdf", 11, None, "v", [])           # as the sync layer rebuilds it
    assert c.refresh_acl(rebuilt) == ["group:finance"]
    assert {tuple(x.file.acl) for x in scan_all(make(box))[0]} == {("*",)}


def test_acl_paths_are_relative_to_the_root_folder(box):
    team = box.folder("Team")
    box.file("pay.csv", b"1", parent=box.folder("Finance", parent=team))
    box.file("readme.txt", b"1", parent=team)
    c = make(box, root_folder_id=team, acl_rules=[{"prefix": "Finance", "principals": ["group:finance"]}], default_acl=["group:all"])
    assert {x.file.path: x.file.acl for x in scan_all(c)[0]} == {"Finance/pay.csv": ["group:finance"], "readme.txt": ["group:all"]}


# ── download ──


def test_open_downloads_exact_bytes_following_the_redirect_without_leaking_the_token(box):
    payload = bytes(range(256)) * 40_000                                                      # ~10 MB, every byte value
    fid = box.file("blob.bin", payload)
    c = make(box)
    sf = scan_all(c)[0][0].file
    with c.open(sf) as fh:
        assert fh.read() == payload
    api, dl = box.routes(f"/files/{fid}/content"), [r for r in box.requests if r.url.host == "dl.boxcloud.com"]
    assert api[0].headers["authorization"] == f"Bearer {TOKEN}" and len(dl) == 1
    rebuilt = SourceFile(fid, "blob.bin", "blob.bin", 0, None, "v", [])                       # no `extra`, as rebuilt by the sync layer
    with c.open(rebuilt) as fh:
        assert fh.read(5) == payload[:5]


def test_a_file_that_is_not_ready_yet_is_retried_with_backoff(box):
    fid = box.file("slow.txt", b"finally")
    c = make(box)
    box.not_ready = 2
    with c.open(SourceFile(fid, "slow.txt", "slow.txt", 7, None, "v", [])) as fh:
        assert fh.read() == b"finally"
    assert c.sleeps == [1, 2]
    box.not_ready = 99
    with pytest.raises(HttpError) as err:
        c.open(SourceFile(fid, "slow.txt", "slow.txt", 7, None, "v", []))
    assert err.value.status == 202 and "not prepared" in str(err.value) and not isinstance(err.value, AuthError)
    assert len(c.sleeps) == 2 + 5                                                             # max_retries sleeps, then give up


def test_open_reports_a_vanished_file_as_a_plain_error(box):
    fid = box.file("gone.txt", b"x")
    c = make(box)
    sf = scan_all(c)[0][0].file
    box.delete(fid)
    with pytest.raises(HttpError) as err:
        c.open(sf)
    assert err.value.status == 404 and not isinstance(err.value, AuthError)


def test_open_refuses_a_body_larger_than_max_file_bytes_even_if_the_listing_said_small(box):
    fid = box.file("grew.bin", b"s" * 10)
    c = make(box, max_file_bytes=100)
    sf = scan_all(c)[0][0].file
    box.upload(fid, b"b" * 5000)
    with pytest.raises(TooLarge):
        c.open(sf)


# ── rate limits and errors ──


@pytest.mark.parametrize("status,headers", [(429, {"Retry-After": "5"}), (429, {}), (503, {"Retry-After": "2"}), (503, {}), (502, {})])
def test_rate_limits_are_retried_on_listing_and_download(box, status, headers):
    populate(box)
    c = make(box)
    body = box_error(status, "rate_limit_exceeded", "Request rate limit exceeded")
    box.script.extend([("/items", status, headers, body)] * 2)
    changes, _ = scan_all(c)
    assert len(changes) == 5 and len(c.sleeps) == 2
    assert c.sleeps == ([float(headers["Retry-After"])] * 2 if headers else [1, 2])
    sf = by_path(changes)["top.txt"].file
    box.script.append(("/content", status, headers, body))
    with c.open(sf) as fh:
        assert fh.read() == b"top"


def test_persistent_rate_limiting_gives_up_with_a_clear_non_auth_error(box):
    c = make(box, max_retries=2)
    box.script.extend([("/items", 429, {"Retry-After": "9"}, box_error(429, "rate_limit_exceeded"))] * 5)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert not isinstance(err.value, AuthError) and err.value.status == 429 and c.sleeps == [9.0, 9.0]


def test_invalid_token_is_an_auth_error_that_does_not_leak_it(box, monkeypatch):
    monkeypatch.setenv("T_BOX_TOKEN", "WRONG-" + TOKEN)
    c = make(box)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert err.value.status == 401 and TOKEN not in blob and "WRONG-" not in blob and "unauthorized" in blob and c.sleeps == []


def test_unset_token_variable_is_named_and_nothing_is_sent(box, monkeypatch):
    monkeypatch.delenv("T_BOX_TOKEN")
    with pytest.raises(HttpError) as err:
        scan_all(make(box))
    assert "T_BOX_TOKEN" in str(err.value) and box.requests == []
    with pytest.raises(HttpError, match="no environment variable name"):
        scan_all(make(box, access_token_env=None))


def test_secrets_never_reach_cursors_source_files_or_the_stored_config(box):
    populate(box)
    c = make(box, batch_size=1)
    changes, batches = scan_all(c)
    assert TOKEN not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)


def test_as_user_header_is_sent_on_every_request(box):
    populate(box)
    c = make(box, as_user="3333")
    changes, _ = scan_all(c)
    with c.open(by_path(changes)["top.txt"].file):
        pass
    api = [r for r in box.requests if r.url.host == "api.box.com"]
    assert len(api) >= 5 and all(r.headers["as-user"] == "3333" for r in api)
    assert "as-user" not in make(box).http.client.headers


# ── client-credentials authentication ──


@pytest.fixture
def ccg_env(monkeypatch, box):
    monkeypatch.delenv("T_BOX_TOKEN", raising=False)
    monkeypatch.setenv("T_BOX_ID", CLIENT_ID)
    monkeypatch.setenv("T_BOX_SECRET", CLIENT_SECRET)
    box.valid_token = "none-yet"


CCG = {"access_token_env": None, "client_id_env": "T_BOX_ID", "client_secret_env": "T_BOX_SECRET", "subject_type": "enterprise",
       "subject_id": "98765"}


def test_client_credentials_mint_one_token_and_cache_it(box, ccg_env):
    populate(box)
    c = make(box, **CCG)
    changes, _ = scan_all(c)
    assert len(changes) == 5 and len(box.token_calls) == 1
    form = box.token_calls[0]
    assert form["grant_type"] == "client_credentials" and form["box_subject_type"] == "enterprise" and form["box_subject_id"] == "98765"
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
    assert box.requests[-1].headers["authorization"] == "Bearer minted-1"
    scan_all(c)
    assert len(box.token_calls) == 1
    user = make(box, **{**CCG, "subject_type": "user", "subject_id": "42"})
    scan_all(user)
    assert box.token_calls[-1]["box_subject_type"] == "user"


def test_the_token_is_renewed_shortly_before_it_expires(box, ccg_env):
    populate(box)
    box.token_lifetime = 3600
    c = make(box, **CCG)
    scan_all(c)
    c.clock.now += 3600 - 400
    scan_all(c)
    assert len(box.token_calls) == 1
    c.clock.now += 200
    scan_all(c)
    assert len(box.token_calls) == 2 and box.requests[-1].headers["authorization"] == "Bearer minted-2"


def test_a_401_with_client_credentials_renews_once_and_retries_without_looping(box, ccg_env):
    populate(box)
    c = make(box, **CCG)
    scan_all(c)
    box.valid_token = "minted-2"                                                              # the server dropped minted-1 early
    assert len(scan_all(c)[0]) == 5 and len(box.token_calls) == 2
    c2 = make(box, **CCG)
    box.script.extend([("/items", 401, {}, box_error(401, "unauthorized"))] * 3)
    box.token_calls.clear()
    with pytest.raises(AuthError):
        scan_all(c2)
    assert len(box.token_calls) == 2                                                          # one mint, one forced renewal, then fail


def test_rejected_client_credentials_are_a_clear_auth_error_without_secrets(box, ccg_env, monkeypatch):
    monkeypatch.setenv("T_BOX_SECRET", "WRONG-" + CLIENT_SECRET)
    c = make(box, **CCG)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value) + repr(err.value.__cause__)
    assert "invalid_client" in blob and "client credentials" in blob
    for secret in (CLIENT_SECRET, "WRONG-", CLIENT_ID):
        assert secret not in blob
    assert c.sleeps == [] and not box.listings()


def test_missing_credential_settings_are_reported_by_name_or_field(box, ccg_env, monkeypatch):
    monkeypatch.delenv("T_BOX_SECRET")
    with pytest.raises(HttpError, match="T_BOX_SECRET"):
        scan_all(make(box, **CCG))
    assert box.token_calls == []
    monkeypatch.setenv("T_BOX_SECRET", CLIENT_SECRET)
    with pytest.raises(ValueError, match="subject_id"):
        scan_all(make(box, **{**CCG, "subject_id": ""}))
    with pytest.raises(ValueError, match="subject_type"):
        scan_all(make(box, **{**CCG, "subject_type": "robot"}))
    assert box.token_calls == []
