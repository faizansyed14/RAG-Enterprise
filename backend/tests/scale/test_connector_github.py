"""GitHub connector against a fake REST API shaped like the GitHub documentation: git trees (recursive + truncated),
raw blobs, issues with `Link: rel="next"` pagination (the Link URLs use /repositories/{id}/..., as GitHub's do),
issue comments, pull request reviews and review comments, primary and secondary rate limits.

Not certain of (written from memory of the docs): the exact wording of GitHub's rate-limit 403 body, and whether
branch names containing "/" are accepted percent-encoded in the trees endpoint.
"""

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.github import GitHubConnector

TOKEN = "ghp_SECRETtoken0123456789"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
API = "https://api.github.com"


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def entry(path, content=b"x", mode="100644", kind="blob"):
    return {"path": path, "mode": mode, "type": kind, "sha": blob_sha(content), "size": len(content),
            "url": f"{API}/repos/acme/payments/git/blobs/{blob_sha(content)}"}


def user(login):
    return {"login": login, "id": abs(hash(login)) % 10000, "type": "User"}


def issue(n, title, *, body="Something broke", updated="2026-03-01T10:00:00Z", pr=False, state="open", labels=(), comments=0, **kw):
    item = {"id": 5000 + n, "number": n, "title": title, "state": state, "body": body, "user": user("ada"),
            "labels": [{"name": name, "color": "d73a4a"} for name in labels], "assignees": [user("grace")], "milestone": None,
            "created_at": "2026-02-01T09:00:00Z", "updated_at": updated, "closed_at": None, "comments": comments,
            "html_url": f"https://github.com/acme/payments/{'pull' if pr else 'issues'}/{n}", "author_association": "MEMBER", **kw}
    if pr:
        item["pull_request"] = {"url": f"{API}/repos/acme/payments/pulls/{n}", "html_url": item["html_url"]}
    return item


def comment(cid, login, body, created="2026-03-02T08:00:00Z"):
    return {"id": cid, "user": user(login), "created_at": created, "updated_at": created, "body": body, "author_association": "MEMBER"}


class GitHub:
    def __init__(self, repos, *, prefix="", per_page_cap=2, repo_ids=None):
        self.repos = copy.deepcopy(repos)           # "owner/repo" -> dict(files=..., issues=..., ...)
        self.prefix = prefix
        self.cap = per_page_cap
        self.ids = repo_ids or {name: 100 + i for i, name in enumerate(self.repos)}
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def paths(self):
        return [r.url.path.removeprefix(self.prefix) for r in self.requests]

    def calls(self, pattern):
        return [r for r in self.requests if re.search(pattern, r.url.path)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"})
        assert request.headers.get("x-github-api-version") == "2022-11-28"
        path = request.url.path.removeprefix(self.prefix)
        q = dict(request.url.params)
        m = re.fullmatch(r"/repositories/(\d+)(/.*)", path)
        if m:
            name = next(n for n, i in self.ids.items() if i == int(m.group(1)))
            path = f"/repos/{name}{m.group(2)}"
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if not m or m.group(1) not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        name, rest, repo = m.group(1), m.group(2) or "", self.repos[m.group(1)]
        if rest == "":
            return httpx.Response(200, json={"id": self.ids[name], "full_name": name, "default_branch": repo.get("default_branch", "main"), "private": True})
        m = re.fullmatch(r"/git/trees/(.+)", rest)
        if m:
            return self._tree(repo, m.group(1), q)
        m = re.fullmatch(r"/git/blobs/([0-9a-f]+)", rest)
        if m:
            return self._blob(repo, m.group(1), request)
        if rest == "/issues":
            assert q.get("sort") == "updated" and q.get("direction") == "asc"
            items = [i for i in repo.get("issues", []) if q.get("state", "open") in ("all", i["state"])]
            if "since" in q:
                items = [i for i in items if i["updated_at"] >= q["since"]]
            return self._paged(items, q, name, "/issues")
        m = re.fullmatch(r"/(issues|pulls)/(\d+)(/comments|/reviews)?", rest)
        if m:
            kind, number, tail = m.group(1), int(m.group(2)), m.group(3)
            if tail is None:
                found = [i for i in repo.get("issues", []) if i["number"] == number]
                return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={"message": "Not Found"})
            key = {("issues", "/comments"): "comments", ("pulls", "/reviews"): "reviews", ("pulls", "/comments"): "review_comments"}[(kind, tail)]
            return self._paged(repo.get(key, {}).get(number, []), q, name, rest)
        return httpx.Response(404, json={"message": "Not Found"})

    def _tree(self, repo, ref, q):
        if repo.get("empty"):
            return httpx.Response(409, json={"message": "Git Repository is empty.", "documentation_url": "x"})
        if ref in ("main", "dev", "feature%2Fx", "feature/x") or ref == repo.get("default_branch", "main"):
            entries = repo["files"]
            if q.get("recursive") == "1":
                if repo.get("truncated"):
                    return httpx.Response(200, json={"sha": "roottree", "url": "u", "tree": entries[:1], "truncated": True})
                return httpx.Response(200, json={"sha": "roottree", "url": "u", "tree": entries, "truncated": False})
            return httpx.Response(200, json={"sha": "roottree", "url": "u", "tree": self._level(entries, ""), "truncated": False})
        if ref == "roottree":
            return httpx.Response(200, json={"sha": "roottree", "tree": self._level(repo["files"], ""), "truncated": False})
        m = re.fullmatch(r"dir-(.*)", ref)
        if m:
            prefix = m.group(1) + "/"
            return httpx.Response(200, json={"sha": ref, "tree": self._level(repo["files"], prefix), "truncated": False})
        return httpx.Response(404, json={"message": "Not Found"})

    @staticmethod
    def _level(entries, prefix):
        out, dirs = [], set()
        for e in entries:
            if not e["path"].startswith(prefix) or e["type"] == "tree":
                continue
            rest = e["path"][len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/")[0])
            else:
                out.append({**e, "path": rest})
        return out + [{"path": d, "mode": "040000", "type": "tree", "sha": f"dir-{prefix}{d}".rstrip("/")} for d in sorted(dirs)]

    def _blob(self, repo, sha, request):
        data = repo.get("blobs", {}).get(sha)
        if data is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.headers.get("accept") == "application/vnd.github.raw+json":
            return httpx.Response(200, content=data, headers={"content-type": "application/vnd.github.raw"})
        import base64
        return httpx.Response(200, json={"sha": sha, "size": len(data), "encoding": "base64", "content": base64.b64encode(data).decode()})

    def _paged(self, items, q, name, endpoint):
        page = int(q.get("page", 1))
        part = items[(page - 1) * self.cap: page * self.cap]
        headers = {}
        if page * self.cap < len(items):
            nq = {**q, "page": page + 1}
            last = -(-len(items) // self.cap)
            base = f"{API}{self.prefix}/repositories/{self.ids[name]}{endpoint}"
            headers["link"] = f'<{base}?{urlencode(nq)}>; rel="next", <{base}?{urlencode({**q, "page": last})}>; rel="last"'
        return httpx.Response(200, json=part, headers=headers)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "GH_TOKEN", "repos": ["acme/payments"], "default_acl": ["group:eng"], **cfg}
    connector = GitHubConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


README, MAIN, DOC, CFG = b"# Payments\nHello", b"print('hi')\n", b"Design notes\n", b'{"a": 1}'
FILES = [entry("README.md", README), entry("src/main.py", MAIN), entry("docs/design.md", DOC), entry("conf/app.json", CFG)]
BLOBS = {blob_sha(c): c for c in (README, MAIN, DOC, CFG)}


def repo(files=None, **kw):
    return {"files": copy.deepcopy(FILES if files is None else files), "blobs": BLOBS, **kw}


# ── files ──


def test_tree_is_listed_recursively_and_each_file_carries_its_blob_sha_as_version():
    site = GitHub({"acme/payments": repo()})
    changes, batches = run(make(site))
    by = {c.external_id: c.file for c in changes}
    assert sorted(by) == ["github:acme/payments/blob/README.md", "github:acme/payments/blob/conf/app.json",
                          "github:acme/payments/blob/docs/design.md", "github:acme/payments/blob/src/main.py"]
    f = by["github:acme/payments/blob/src/main.py"]
    assert f.path == "github/acme/payments/src/main.py" and f.name == "main.py" and f.version == blob_sha(MAIN) and f.size == len(MAIN)
    assert f.acl == ["group:eng"] and f.mime in ("text/x-python", "text/x-python-script")
    assert site.paths() == ["/repos/acme/payments", "/repos/acme/payments/git/trees/main"]
    assert site.requests[1].url.params["recursive"] == "1"
    assert batches[-1].done and batches[-1].changes == []
    scoped = [b for b in batches if b.complete_scope is not None]
    assert [b.complete_scope for b in scoped] == ["github:acme/payments/blob/"]          # the listing is complete => authoritative for the repo's files


def test_an_explicit_ref_skips_the_default_branch_lookup():
    site = GitHub({"acme/payments": repo(default_branch="main")})
    run(make(site, repos=[{"repo": "acme/payments", "ref": "dev"}]))
    assert site.paths() == ["/repos/acme/payments/git/trees/dev"]
    site = GitHub({"acme/payments": repo()})
    run(make(site, repos=["acme/payments@feature/x"]))
    assert site.requests[0].url.raw_path.startswith(b"/repos/acme/payments/git/trees/feature%2Fx")


def test_symlinks_submodules_vendored_dirs_binaries_and_large_files_are_not_files_to_index():
    files = FILES + [entry("link.md", b"README.md", mode="120000"), entry("lib/sub", b"", mode="160000", kind="commit"),
                     entry("node_modules/x/index.js", b"js"), entry("web/node_modules/y.js", b"js"), entry("logo.png", b"\x89PNG"),
                     entry("big.md", b"x" * 3 * 1024 * 1024)]
    site = GitHub({"acme/payments": repo(files)})
    paths = sorted(c.file.path.removeprefix("github/acme/payments/") for c in run(make(site, max_file_mb=1))[0])
    assert paths == ["README.md", "conf/app.json", "docs/design.md", "src/main.py"]
    assert sorted(c.file.path.removeprefix("github/acme/payments/") for c in run(make(site, max_file_mb=5, exclude=[]))[0]) == sorted(
        ["README.md", "conf/app.json", "docs/design.md", "src/main.py", "node_modules/x/index.js", "web/node_modules/y.js", "logo.png", "big.md"])


def test_include_globs_select_files():
    site = GitHub({"acme/payments": repo()})
    paths = sorted(c.file.path.removeprefix("github/acme/payments/") for c in run(make(site, include=["*.md", "src/*"]))[0])
    assert paths == ["README.md", "docs/design.md", "src/main.py"]


def test_batches_resume_after_the_last_path_and_a_resumed_listing_is_not_authoritative():
    site = GitHub({"acme/payments": repo()})
    batches = list(make(site, batch_size=2).scan({}))
    first = batches[0]
    assert [c.file.path.removeprefix("github/acme/payments/") for c in first.changes] == ["README.md", "conf/app.json"]
    assert first.cursor["pos"] == {"repo": 0, "phase": "files", "after": "conf/app.json"} and first.complete_scope is None
    assert json.loads(json.dumps(first.cursor)) == first.cursor
    site.reset()
    changes, resumed = run(make(site, batch_size=2), first.cursor)
    assert [c.file.path.removeprefix("github/acme/payments/") for c in changes] == ["docs/design.md", "src/main.py"]
    assert all(b.complete_scope is None for b in resumed)
    full = [b for b in batches if b.complete_scope]
    assert len(full) == 1 and full[0] is batches[1]                                       # only the batch that ends an uninterrupted listing


def test_repos_are_scanned_in_order_each_with_its_own_scope_and_resume_picks_up_at_the_right_repo():
    site = GitHub({"acme/payments": repo(), "acme/website": repo([entry("index.md", b"hello")])})
    batches = list(make(site, repos=["acme/payments", "acme/website"]).scan({}))
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/blob/", "github:acme/website/blob/"]
    boundary = next(b for b in batches if b.complete_scope == "github:acme/payments/blob/")       # the batch that finishes repo 0
    assert boundary.cursor["pos"] == {"repo": 1, "phase": "files"}
    site.reset()
    changes, resumed = run(make(site, repos=["acme/payments", "acme/website"]), boundary.cursor)
    assert [c.external_id for c in changes] == ["github:acme/website/blob/index.md"]
    assert not site.calls(r"/acme/payments")
    assert [b.complete_scope for b in resumed if b.complete_scope] == ["github:acme/website/blob/"]


def test_a_truncated_recursive_tree_is_walked_directory_by_directory():
    site = GitHub({"acme/payments": repo(truncated=True)})
    changes, batches = run(make(site))
    assert sorted(c.file.path.removeprefix("github/acme/payments/") for c in changes) == ["README.md", "conf/app.json", "docs/design.md", "src/main.py"]
    walked = [p for p in site.paths() if "/git/trees/" in p and not p.endswith("/main")]
    assert len(walked) >= 4 and any(p.endswith("dir-docs") for p in walked)             # root + one request per directory
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/blob/"]


def test_an_empty_repository_yields_no_files_and_the_scope_so_old_files_are_deleted():
    site = GitHub({"acme/payments": repo(empty=True)})
    changes, batches = run(make(site))
    assert changes == [] and [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/blob/"]


def test_files_can_be_switched_off_for_an_issues_only_connection():
    site = GitHub({"acme/payments": repo(issues=[issue(1, "A")])})
    changes, _ = run(make(site, include_files=False, include_issues=True))
    assert [c.external_id for c in changes] == ["github:acme/payments/issue/1"]
    assert not site.calls("/git/trees")


def test_max_items_and_max_pages_truncate_and_never_delete():
    site = GitHub({"acme/payments": repo(), "acme/website": repo([entry("index.md", b"hello")])})
    changes, batches = run(make(site, max_items=2))
    assert len(changes) == 2 and all(b.complete_scope is None for b in batches) and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=1, repos=["acme/payments", "acme/website"]))
    assert {c.external_id.split("/")[1] for c in changes} == {"payments"} and len(changes) == 4          # website was never reached
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/blob/"]    # payments itself was complete
    assert "since" not in batches[-1].cursor and batches[-1].done


# ── open() ──


def test_open_downloads_the_blob_by_sha_as_raw_bytes_from_persisted_columns_only():
    site = GitHub({"acme/payments": repo()})
    by = {c.external_id: c.file for c in run(make(site))[0]}
    site.reset()
    fresh = make(site)
    with fresh.open(persisted(by["github:acme/payments/blob/src/main.py"])) as fh:
        assert fh.read() == MAIN
    request = site.requests[-1]
    assert request.url.path == f"/repos/acme/payments/git/blobs/{blob_sha(MAIN)}" and request.headers["accept"] == "application/vnd.github.raw+json"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_open_enforces_the_size_cap_and_rejects_bad_ids_without_a_request():
    big = b"y" * 3000
    site = GitHub({"acme/payments": repo([entry("big.txt", big)], blobs={blob_sha(big): big})})
    connector = make(site, max_file_mb=0.001)
    with pytest.raises(TooLarge):
        connector.open(SourceFile("github:acme/payments/blob/big.txt", "p", "big.txt", 0, None, blob_sha(big), []))
    site.reset()
    for ext, version in (("github:acme/payments/blob/a.md", "not-a-sha"), ("github:../x/y/blob/a", blob_sha(b"")), ("other:acme/payments/blob/a", blob_sha(b"")),
                         ("github:acme/payments/issue/abc", "v")):
        with pytest.raises(HttpError):
            connector.open(SourceFile(ext, "p", "n", 0, None, version, []))
    assert site.requests == []


def test_missing_blob_is_an_error_with_a_status():
    site = GitHub({"acme/payments": repo()})
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("github:acme/payments/blob/a.md", "p", "a.md", 0, None, "0" * 40, []))
    assert err.value.status == 404


# ── permissions ──


def test_acl_rules_per_repository_prefix_fail_closed():
    site = GitHub({"acme/payments": repo(), "acme/website": repo([entry("index.md", b"hi")])})
    rules = [{"prefix": "github/acme/payments", "principals": ["group:payments"]},
             {"prefix": "github/acme/payments/docs", "principals": ["*"]}]
    connector = make(site, repos=["acme/payments", "acme/website"], acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["github:acme/payments/blob/src/main.py"].acl == ["group:payments"]
    assert by["github:acme/payments/blob/docs/design.md"].acl == ["*"]
    assert by["github:acme/website/blob/index.md"].acl == []
    assert connector.refresh_acl(persisted(by["github:acme/payments/blob/README.md"])) == ["group:payments"]


# ── issues and pull requests ──


ISSUES = [issue(n, f"Issue {n}", updated=f"2026-03-0{n}T10:00:00Z", labels=["bug"] if n % 2 else [], comments=n % 2) for n in range(1, 6)]
ISSUES.append(issue(6, "A pull request", updated="2026-03-06T10:00:00Z", pr=True))


def with_issues(**kw):
    return GitHub({"acme/payments": repo(issues=copy.deepcopy(ISSUES), **kw)})


def test_issues_follow_the_link_header_across_pages_including_repositories_urls():
    site = with_issues()
    changes, batches = run(make(site, include_files=False, include_issues=True))
    assert [c.external_id for c in changes] == [f"github:acme/payments/issue/{n}" for n in range(1, 6)]        # the PR is not an issue
    listing = site.calls(r"/issues$")
    assert listing[0].url.path == "/repos/acme/payments/issues" and all(r.url.path == "/repositories/100/issues" for r in listing[1:])
    assert [r.url.params.get("page") for r in listing] == [None, "2", "3"]        # 6 items, 2 per page, the PR included
    first = listing[0].url.params
    assert (first["state"], first["sort"], first["direction"], first["per_page"]) == ("all", "updated", "asc", "100") and "since" not in first
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/issue/"]
    assert changes[0].file.path == "github/acme/payments/_issues/1-Issue 1.md"


def test_pull_requests_are_included_on_request_and_get_reviews_and_inline_comments():
    repo_data = repo(issues=copy.deepcopy(ISSUES),
                     comments={6: [comment(1, "grace", "Please add tests")]},
                     reviews={6: [{"id": 9, "user": user("linus"), "state": "CHANGES_REQUESTED", "body": "Needs work", "submitted_at": "2026-03-06T11:00:00Z"},
                                  {"id": 10, "user": user("ada"), "state": "PENDING", "body": "draft thoughts", "submitted_at": None},
                                  {"id": 11, "user": user("bot"), "state": "APPROVED", "body": "", "submitted_at": "2026-03-06T12:00:00Z"}]},
                     review_comments={6: [{"id": 20, "user": user("linus"), "path": "src/main.py", "line": 3, "body": "typo here", "created_at": "2026-03-06T11:01:00Z"}]})
    site = GitHub({"acme/payments": repo_data})
    connector = make(site, include_files=False, include_pulls=True)
    changes, _ = run(connector)
    assert [c.external_id for c in changes] == ["github:acme/payments/issue/6"]
    sf = changes[0].file
    assert sf.path == "github/acme/payments/_pulls/6-A pull request.md"
    text = read(make(GitHub({"acme/payments": repo_data})), sf)
    assert text.startswith("# acme/payments#6: A pull request\n") and "type: pull request" in text
    assert "### grace (2026-03-02T08:00:00Z)\n\nPlease add tests" in text
    assert "### linus: CHANGES_REQUESTED (2026-03-06T11:00:00Z)\n\nNeeds work" in text and "### bot: APPROVED" in text
    assert "draft thoughts" not in text                                                    # pending reviews are private to their author
    assert "### linus on src/main.py (2026-03-06T11:01:00Z)\n\ntypo here" in text
    off = make(GitHub({"acme/payments": repo_data}), include_files=False, include_pulls=True, include_pr_reviews=False)
    assert "Needs work" not in read(off, sf)


def test_issue_text_has_metadata_labels_comments_and_no_html_comments():
    body = "Steps:\n<!-- please describe the bug -->\n1. do it\n<!-- thanks -->"
    data = repo(issues=[issue(7, "Crash on start", body=body, labels=["bug", "p1"], comments=2)],
                comments={7: [comment(1, "grace", "Reproduced"), comment(2, "ada", "")]})
    site = GitHub({"acme/payments": data})
    sf = run(make(site, include_files=False, include_issues=True))[0][0].file
    text = read(make(GitHub({"acme/payments": data})), sf)
    for line in ("repository: acme/payments", "type: issue", "state: open", "author: ada", "labels: bug, p1", "assignees: grace",
                 "url: https://github.com/acme/payments/issues/7"):
        assert line in text
    assert "1. do it" in text and "<!--" not in text and "please describe" not in text
    assert "### grace" in text and "Reproduced" in text and "### ada" not in text         # empty comments are dropped
    assert sf.size == len(text.encode()) and sf.version.startswith("2026-03-01T10:00:00Z-")


def test_comment_lists_are_paginated_too_and_the_version_follows_the_text():
    many = [comment(n, "grace", f"comment number {n}") for n in range(1, 6)]
    data = repo(issues=[issue(7, "Chatty", comments=5)], comments={7: many})
    site = GitHub({"acme/payments": data})
    connector = make(site, include_files=False, include_issues=True)
    sf = run(connector)[0][0].file
    pages = site.calls(r"/issues/7/comments$|/issues/comments|/repositories/100/issues/7/comments")
    assert len(pages) == 3
    text = read(connector, sf)
    assert all(f"comment number {n}" in text for n in range(1, 6))
    data2 = copy.deepcopy(data)
    data2["comments"][7].append(comment(6, "ada", "one more", created="2026-03-03T08:00:00Z"))
    other = run(make(GitHub({"acme/payments": data2}), include_files=False, include_issues=True))[0][0].file
    assert other.version != sf.version                                                      # same updated_at, new comment => new version
    assert run(make(GitHub({"acme/payments": data}), include_files=False, include_issues=True))[0][0].file.version == sf.version


def test_issue_scan_after_a_finished_one_uses_since_with_overlap_and_is_not_authoritative():
    site = with_issues()
    _, batches = run(make(site, include_files=False, include_issues=True, include_pulls=True))
    final = batches[-1].cursor
    assert final["since"] == "2026-03-06T10:00:00Z" and final["last_full"] == NOW
    site.reset()
    changes, inc = run(make(site, include_files=False, include_issues=True, include_pulls=True), final)
    assert site.calls(r"/issues$")[0].url.params["since"] == "2026-03-06T09:55:00Z"       # 5 minutes of overlap
    assert [c.external_id for c in changes] == ["github:acme/payments/issue/6"]
    assert all(b.complete_scope is None for b in inc) and inc[-1].cursor["last_full"] == final["last_full"]


def test_full_resync_of_issues_when_due_and_issues_since_config_limits_the_first_scan():
    site = with_issues()
    _, batches = run(make(site, include_files=False, include_issues=True), {"since": "2026-03-05T00:00:00Z", "last_full": NOW - 30 * 3600})
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/issue/"]
    site.reset()
    changes, _ = run(make(site, include_files=False, include_issues=True, issues_since="2026-03-04"))
    assert site.calls(r"/issues$")[0].url.params["since"] == "2026-03-04T00:00:00Z"
    assert [c.external_id for c in changes] == ["github:acme/payments/issue/4", "github:acme/payments/issue/5"]


def test_issue_listing_resumes_at_the_saved_link_and_does_not_claim_completeness():
    site = with_issues()
    connector = make(site, include_issues=True)
    mid = next(b for b in connector.scan({}) if b.cursor["pos"].get("phase") == "issues" and b.cursor["pos"].get("next"))
    assert mid.cursor["pos"]["phase"] == "issues" and mid.cursor["pos"]["next"].startswith(f"{API}/repositories/100/issues?")
    site.reset()
    changes, batches = run(make(site, include_issues=True), mid.cursor)
    assert [c.external_id for c in changes] == [f"github:acme/payments/issue/{n}" for n in (3, 4, 5)]
    assert site.requests[0].url.params["page"] == "2" and all(b.complete_scope is None for b in batches)
    assert not site.calls("/git/trees") and not site.calls(r"^/repos/acme/payments$")          # the files of this repo were already done


def test_issue_open_refetches_the_issue_and_renders_the_same_bytes():
    site = with_issues()
    connector = make(site, include_files=False, include_issues=True)
    by = {c.external_id: c.file for c in run(connector)[0]}
    site.reset()
    for sf in by.values():
        assert len(read(make(site), sf).encode()) == sf.size
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("github:acme/payments/issue/404", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


# ── rate limits, auth, hosts ──


def test_primary_rate_limit_403_is_waited_out_until_the_reset_time_not_treated_as_bad_credentials():
    site = GitHub({"acme/payments": repo()})
    reset = int(time.time()) + 30
    site.script = [httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset), "x-ratelimit-limit": "5000"},
                                  json={"message": "API rate limit exceeded for user ID 1.", "documentation_url": "x"})]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 4 and len(connector.sleeps) == 1 and 25 <= connector.sleeps[0] <= 31


def test_secondary_rate_limit_and_plain_429_honour_retry_after():
    site = GitHub({"acme/payments": repo()})
    site.script = [httpx.Response(403, headers={"retry-after": "7"}, json={"message": "You have exceeded a secondary rate limit."}),
                   httpx.Response(429, headers={"retry-after": "3"}, json={"message": "slow down"}),
                   httpx.Response(403, json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes."})]
    connector = make(site)
    run(connector)
    assert connector.sleeps == [7.0, 3.0, 60.0]


def test_a_real_403_is_an_auth_error_and_is_not_retried():
    site = GitHub({"acme/payments": repo()})
    site.script = [httpx.Response(403, json={"message": "Resource not accessible by personal access token"})]
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "403" in str(err.value) and "GH_TOKEN" in str(err.value) and connector.sleeps == []


def test_401_bad_credentials_never_shows_the_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_wrongwrongwrong")
    site = GitHub({"acme/payments": repo()})
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "401" in str(err.value) and "ghp_wrongwrongwrong" not in str(err.value) and TOKEN not in str(err.value)
    assert len(site.requests) == 1


def test_a_link_to_another_host_is_refused_and_the_token_is_not_sent_there():
    class Evil(GitHub):
        def _paged(self, items, q, name, endpoint):
            r = super()._paged(items, q, name, endpoint)
            if "link" in r.headers:
                return httpx.Response(200, json=r.json(), headers={"link": '<https://evil.example/steal?page=2>; rel="next"'})
            return r
    site = Evil({"acme/payments": repo(issues=copy.deepcopy(ISSUES))})
    with pytest.raises(HttpError, match="another host"):
        run(make(site, include_files=False, include_issues=True))
    assert {r.url.host for r in site.requests} == {"api.github.com"}


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(GitHub({"acme/payments": repo()})))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("GH_TOKEN")
    with pytest.raises(HttpError, match="GH_TOKEN"):
        run(make(GitHub({"acme/payments": repo()})))


def test_enterprise_server_uses_its_own_api_url_for_every_request_including_links():
    site = GitHub({"acme/payments": repo(issues=copy.deepcopy(ISSUES))}, prefix="/api/v3")
    connector = GitHubConnector({"token_env": "GH_TOKEN", "repos": ["acme/payments"], "api_url": "https://ghe.corp.example/api/v3", "include_issues": True,
                                 "default_acl": ["*"]}, transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    # the fake builds Link URLs on api.github.com; point them at the enterprise host the way a real GHES does
    original = site._paged

    def paged(items, q, name, endpoint):
        r = original(items, q, name, endpoint)
        if "link" in r.headers:
            return httpx.Response(200, json=r.json(), headers={"link": r.headers["link"].replace("https://api.github.com", "https://ghe.corp.example")})
        return r
    site._paged = paged
    changes, batches = run(connector)
    assert len([c for c in changes if "/blob/" in c.external_id]) == 4 and len([c for c in changes if "/issue/" in c.external_id]) == 5
    assert {r.url.host for r in site.requests} == {"ghe.corp.example"} and all(r.url.path.startswith("/api/v3/") for r in site.requests)


def test_configuration_errors_are_rejected_up_front():
    with pytest.raises(ValueError, match="repos"):
        GitHubConnector({"token_env": "X"})
    for bad in ("justaname", "a/b/c", "ow ner/repo", "../x/y", "x/..", "./y", "a/b?c=1"):
        with pytest.raises(ValueError, match="repository"):
            GitHubConnector({"repos": [bad]})


def test_a_repository_listed_twice_is_scanned_once():
    site = GitHub({"acme/payments": repo()})
    changes, batches = run(make(site, repos=["acme/payments", {"repo": "acme/payments", "ref": "main"}, "acme/payments@dev"]))
    assert len(changes) == 4 and len(site.calls(r"/git/trees/")) == 1
    assert [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/blob/"]


def test_a_trailing_slash_on_api_url_does_not_double_the_path():
    site = GitHub({"acme/payments": repo()}, prefix="/api/v3")
    connector = GitHubConnector({"token_env": "GH_TOKEN", "repos": ["acme/payments"], "api_url": "https://ghe.corp.example/api/v3/",
                                 "default_acl": ["*"]}, transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    assert len(run(connector)[0]) == 4
    assert all("//" not in r.url.path for r in site.requests)


def test_open_refuses_object_ids_whose_owner_or_repository_is_not_a_plain_name_without_any_request():
    site = GitHub({"acme/payments": repo()})
    connector = make(site)
    sha = blob_sha(README)
    for ext in ("github:../x/blob/a.md", "github:acme/../blob/a.md", "github:ac me/pay/blob/a.md", "github:acme/pay?x=1/blob/a.md", "github:./y/blob/a.md"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(ext, "p", "a.md", 0, None, sha, []))
    assert site.requests == []


def test_a_server_that_echoes_the_token_in_an_error_body_never_gets_it_into_the_message():
    site = GitHub({"acme/payments": repo()})
    site.script = [httpx.Response(500, text=f"upstream rejected {TOKEN}")] * 6
    connector = make(site)
    with pytest.raises(HttpError) as err:
        run(connector)
    assert TOKEN not in str(err.value) and "***" in str(err.value)
    # the blob download goes through a different code path
    site = GitHub({"acme/payments": repo()})
    by = {c.external_id: c.file for c in run(make(site))[0]}
    site.script = [httpx.Response(500, text=f"upstream rejected {TOKEN}")] * 6
    with pytest.raises(HttpError) as err:
        make(site).open(persisted(by["github:acme/payments/blob/README.md"]))
    assert TOKEN not in str(err.value) and "***" in str(err.value)
    site.script = [httpx.Response(401, text=f"bad token {TOKEN}")]
    with pytest.raises(AuthError) as err:
        make(site).open(persisted(by["github:acme/payments/blob/README.md"]))
    assert TOKEN not in str(err.value) and "***" in str(err.value)


def test_issues_clipped_by_max_items_do_not_claim_the_listing_was_complete():
    site = GitHub({"acme/payments": repo(issues=copy.deepcopy(ISSUES))}, per_page_cap=100)
    changes, batches = run(make(site, include_files=False, include_issues=True, max_items=3))
    assert len(changes) == 3 and all(b.complete_scope is None for b in batches)
    assert "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, include_files=False, include_issues=True))
    assert len(changes) == 5 and [b.complete_scope for b in batches if b.complete_scope] == ["github:acme/payments/issue/"]


def test_max_pages_stops_the_issue_listing_and_leaves_a_resumable_cursor():
    site = with_issues()
    changes, batches = run(make(site, include_files=False, include_issues=True, max_pages=1))
    assert len(site.calls(r"/issues$")) == 1 and [c.external_id for c in changes] == ["github:acme/payments/issue/1", "github:acme/payments/issue/2"]
    assert all(b.complete_scope is None for b in batches) and batches[-1].done and "since" not in batches[-1].cursor
    assert batches[-2].cursor["pos"]["next"].endswith("page=2") or "page=2" in batches[-2].cursor["pos"]["next"]


def test_issue_and_pull_request_paths_follow_the_acl_rules_and_fail_closed():
    site = GitHub({"acme/payments": repo(issues=copy.deepcopy(ISSUES))}, per_page_cap=100)
    rules = [{"prefix": "github/acme/payments/_issues", "principals": ["group:support"]}]
    connector = make(site, include_files=False, include_issues=True, include_pulls=True, acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["github:acme/payments/issue/1"].acl == ["group:support"]
    assert by["github:acme/payments/issue/6"].acl == []                                     # _pulls/ matches no rule and there is no default
    assert connector.refresh_acl(persisted(by["github:acme/payments/issue/1"])) == ["group:support"]
    assert connector.refresh_acl(persisted(by["github:acme/payments/issue/6"])) == []
