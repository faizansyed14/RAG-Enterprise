"""Google Drive connector tests (MOCK fixtures, not a real server).

There is no pip-installable Drive emulator, so these tests run the connector against `FakeDrive`: a stateful, in-memory
imitation of the Drive v3 REST API behind an httpx.MockTransport. It implements files.list (q for folders / non-folders,
pageSize, opaque pageToken), changes.getStartPageToken, changes.list (pageToken / nextPageToken / newStartPageToken, `removed`),
files.get (metadata and alt=media), files.export, permissions.list, drives.get, the `root` alias, Bearer authentication, and the
OAuth token endpoint of the service-account flow (it verifies the RS256 signature of the JWT with the public key). Response bodies
follow the documented shapes (drive#fileList, drive#changeList, error bodies with `errors[].reason`). Passing here proves the
connector's logic against these shapes; it does not prove Google behaves identically.
"""

import base64
import itertools
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.scale import sync
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.gdrive import (
    EXPORTS, FOLDER, GDriveConnector, sign_jwt_rs256,
)

TOKEN = "ya29.ACCESS-TOKEN-MUST-NOT-LEAK"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
FORM = "application/vnd.google-apps.form"
DOCX = EXPORTS[DOC]["office"][0]
ROOT = "root-folder-id"


def error_body(code, reason, message="boom", status=""):
    return {"error": {"code": code, "message": message, "status": status,
                      "errors": [{"message": message, "domain": "global", "reason": reason}]}}


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.exports: dict[tuple[str, str], bytes] = {}
        self.perms: dict[str, list[dict]] = {}
        self.drives: dict[str, str] = {}
        self.change_log: list[str] = []             # file ids, in change order; token N == "after the first N entries"
        self.requests: list[httpx.Request] = []
        self.script: list[tuple[str, int, dict, dict | str]] = []    # (path substring, status, headers, body) for the next matches
        self.valid_token = TOKEN
        self.counter = itertools.count(1)
        self.stale_tokens: set[str] = set()
        self.token_requests: list[dict] = []
        self.token_response: tuple[int, dict] | None = None
        self.sa_public_key = None

    # ── state ──

    def add(self, name, *, parent=ROOT, mime="text/plain", data=b"", fid=None, drive_id=None, **extra):
        fid = fid or f"id{next(self.counter):04d}"
        meta = {"id": fid, "name": name, "mimeType": mime, "parents": [parent] if parent else [], "trashed": False,
                "modifiedTime": "2026-09-01T10:00:00.000Z", "capabilities": {"canDownload": True}, **extra}
        if drive_id:
            meta["driveId"] = drive_id
        if mime != FOLDER and not mime.startswith("application/vnd.google-apps."):
            meta["size"] = str(len(data))
            meta["md5Checksum"] = __import__("hashlib").md5(data).hexdigest()
        self.files[fid] = meta
        self.content[fid] = data
        self.change_log.append(fid)
        return fid

    def folder(self, name, parent=ROOT, **kw):
        return self.add(name, parent=parent, mime=FOLDER, **kw)

    def edit(self, fid, data=None, **fields):
        meta = self.files[fid]
        if data is not None:
            self.content[fid] = data
            if "md5Checksum" in meta:
                meta["md5Checksum"] = __import__("hashlib").md5(data).hexdigest()
                meta["size"] = str(len(data))
        meta.update(fields)
        self.change_log.append(fid)

    def raw_change(self, entry):
        self.change_log.append(entry)

    def trash(self, fid):
        self.edit(fid, trashed=True)

    def remove(self, fid):
        del self.files[fid]
        self.change_log.append(fid)

    @property
    def head(self):
        return str(len(self.change_log))

    def calls(self, needle):
        return [r for r in self.requests if needle in r.url.path]

    # ── protocol ──

    def respond(self, status, body, headers=None):
        return httpx.Response(status, json=body, headers=headers or {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            return self.token_endpoint(request)
        for i, (needle, status, headers, body) in enumerate(self.script):
            if needle in path:
                self.script.pop(i)
                return httpx.Response(status, headers=headers, content=body if isinstance(body, str) else json.dumps(body))
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            return self.respond(401, error_body(401, "authError", "Invalid Credentials", "UNAUTHENTICATED"))
        p = request.url.params
        assert path.startswith("/drive/v3/"), path
        route = path[len("/drive/v3/"):]
        if route == "changes/startPageToken":
            return self.respond(200, {"kind": "drive#startPageToken", "startPageToken": self.head})
        if route == "changes":
            return self.changes(p)
        if route == "files":
            return self.list_files(p)
        if route == "files/root":
            return self.respond(200, {"id": ROOT})
        if route.startswith("drives/"):
            name = self.drives.get(route.split("/")[1])
            return self.respond(200, {"id": route.split("/")[1], "name": name}) if name else self.respond(404, error_body(404, "notFound"))
        if route.startswith("files/"):
            parts = route.split("/")
            fid = parts[1]
            if fid not in self.files:
                return self.respond(404, error_body(404, "notFound", "File not found: " + fid))
            if len(parts) == 3 and parts[2] == "permissions":
                return self.respond(200, {"kind": "drive#permissionList", "permissions": self.perms.get(fid, [])})
            if len(parts) == 3 and parts[2] == "export":
                mime = p.get("mimeType")
                if self.files[fid]["mimeType"] not in EXPORTS or mime not in {v[0] for v in EXPORTS[self.files[fid]["mimeType"]].values()}:
                    return self.respond(400, error_body(400, "badRequest", "Export format not supported"))
                return httpx.Response(200, content=self.exports.get((fid, mime), b"EXPORT[" + mime.encode() + b"]:" + fid.encode()))
            if p.get("alt") == "media":
                if self.files[fid]["mimeType"].startswith("application/vnd.google-apps."):
                    return self.respond(403, error_body(403, "fileNotDownloadable", "Only files with binary content can be downloaded."))
                return httpx.Response(200, content=self.content[fid])
            return self.respond(200, self.files[fid])
        return self.respond(404, error_body(404, "notFound"))

    def list_files(self, p):
        q = p.get("q", "")
        assert "trashed = false" in q
        want_folders = f"mimeType = '{FOLDER}'" in q
        assert want_folders or f"mimeType != '{FOLDER}'" in q, q
        if p.get("corpora") == "drive":
            assert p.get("driveId") and p.get("includeItemsFromAllDrives") == "true" and p.get("supportsAllDrives") == "true"
        rows = [m for m in sorted(self.files.values(), key=lambda m: m["id"])
                if not m["trashed"] and (m["mimeType"] == FOLDER) == want_folders
                and (p.get("corpora") != "drive" or m.get("driveId") == p["driveId"])]
        offset = 0
        if p.get("pageToken"):
            if p["pageToken"][:2] not in ("F-", "N-"):
                return self.respond(400, error_body(400, "badRequest", "Invalid Value"))
            offset = int(p["pageToken"].split("-")[1])
        size = int(p.get("pageSize", 100))
        page = rows[offset: offset + size]
        body = {"kind": "drive#fileList", "incompleteSearch": False, "files": page}
        if offset + size < len(rows):
            body["nextPageToken"] = f"{'F' if want_folders else 'N'}-{offset + size}"
        return self.respond(200, body)

    def changes(self, p):
        token = p["pageToken"]
        if token in self.stale_tokens or not token.isdigit():
            return self.respond(400, error_body(400, "invalidParameter", "Invalid Value"))
        assert p.get("includeRemoved") == "true" and p.get("supportsAllDrives") == "true"
        start = int(token)
        size = int(p.get("pageSize", 100))
        window = self.change_log[start: start + size]
        out = []
        for offset, fid in enumerate(window):
            seq = start + offset + 1
            if isinstance(fid, dict):                      # a hand-written change entry (e.g. changeType "drive")
                out.append(fid)
            elif fid not in self.files:
                out.append({"kind": "drive#change", "changeType": "file", "removed": True, "fileId": fid, "time": "t"})
            else:
                out.append({"kind": "drive#change", "changeType": "file", "removed": False, "fileId": fid,
                            "file": self.files[fid], "time": "t", "seq": seq})
        body = {"kind": "drive#changeList", "changes": out}
        if start + size < len(self.change_log):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = self.head
        return self.respond(200, body)

    def token_endpoint(self, request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        self.token_requests.append(form)
        if self.token_response:
            status, body = self.token_response
            return self.respond(status, body)
        header_b64, claims_b64, sig_b64 = form["assertion"].split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.sa_public_key.verify(base64.urlsafe_b64decode(pad(sig_b64)), f"{header_b64}.{claims_b64}".encode(),
                                  padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
        self.last_claims = claims
        self.valid_token = f"minted-{len(self.token_requests)}"
        return self.respond(200, {"access_token": self.valid_token, "expires_in": 3599, "token_type": "Bearer"})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_GD_TOKEN", TOKEN)


@pytest.fixture
def drive():
    return FakeDrive()


def make(drive, **cfg):
    sleeps: list[float] = []
    clock = cfg.pop("clock", None) or Clock()
    config = {"access_token_env": "T_GD_TOKEN", "default_acl": ["*"], **cfg}
    c = GDriveConnector(config, transport=httpx.MockTransport(drive.handler), sleep=sleeps.append, clock=clock)
    c.sleeps, c.clock = sleeps, clock
    return c


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 200))
    assert len(batches) < 200, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def by_id(changes):
    return {c.external_id: c for c in changes}


def populate(drive):
    docs = drive.folder("Docs", fid="fDocs")
    q1 = drive.folder("2026", parent=docs, fid="f2026")
    drive.add("notes.txt", parent=ROOT, data=b"root notes", fid="n1")
    drive.add("Q1.pdf", parent=q1, mime="application/pdf", data=b"%PDF-1.4 q1", fid="q1")
    drive.add("Plan", parent=docs, mime=DOC, fid="plan")
    drive.add("Budget", parent=q1, mime=SHEET, fid="budget")
    drive.add("Deck", parent=docs, mime=SLIDES, fid="deck")
    return drive


# ── listing and pagination ──


def test_full_scan_pages_through_folders_and_files_and_builds_paths_and_export_names(drive):
    populate(drive)
    for i in range(6):
        drive.add(f"extra{i}.txt", parent="fDocs", data=f"x{i}".encode())
    c = make(drive, page_size=3)
    changes, batches = scan_all(c)
    got = {x.external_id: x.file for x in changes}
    assert got["n1"].path == "notes.txt" and got["q1"].path == "Docs/2026/Q1.pdf"
    assert got["plan"].path == "Docs/Plan.docx" and got["plan"].name == "Plan.docx" and got["plan"].mime == DOCX
    assert got["budget"].path == "Docs/2026/Budget.xlsx" and got["deck"].name == "Deck.pptx"
    assert len(got) == 5 + 6                                                    # folders are not files
    assert [len(b.changes) for b in batches] == [3, 3, 3, 2] and [b.done for b in batches] == [False] * 3 + [True]
    file_lists = [r for r in drive.calls("/files") if r.url.path.endswith("/files") and "!=" in r.url.params["q"]]
    assert len(file_lists) == 4 and "pageToken" in file_lists[1].url.params     # pagination used the tokens
    assert len([r for r in drive.calls("/files") if r.url.path.endswith("/files") and "mimeType = " in r.url.params["q"]]) == 1
    last = batches[-1]
    assert last.complete_scope == "" and last.cursor == {"token": drive.head, "last_full": c.clock.now}
    assert batches[0].cursor["mode"] == "full" and batches[0].cursor["page"].startswith("N-")
    json.dumps([b.cursor for b in batches])
    assert drive.calls("changes/startPageToken")                                # captured before listing


def test_start_page_token_is_taken_before_listing_so_edits_made_during_the_scan_are_not_lost(drive):
    populate(drive)
    c = make(drive, page_size=2)
    stream = c.scan({})
    first = next(stream)
    token_at_start = first.cursor["start"]
    drive.edit("n1", data=b"changed while we were listing")
    rest = list(stream)
    assert rest[-1].cursor["token"] == token_at_start != drive.head
    changes, _ = scan_all(c, rest[-1].cursor)                                   # the incremental that follows sees the edit
    assert "n1" in by_id(changes)


def test_native_types_without_an_export_shortcuts_trashed_and_junk_are_skipped(drive):
    populate(drive)
    drive.add("Survey", mime=FORM, fid="form")
    drive.add("link", mime="application/vnd.google-apps.shortcut", fid="short")
    drive.add("deleted.txt", data=b"d", fid="trashed", trashed=True)
    drive.add("Thumbs.db", data=b"j", fid="junk")
    drive.add("locked.pdf", mime="application/pdf", data=b"x", fid="locked", capabilities={"canDownload": False})
    c = make(drive)
    changes, _ = scan_all(c)
    assert not ({"form", "short", "trashed", "junk", "locked"} & set(by_id(changes)))
    assert c.stats["unexportable_type"] == 1 and c.stats["not_downloadable"] == 1


def test_names_are_made_safe_and_get_an_extension_the_pipeline_can_use(drive):
    drive.add("a/b: c?.txt", data=b"1", fid="slash")
    drive.add("Contract", mime="application/pdf", data=b"%PDF", fid="noext")
    drive.add("Q1.2026", mime="application/pdf", data=b"%PDF", fid="dotted")
    drive.add("scan.docx", mime="application/octet-stream", data=b"PK", fid="docx")
    drive.folder("a/b", fid="slashdir")
    drive.add("in-slash-dir.txt", parent="slashdir", data=b"1", fid="inslash")
    c = make(drive)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert got["slash"].name == "a b c .txt" and "/" not in got["slash"].name
    assert got["noext"].name == "Contract.pdf" and got["dotted"].name == "Q1.2026.pdf" and got["docx"].name == "scan.docx"
    assert got["inslash"].path == "a b/in-slash-dir.txt"                         # a "/" in a folder name cannot fake nesting


def test_text_export_mode_picks_text_formats(drive):
    populate(drive)
    c = make(drive, export_mode="text")
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert (got["plan"].name, got["budget"].name, got["deck"].name) == ("Plan.txt", "Budget.csv", "Deck.txt")
    assert got["plan"].mime == "text/plain"
    with c.open(got["budget"]) as fh:
        assert fh.read().startswith(b"EXPORT[text/csv]")


def test_shared_with_me_and_shared_drive_items_get_prefixes(drive):
    drive.drives["sd1"] = "Finance Team"
    sd_folder = drive.folder("Budgets", parent="sd1", fid="sdf", drive_id="sd1")
    drive.add("fy26.xlsx", parent=sd_folder, mime="application/vnd.ms-excel", data=b"x", fid="sdfile", drive_id="sd1")
    drive.add("top-of-drive.txt", parent="sd1", data=b"t", fid="sdtop", drive_id="sd1")
    drive.add("orphan.txt", parent=None, data=b"o", fid="orphan")                       # shared with me, parent invisible
    foreign_folder = drive.folder("Partner share", parent="someone-elses-folder", fid="foreign")
    drive.add("deal.txt", parent=foreign_folder, data=b"d", fid="deal")
    c = make(drive, corpora="allDrives")
    got = {x.external_id: x.file.path for x in scan_all(c)[0]}
    assert got["sdfile"] == "Finance Team/Budgets/fy26.xlsx" and got["sdtop"] == "Finance Team/top-of-drive.txt"
    assert got["orphan"] == "Shared with me/orphan.txt" and got["deal"] == "Shared with me/Partner share/deal.txt"


def test_corpora_drive_scopes_requests_to_one_shared_drive(drive):
    drive.drives["sd1"] = "Eng"
    drive.add("in.txt", parent="sd1", data=b"1", fid="in", drive_id="sd1")
    drive.add("out.txt", parent=ROOT, data=b"2", fid="out")
    c = make(drive, corpora="drive", drive_id="sd1")
    changes, batches = scan_all(c)
    assert list(by_id(changes)) == ["in"] and by_id(changes)["in"].file.path == "Eng/in.txt"
    assert all(r.url.params.get("driveId") == "sd1" for r in drive.calls("changes/startPageToken"))
    with pytest.raises(ValueError):
        GDriveConnector({"corpora": "drive"})


def test_root_folder_id_limits_the_index_to_a_subtree_with_relative_paths(drive):
    populate(drive)
    drive.add("elsewhere.txt", parent=ROOT, data=b"e", fid="elsewhere")
    c = make(drive, root_folder_id="fDocs")
    changes, batches = scan_all(c)
    got = {x.external_id: x.file.path for x in changes}
    assert got == {"q1": "2026/Q1.pdf", "plan": "Plan.docx", "budget": "2026/Budget.xlsx", "deck": "Deck.pptx"}
    assert c.stats["outside_root_folder"] == 2
    token = batches[-1].cursor
    drive.edit("q1", parents=[ROOT])                                   # moved out of the indexed folder
    drive.edit("elsewhere", name="renamed.txt")
    incr, _ = scan_all(c, token)
    assert [(x.kind, x.external_id) for x in incr] == [("delete", "q1"), ("delete", "elsewhere")]


# ── versions ──


def test_version_follows_content_for_blobs_and_modified_time_for_native_documents(drive):
    populate(drive)
    c = make(drive)
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}
    drive.edit("n1", data=b"ROOT NOTES")                                                  # same size, different bytes
    drive.edit("plan", modifiedTime="2026-09-02T09:00:00.000Z")
    drive.edit("q1", name="Q1-renamed.pdf")                                               # metadata only: same content
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["n1"] != before["n1"] and after["plan"] != before["plan"]
    assert after["q1"] == before["q1"] and after["budget"] == before["budget"]
    c2 = make(drive, export_mode="text")                                                  # a different export => re-parse
    assert {x.external_id: x.file.version for x in scan_all(c2)[0]}["plan"] != after["plan"]


# ── deletions, resume, incremental ──


def test_full_scan_is_authoritative_so_deleted_and_trashed_files_are_simply_absent(drive):
    populate(drive)
    c = make(drive)
    indexed = set(by_id(scan_all(c)[0]))
    drive.trash("n1")
    drive.remove("deck")
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope == "" and indexed - set(by_id(changes)) == {"n1", "deck"}


def test_resumed_full_scan_continues_at_the_page_and_is_not_authoritative(drive):
    for i in range(7):
        drive.add(f"f{i}.txt", data=b"x")
    c = make(drive, page_size=3)
    stream = c.scan({})
    first = next(stream)
    stream.close()                                                                       # crash after one batch
    seen_first = list(by_id(first.changes))
    drive.edit("id0001", data=b"edited after the crash")             # moves the change head past the original start token
    changes, batches = scan_all(c, first.cursor)
    assert not (set(seen_first) & set(by_id(changes))) and len(seen_first) + len(changes) == 7
    assert batches[-1].done and batches[-1].complete_scope is None
    assert batches[-1].cursor["token"] == first.cursor["start"] != drive.head       # the token from BEFORE the listing began
    edited, _ = scan_all(c, batches[-1].cursor)
    assert "id0001" in by_id(edited)


def test_a_page_token_google_no_longer_accepts_restarts_the_scan_from_scratch(drive):
    for i in range(5):
        drive.add(f"f{i}.txt", data=b"x")
    c = make(drive, page_size=2)
    first = next(iter(c.scan({})))
    cursor = {**first.cursor, "page": "token-from-last-week"}                              # Google answers 400 Invalid Value
    changes, batches = scan_all(c, cursor)
    assert len(changes) == 5 and batches[-1].complete_scope == ""                         # a fresh, authoritative listing


def test_incremental_scan_uses_the_changes_feed_and_reports_edits_new_files_and_deletions(drive):
    populate(drive)
    c = make(drive)
    _, batches = scan_all(c)
    cursor = batches[-1].cursor
    n_before = len(drive.requests)
    drive.edit("n1", data=b"edited")
    drive.add("fresh.txt", parent="f2026", data=b"new", fid="fresh")
    drive.trash("deck")
    drive.remove("plan")
    changes, batches = scan_all(c, cursor)
    got = {x.external_id: x for x in changes}
    assert got["n1"].kind == "upsert" and got["fresh"].file.path == "Docs/2026/fresh.txt"
    assert got["deck"].kind == "delete" and got["plan"].kind == "delete" and "budget" not in got
    assert batches[-1].done and batches[-1].complete_scope is None
    assert batches[-1].cursor == {"token": drive.head, "last_full": cursor["last_full"]}
    assert not [r for r in drive.requests[n_before:] if r.url.path.endswith("/files") and "q" in r.url.params]   # no re-listing
    quiet, qb = scan_all(c, batches[-1].cursor)
    assert quiet == [] and qb[-1].cursor["token"] == drive.head


def test_incremental_ignores_shared_drive_entries_and_folder_events_but_uses_them_for_paths(drive):
    populate(drive)
    c = make(drive)
    cursor = scan_all(c)[1][-1].cursor
    drive.raw_change({"kind": "drive#change", "changeType": "drive", "driveId": "sd1", "removed": False,
                      "drive": {"id": "sd1", "name": "Eng"}})
    drive.edit("fDocs", name="Documents")                                           # a folder was renamed
    drive.edit("plan", modifiedTime="2026-09-05T00:00:00.000Z")
    changes, _ = scan_all(c, cursor)
    assert [x.external_id for x in changes] == ["plan"]                             # no entry for the drive or the folder
    assert changes[0].file.path == "Documents/Plan.docx"                           # but the new folder name is what paths use


def test_incremental_changes_are_paged_and_resumable(drive):
    populate(drive)
    c = make(drive, page_size=2)
    cursor = scan_all(c)[1][-1].cursor
    for i in range(5):
        drive.add(f"n{i}.txt", data=b"x", fid=f"new{i}")
    changes, batches = scan_all(c, cursor)
    assert sorted(by_id(changes)) == [f"new{i}" for i in range(5)] and [len(b.changes) for b in batches] == [2, 2, 1]
    assert batches[0].cursor == {"mode": "inc", "page": batches[0].cursor["page"], "last_full": cursor["last_full"]}
    resumed, rb = scan_all(c, batches[0].cursor)                                          # crash after the first page
    assert sorted(by_id(resumed)) == [f"new{i}" for i in range(2, 5)] and rb[-1].cursor["token"] == drive.head


def test_rename_and_move_update_the_path_without_changing_the_version(drive):
    populate(drive)
    drive.folder("Archive", fid="fArch")
    c = make(drive)
    full, batches = scan_all(c)
    before = by_id(full)["q1"].file
    drive.edit("q1", parents=["fArch"])                                                    # moved: metadata only
    drive.edit("n1", name="renamed.txt")
    changes, _ = scan_all(c, batches[-1].cursor)
    got = by_id(changes)
    assert got["q1"].file.path == "Archive/Q1.pdf" and got["q1"].file.version == before.version
    assert got["n1"].file.path == "renamed.txt"
    assert drive.calls("files/fArch")                                                      # parent name resolved lazily


def test_a_rejected_change_token_falls_back_to_an_authoritative_full_scan(drive):
    populate(drive)
    c = make(drive)
    cursor = scan_all(c)[1][-1].cursor
    drive.stale_tokens.add(cursor["token"])
    changes, batches = scan_all(c, cursor)
    assert len(changes) == 5 and batches[-1].complete_scope == ""


def test_a_stale_cursor_with_no_page_does_not_loop(drive):
    populate(drive)
    changes, batches = scan_all(make(drive), {"mode": "inc"})
    assert len(changes) == 5 and batches[-1].complete_scope == ""


def test_an_incremental_cursor_older_than_full_resync_hours_triggers_a_full_scan(drive):
    populate(drive)
    c = make(drive, full_resync_hours=24)
    cursor = scan_all(c)[1][-1].cursor
    c.clock.now += 3600
    assert scan_all(c, cursor)[1][-1].complete_scope is None                                # still incremental
    c.clock.now += 25 * 3600
    _, batches = scan_all(c, cursor)
    assert batches[-1].complete_scope == "" and batches[-1].cursor["last_full"] == c.clock.now
    never = make(drive, full_resync_hours=0, clock=Clock(c.clock.now + 10_000 * 3600))
    assert scan_all(never, cursor)[1][-1].complete_scope is None


def test_a_file_that_stops_being_indexable_is_deleted_by_the_feed(drive):
    drive.add("big.bin", mime="application/octet-stream", data=b"x" * 50, fid="b")
    c = make(drive, max_file_bytes=1000)
    cursor = scan_all(c)[1][-1].cursor
    drive.edit("b", data=b"x" * 5000)                                                      # grew past the cap
    changes, _ = scan_all(c, cursor)
    assert [(x.kind, x.external_id) for x in changes] == [("delete", "b")]


def test_sync_layer_deletes_via_complete_scope_whether_or_not_the_final_cursor_is_kept(drive):
    from sqlalchemy import text
    from app.scale.db import engine

    populate(drive)
    c = make(drive)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it; run_scan only
    # uses the kind to build a connector when none is passed
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["upserts"] == 5 and stats["queued"] == 5
    drive.trash("n1")
    stats = sync.run_scan(cid, connector=c)
    with engine().connect() as conn:
        rows = dict(conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c"), {"c": cid}).all())
    assert stats["deletes"] == 1 and rows["n1"] is True and rows["q1"] is False


# ── permissions ──


def test_acl_rules_apply_by_path_and_fail_closed(drive):
    populate(drive)
    drive.add("pay.csv", parent=ROOT, mime="text/csv", data=b"1", fid="pay")
    rules = [{"prefix": "Docs", "principals": ["group:Staff"]}, {"prefix": "Docs/2026", "principals": ["group:finance"]}]
    c = make(drive, acl_rules=rules, default_acl=None)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert got["plan"].acl == ["group:staff"] and got["q1"].acl == ["group:finance"]
    assert got["pay"].acl == [] and got["n1"].acl == []
    assert c.refresh_acl(got["q1"]) == ["group:finance"] and c.refresh_acl(got["pay"]) == []


def test_drive_permissions_map_users_and_groups_and_ignore_anyone_and_domain_by_default(drive):
    drive.add("a.txt", data=b"1", fid="a")
    drive.perms["a"] = [
        {"type": "user", "emailAddress": "Owner@Corp.com", "role": "owner"},
        {"type": "user", "emailAddress": "reader@corp.com", "role": "reader"},
        {"type": "group", "emailAddress": "Finance@corp.com", "role": "writer"},
        {"type": "domain", "domain": "corp.com", "role": "reader"},
        {"type": "anyone", "role": "reader"},
        {"type": "user", "emailAddress": "gone@corp.com", "role": "reader", "deleted": True},
    ]
    c = make(drive, use_drive_permissions=True, acl_rules=[{"prefix": "", "principals": ["*"]}], default_acl=["*"])
    sf = scan_all(c)[0][0].file
    assert sf.acl == ["group:finance@corp.com", "user:owner@corp.com", "user:reader@corp.com"]    # rules are not consulted
    opened = make(drive, use_drive_permissions=True, anyone_principals=["*"], domain_principals={"CORP.com": ["group:all-staff"]})
    acl = scan_all(opened)[0][0].file.acl
    assert "*" in acl and "group:all-staff" in acl and "user:gone@corp.com" not in acl
    assert opened.refresh_acl(SourceFile("a", "a.txt", "a.txt", 1, None, "v", [])) == acl


def test_unreadable_permissions_mean_nobody_can_read_the_file(drive):
    drive.add("a.txt", data=b"1", fid="a")
    c = make(drive, use_drive_permissions=True)
    drive.script.append(("permissions", 403, {}, error_body(403, "insufficientFilePermissions")))
    assert scan_all(c)[0][0].file.acl == []
    drive.script.append(("permissions", 404, {}, error_body(404, "notFound")))
    assert c.refresh_acl(SourceFile("a", "a.txt", "a.txt", 1, None, "v", [])) == []
    assert make(drive, use_drive_permissions=True).refresh_acl(SourceFile("a", "a.txt", "a.txt", 1, None, "v", [])) == []


def test_a_failure_on_a_later_permissions_page_leaves_the_file_unreadable(drive):
    drive.add("a.txt", data=b"1", fid="a")
    c = make(drive, use_drive_permissions=True)
    drive.script.append(("permissions", 200, {}, {"permissions": [{"type": "user", "emailAddress": "a@x.com"}], "nextPageToken": "p2"}))
    drive.script.append(("permissions", 404, {}, error_body(404, "notFound")))
    assert scan_all(c)[0][0].file.acl == []


def test_permissions_are_read_in_pages(drive):
    drive.add("a.txt", data=b"1", fid="a")
    c = make(drive, use_drive_permissions=True)
    drive.script.append(("permissions", 200, {}, {"permissions": [{"type": "user", "emailAddress": "a@x.com"}], "nextPageToken": "p2"}))
    drive.perms["a"] = [{"type": "user", "emailAddress": "b@x.com"}]
    assert sorted(scan_all(c)[0][0].file.acl) == ["user:a@x.com", "user:b@x.com"]


# ── bytes ──


def test_open_downloads_blobs_and_exports_native_files_with_the_right_mime_type(drive):
    populate(drive)
    drive.content["q1"] = bytes(range(256)) * 400
    drive.exports[("plan", DOCX)] = b"PK-docx-bytes"
    c = make(drive)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    with c.open(got["q1"]) as fh:
        assert fh.read() == bytes(range(256)) * 400
    with c.open(got["plan"]) as fh:
        assert fh.read() == b"PK-docx-bytes"
    export_call = drive.calls("/export")[0]
    assert export_call.url.params["mimeType"] == DOCX
    bare = SourceFile("budget", "x", "x", 0, None, "v", [])                                 # what the sync layer persists
    with c.open(bare) as fh:
        assert fh.read().startswith(b"EXPORT[application/vnd.openxmlformats-officedocument.spreadsheetml.sheet]")
    assert drive.calls("files/budget") and any(r.url.path.endswith("/files/budget") for r in drive.requests)   # looked the type up
    media = [r for r in drive.requests if r.url.params.get("alt") == "media"]
    assert media and all(r.url.params["supportsAllDrives"] == "true" for r in media)


def test_open_explains_exports_that_are_too_large_or_forbidden_without_calling_it_an_auth_failure(drive):
    populate(drive)
    c = make(drive)
    sf = SourceFile("plan", "Docs/Plan.docx", "Plan.docx", 0, None, "v", [], extra={"gdrive_mime": DOC})
    drive.script.append(("/export", 403, {}, error_body(403, "exportSizeLimitExceeded", "This file is too large to be exported.")))
    with pytest.raises(HttpError) as err:
        c.open(sf)
    assert not isinstance(err.value, AuthError) and err.value.status == 403 and "cannot be downloaded" in str(err.value)
    drive.script.append(("files/q1", 403, {}, error_body(403, "cannotDownloadFile", "The user does not have sufficient permissions")))
    with pytest.raises(HttpError) as err2:
        c.open(SourceFile("q1", "p", "Q1.pdf", 0, None, "v", [], extra={"gdrive_mime": "application/pdf"}))
    assert not isinstance(err2.value, AuthError)
    with pytest.raises(HttpError, match="cannot be exported"):
        c.open(SourceFile("form", "p", "f", 0, None, "v", [], extra={"gdrive_mime": FORM}))


def test_oversize_files_are_skipped_in_listings_and_refused_by_open(drive):
    drive.add("small.bin", mime="application/octet-stream", data=b"s" * 10, fid="small")
    drive.add("big.bin", mime="application/octet-stream", data=b"b" * 5000, fid="big")
    c = make(drive, max_file_bytes=1000)
    changes, _ = scan_all(c)
    assert list(by_id(changes)) == ["small"] and c.stats["oversize"] == 1
    with pytest.raises(TooLarge):
        c.open(SourceFile("big", "p", "big.bin", 0, None, "v", []))                         # size looked up via files.get
    assert not [r for r in drive.requests if r.url.params.get("alt") == "media"]            # refused before any download
    drive.exports[("plan2", DOCX)] = b"x" * 5000
    drive.add("Plan2", mime=DOC, fid="plan2")
    with pytest.raises(TooLarge):
        c.open(SourceFile("plan2", "p", "Plan2.docx", 0, None, "v", [], extra={"gdrive_mime": DOC}))


# ── rate limits and errors ──


@pytest.mark.parametrize("status,reason,headers", [
    (403, "userRateLimitExceeded", {"Retry-After": "5"}), (403, "rateLimitExceeded", {}),
    (403, "sharingRateLimitExceeded", {}), (429, "rateLimitExceeded", {"Retry-After": "5"}), (500, "backendError", {}),
    (503, "backendError", {}),
])
def test_rate_limits_are_retried_on_listing_and_download(drive, status, reason, headers):
    populate(drive)
    c = make(drive)
    drive.script.extend([("/files", status, headers, error_body(status, reason))] * 2)
    changes, _ = scan_all(c)
    assert len(changes) == 5 and len(c.sleeps) == 2
    assert c.sleeps == ([5.0, 5.0] if headers else [1, 2])
    sf = by_id(changes)["n1"].file
    drive.script.append(("files/n1", status, headers, error_body(status, reason)))
    with c.open(sf) as fh:
        assert fh.read() == b"root notes"


def test_persistent_rate_limiting_gives_up_with_a_clear_non_auth_error(drive):
    c = make(drive, max_retries=2)
    drive.script.extend([("/changes/startPageToken", 403, {}, error_body(403, "userRateLimitExceeded", "slow down"))] * 5)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert not isinstance(err.value, AuthError) and err.value.status == 429 and c.sleeps == [1, 2]


def test_other_403_errors_are_auth_errors_and_are_not_retried(drive):
    c = make(drive)
    drive.script.append(("/changes/startPageToken", 403, {}, error_body(403, "insufficientPermissions", "Insufficient Permission")))
    with pytest.raises(AuthError) as err:
        scan_all(c)
    assert err.value.status == 403 and c.sleeps == []


def test_invalid_token_is_an_auth_error_that_does_not_leak_it(drive, monkeypatch):
    monkeypatch.setenv("T_GD_TOKEN", "WRONG-" + TOKEN)
    c = make(drive)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert err.value.status == 401 and TOKEN not in blob and "WRONG-" not in blob and c.sleeps == []


def test_unset_token_variable_is_named_and_nothing_is_sent(drive, monkeypatch):
    monkeypatch.delenv("T_GD_TOKEN")
    with pytest.raises(HttpError) as err:
        scan_all(make(drive))
    assert "T_GD_TOKEN" in str(err.value) and drive.requests == []
    with pytest.raises(HttpError, match="configure access_token_env"):
        scan_all(make(drive, access_token_env=None))


def test_secrets_never_reach_cursors_or_source_files(drive):
    populate(drive)
    c = make(drive, page_size=2)
    changes, batches = scan_all(c)
    assert TOKEN not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)


# ── service-account authentication (JWT bearer) ──


@pytest.fixture(scope="module")
def sa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sa_file(tmp_path, sa_key, monkeypatch, drive):
    pem = sa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"type": "service_account", "client_email": "svc@proj.iam.gserviceaccount.com",
                                "private_key": pem, "private_key_id": "kid-1", "token_uri": "https://oauth2.googleapis.com/token"}))
    monkeypatch.setenv("T_GD_SA_FILE", str(path))
    monkeypatch.delenv("T_GD_TOKEN", raising=False)
    drive.sa_public_key = sa_key.public_key()
    drive.valid_token = "none-yet"
    return path


def test_service_account_flow_signs_a_valid_rs256_jwt_and_caches_the_access_token(drive, sa_file):
    populate(drive)
    c = make(drive, access_token_env=None, service_account_file_env="T_GD_SA_FILE", delegated_user="admin@corp.com")
    changes, _ = scan_all(c)
    assert len(changes) == 5
    assert len(drive.token_requests) == 1                                  # one token for many API calls
    form = drive.token_requests[0]
    assert form["grant_type"].startswith("urn%3Aietf") or form["grant_type"].startswith("urn:ietf")
    claims = drive.last_claims                                              # signature was verified by the fake with the public key
    assert claims["iss"] == "svc@proj.iam.gserviceaccount.com" and claims["sub"] == "admin@corp.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/drive.readonly" and claims["aud"] == "https://oauth2.googleapis.com/token"
    assert claims["exp"] - claims["iat"] == 3600 and claims["iat"] == int(c.clock.now)
    header = json.loads(base64.urlsafe_b64decode(form["assertion"].split(".")[0] + "=="))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    c.clock.now += 3000                                                    # still valid
    scan_all(c)
    assert len(drive.token_requests) == 1
    c.clock.now += 400                                                     # inside the 5 minute safety margin => refresh
    scan_all(c)
    assert len(drive.token_requests) == 2


def test_a_401_with_a_service_account_refreshes_the_token_once_and_retries(drive, sa_file):
    populate(drive)
    c = make(drive, access_token_env=None, service_account_file_env="T_GD_SA_FILE")
    scan_all(c)
    assert len(drive.token_requests) == 1
    drive.valid_token = "revoked-and-reissued-elsewhere"                   # the cached token stops working
    drive.sa_public_key = drive.sa_public_key
    original = drive.token_endpoint

    def reissue(request):
        response = original(request)
        drive.valid_token = "fresh-token"
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3599})

    drive.token_endpoint = reissue
    changes, _ = scan_all(c)
    assert len(changes) == 5 and len(drive.token_requests) == 2


def test_rejected_service_account_credentials_are_a_clear_auth_error_without_the_key(drive, sa_file, sa_key):
    c = make(drive, access_token_env=None, service_account_file_env="T_GD_SA_FILE")
    drive.token_response = (400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
    with pytest.raises(AuthError) as err:
        scan_all(c)
    text_ = str(err.value) + repr(err.value)
    assert "invalid_grant" in text_ and "PRIVATE KEY" not in text_ and "assertion" not in text_ and c.sleeps == []


def test_broken_service_account_files_are_reported_by_variable_name_not_content(drive, sa_file, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"client_email": "x"}')
    monkeypatch.setenv("T_GD_SA_FILE", str(bad))
    c = make(drive, access_token_env=None, service_account_file_env="T_GD_SA_FILE")
    with pytest.raises(AuthError, match="T_GD_SA_FILE"):
        scan_all(c)
    monkeypatch.setenv("T_GD_SA_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(AuthError, match="unreadable"):
        scan_all(make(drive, access_token_env=None, service_account_file_env="T_GD_SA_FILE"))


def test_token_provider_hook_allows_application_default_credentials(drive):
    drive.valid_token = "adc-token"
    c = GDriveConnector({"default_acl": ["*"]}, transport=httpx.MockTransport(drive.handler), token_provider=lambda: "adc-token")
    populate(drive)
    assert len(scan_all(c)[0]) == 5


def test_sign_jwt_rs256_matches_the_standard_structure(sa_key):
    pem = sa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    token = sign_jwt_rs256({"iss": "a", "n": 1}, pem)
    head, body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    sa_key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(base64.urlsafe_b64decode(pad(body))) == {"iss": "a", "n": 1}
