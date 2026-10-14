"""Bitbucket Cloud connector against a fake REST API 2.0 shaped like the Bitbucket documentation: repositories, branch
refs, the `src` listing (commit_file / commit_directory entries, `next` pagination, max_depth), `?format=meta`, raw
files, diffstat between two commits (git-like: every commit is a snapshot), the issue tracker with `q` / `sort`.

Not certain of (written from memory of the docs): whether the `commit.hash` of a listing entry is the listed commit or
the last commit that touched the file (the connector is correct either way), the direction of the `a..b` diffstat spec
(the connector uses only the set of touched paths, and the fake can answer inverted), the exact `q` syntax for dates
(written unquoted), and the wording of Bitbucket's error bodies.
"""

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlencode

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.bitbucket import BitbucketConnector

TOKEN = "bbat-SECRETtoken0123456789"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
API = "https://api.bitbucket.org/2.0"


def commit_hash(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()


C1, C2, C3 = commit_hash("c1"), commit_hash("c2"), commit_hash("c3")
README, MAIN, DOC, CFG = b"# Payments\nHello", b"print('hi')\n", b"Design notes\n", b'{"a": 1}'
SNAP1 = {"README.md": README, "src/main.py": MAIN, "src/util/helpers.py": b"def h(): pass\n", "docs/design.md": DOC, "conf/app.json": CFG}


def person(name):
    return {"type": "user", "display_name": name, "nickname": name.split()[0].lower(), "uuid": "{" + commit_hash(name)[:8] + "}"}


def issue(n, title, *, content="Something broke", updated="2026-03-01T10:00:00.000000+00:00", state="new", **kw):
    return {"type": "issue", "id": n, "title": title, "content": {"type": "rendered", "raw": content, "markup": "markdown", "html": f"<p>{content}</p>"},
            "state": state, "priority": "major", "kind": "bug", "reporter": person("Ada Lovelace"), "assignee": person("Grace Hopper"),
            "created_on": "2026-02-01T09:00:00.000000+00:00", "updated_on": updated, "edited_on": None, "votes": 0, "watches": 1,
            "component": None, "milestone": None, "version": None,
            "links": {"html": {"href": f"https://bitbucket.org/acme/payments/issues/{n}"}}, **kw}


def comment(cid, name, raw, created="2026-03-02T08:00:00.000000+00:00", deleted=False):
    return {"type": "issue_comment", "id": cid, "content": {"type": "rendered", "raw": raw, "markup": "markdown", "html": f"<p>{raw}</p>"},
            "user": person(name), "created_on": created, "updated_on": created, "deleted": deleted}


def repo(snapshots=None, branches=None, **kw):
    return {"info": {"type": "repository", "full_name": "acme/payments", "slug": "payments", "is_private": True, "has_issues": True,
                     "mainbranch": {"type": "branch", "name": "main"}, "links": {"html": {"href": "https://bitbucket.org/acme/payments"}}},
            "commits": copy.deepcopy(snapshots or {C1: SNAP1}), "branches": branches or {"main": C1}, "attrs": {}, **kw}


class Bitbucket:
    def __init__(self, repos, *, cap=2, header=None, honour_depth=True, swap_diffstat=False, reverse=True, per_file_commit=False,
                 bad_commit=False, no_meta_path=False):
        self.repos = copy.deepcopy(repos)
        self.cap, self.honour_depth, self.swap = cap, honour_depth, swap_diffstat
        self.reverse, self.per_file_commit, self.bad_commit, self.no_meta_path = reverse, per_file_commit, bad_commit, no_meta_path
        self.header = header or f"Bearer {TOKEN}"
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def paths(self):
        return [self.raw(r).removeprefix("/2.0") for r in self.requests]

    @staticmethod
    def raw(request):
        return request.url.raw_path.decode().split("?")[0]

    def calls(self, pattern):
        return [r for r in self.requests if re.search(pattern, self.raw(r))]

    @staticmethod
    def error(status, message):
        return httpx.Response(status, json={"type": "error", "error": {"message": message}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != self.header:
            return self.error(401, "Invalid credentials")
        path = self.raw(request)
        m = re.fullmatch(r"/2\.0/repositories/([^/]+)/([^/]+)(/.*)?", path)
        if not m or f"{m.group(1)}/{m.group(2)}" not in self.repos:
            return self.error(404, "Repository not found")
        data, rest, q = self.repos[f"{m.group(1)}/{m.group(2)}"], m.group(3) or "", dict(request.url.params)
        if rest == "":
            return httpx.Response(200, json=data["info"])
        m = re.fullmatch(r"/refs/branches/(.+)", rest)
        if m:
            if "/" in m.group(1):                                              # a branch name with a slash must arrive percent-encoded
                return self.error(404, "Branch not found")
            name = unquote(m.group(1))
            if name not in data["branches"]:
                return self.error(404, f"Branch {name} not found")
            return httpx.Response(200, json={"type": "branch", "name": name, "target": {"type": "commit", "hash": data["branches"][name]}})
        m = re.fullmatch(r"/diffstat/([0-9a-f]{40})\.\.([0-9a-f]{40})", rest)
        if m:
            return self.diffstat(data, m.group(1), m.group(2), q, request)
        m = re.fullmatch(r"/src/([0-9a-f]{40})/(.*)", rest)
        if m:
            return self.src(data, m.group(1), unquote(m.group(2)), q, request)
        if rest == "/issues":
            return self.issues(data, q, request)
        m = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
        if m:
            number = int(m.group(1))
            if m.group(2):
                return self.page(data.get("comments", {}).get(number, []), q, request)
            found = [i for i in data.get("issues", []) if i["id"] == number]
            return httpx.Response(200, json=found[0]) if found else self.error(404, "No Issue matches the given query.")
        return self.error(404, "Not found")

    def page(self, items, q, request):
        page, per = int(q.get("page", 1)), min(int(q.get("pagelen", 10)), self.cap)
        body = {"pagelen": per, "page": page, "size": len(items), "values": items[(page - 1) * per: page * per]}
        if page * per < len(items):
            body["next"] = f"{API}{self.raw(request).removeprefix('/2.0')}?{urlencode({**q, 'page': page + 1})}"
        if page > 1:
            body["previous"] = "https://example.invalid/previous"
        return httpx.Response(200, json=body)

    # ── source ──

    @staticmethod
    def last_touch(data, commit, path):
        order = list(data["commits"])
        last = commit
        for earlier in reversed(order[:order.index(commit)]):
            if data["commits"][earlier].get(path) != data["commits"][commit].get(path):
                break
            last = earlier
        return last

    def entry(self, data, commit, path, kind):
        snapshot = data["commits"][commit]
        shown = self.last_touch(data, commit, path) if (self.per_file_commit and kind == "commit_file") else commit
        shown = shown[:7] if self.bad_commit else shown
        body = {"path": path, "type": kind, "commit": {"type": "commit", "hash": shown}, "attributes": list(data["attrs"].get(path, [])),
                "escaped_path": path, "links": {"self": {"href": f"{API}/repositories/acme/payments/src/{commit}/{path}"},
                                                "meta": {"href": f"{API}/repositories/acme/payments/src/{commit}/{path}?format=meta"}}}
        if kind == "commit_file":
            body["size"] = len(snapshot[path])
        return body

    def src(self, data, commit, path, q, request):
        if commit not in data["commits"]:
            return self.error(404, "Commit not found")
        snapshot = data["commits"][commit]
        if q.get("format") == "meta":
            if path in snapshot:
                body = self.entry(data, commit, path, "commit_file")
                if self.no_meta_path:
                    body.pop("path")
                return httpx.Response(200, json=body)
            if any(p.startswith(path + "/") for p in snapshot):
                return httpx.Response(200, json=self.entry(data, commit, path, "commit_directory"))
            return self.error(404, "No such file or directory")
        if path == "" or path.endswith("/"):
            directory = path.rstrip("/")
            depth = int(q.get("max_depth", 1)) if self.honour_depth else 1
            found = {}
            for p in snapshot:
                if directory and not p.startswith(directory + "/"):
                    continue
                parts = (p[len(directory) + 1:] if directory else p).split("/")
                if len(parts) <= depth:
                    found[p] = self.entry(data, commit, p, "commit_file")
                for i in range(1, min(len(parts), depth + 1)):
                    d = (directory + "/" if directory else "") + "/".join(parts[:i])
                    found[d] = self.entry(data, commit, d, "commit_directory")
            if not found and directory:
                return self.error(404, "No such directory")
            return self.page([found[k] for k in sorted(found, reverse=self.reverse)], q, request)
        if path not in snapshot:
            return self.error(404, "No such file")
        return httpx.Response(200, content=snapshot[path], headers={"content-type": "application/octet-stream"})

    def diffstat(self, data, new, old, q, request):
        if new not in data["commits"] or old not in data["commits"]:
            return self.error(404, "Commit not found")
        a, b = data["commits"][new], data["commits"][old]
        records = []
        for p in sorted(set(a) | set(b)):
            if a.get(p) == b.get(p):
                continue
            status = "added" if p not in b else "removed" if p not in a else "modified"
            side = lambda present: {"path": p, "type": "commit_file", "escaped_path": p} if present else None      # noqa: E731
            record = {"type": "diffstat", "status": status, "lines_added": 1, "lines_removed": 0, "old": side(p in b), "new": side(p in a)}
            if self.swap:
                record = {**record, "status": {"added": "removed", "removed": "added"}.get(status, status), "old": record["new"], "new": record["old"]}
            records.append(record)
        return self.page(records, q, request)

    # ── issues ──

    def issues(self, data, q, request):
        assert q.get("sort") == "updated_on"
        items = list(data.get("issues", []))
        if "q" in q:
            m = re.fullmatch(r"updated_on>(\S+)", q["q"])
            assert m, f"unexpected q: {q['q']}"
            floor = datetime.fromisoformat(m.group(1))
            items = [i for i in items if datetime.fromisoformat(i["updated_on"]) > floor]
        return self.page(sorted(items, key=lambda i: i["updated_on"]), q, request)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BB_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "BB_TOKEN", "repos": ["acme/payments"], "default_acl": ["group:eng"], **cfg}
    connector = BitbucketConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
    connector.sleeps = sleeps
    return connector


def run(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


def persisted(sf):
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


def read(connector, sf):
    with connector.open(persisted(sf)) as fh:
        return fh.read().decode()


def scopes(batches):
    return [b.complete_scope for b in batches if b.complete_scope]


def one(**kw):
    return Bitbucket({"acme/payments": repo(**kw)})


def key(change):
    return (change.kind, change.external_id.removeprefix("bitbucket:acme/payments/src/"))


# ── files: the full listing ──


def test_listing_at_the_branch_tip_follows_next_links_and_describes_every_file():
    site = one()
    changes, batches = run(make(site))
    by = {c.external_id: c.file for c in changes}
    assert sorted(by) == [f"bitbucket:acme/payments/src/{p}" for p in sorted(SNAP1)]
    f = by["bitbucket:acme/payments/src/src/main.py"]
    assert f.path == "bitbucket/acme/payments/src/main.py" and f.name == "main.py" and f.size == len(MAIN) and f.acl == ["group:eng"]
    assert f.version == f"{len(MAIN)}-{C1}" and f.mime in ("text/x-python", "text/x-python-script")
    assert site.paths()[:2] == ["/repositories/acme/payments", "/repositories/acme/payments/refs/branches/main"]
    listing = site.calls(r"/src/[0-9a-f]{40}/$")
    assert listing[0].url.params["max_depth"] == "10" and listing[0].url.params["pagelen"] == "100"
    assert [r.url.params.get("page") for r in listing] == [None, "2", "3", "4", "5"]         # 5 files + 4 directories, 2 per page
    assert all(f"/src/{C1}/" in site.raw(r) for r in listing)                              # the commit, not the branch name
    assert batches[-1].done and scopes(batches) == ["bitbucket:acme/payments/src/"]
    assert batches[-1].cursor["heads"] == {"acme/payments": C1}


def test_a_next_link_to_another_host_is_refused_and_the_credentials_never_go_there():
    site = one()
    original = site.page

    def evil(items, q, request):
        r = original(items, q, request)
        body = r.json()
        if "next" in body:
            body["next"] = "https://evil.example/steal?page=2"
        return httpx.Response(200, json=body)
    site.page = evil
    with pytest.raises(HttpError, match="another host"):
        run(make(site))
    assert {r.url.host for r in site.requests} == {"api.bitbucket.org"}


def test_directories_cut_off_by_the_depth_limit_are_listed_on_their_own_even_if_the_server_ignores_max_depth():
    for kwargs, site in (({"max_depth": 1}, one()), ({}, Bitbucket({"acme/payments": repo()}, honour_depth=False))):
        site.reset()
        changes, batches = run(make(site, **kwargs))
        assert sorted(c.external_id for c in changes) == [f"bitbucket:acme/payments/src/{p}" for p in sorted(SNAP1)]
        assert scopes(batches) == ["bitbucket:acme/payments/src/"]
        assert any(site.raw(r).endswith("/src/util/") for r in site.requests)                # the nested directory was asked for by itself


def test_symlinks_submodules_vendored_binaries_and_large_files_are_not_files_to_index():
    snap = {"a.md": b"a", "link": b"a.md", "sub": b"", "node_modules/x/index.js": b"js", "logo.png": b"png", "big.txt": b"y" * 3000}
    site = one(snapshots={C1: snap})
    site.repos["acme/payments"]["attrs"] = {"link": ["link"], "sub": ["subrepository"]}
    changes, _ = run(make(site, max_file_mb=0.001))
    assert [c.external_id for c in changes] == ["bitbucket:acme/payments/src/a.md"]


def test_include_and_exclude_globs_select_files():
    changes, _ = run(make(one(), include=["*.py", "*.md"], exclude=["README.md"]))
    assert sorted(c.file.path.removeprefix("bitbucket/acme/payments/") for c in changes) == ["docs/design.md", "src/main.py", "src/util/helpers.py"]


def test_an_empty_repository_lists_nothing_and_declares_the_scope_so_old_files_are_deleted():
    site = one()
    site.repos["acme/payments"]["info"]["mainbranch"] = None
    changes, batches = run(make(site))
    assert changes == [] and scopes(batches) == ["bitbucket:acme/payments/src/"] and not site.calls("/src/")


def test_branch_and_commit_refs_and_a_missing_branch():
    site = Bitbucket({"acme/payments": repo({C1: SNAP1, C2: {"only.md": b"x"}}, {"main": C1, "dev": C2})})
    for entry, expected in (("acme/payments@dev", "bitbucket:acme/payments/src/only.md"), ({"repo": "acme/payments", "ref": "dev"}, "bitbucket:acme/payments/src/only.md"),
                            (f"acme/payments@{C2}", "bitbucket:acme/payments/src/only.md")):
        site.reset()
        changes, _ = run(make(site, repos=[entry]))
        assert [c.external_id for c in changes] == [expected]
    site.reset()
    run(make(site, repos=[f"acme/payments@{C2}"]))
    assert not site.calls("/refs/branches/") and not site.calls(r"payments$")                 # a commit hash needs no lookup at all
    with pytest.raises(HttpError) as err:
        run(make(site, repos=["acme/payments@nope"]))
    assert err.value.status == 404


def test_batches_resume_after_the_last_path_and_a_resumed_listing_is_not_authoritative():
    site = one()
    batches = list(make(site, batch_size=2).scan({}))
    first = batches[0]
    assert [c.file.path.removeprefix("bitbucket/acme/payments/") for c in first.changes] == ["README.md", "conf/app.json"]
    assert first.cursor["pos"] == {"repo": 0, "phase": "files", "head": C1, "full": True, "after": "conf/app.json"} and first.complete_scope is None
    assert first.cursor["heads"] == {}                                                     # not recorded before the repository is done
    assert json.loads(json.dumps(first.cursor)) == first.cursor
    site.reset()
    changes, resumed = run(make(site, batch_size=2), first.cursor)
    assert [c.file.path.removeprefix("bitbucket/acme/payments/") for c in changes] == ["docs/design.md", "src/main.py", "src/util/helpers.py"]
    assert scopes(resumed) == [] and not site.calls("/refs/branches/")                      # same commit as before the crash
    assert [b.complete_scope for b in batches if b.complete_scope] == ["bitbucket:acme/payments/src/"]


def test_repositories_are_scanned_in_order_each_with_its_own_scope_and_resume_picks_up_at_the_right_one():
    site = Bitbucket({"acme/payments": repo(), "acme/website": repo({C3: {"index.md": b"hello"}}, {"main": C3})})
    repos = ["acme/payments", "acme/website"]
    batches = list(make(site, repos=repos).scan({}))
    assert scopes(batches) == ["bitbucket:acme/payments/src/", "bitbucket:acme/website/src/"]
    boundary = next(b for b in batches if b.complete_scope == "bitbucket:acme/payments/src/")
    assert boundary.cursor["pos"] == {"repo": 1, "phase": "files"} and boundary.cursor["heads"] == {"acme/payments": C1}
    site.reset()
    changes, resumed = run(make(site, repos=repos), boundary.cursor)
    assert [c.external_id for c in changes] == ["bitbucket:acme/website/src/index.md"] and scopes(resumed) == ["bitbucket:acme/website/src/"]
    assert not site.calls("repositories/acme/payments")
    assert resumed[-1].cursor["heads"] == {"acme/payments": C1, "acme/website": C3}


def test_a_repository_listed_twice_is_scanned_once():
    site = one()
    changes, batches = run(make(site, repos=["acme/payments", "acme/payments@main", {"repo": "acme/payments", "ref": "dev"}]))
    assert len(changes) == 5 and scopes(batches) == ["bitbucket:acme/payments/src/"]


def test_max_items_and_max_pages_truncate_never_declare_completeness_and_never_advance_the_commit():
    site = Bitbucket({"acme/payments": repo(), "acme/website": repo({C3: {"index.md": b"hello"}}, {"main": C3})})
    repos = ["acme/payments", "acme/website"]
    changes, batches = run(make(site, repos=repos, max_items=2, batch_size=1))
    assert len(changes) == 2 and scopes(batches) == [] and "since" not in batches[-1].cursor and batches[-1].cursor["heads"] == {}
    assert len(batches) == 4 and not site.calls("repositories/acme/website")                # 2 items, the cut batch, done
    site.reset()
    changes, batches = run(make(site, repos=repos, max_pages=2))                             # the listing needs five pages
    assert scopes(batches) == [] and batches[-1].done and batches[-1].cursor["heads"] == {} and len(site.calls(r"/src/")) == 2
    site.reset()
    changes, batches = run(make(site, repos=repos, max_pages=5))                             # exactly the first repository's listing
    assert len(changes) == 5 and scopes(batches) == ["bitbucket:acme/payments/src/"] and batches[-1].cursor["heads"] == {"acme/payments": C1}
    assert not site.calls("repositories/acme/website")


# ── files: incremental through diffstat ──


def pushed(extra_branches=None):
    """C1 -> C2: edit, add, delete, rename, and a file that is excluded."""
    snap2 = dict(SNAP1)
    snap2["src/main.py"] = b"print('hello')\n"
    snap2["NEW.md"] = b"new file"
    del snap2["conf/app.json"]
    snap2["docs/architecture.md"] = snap2.pop("docs/design.md")
    snap2["logo.png"] = b"png"
    return {C1: SNAP1, C2: snap2}


def finished_cursor(site, **cfg):
    _, batches = run(make(site, **cfg))
    return batches[-1].cursor


def test_after_a_push_only_the_touched_paths_are_looked_at_and_settled_against_the_new_head():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})})
    cursor = finished_cursor(site)
    assert cursor["heads"] == {"acme/payments": C1} and cursor["since"] and cursor["last_full"] == NOW
    site.repos["acme/payments"]["branches"]["main"] = C2
    site.reset()
    changes, batches = run(make(site, clock=lambda: NOW + 3600), cursor)
    assert sorted(key(c) for c in changes) == [("delete", "conf/app.json"), ("delete", "docs/design.md"), ("delete", "logo.png"),
                                               ("upsert", "NEW.md"), ("upsert", "docs/architecture.md"), ("upsert", "src/main.py")]
    assert scopes(batches) == [] and batches[-1].cursor["heads"] == {"acme/payments": C2}
    assert site.paths()[2] == f"/repositories/acme/payments/diffstat/{C2}..{C1}"
    assert not [r for r in site.requests if "max_depth" in r.url.params]                  # no listing happened
    main = next(c.file for c in changes if c.external_id.endswith("src/main.py"))
    new_content = b"print('hello')\n"
    assert main.version == f"{len(new_content)}-{C2}" and main.size == len(new_content)
    assert read(make(site), main) == new_content.decode()


def test_an_unchanged_head_costs_two_requests_and_lists_nothing():
    site = one()
    cursor = finished_cursor(site)
    site.reset()
    changes, batches = run(make(site, clock=lambda: NOW + 60), cursor)
    assert changes == [] and scopes(batches) == [] and site.paths() == ["/repositories/acme/payments", "/repositories/acme/payments/refs/branches/main"]
    assert batches[-1].cursor["heads"] == {"acme/payments": C1}


def test_the_diff_is_read_by_paths_only_so_an_inverted_server_cannot_cause_wrong_deletes():
    inverted = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})}, swap_diffstat=True)
    cursor = finished_cursor(inverted)
    inverted.repos["acme/payments"]["branches"]["main"] = C2
    changes, _ = run(make(inverted, clock=lambda: NOW + 3600), cursor)
    assert sorted(key(c) for c in changes) == [("delete", "conf/app.json"), ("delete", "docs/design.md"), ("delete", "logo.png"),
                                               ("upsert", "NEW.md"), ("upsert", "docs/architecture.md"), ("upsert", "src/main.py")]


def test_diffstat_is_paginated_and_resumes_after_the_last_settled_path():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})})
    cursor = finished_cursor(site)
    site.repos["acme/payments"]["branches"]["main"] = C2
    site.reset()
    batches = list(make(site, clock=lambda: NOW + 3600, batch_size=2).scan(cursor))
    assert len(site.calls("/diffstat/")) == 3                                               # 6 touched paths (logo.png among them), 2 per page
    first = batches[0]
    assert first.cursor["pos"]["full"] is False and first.cursor["prev"] == {"acme/payments": C1} and first.cursor["heads"] == {}
    seen = [key(c) for b in batches for c in b.changes]
    site.reset()
    changes, resumed = run(make(site, clock=lambda: NOW + 3600, batch_size=2), first.cursor)
    assert [key(c) for c in first.changes] + [key(c) for c in changes] == seen
    assert resumed[-1].cursor["heads"] == {"acme/payments": C2} and scopes(resumed) == []


def test_a_diffstat_that_fails_falls_back_to_the_full_listing_which_is_authoritative():
    site = Bitbucket({"acme/payments": repo({C2: pushed()[C2]}, {"main": C2})})                  # C1 is gone (a force push)
    cursor = {"since": "2026-03-01T00:00:00Z", "last_full": NOW, "heads": {"acme/payments": C1}}
    changes, batches = run(make(site, clock=lambda: NOW + 60), cursor)
    assert len(site.calls("/diffstat/")) == 1 and scopes(batches) == ["bitbucket:acme/payments/src/"]
    assert len(changes) == len(pushed()[C2]) - 1                                            # logo.png is excluded
    assert batches[-1].cursor["heads"] == {"acme/payments": C2}


def test_a_full_resync_lists_everything_again_even_when_the_commit_is_known():
    site = one()
    cursor = finished_cursor(site)
    site.reset()
    changes, batches = run(make(site, clock=lambda: NOW + 25 * 3600), cursor)
    assert len(changes) == 5 and scopes(batches) == ["bitbucket:acme/payments/src/"] and not site.calls("/diffstat/")


def test_an_auth_failure_in_diffstat_is_an_error_not_a_reason_to_fall_back():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C2})})
    cursor = {"since": "2026-03-01T00:00:00Z", "last_full": NOW, "heads": {"acme/payments": C1}}
    site.script = [httpx.Response(200, json=site.repos["acme/payments"]["info"]), httpx.Response(200, json={"target": {"hash": C2}}),
                   httpx.Response(403, json={"type": "error", "error": {"message": "no"}})]
    with pytest.raises(AuthError):
        run(make(site, clock=lambda: NOW + 60), cursor)


# ── open() ──


def test_open_downloads_the_file_at_the_commit_in_its_version_from_persisted_columns_only():
    site = one()
    by = {c.external_id: c.file for c in run(make(site))[0]}
    site.reset()
    assert read(make(site), by["bitbucket:acme/payments/src/src/util/helpers.py"]) == "def h(): pass\n"
    assert site.paths() == [f"/repositories/acme/payments/src/{C1}/src/util/helpers.py"]
    assert site.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_open_enforces_the_size_cap_and_rejects_bad_ids_without_a_request():
    big = b"y" * 3000
    site = one(snapshots={C1: {"big.txt": big}})
    with pytest.raises(TooLarge):
        make(site, max_file_mb=0.001).open(SourceFile("bitbucket:acme/payments/src/big.txt", "p", "big.txt", 0, None, f"3000-{C1}", []))
    site.reset()
    connector = make(site)
    for ext, version in (("bitbucket:acme/payments/src/a.md", "not-a-version"), ("bitbucket:acme/payments/src/a.md", f"x-{C1}"),
                         ("bitbucket:../x/src/a.md", f"1-{C1}"), ("bitbucket:ac me/pay/src/a.md", f"1-{C1}"), ("github:acme/payments/src/a.md", f"1-{C1}"),
                         ("bitbucket:acme/payments/src/../../x", f"1-{C1}"), ("bitbucket:acme/payments/src/a//b", f"1-{C1}"),
                         ("bitbucket:acme/payments/issue/abc", "v"), ("bitbucket:acme/payments/other/a", f"1-{C1}")):
        with pytest.raises(HttpError):
            connector.open(SourceFile(ext, "p", "a", 0, None, version, []))
    assert site.requests == []


def test_a_file_that_vanished_is_an_error_with_a_status():
    with pytest.raises(HttpError) as err:
        make(one()).open(SourceFile("bitbucket:acme/payments/src/gone.md", "p", "gone.md", 0, None, f"3-{C1}", []))
    assert err.value.status == 404


def test_paths_with_spaces_and_unicode_are_encoded_in_every_url():
    snap = {"docs/my notes/ü.md": b"hello"}
    site = one(snapshots={C1: snap})
    changes, _ = run(make(site))
    assert [c.external_id for c in changes] == ["bitbucket:acme/payments/src/docs/my notes/ü.md"]
    assert read(make(site), changes[0].file) == "hello"
    site.repos["acme/payments"]["commits"][C2] = {**snap, "docs/my notes/ü.md": b"changed"}
    site.repos["acme/payments"]["branches"]["main"] = C2
    cursor = {"since": "2026-03-01T00:00:00Z", "last_full": NOW, "heads": {"acme/payments": C1}}
    changes, _ = run(make(site, clock=lambda: NOW + 60), cursor)
    assert [key(c) for c in changes] == [("upsert", "docs/my notes/ü.md")]


# ── issues ──

ISSUES = [issue(n, f"Issue {n}", updated=f"2026-03-0{n}T10:00:00.000000+00:00", state="open" if n % 2 else "resolved") for n in range(1, 6)]


def with_issues(**kw):
    return Bitbucket({"acme/payments": repo(issues=copy.deepcopy(ISSUES), **kw)})


def issues_only(site, **cfg):
    return make(site, include_files=False, include_issues=True, **cfg)


def test_issues_follow_next_links_and_a_full_listing_is_authoritative():
    site = with_issues()
    changes, batches = run(issues_only(site))
    assert [c.external_id for c in changes] == [f"bitbucket:acme/payments/issue/{n}" for n in range(1, 6)]
    listing = site.calls(r"/issues$")
    assert [r.url.params.get("page") for r in listing] == [None, "2", "3"]
    assert listing[0].url.params["sort"] == "updated_on" and listing[0].url.params["pagelen"] == "100" and "q" not in listing[0].url.params
    assert scopes(batches) == ["bitbucket:acme/payments/issue/"] and batches[-1].done
    assert changes[0].file.path == "bitbucket/acme/payments/_issues/1-Issue 1.md"


def test_issue_text_has_metadata_content_and_live_comments_across_pages():
    comments = {7: [comment(n, "Grace Hopper", f"comment number {n}") for n in range(1, 4)] + [comment(9, "Ada Lovelace", "gone", deleted=True), comment(10, "Ada Lovelace", "  ")]}
    site = Bitbucket({"acme/payments": repo(issues=[issue(7, "Crash on start", content="Steps:\n1. do it")], comments=comments)})
    connector = issues_only(site)
    sf = run(connector)[0][0].file
    assert len(site.calls(r"/issues/7/comments$")) == 3
    text = read(connector, sf)
    for line in ("repository: acme/payments", "type: issue", "state: new", "kind: bug", "priority: major", "reporter: Ada Lovelace",
                 "assignee: Grace Hopper", "url: https://bitbucket.org/acme/payments/issues/7"):
        assert line in text
    assert text.startswith("# acme/payments#7: Crash on start\n") and "1. do it" in text
    assert all(f"comment number {n}" in text for n in (1, 2, 3)) and "gone" not in text and "### Ada" not in text
    assert sf.size == len(text.encode()) and sf.version.startswith("2026-03-01T10:00:00.000000+00:00-")


def test_issue_content_without_raw_falls_back_to_the_html_converted_to_text():
    item = issue(3, "HTML only", content="")
    item["content"] = {"type": "rendered", "html": "<p>Hello <b>world</b></p><ul><li>one</li><li>two</li></ul><script>x()</script>"}
    site = Bitbucket({"acme/payments": repo(issues=[item])})
    text = read(issues_only(site), run(issues_only(site))[0][0].file)
    assert "Hello **world**" in text or "Hello world" in text
    assert "one" in text and "two" in text and "<p>" not in text and "x()" not in text


def test_issue_version_follows_the_text_not_just_the_timestamp():
    base = repo(issues=[issue(7, "Chatty")], comments={7: [comment(1, "Grace Hopper", "first")]})
    v1 = run(issues_only(Bitbucket({"acme/payments": base})))[0][0].file.version
    assert run(issues_only(Bitbucket({"acme/payments": copy.deepcopy(base)})))[0][0].file.version == v1
    edited = copy.deepcopy(base)
    edited["comments"][7][0]["content"]["raw"] = "edited later"
    assert run(issues_only(Bitbucket({"acme/payments": edited})))[0][0].file.version != v1
    bumped = copy.deepcopy(base)
    bumped["issues"][0]["updated_on"] = "2026-03-09T10:00:00.000000+00:00"
    assert run(issues_only(Bitbucket({"acme/payments": bumped})))[0][0].file.version != v1


def test_issue_scan_after_a_finished_one_filters_on_updated_on_with_overlap_and_is_not_authoritative():
    site = with_issues()
    _, batches = run(issues_only(site))
    cursor = batches[-1].cursor
    assert cursor["since"] == "2026-03-05T10:00:00Z" and cursor["last_full"] == NOW
    site.reset()
    changes, batches = run(issues_only(site, clock=lambda: NOW + 3600), cursor)
    assert {r.url.params["q"] for r in site.calls(r"/issues$")} == {"updated_on>2026-03-05T09:55:00+00:00"}
    assert [c.external_id for c in changes] == ["bitbucket:acme/payments/issue/5"] and scopes(batches) == []
    assert batches[-1].cursor["since"] == "2026-03-05T10:00:00Z" and batches[-1].cursor["last_full"] == NOW


def test_full_resync_when_due_and_issues_since_limits_the_first_scan():
    site = with_issues()
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW}
    changes, batches = run(issues_only(site, clock=lambda: NOW + 25 * 3600), cursor)
    assert len(changes) == 5 and scopes(batches) == ["bitbucket:acme/payments/issue/"] and "q" not in site.calls(r"/issues$")[0].url.params
    site.reset()
    changes, _ = run(issues_only(site, issues_since="2026-03-04"))
    assert [c.external_id for c in changes] == ["bitbucket:acme/payments/issue/4", "bitbucket:acme/payments/issue/5"]
    assert site.calls(r"/issues$")[0].url.params["q"] == "updated_on>2026-03-04T00:00:00+00:00"


def test_issue_listing_resumes_at_the_saved_next_link_and_does_not_claim_completeness():
    site = with_issues()
    connector = make(site, include_issues=True)
    mid = next(b for b in connector.scan({}) if b.cursor["pos"].get("phase") == "issues" and b.cursor["pos"].get("next"))
    assert mid.cursor["pos"]["next"].startswith(f"{API}/repositories/acme/payments/issues?")
    site.reset()
    changes, batches = run(make(site, include_issues=True), mid.cursor)
    assert [c.external_id for c in changes] == [f"bitbucket:acme/payments/issue/{n}" for n in (3, 4, 5)]
    assert site.calls(r"/issues$")[0].url.params["page"] == "2" and scopes(batches) == []
    assert not site.calls("/src/") and not site.calls(r"payments$")                          # nothing re-listed, no repo lookup for the tracker check


def test_a_repository_without_an_issue_tracker_is_an_authoritative_empty_listing():
    site = with_issues()
    site.repos["acme/payments"]["info"]["has_issues"] = False
    changes, batches = run(issues_only(site))
    assert changes == [] and scopes(batches) == ["bitbucket:acme/payments/issue/"] and not site.calls("/issues")


def test_max_items_and_max_pages_limit_the_issue_listing_without_claiming_completeness():
    site = with_issues()
    changes, batches = run(issues_only(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(issues_only(site, max_pages=2))
    assert len(site.calls(r"/issues$")) == 2 and len(changes) == 4 and scopes(batches) == []
    assert batches[-2].cursor["pos"]["phase"] == "issues" and batches[-2].cursor["pos"]["next"]


def test_issue_open_refetches_the_issue_and_renders_the_same_bytes():
    site = with_issues()
    by = {c.external_id: c.file for c in run(issues_only(site))[0]}
    site.reset()
    for sf in by.values():
        assert len(read(make(site), sf).encode()) == sf.size
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("bitbucket:acme/payments/issue/404", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


def test_files_and_issues_share_a_scan_files_first_each_with_its_scope():
    site = with_issues()
    changes, batches = run(make(site, include_issues=True))
    assert [c.external_id.split("/")[2] for c in changes] == ["src"] * 5 + ["issue"] * 5
    assert scopes(batches) == ["bitbucket:acme/payments/src/", "bitbucket:acme/payments/issue/"]
    assert site.paths().count("/repositories/acme/payments") == 1                            # the repository record is read once per scan


# ── permissions ──


def test_acl_rules_per_repository_prefix_fail_closed_for_files_and_issues():
    site = Bitbucket({"acme/payments": repo(issues=copy.deepcopy(ISSUES)), "acme/website": repo({C3: {"index.md": b"hi"}}, {"main": C3})})
    rules = [{"prefix": "bitbucket/acme/payments", "principals": ["group:payments"]},
             {"prefix": "bitbucket/acme/payments/docs", "principals": ["*"]},
             {"prefix": "bitbucket/acme/payments/_issues", "principals": ["group:support"]}]
    connector = make(site, repos=["acme/payments", "acme/website"], include_issues=True, acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["bitbucket:acme/payments/src/src/main.py"].acl == ["group:payments"] and by["bitbucket:acme/payments/src/docs/design.md"].acl == ["*"]
    assert by["bitbucket:acme/payments/issue/1"].acl == ["group:support"] and by["bitbucket:acme/website/src/index.md"].acl == []
    assert connector.refresh_acl(persisted(by["bitbucket:acme/payments/issue/1"])) == ["group:support"]
    assert connector.refresh_acl(persisted(by["bitbucket:acme/website/src/index.md"])) == []


# ── rate limits, auth ──


def test_429_is_retried_after_retry_after_and_the_listing_continues():
    site = one()
    site.script = [httpx.Response(429, headers={"retry-after": "7"}, json={"type": "error", "error": {"message": "Rate limit exceeded"}}),
                   httpx.Response(503, text="busy")]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [7.0, 2]            # Retry-After, then the default backoff of the second attempt


def test_401_never_shows_the_token_in_bearer_or_basic_mode_and_403_names_the_variable(monkeypatch):
    monkeypatch.setenv("BB_TOKEN", "bbat-wrongwrongwrong")
    for cfg, header in (({}, f"Bearer {TOKEN}"), ({"username": "ada"}, "x")):
        site = Bitbucket({"acme/payments": repo()}, header=header)
        connector = make(site, **cfg)
        with pytest.raises(AuthError) as err:
            run(connector)
        assert "401" in str(err.value) and "bbat-wrongwrongwrong" not in str(err.value) and "BB_TOKEN" in str(err.value)
        assert len(site.requests) == 1 and connector.sleeps == []
    site = one()
    site.script = [httpx.Response(403, json={"type": "error", "error": {"message": "You do not have access"}})]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert "403" in str(err.value) and "BB_TOKEN" in str(err.value)


def test_basic_auth_with_an_app_password_sends_username_and_secret_as_http_basic():
    import base64
    expected = "Basic " + base64.b64encode(f"ada:{TOKEN}".encode()).decode()
    site = Bitbucket({"acme/payments": repo()}, header=expected)
    changes, _ = run(make(site, username="ada"))
    assert len(changes) == 5
    changes, _ = run(make(site, username="ada", auth="basic", token_env=None, password_env="BB_TOKEN"))
    assert len(changes) == 5


def test_a_server_that_echoes_the_credential_in_an_error_body_never_gets_it_into_the_message():
    for username in (None, "ada"):
        import base64
        header = f"Bearer {TOKEN}" if username is None else "Basic " + base64.b64encode(f"ada:{TOKEN}".encode()).decode()
        for call in ("scan", "download", "auth-download"):
            site = Bitbucket({"acme/payments": repo()}, header=header)
            cfg = {"username": username} if username else {}
            by = {c.external_id: c.file for c in run(make(site, **cfg))[0]}
            bad = header.split(" ", 1)[1]
            body = f"rejected {TOKEN} / {bad}"
            if call == "scan":
                site.script = [httpx.Response(500, text=body)] * 6
                action = lambda: run(make(site, **cfg))        # noqa: E731
            else:
                site.script = [httpx.Response(401 if call == "auth-download" else 500, text=body)] * 6
                action = lambda: make(site, **cfg).open(persisted(by["bitbucket:acme/payments/src/README.md"]))        # noqa: E731
            with pytest.raises(HttpError) as err:
                action()
            assert TOKEN not in str(err.value) and bad not in str(err.value) and "***" in str(err.value)


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(with_issues(), include_issues=True))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("BB_TOKEN")
    with pytest.raises(HttpError, match="BB_TOKEN"):
        run(make(one()))


def test_configuration_errors_are_rejected_up_front():
    for bad in ({}, {"repos": []}):
        with pytest.raises(ValueError, match="repos"):
            BitbucketConnector({"token_env": "X", **bad})
    for bad in ("justaname", "a/b/c", "ow ner/repo", "../x", "x/..", "a/b?x=1"):
        with pytest.raises(ValueError, match="repository"):
            BitbucketConnector({"repos": [bad]})
    with pytest.raises(ValueError, match="auth"):
        BitbucketConnector({"repos": ["a/b"], "auth": "oauth"})
    with pytest.raises(ValueError, match="username"):
        BitbucketConnector({"repos": ["a/b"], "auth": "basic"})
    with pytest.raises(ValueError, match="nothing to index"):
        BitbucketConnector({"repos": ["a/b"], "include_files": False})
    assert BitbucketConnector({"repos": ["a/b@dev", {"repo": "c/d", "ref": "x"}]}).repos == [("a", "b", "dev"), ("c", "d", "x")]


def test_a_cursor_written_for_a_different_configuration_starts_over_instead_of_failing():
    cursor = {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"repo": 0, "phase": "issues", "next": "x"}, "prev": {}, "heads": {}}
    changes, _ = run(make(one()), cursor)
    assert len(changes) == 5


def test_api_url_override_and_trailing_slash_are_used_for_every_request():
    site = one()
    connector = BitbucketConnector({"token_env": "BB_TOKEN", "repos": ["acme/payments"], "api_url": "https://api.bitbucket.org/2.0/", "default_acl": ["*"]},
                                   transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    assert len(run(connector)[0]) == 5 and all("//" not in site.raw(r) for r in site.requests)


# ── more on files ──


def test_characters_that_mean_something_in_a_url_are_encoded_in_file_paths():
    snap = {"notes/a#b?c%d+e f.md": b"hello", "plain.md": b"p"}
    site = one(snapshots={C1: snap})
    changes, _ = run(make(site))
    assert sorted(c.external_id for c in changes) == ["bitbucket:acme/payments/src/notes/a#b?c%d+e f.md", "bitbucket:acme/payments/src/plain.md"]
    assert read(make(site), next(c.file for c in changes if "#" in c.external_id)) == "hello"
    site.repos["acme/payments"]["commits"][C2] = {**snap, "notes/a#b?c%d+e f.md": b"changed"}
    site.repos["acme/payments"]["branches"]["main"] = C2
    cursor = {"since": "2026-03-01T00:00:00Z", "last_full": NOW, "heads": {"acme/payments": C1}}
    changes, _ = run(make(site, clock=lambda: NOW + 60), cursor)
    assert [key(c) for c in changes] == [("upsert", "notes/a#b?c%d+e f.md")]


def test_a_path_that_became_a_directory_is_deleted_as_a_file_and_its_new_files_are_added():
    c1, c2 = {"NOTES": b"a file", "a.md": b"a"}, {"NOTES/b.md": b"b", "a.md": b"a"}
    site = Bitbucket({"acme/payments": repo({C1: c1, C2: c2}, {"main": C1})})
    cursor = finished_cursor(site)
    site.repos["acme/payments"]["branches"]["main"] = C2
    changes, _ = run(make(site, clock=lambda: NOW + 60), cursor)
    assert sorted(key(c) for c in changes) == [("delete", "NOTES"), ("upsert", "NOTES/b.md")]


def test_a_settled_entry_without_a_path_still_gets_the_path_it_was_asked_for():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})}, no_meta_path=True)
    cursor = finished_cursor(site)
    site.repos["acme/payments"]["branches"]["main"] = C2
    changes, _ = run(make(site, clock=lambda: NOW + 60), cursor)
    assert ("upsert", "src/main.py") in [key(c) for c in changes]


def test_branch_names_with_a_slash_are_sent_percent_encoded_and_a_branch_without_a_hash_is_an_error():
    site = Bitbucket({"acme/payments": repo({C1: SNAP1, C2: {"only.md": b"x"}}, {"main": C1, "feature/x": C2})})
    changes, _ = run(make(site, repos=["acme/payments@feature/x"]))
    assert [c.external_id for c in changes] == ["bitbucket:acme/payments/src/only.md"]
    assert "/repositories/acme/payments/refs/branches/feature%2Fx" in site.paths()
    site = one()
    site.script = [httpx.Response(200, json=site.repos["acme/payments"]["info"]), httpx.Response(200, json={"name": "main", "target": {"hash": "nothex"}})]
    with pytest.raises(HttpError, match="without a commit hash"):
        run(make(site))


def test_a_server_that_lists_a_directory_without_children_again_and_again_does_not_loop():
    site = one()
    asked = []

    def stubborn(data, commit, path, q, request):
        """The root holds one directory `d`; listing `d/` returns just `d` itself, so it never gains children."""
        asked.append(path)
        assert len(asked) < 40, "the listing never ends"
        values = [{"path": "d", "type": "commit_directory", "commit": {"hash": commit}}]
        return httpx.Response(200, json={"pagelen": 10, "page": 1, "values": values})
    site.src = stubborn
    changes, batches = run(make(site))
    assert changes == [] and batches[-1].done and asked == ["", "d/"]


def test_entries_that_name_the_last_commit_that_touched_a_file_keep_their_versions_across_pushes():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})}, per_file_commit=True)
    before = {c.external_id: c.file.version for c in run(make(site))[0]}
    assert before["bitbucket:acme/payments/src/README.md"] == f"{len(README)}-{C1}"
    site.repos["acme/payments"]["branches"]["main"] = C2
    after = {c.external_id: c.file.version for c in run(make(site))[0]}
    assert after["bitbucket:acme/payments/src/README.md"] == before["bitbucket:acme/payments/src/README.md"]          # untouched: same version
    assert after["bitbucket:acme/payments/src/src/main.py"] != before["bitbucket:acme/payments/src/src/main.py"]    # touched by C2
    assert after["bitbucket:acme/payments/src/src/main.py"].endswith(C2)
    plain = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})})
    v1 = {c.external_id: c.file.version for c in run(make(plain))[0]}
    plain.repos["acme/payments"]["branches"]["main"] = C2
    v2 = {c.external_id: c.file.version for c in run(make(plain))[0]}
    assert v1["bitbucket:acme/payments/src/README.md"] != v2["bitbucket:acme/payments/src/README.md"]               # listed commit: every file moves with the head


def test_a_listing_entry_with_a_short_commit_hash_is_versioned_with_the_head_commit_instead():
    site = Bitbucket({"acme/payments": repo()}, bad_commit=True)
    changes, _ = run(make(site))
    assert all(c.file.version == f"{c.file.size}-{C1}" for c in changes)


def test_a_listing_that_follows_a_failed_diffstat_resumes_as_a_listing_not_as_a_diff():
    site = Bitbucket({"acme/payments": repo({C2: pushed()[C2]}, {"main": C2})})                     # C1 is gone
    cursor = {"since": "2026-03-01T00:00:00Z", "last_full": NOW, "heads": {"acme/payments": C1}}
    batches = list(make(site, clock=lambda: NOW + 60, batch_size=2).scan(cursor))
    first = batches[0]
    assert first.cursor["pos"]["full"] is True and first.cursor["prev"] == {"acme/payments": C1}
    site.reset()
    changes, resumed = run(make(site, clock=lambda: NOW + 60, batch_size=2), first.cursor)
    assert not site.calls("/diffstat/") and scopes(resumed) == []
    assert [c.file.path for b in batches for c in b.changes][:2] + [c.file.path for c in changes] == sorted(
        f"bitbucket/acme/payments/{p}" for p in pushed()[C2] if p != "logo.png")


def test_a_diffstat_cut_short_by_max_pages_changes_nothing_and_keeps_the_old_commit():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1})})
    cursor = finished_cursor(site)
    site.repos["acme/payments"]["branches"]["main"] = C2
    site.reset()
    changes, batches = run(make(site, clock=lambda: NOW + 3600, max_pages=1), cursor)
    assert changes == [] and scopes(batches) == [] and batches[-1].cursor["heads"] == {"acme/payments": C1}
    assert len(site.calls("/diffstat/")) == 1 and not [r for r in site.requests if "max_depth" in r.url.params]
    assert batches[-1].cursor["since"] == cursor["since"]                                         # the watermark did not move either


def test_the_commit_is_not_recorded_when_the_last_batch_is_cut_by_max_items():
    site = one()
    changes, batches = run(make(site, max_items=4, batch_size=2))
    assert len(changes) == 4 and batches[-1].cursor["heads"] == {} and scopes(batches) == []


def test_a_repository_that_was_not_reached_keeps_the_commit_of_the_previous_scan():
    site = Bitbucket({"acme/payments": repo(pushed(), {"main": C1}), "acme/website": repo({C3: {"index.md": b"hello"}}, {"main": C3})})
    repos = ["acme/payments", "acme/website"]
    cursor = finished_cursor(site, repos=repos)
    assert cursor["heads"] == {"acme/payments": C1, "acme/website": C3}
    site.repos["acme/payments"]["branches"]["main"] = C2
    changes, batches = run(make(site, repos=repos, clock=lambda: NOW + 3600, max_pages=3), cursor)
    assert batches[-1].cursor["heads"] == {"acme/payments": C2, "acme/website": C3} and len(changes) == 6


def test_the_repository_record_is_read_afresh_by_every_scan():
    site = Bitbucket({"acme/payments": repo({C1: SNAP1, C2: {"only.md": b"x"}}, {"main": C1, "dev": C2})})
    connector = make(site)
    assert len(run(connector)[0]) == 5
    site.repos["acme/payments"]["info"]["mainbranch"]["name"] = "dev"
    assert [c.external_id for c in run(connector)[0]] == ["bitbucket:acme/payments/src/only.md"]


def test_resuming_after_the_files_phase_continues_with_the_issues_of_the_same_repository():
    site = with_issues()
    batches = list(make(site, include_issues=True).scan({}))
    boundary = next(b for b in batches if b.complete_scope == "bitbucket:acme/payments/src/")
    assert boundary.cursor["pos"] == {"repo": 0, "phase": "issues"}
    site.reset()
    changes, resumed = run(make(site, include_issues=True), boundary.cursor)
    assert {c.external_id.split("/")[2] for c in changes} == {"issue"} and not site.calls("/src/") and scopes(resumed) == ["bitbucket:acme/payments/issue/"]


def test_issues_clipped_by_max_items_do_not_claim_the_listing_was_complete():
    site = Bitbucket({"acme/payments": repo(issues=copy.deepcopy(ISSUES))}, cap=100)
    changes, batches = run(issues_only(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    changes, batches = run(issues_only(site))
    assert len(changes) == 5 and scopes(batches) == ["bitbucket:acme/payments/issue/"]
