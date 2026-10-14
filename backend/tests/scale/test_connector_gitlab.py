"""GitLab connector against a fake REST API v4 shaped like the GitLab documentation: projects (looked up by
URL-encoded path), repository tree with keyset (Link header + page_token) and offset (X-Next-Page) pagination, raw blobs
by sha, issues / merge requests with offset pagination and notes, wikis, rate limits.

Not certain of (written from memory of the docs): whether a tree entry for a submodule is typed "commit" in every
version, the exact wording of GitLab's error bodies, and whether `issues_enabled` is false (rather than the project
being hidden) for a token that may not see issues.
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
from app.scale.connectors.gitlab import GitLabConnector

TOKEN = "glpat-SECRETtoken0123456789"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
HOST = "gitlab.example.com"
API = f"https://{HOST}/api/v4"


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def entry(path, content=b"x", mode="100644", kind="blob"):
    return {"id": blob_sha(content) if kind == "blob" else f"tree-{path}", "name": path.rsplit("/", 1)[-1], "type": kind, "path": path, "mode": mode}


def person(username):
    return {"id": abs(hash(username)) % 1000, "username": username, "name": username.title(), "state": "active"}


def issue(iid, title, *, body="Something broke", updated="2026-03-01T10:00:00Z", state="opened", labels=(), notes=0, confidential=False, **kw):
    return {"id": 9000 + iid, "iid": iid, "project_id": 42, "title": title, "description": body, "state": state,
            "created_at": "2026-02-01T09:00:00.000Z", "updated_at": updated, "closed_at": None, "labels": list(labels),
            "milestone": None, "assignees": [person("grace")], "author": person("ada"), "user_notes_count": notes,
            "web_url": f"https://{HOST}/acme/payments/-/issues/{iid}", "confidential": confidential, "issue_type": "issue", **kw}


def merge_request(iid, title, *, updated="2026-03-01T10:00:00Z", **kw):
    item = issue(iid, title, updated=updated, **kw)
    item.pop("issue_type")
    item["web_url"] = f"https://{HOST}/acme/payments/-/merge_requests/{iid}"
    item.update({"source_branch": "feature/x", "target_branch": "main", "merged_at": None, "draft": False, "reviewers": [person("linus")]})
    return item


def note(nid, username, body, *, system=False, internal=False, created="2026-03-02T08:00:00.000Z"):
    return {"id": nid, "type": None, "body": body, "author": person(username), "created_at": created, "updated_at": created,
            "system": system, "internal": internal, "noteable_type": "Issue"}


def wiki_page(slug, content, title=None, fmt="markdown"):
    return {"content": content, "format": fmt, "slug": slug, "title": title or slug.rsplit("/", 1)[-1], "encoding": "UTF-8"}


def project(pid=42, path="acme/payments", **kw):
    return {"id": pid, "name": path.rsplit("/", 1)[-1], "path_with_namespace": path, "default_branch": "main",
            "web_url": f"https://{HOST}/{path}", "empty_repo": False, "issues_enabled": True, "merge_requests_enabled": True,
            "wiki_enabled": True, **kw}


class GitLab:
    def __init__(self, projects, *, cap=2, prefix="/api/v4", legacy_tree=False, header="PRIVATE-TOKEN", value=TOKEN):
        self.projects = copy.deepcopy(projects)       # id -> dict(info, files, blobs, issues, merge_requests, notes, wiki)
        self.cap, self.prefix, self.legacy_tree = cap, prefix, legacy_tree
        self.header, self.value = header, value
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    @staticmethod
    def raw(request):
        return request.url.raw_path.decode().split("?")[0]

    def paths(self):
        return [self.raw(r).removeprefix(self.prefix) for r in self.requests]

    def calls(self, pattern):
        return [r for r in self.requests if re.search(pattern, self.raw(r))]

    def find(self, key):
        for pid, data in self.projects.items():
            if key == str(pid) or key == data["info"]["path_with_namespace"]:
                return data
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get(self.header) != self.value:
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        raw = self.raw(request)
        assert raw.startswith(self.prefix + "/")
        parts = raw[len(self.prefix):].split("/")                      # ['', 'projects', key, ...]
        q = dict(request.url.params)
        if len(parts) < 3 or parts[1] != "projects":
            return httpx.Response(404, json={"message": "404 Not Found"})
        data = self.find(unquote(parts[2]))
        if data is None:
            return httpx.Response(404, json={"message": "404 Project Not Found"})
        rest = "/" + "/".join(parts[3:]) if len(parts) > 3 else ""
        if rest == "":
            return httpx.Response(200, json=data["info"])
        assert unquote(parts[2]) == str(data["info"]["id"]), "after the lookup the numeric id must be used"
        if rest == "/repository/tree":
            return self.tree(data, q)
        m = re.fullmatch(r"/repository/blobs/([0-9a-f]+)/raw", rest)
        if m:
            content = data.get("blobs", {}).get(m.group(1))
            return httpx.Response(200, content=content, headers={"content-type": "text/plain"}) if content is not None else httpx.Response(404, json={"message": "404 File Not Found"})
        if rest == "/wikis":
            if data.get("no_wiki"):
                return httpx.Response(404, json={"message": "404 Wiki Not Found"})
            assert q.get("with_content") == "1"
            return self.paged(data.get("wiki", []), q)
        m = re.fullmatch(r"/wikis/(.+)", rest)
        if m:
            found = [p for p in data.get("wiki", []) if p["slug"] == unquote(m.group(1))]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={"message": "404 Wiki Page Not Found"})
        m = re.fullmatch(r"/(issues|merge_requests)", rest)
        if m:
            assert (q.get("scope"), q.get("order_by"), q.get("sort")) == ("all", "updated_at", "asc")
            items = [i for i in data.get(m.group(1), []) if q.get("state", "all") == "all" or (q["state"] == "opened") == (i["state"] == "opened")]
            if "updated_after" in q:
                items = [i for i in items if i["updated_at"] >= q["updated_after"]]
            return self.paged(sorted(items, key=lambda i: i["updated_at"]), q)
        m = re.fullmatch(r"/(issues|merge_requests)/(\d+)(/notes)?", rest)
        if m:
            kind, iid = m.group(1), int(m.group(2))
            if m.group(3):
                assert (q.get("sort"), q.get("order_by")) == ("asc", "created_at")
                return self.paged(data.get("notes", {}).get((kind, iid), []), q)
            found = [i for i in data.get(kind, []) if i["iid"] == iid]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404, json={"message": "404 Not found"})
        return httpx.Response(404, json={"message": "404 Not Found"})

    def paged(self, items, q):
        page, per = int(q.get("page", 1)), min(int(q.get("per_page", 20)), self.cap)
        part = items[(page - 1) * per: page * per]
        more = page * per < len(items)
        return httpx.Response(200, json=part, headers={
            "x-page": str(page), "x-per-page": str(per), "x-total": str(len(items)), "x-total-pages": str(-(-len(items) // per)),
            "x-next-page": str(page + 1) if more else "", "x-prev-page": str(page - 1) if page > 1 else ""})

    def tree(self, data, q):
        assert q.get("recursive") == "true"
        info = data["info"]
        if q.get("ref") not in (info["default_branch"], "dev", "feature/x"):
            return httpx.Response(404, json={"message": "404 Tree Not Found"})
        entries = list(reversed(data["files"]))                      # no particular order promised
        if q.get("pagination") == "keyset" and not self.legacy_tree:
            per = min(int(q.get("per_page", 20)), self.cap)
            start = int(q["page_token"].removeprefix("tok-")) if q.get("page_token") else 0
            part, headers = entries[start:start + per], {}
            if start + per < len(entries):
                nq = {"id": info["id"], "page_token": f"tok-{start + per}", "pagination": "keyset", "per_page": q.get("per_page"),
                      "recursive": "true", "ref": q["ref"]}
                # GitLab builds this URL from its own idea of its address, which need not be the one we were given
                headers["link"] = f'<https://gitlab-internal.example:8443/api/v4/projects/{info["id"]}/repository/tree?{urlencode(nq)}>; rel="next"'
            return httpx.Response(200, json=part, headers=headers)
        return self.paged(entries, q)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GL_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "GL_TOKEN", "api_url": API, "projects": ["acme/payments"], "default_acl": ["group:eng"], **cfg}
    connector = GitLabConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


README, MAIN, DOC, CFG = b"# Payments\nHello", b"print('hi')\n", b"Design notes\n", b'{"a": 1}'
FILES = [entry("docs", kind="tree"), entry("src", kind="tree"), entry("README.md", README), entry("src/main.py", MAIN),
         entry("docs/design.md", DOC), entry("conf/app.json", CFG)]
BLOBS = {blob_sha(c): c for c in (README, MAIN, DOC, CFG)}


def proj(files=None, pid=42, path="acme/payments", info=None, **kw):
    return {"info": project(pid, path, **(info or {})), "files": copy.deepcopy(FILES if files is None else files), "blobs": BLOBS, **kw}


def one(**kw):
    return GitLab({42: proj(**kw)})


# ── files ──


def test_tree_is_followed_across_keyset_pages_with_only_the_page_token_and_never_to_another_host():
    site = one()
    changes, batches = run(make(site))
    by = {c.external_id: c.file for c in changes}
    assert sorted(by) == ["gitlab:42/blob/README.md", "gitlab:42/blob/conf/app.json", "gitlab:42/blob/docs/design.md", "gitlab:42/blob/src/main.py"]
    f = by["gitlab:42/blob/src/main.py"]
    assert f.path == "gitlab/acme/payments/src/main.py" and f.name == "main.py" and f.version == blob_sha(MAIN) and f.acl == ["group:eng"]
    assert f.mime in ("text/x-python", "text/x-python-script") and f.size == 0
    assert site.paths()[0] == "/projects/acme%2Fpayments"                     # looked up by URL-encoded path, once
    trees = site.calls(r"/repository/tree$")
    assert len(trees) == 3                                                    # 6 entries, 2 per page (directories are entries too)
    assert [r.url.params.get("page_token") for r in trees] == [None, "tok-2", "tok-4"]
    first = trees[0].url.params
    assert (first["recursive"], first["ref"], first["pagination"], first["per_page"]) == ("true", "main", "keyset", "100")
    assert {r.url.host for r in site.requests} == {HOST}                      # the Link pointed at another host; only its token was used
    assert batches[-1].done and batches[-1].changes == [] and scopes(batches) == ["gitlab:42/blob/"]


def test_offset_pagination_is_followed_through_x_next_page_and_a_server_that_ignores_keyset_still_works():
    for kwargs in ({"tree_pagination": "offset"}, {}):
        site = GitLab({42: proj()}, legacy_tree=not kwargs)
        changes, batches = run(make(site, **kwargs))
        assert len(changes) == 4 and scopes(batches) == ["gitlab:42/blob/"]
        trees = site.calls(r"/repository/tree$")
        assert [r.url.params.get("page") for r in trees] == [None, "2", "3"]
        assert ("pagination" in trees[0].url.params) == (not kwargs)


def test_numeric_ids_are_used_as_they_are_and_an_explicit_ref_replaces_the_default_branch():
    site = one()
    run(make(site, projects=[42]))
    assert site.paths()[0] == "/projects/42"
    site.reset()
    for entry_ in ("acme/payments@dev", {"project": "acme/payments", "ref": "dev"}):
        site.reset()
        run(make(site, projects=[entry_]))
        assert {r.url.params["ref"] for r in site.calls(r"/repository/tree$")} == {"dev"}
    site.reset()
    run(make(site))
    assert {r.url.params["ref"] for r in site.calls(r"/repository/tree$")} == {"main"}


def test_a_ref_that_does_not_exist_is_an_error_not_an_empty_listing():
    site = one()
    with pytest.raises(HttpError, match="ref does not exist") as err:
        run(make(site, projects=["acme/payments@nope"]))
    assert err.value.status == 404


def test_a_project_that_cannot_be_found_is_an_error_with_a_hint():
    with pytest.raises(HttpError, match="cannot see it") as err:
        run(make(one(), projects=["acme/missing"]))
    assert err.value.status == 404 and TOKEN not in str(err.value)


def test_directories_symlinks_submodules_vendored_and_binary_files_are_not_files_to_index():
    files = [entry("a.md", b"a"), entry("link", b"a.md", mode="120000"), entry("sub", kind="commit", mode="160000"), entry("src", kind="tree"),
             entry("node_modules/x/index.js", b"js"), entry("logo.png", b"png"), entry("vendor/lib.py", b"v"), entry("app.min.js", b"m")]
    changes, _ = run(make(one(files=files)))
    assert [c.external_id for c in changes] == ["gitlab:42/blob/a.md"]


def test_include_and_exclude_globs_select_files():
    changes, _ = run(make(one(), include=["*.py", "*.md"], exclude=["README.md"]))
    assert sorted(c.external_id for c in changes) == ["gitlab:42/blob/docs/design.md", "gitlab:42/blob/src/main.py"]


def test_empty_repository_or_no_default_branch_lists_nothing_and_the_scope_so_old_files_are_deleted():
    for info in ({"empty_repo": True}, {"default_branch": None}):
        site = one(info=info)
        changes, batches = run(make(site))
        assert changes == [] and scopes(batches) == ["gitlab:42/blob/"] and not site.calls("/repository/tree")


def test_batches_resume_after_the_last_path_and_a_resumed_listing_is_not_authoritative():
    site = one()
    batches = list(make(site, batch_size=2).scan({}))
    first = batches[0]
    assert [c.file.path.removeprefix("gitlab/acme/payments/") for c in first.changes] == ["README.md", "conf/app.json"]
    assert first.cursor["pos"] == {"project": 0, "phase": "files", "after": "conf/app.json"} and first.complete_scope is None
    assert json.loads(json.dumps(first.cursor)) == first.cursor
    site.reset()
    changes, resumed = run(make(site, batch_size=2), first.cursor)
    assert [c.file.path.removeprefix("gitlab/acme/payments/") for c in changes] == ["docs/design.md", "src/main.py"]
    assert scopes(resumed) == []
    assert len([b for b in batches if b.complete_scope]) == 1 and batches[1].complete_scope == "gitlab:42/blob/"


def test_projects_are_scanned_in_order_each_with_its_own_scope_and_resume_picks_up_at_the_right_project():
    site = GitLab({42: proj(), 43: proj([entry("index.md", b"hello")], pid=43, path="acme/website")})
    projects = ["acme/payments", "acme/website"]
    batches = list(make(site, projects=projects).scan({}))
    assert scopes(batches) == ["gitlab:42/blob/", "gitlab:43/blob/"]
    boundary = next(b for b in batches if b.complete_scope == "gitlab:42/blob/")
    assert boundary.cursor["pos"] == {"project": 1, "phase": "files"}
    site.reset()
    changes, resumed = run(make(site, projects=projects), boundary.cursor)
    assert [c.external_id for c in changes] == ["gitlab:43/blob/index.md"] and scopes(resumed) == ["gitlab:43/blob/"]
    assert not site.calls(r"/projects/(42|acme%2Fpayments)")


def test_a_project_listed_twice_is_scanned_once():
    site = one()
    changes, batches = run(make(site, projects=["acme/payments", {"project": "acme/payments", "ref": "dev"}, "acme/payments@main"]))
    assert len(changes) == 4 and len(site.calls(r"/repository/tree$")) == 3 and scopes(batches) == ["gitlab:42/blob/"]


def test_max_items_and_max_pages_truncate_never_declare_completeness_and_stop_before_the_next_project():
    site = GitLab({42: proj(), 43: proj([entry("index.md", b"hello")], pid=43, path="acme/website")})
    projects = ["acme/payments", "acme/website"]
    changes, batches = run(make(site, projects=projects, max_items=2))
    assert len(changes) == 2 and scopes(batches) == [] and "since" not in batches[-1].cursor and not site.calls("website|/projects/43")
    assert site.paths().count("/projects/acme%2Fwebsite") == 0
    site.reset()
    changes, batches = run(make(site, projects=projects, max_pages=3))              # exactly the three tree pages of the first project
    assert len(changes) == 4 and scopes(batches) == ["gitlab:42/blob/"] and not site.calls("website|/projects/43")      # not even looked up
    assert batches[-1].done and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=2))                                 # the tree is cut short: what was read is kept, nothing is declared complete
    assert len(changes) == 4 and scopes(batches) == [] and len(site.calls(r"/repository/tree$")) == 2     # (the two pages happen to hold the four blobs)
    site.reset()
    changes, batches = run(make(site, max_pages=1))
    assert len(changes) == 2 and scopes(batches) == [] and len(site.calls(r"/repository/tree$")) == 1


# ── open() ──


def test_open_downloads_the_blob_by_sha_from_persisted_columns_only():
    site = one()
    by = {c.external_id: c.file for c in run(make(site))[0]}
    site.reset()
    with make(site).open(persisted(by["gitlab:42/blob/src/main.py"])) as fh:
        assert fh.read() == MAIN
    assert site.paths() == [f"/projects/42/repository/blobs/{blob_sha(MAIN)}/raw"] and site.requests[0].headers["private-token"] == TOKEN


def test_open_enforces_the_size_cap_and_rejects_bad_ids_without_a_request():
    big = b"y" * 3000
    site = one(files=[entry("big.txt", big)], blobs={blob_sha(big): big})
    site.projects[42]["blobs"] = {blob_sha(big): big}
    connector = make(site, max_file_mb=0.001)
    with pytest.raises(TooLarge):
        connector.open(SourceFile("gitlab:42/blob/big.txt", "p", "big.txt", 0, None, blob_sha(big), []))
    site.reset()
    sha = blob_sha(b"")
    for ext, version in (("gitlab:42/blob/a.md", "not-a-sha"), ("gitlab:abc/blob/a.md", sha), ("github:42/blob/a.md", sha), ("gitlab:42/other/a.md", sha),
                         ("gitlab:42/issue/abc", "v"), ("gitlab:42/blob/", sha)):
        with pytest.raises(HttpError):
            connector.open(SourceFile(ext, "p", "a", 0, None, version, []))
    assert site.requests == []


def test_a_blob_that_vanished_is_an_error_with_a_status():
    site = one()
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("gitlab:42/blob/a.md", "p", "a.md", 0, None, blob_sha(b"gone"), []))
    assert err.value.status == 404


# ── wiki ──

WIKI = [wiki_page("home", "# Welcome\nStart <b>here</b> <!-- draft -->"), wiki_page("ops/runbook", "Restart the service.", "Runbook"),
        wiki_page("ops/oncall", "Page the *on-call*.", "On call"), wiki_page("faq", "Q&A", fmt="asciidoc")]


def test_wiki_pages_become_files_with_a_content_version_and_their_own_scope():
    site = one(wiki=WIKI)
    changes, batches = run(make(site, include_files=False, include_wiki=True))
    by = {c.external_id: c.file for c in changes}
    assert sorted(by) == ["gitlab:42/wiki/faq", "gitlab:42/wiki/home", "gitlab:42/wiki/ops/oncall", "gitlab:42/wiki/ops/runbook"]
    assert by["gitlab:42/wiki/ops/runbook"].path == "gitlab/acme/payments/_wiki/ops/runbook.md"
    assert len(site.calls(r"/wikis$")) == 2                                     # the fake paginates (2 per page) and X-Next-Page is followed
    text = read(make(site, include_files=False, include_wiki=True), by["gitlab:42/wiki/ops/runbook"])
    assert text.startswith("# Runbook\n") and "project: acme/payments" in text and "Restart the service." in text
    assert f"url: https://{HOST}/acme/payments/-/wikis/ops/runbook" in text
    assert "format: asciidoc" in read(make(site, include_files=False, include_wiki=True), by["gitlab:42/wiki/faq"])
    assert scopes(batches) == ["gitlab:42/wiki/"]


def test_wiki_version_changes_only_when_the_page_changes():
    first = {c.external_id: c.file.version for c in run(make(one(wiki=WIKI), include_files=False, include_wiki=True))[0]}
    again = {c.external_id: c.file.version for c in run(make(one(wiki=WIKI), include_files=False, include_wiki=True))[0]}
    assert first == again
    edited = copy.deepcopy(WIKI)
    edited[1]["content"] += " Then check the logs."
    after = {c.external_id: c.file.version for c in run(make(one(wiki=edited), include_files=False, include_wiki=True))[0]}
    assert {k for k in first if first[k] != after[k]} == {"gitlab:42/wiki/ops/runbook"}


def test_wiki_open_refetches_the_page_by_its_encoded_slug_and_a_deleted_page_is_a_404():
    site = one(wiki=WIKI)
    by = {c.external_id: c.file for c in run(make(site, include_files=False, include_wiki=True))[0]}
    site.reset()
    connector = make(site)
    assert "Page the *on-call*." in read(connector, by["gitlab:42/wiki/ops/oncall"])
    assert "/projects/42/wikis/ops%2Foncall" in site.paths()
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile("gitlab:42/wiki/gone", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


def test_a_project_with_the_wiki_switched_off_is_an_authoritative_empty_listing_but_a_404_is_not():
    site = one(wiki=WIKI, info={"wiki_enabled": False})
    changes, batches = run(make(site, include_files=False, include_wiki=True))
    assert changes == [] and scopes(batches) == ["gitlab:42/wiki/"] and not site.calls("/wikis")
    site = one(wiki=WIKI, no_wiki=True)
    changes, batches = run(make(site, include_files=False, include_wiki=True))
    assert changes == [] and scopes(batches) == []


def test_wiki_batches_resume_after_the_last_slug():
    site = one(wiki=WIKI)
    batches = list(make(site, include_files=False, include_wiki=True, batch_size=2).scan({}))
    assert batches[0].cursor["pos"] == {"project": 0, "phase": "wiki", "after": "home"}              # sorted: faq, home | ops/oncall, ops/runbook
    site.reset()
    changes, resumed = run(make(site, include_files=False, include_wiki=True, batch_size=2), batches[0].cursor)
    assert [c.external_id for c in changes] == ["gitlab:42/wiki/ops/oncall", "gitlab:42/wiki/ops/runbook"] and scopes(resumed) == []


# ── issues and merge requests ──

ISSUES = [issue(n, f"Issue {n}", updated=f"2026-03-0{n}T10:00:00Z", labels=["bug"] if n % 2 else [], notes=n % 2) for n in range(1, 6)]
MRS = [merge_request(7, "Add retries", updated="2026-03-07T10:00:00Z", notes=1), merge_request(8, "Fix typo", updated="2026-03-08T10:00:00Z", state="merged")]


def with_items(**kw):
    return GitLab({42: proj(issues=copy.deepcopy(ISSUES), merge_requests=copy.deepcopy(MRS), **kw)})


def issues_only(site, **cfg):
    return make(site, include_files=False, include_issues=True, **cfg)


def test_issues_follow_x_next_page_across_pages_and_a_full_listing_is_authoritative():
    site = with_items()
    changes, batches = run(issues_only(site))
    assert [c.external_id for c in changes] == [f"gitlab:42/issue/{n}" for n in range(1, 6)]
    listing = site.calls(r"/issues$")
    assert [r.url.params["page"] for r in listing] == ["1", "2", "3"]
    first = listing[0].url.params
    assert (first["scope"], first["state"], first["order_by"], first["sort"], first["per_page"]) == ("all", "all", "updated_at", "asc", "100")
    assert "updated_after" not in first
    assert scopes(batches) == ["gitlab:42/issue/"] and batches[-1].done
    assert changes[0].file.path == "gitlab/acme/payments/_issues/1-Issue 1.md"


def test_issue_text_has_metadata_labels_notes_and_no_html_comments_and_system_or_internal_notes_are_left_out():
    notes = {("issues", 7): [note(1, "grace", "Reproduced"), note(2, "gitlab-bot", "changed the description", system=True),
                             note(3, "ada", "Customer data inside", internal=True), note(4, "ada", "")]}
    data = proj(issues=[issue(7, "Crash on start", body="Steps:\n<!-- describe -->\n1. do it", labels=["bug", "p1"], notes=4)], notes=notes)
    site = GitLab({42: data})
    sf = run(issues_only(site))[0][0].file
    text = read(make(GitLab({42: data})), sf)
    for line in ("project: acme/payments", "type: issue", "state: opened", "author: ada", "labels: bug, p1", "assignees: grace",
                 f"url: https://{HOST}/acme/payments/-/issues/7"):
        assert line in text
    assert text.startswith("# acme/payments#7: Crash on start\n") and "1. do it" in text and "<!--" not in text and "describe" not in text
    assert "### grace (2026-03-02T08:00:00.000Z)\n\nReproduced" in text
    assert "changed the description" not in text and "Customer data" not in text and "### ada" not in text
    assert sf.size == len(text.encode()) and sf.version.startswith("2026-03-01T10:00:00Z-")
    internal = read(make(GitLab({42: data}), include_internal_notes=True), sf)
    assert "Customer data inside" in internal and "changed the description" not in internal


def test_notes_are_paginated_and_not_requested_for_an_issue_without_any():
    many = [note(n, "grace", f"comment number {n}") for n in range(1, 6)]
    data = proj(issues=[issue(7, "Chatty", notes=5), issue(8, "Quiet", notes=0, updated="2026-03-02T10:00:00Z")], notes={("issues", 7): many})
    site = GitLab({42: data})
    connector = issues_only(site)
    changes, _ = run(connector)
    assert len(site.calls(r"/issues/7/notes$")) == 3 and not site.calls(r"/issues/8/notes$")
    assert all(f"comment number {n}" in read(connector, changes[0].file) for n in range(1, 6))


def test_issue_version_follows_the_text_not_just_the_timestamp():
    base = proj(issues=[issue(7, "Chatty", notes=1)], notes={("issues", 7): [note(1, "grace", "first")]})
    v1 = run(issues_only(GitLab({42: base})))[0][0].file.version
    assert run(issues_only(GitLab({42: copy.deepcopy(base)})))[0][0].file.version == v1
    edited = copy.deepcopy(base)
    edited["notes"][("issues", 7)][0]["body"] = "edited later"
    assert run(issues_only(GitLab({42: edited})))[0][0].file.version != v1                 # same updated_at, other text
    retitled = copy.deepcopy(base)
    retitled["issues"][0]["updated_at"] = "2026-03-09T10:00:00Z"
    assert run(issues_only(GitLab({42: retitled})))[0][0].file.version != v1


def test_confidential_issues_are_not_indexed_and_a_newly_confidential_one_is_deleted():
    secret = issue(3, "Security hole", updated="2026-03-03T10:00:00Z", confidential=True)
    site = GitLab({42: proj(issues=[issue(1, "Public"), secret])})
    changes, batches = run(issues_only(site))
    assert sorted((c.kind, c.external_id) for c in changes) == [("delete", "gitlab:42/issue/3"), ("upsert", "gitlab:42/issue/1")]
    assert not site.calls(r"/issues/3")
    changes, _ = run(issues_only(site, include_confidential=True))
    assert sorted((c.kind, c.external_id) for c in changes) == [("upsert", "gitlab:42/issue/1"), ("upsert", "gitlab:42/issue/3")]
    with pytest.raises(HttpError) as err:                                           # opening one directly is refused too
        issues_only(site).open(SourceFile("gitlab:42/issue/3", "p", "n", 0, None, "v", []))
    assert err.value.status == 403
    assert "Security hole" in read(issues_only(site, include_confidential=True), SourceFile("gitlab:42/issue/3", "p", "n", 0, None, "v", []))


def test_merge_requests_are_a_separate_kind_with_branches_and_their_own_scope():
    notes = {("merge_requests", 7): [note(1, "linus", "Looks good")]}
    site = with_items(notes=notes)
    changes, batches = run(make(site, include_files=False, include_merge_requests=True))
    assert [c.external_id for c in changes] == ["gitlab:42/mr/7", "gitlab:42/mr/8"]
    assert scopes(batches) == ["gitlab:42/mr/"] and not site.calls(r"/issues")
    sf = changes[0].file
    assert sf.path == "gitlab/acme/payments/_merge_requests/7-Add retries.md"
    text = read(make(site, include_merge_requests=True), sf)
    assert text.startswith("# acme/payments!7: Add retries\n") and "type: merge request" in text and "source branch: feature/x" in text
    assert "target branch: main" in text and "reviewers: linus" in text and "### linus" in text and "Looks good" in text
    assert "draft" not in text and "state: merged" in read(make(site), changes[1].file)


def test_issues_and_merge_requests_can_share_a_scan_in_a_fixed_order_with_files_first():
    site = with_items(wiki=WIKI)
    changes, batches = run(make(site, include_wiki=True, include_issues=True, include_merge_requests=True))
    kinds = [c.external_id.split("/")[1] for c in changes]
    assert kinds == ["blob"] * 4 + ["wiki"] * 4 + ["issue"] * 5 + ["mr"] * 2
    assert scopes(batches) == ["gitlab:42/blob/", "gitlab:42/wiki/", "gitlab:42/issue/", "gitlab:42/mr/"]
    assert site.paths().count("/projects/acme%2Fpayments") == 1                      # the project is looked up once for all four kinds


def test_issue_scan_after_a_finished_one_uses_updated_after_with_overlap_and_is_not_authoritative():
    site = with_items()
    connector = issues_only(site)
    _, batches = run(connector)
    cursor = batches[-1].cursor
    assert cursor["since"] == "2026-03-05T10:00:00Z" and cursor["last_full"] == NOW
    site.reset()
    later = issues_only(site, clock=lambda: NOW + 3600)
    changes, batches = run(later, cursor)
    assert {r.url.params["updated_after"] for r in site.calls(r"/issues$")} == {"2026-03-05T09:55:00Z"}
    assert [c.external_id for c in changes] == ["gitlab:42/issue/5"] and scopes(batches) == []
    assert batches[-1].cursor["since"] == "2026-03-05T10:00:00Z" and batches[-1].cursor["last_full"] == NOW


def test_full_resync_when_due_and_issues_since_limits_the_first_scan():
    site = with_items()
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW}
    changes, batches = run(issues_only(site, clock=lambda: NOW + 25 * 3600), cursor)
    assert len(changes) == 5 and scopes(batches) == ["gitlab:42/issue/"]
    assert "updated_after" not in site.calls(r"/issues$")[0].url.params
    site.reset()
    changes, batches = run(issues_only(site, issues_since="2026-03-04"))
    assert [c.external_id for c in changes] == ["gitlab:42/issue/4", "gitlab:42/issue/5"]
    assert site.calls(r"/issues$")[0].url.params["updated_after"] == "2026-03-04T00:00:00Z"


def test_issue_listing_resumes_one_page_early_and_does_not_claim_completeness():
    site = with_items()
    connector = make(site, include_issues=True)
    mid = next(b for b in connector.scan({}) if b.cursor["pos"].get("phase") == "issues" and b.cursor["pos"].get("page") == 3)
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, batches = run(make(site, include_issues=True), mid.cursor)
    assert [r.url.params["page"] for r in site.calls(r"/issues$")] == ["2", "3"]          # page 3 was the next one; offsets drift, so one page back
    assert [c.external_id for c in changes] == [f"gitlab:42/issue/{n}" for n in (3, 4, 5)] and scopes(batches) == []
    assert not site.calls("/repository/tree")                                              # the files of this project were done before the crash


def test_issue_state_filter_and_features_switched_off_per_project():
    site = GitLab({42: proj(issues=[issue(1, "Open"), issue(2, "Done", state="closed", updated="2026-03-02T10:00:00Z")])})
    changes, _ = run(issues_only(site, issues_state="opened"))
    assert [c.external_id for c in changes] == ["gitlab:42/issue/1"]
    for flags, kwargs, scope in (({"issues_enabled": False, "merge_requests_enabled": True}, {"include_issues": True}, "gitlab:42/issue/"),
                                 ({"issues_enabled": True, "merge_requests_enabled": False}, {"include_merge_requests": True}, "gitlab:42/mr/")):
        off = GitLab({42: proj(issues=[issue(1, "Open")], merge_requests=copy.deepcopy(MRS), info=flags)})
        changes, batches = run(make(off, include_files=False, **kwargs))
        assert changes == [] and scopes(batches) == [scope] and not off.calls(r"/issues|/merge_requests")


def test_max_items_clips_an_issue_page_and_max_pages_stops_the_listing_without_claiming_completeness():
    site = GitLab({42: proj(issues=copy.deepcopy(ISSUES))}, cap=100)
    changes, batches = run(issues_only(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    site = with_items()
    changes, batches = run(issues_only(site, max_pages=2))
    assert len(site.calls(r"/issues$")) == 2 and len(changes) == 4 and scopes(batches) == []
    assert batches[-2].cursor["pos"] == {"project": 0, "phase": "issues", "page": 3}


def test_issue_open_refetches_the_issue_and_renders_the_same_bytes():
    site = with_items()
    by = {c.external_id: c.file for c in run(issues_only(site))[0]}
    site.reset()
    for sf in by.values():
        assert len(read(make(site), sf).encode()) == sf.size
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("gitlab:42/issue/404", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


# ── permissions ──


def test_acl_rules_per_project_prefix_fail_closed_for_files_wiki_issues_and_merge_requests():
    site = GitLab({42: proj(wiki=WIKI, issues=copy.deepcopy(ISSUES), merge_requests=copy.deepcopy(MRS)),
                   43: proj([entry("index.md", b"hi")], pid=43, path="acme/web/site")})
    rules = [{"prefix": "gitlab/acme/payments", "principals": ["group:payments"]},
             {"prefix": "gitlab/acme/payments/docs", "principals": ["*"]},
             {"prefix": "gitlab/acme/payments/_issues", "principals": ["group:support"]}]
    connector = make(site, projects=["acme/payments", "acme/web/site"], include_wiki=True, include_issues=True, include_merge_requests=True,
                     acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["gitlab:42/blob/src/main.py"].acl == ["group:payments"] and by["gitlab:42/blob/docs/design.md"].acl == ["*"]
    assert by["gitlab:42/wiki/home"].acl == ["group:payments"] and by["gitlab:42/mr/7"].acl == ["group:payments"]
    assert by["gitlab:42/issue/1"].acl == ["group:support"]
    assert by["gitlab:43/blob/index.md"].acl == [] and by["gitlab:43/blob/index.md"].path == "gitlab/acme/web/site/index.md"
    assert connector.refresh_acl(persisted(by["gitlab:42/issue/1"])) == ["group:support"]
    assert connector.refresh_acl(persisted(by["gitlab:43/blob/index.md"])) == []


# ── rate limits, auth ──


def test_429_is_retried_after_retry_after_and_a_listing_continues():
    site = one()
    site.script = [httpx.Response(429, headers={"retry-after": "7"}, json={"message": "Retry later"}),
                   httpx.Response(503, headers={"retry-after": "2"}, text="busy")]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 4 and connector.sleeps == [7.0, 2.0]


def test_401_never_shows_the_token_and_a_403_is_an_auth_error_that_names_the_variable(monkeypatch):
    monkeypatch.setenv("GL_TOKEN", "glpat-wrongwrongwrong")
    site = one()
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "401" in str(err.value) and "glpat-wrongwrongwrong" not in str(err.value) and TOKEN not in str(err.value)
    assert "GL_TOKEN" in str(err.value) and len(site.requests) == 1 and connector.sleeps == []
    site = one()
    site.script = [httpx.Response(403, json={"message": "403 Forbidden"})]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert "403" in str(err.value) and "GL_TOKEN" in str(err.value)


def test_a_server_that_echoes_the_token_in_an_error_body_never_gets_it_into_the_message():
    for call in ("scan", "download", "auth-download"):
        site = one()
        by = {c.external_id: c.file for c in run(make(site))[0]}
        if call == "scan":
            site.script = [httpx.Response(500, text=f"upstream rejected {TOKEN}")] * 6
            action = lambda: run(make(site))        # noqa: E731
        else:
            site.script = [httpx.Response(401 if call == "auth-download" else 500, text=f"upstream rejected {TOKEN}")] * 6
            action = lambda: make(site).open(persisted(by["gitlab:42/blob/README.md"]))        # noqa: E731
        with pytest.raises(HttpError) as err:
            action()
        assert TOKEN not in str(err.value) and "***" in str(err.value)


def test_bearer_auth_sends_an_authorization_header_instead_of_private_token():
    site = GitLab({42: proj()}, header="Authorization", value=f"Bearer {TOKEN}")
    changes, _ = run(make(site, auth="bearer"))
    assert len(changes) == 4 and all("private-token" not in r.headers for r in site.requests)
    with pytest.raises(AuthError):
        run(make(GitLab({42: proj()}, header="Authorization", value=f"Bearer {TOKEN}")))               # the default header is not accepted there


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(with_items(), include_issues=True))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("GL_TOKEN")
    with pytest.raises(HttpError, match="GL_TOKEN"):
        run(make(one()))


def test_a_self_managed_api_url_with_a_path_prefix_is_used_for_every_request():
    site = GitLab({42: proj()}, prefix="/gitlab/api/v4")
    connector = GitLabConnector({"token_env": "GL_TOKEN", "projects": ["acme/payments"], "api_url": "https://git.corp.example/gitlab/api/v4/",
                                 "default_acl": ["*"]}, transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    assert len(run(connector)[0]) == 4
    assert {r.url.host for r in site.requests} == {"git.corp.example"} and all("//" not in site.raw(r) for r in site.requests)


def test_configuration_errors_are_rejected_up_front():
    for bad in ({}, {"projects": []}):
        with pytest.raises(ValueError, match="projects"):
            GitLabConnector({"token_env": "X", **bad})
    for bad in ("justaname", "a//b", "../x", "a/..", "a/b c", "a/b?x=1", ""):
        with pytest.raises(ValueError, match="project"):
            GitLabConnector({"projects": [bad]})
    with pytest.raises(ValueError, match="auth"):
        GitLabConnector({"projects": ["a/b"], "auth": "basic"})
    with pytest.raises(ValueError, match="tree_pagination"):
        GitLabConnector({"projects": ["a/b"], "tree_pagination": "page"})
    with pytest.raises(ValueError, match="nothing to index"):
        GitLabConnector({"projects": ["a/b"], "include_files": False})
    assert GitLabConnector({"projects": ["a/b", 12, "a/b/c@dev"]}).projects == [("a/b", None), ("12", None), ("a/b/c", "dev")]


def test_a_cursor_written_for_a_different_configuration_starts_over_instead_of_failing():
    site = with_items()
    cursor = {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"project": 0, "phase": "wiki", "after": "x"}}
    changes, batches = run(issues_only(site), cursor)
    assert len(changes) == 5


def test_every_object_kind_has_a_stable_external_id_that_open_understands():
    site = with_items(wiki=WIKI)
    connector = make(site, include_wiki=True, include_issues=True, include_merge_requests=True)
    changes, _ = run(connector)
    ids = [c.external_id for c in changes]
    assert len(ids) == len(set(ids)) == 15
    assert all(read(make(site), c.file) for c in changes)


def test_a_keyset_next_link_without_a_page_token_is_an_error_not_a_silent_end_of_the_listing():
    site = one()
    original = site.tree

    def broken(data, q):
        r = original(data, q)
        return httpx.Response(200, json=r.json(), headers={"link": f'<{API}/projects/42/repository/tree?id=42&pagination=keyset>; rel="next"'}) if "link" in r.headers else r
    site.tree = broken
    with pytest.raises(HttpError, match="page_token"):
        run(make(site))


def test_a_truncated_listing_stops_at_the_cut_batch():
    site = one()
    changes, batches = run(make(site, max_items=2, batch_size=1))
    assert len(changes) == 2 and len(batches) == 4 and batches[-1].done and scopes(batches) == []


def test_resuming_after_the_files_phase_continues_with_the_next_phase_of_the_same_project():
    site = one(wiki=WIKI, issues=copy.deepcopy(ISSUES))
    connector = make(site, include_wiki=True, include_issues=True)
    batches = list(connector.scan({}))
    boundary = next(b for b in batches if b.complete_scope == "gitlab:42/blob/")
    assert boundary.cursor["pos"] == {"project": 0, "phase": "wiki"}
    wiki_end = next(b for b in batches if b.complete_scope == "gitlab:42/wiki/")
    assert wiki_end.cursor["pos"] == {"project": 0, "phase": "issues"}
    site.reset()
    changes, resumed = run(make(site, include_wiki=True, include_issues=True), boundary.cursor)
    assert {c.external_id.split("/")[1] for c in changes} == {"wiki", "issue"} and not site.calls("/repository/tree")
    assert scopes(resumed) == ["gitlab:42/wiki/", "gitlab:42/issue/"]                  # both listings ran from their first page to their last


def test_a_files_resume_position_does_not_leak_into_the_wiki_listing_that_follows():
    pages = [wiki_page("Home", "Welcome"), wiki_page("Ops/Runbook", "Restart it")]           # sort before every file path
    site = one(wiki=pages)
    batches = list(make(site, include_wiki=True, batch_size=2).scan({}))
    first = batches[0]
    assert first.cursor["pos"] == {"project": 0, "phase": "files", "after": "conf/app.json"}
    site.reset()
    changes, resumed = run(make(site, include_wiki=True, batch_size=2), first.cursor)
    assert [c.external_id for c in changes if "/wiki/" in c.external_id] == ["gitlab:42/wiki/Home", "gitlab:42/wiki/Ops/Runbook"]
    assert scopes(resumed) == ["gitlab:42/wiki/"]


def test_wiki_version_follows_the_title_too():
    renamed = copy.deepcopy(WIKI)
    renamed[0]["title"] = "Start here"
    before = {c.external_id: c.file.version for c in run(make(one(wiki=WIKI), include_files=False, include_wiki=True))[0]}
    after = {c.external_id: c.file.version for c in run(make(one(wiki=renamed), include_files=False, include_wiki=True))[0]}
    assert {k for k in before if before[k] != after[k]} == {"gitlab:42/wiki/home"}


def test_the_wiki_listing_counts_against_max_pages():
    site = with_items(wiki=WIKI)
    changes, batches = run(make(site, include_files=False, include_wiki=True, include_issues=True, max_pages=1))
    assert len(changes) == 4 and scopes(batches) == ["gitlab:42/wiki/"] and not site.calls(r"/issues") and "since" not in batches[-1].cursor


def test_the_watermark_moves_past_confidential_issues_that_were_skipped():
    site = GitLab({42: proj(issues=[issue(1, "Public"), issue(2, "Secret", updated="2026-03-09T10:00:00Z", confidential=True)])})
    _, batches = run(issues_only(site))
    assert batches[-1].cursor["since"] == "2026-03-09T10:00:00Z"


def test_notes_marked_with_the_older_confidential_flag_are_left_out_as_well():
    old_style = note(3, "ada", "Old-style secret")
    old_style.pop("internal")
    old_style["confidential"] = True
    data = proj(issues=[issue(7, "Crash", notes=2)], notes={("issues", 7): [note(1, "grace", "Visible"), old_style]})
    text = read(make(GitLab({42: data})), run(issues_only(GitLab({42: data})))[0][0].file)
    assert "Visible" in text and "Old-style secret" not in text


def test_a_failing_project_lookup_only_gets_the_hint_when_it_is_a_404_and_a_malformed_project_is_refused():
    site = one()
    site.script = [httpx.Response(500, text="boom")] * 6
    with pytest.raises(HttpError) as err:
        run(make(site))
    assert "cannot see it" not in str(err.value)
    site = one()
    site.script = [httpx.Response(200, json={"message": "something else"})]
    with pytest.raises(HttpError, match="without an id"):
        run(make(site))


def test_a_server_that_keeps_returning_the_same_next_page_is_an_error_not_an_endless_loop():
    data = proj(issues=[issue(7, "Crash", notes=2)], notes={("issues", 7): [note(1, "grace", "a")]})
    site = GitLab({42: data})
    original = site.paged

    def stuck(items, q):
        r = original(items, q)
        return httpx.Response(200, json=r.json(), headers={**r.headers, "x-next-page": "1"})
    site.paged = stuck
    with pytest.raises(HttpError, match="did not advance"):
        run(issues_only(site))
