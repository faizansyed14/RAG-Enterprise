"""Jira connector against a fake site shaped like the Jira REST API documentation:
Cloud `GET /rest/api/3/search/jql` (nextPageToken / isLast), legacy `GET /rest/api/{2,3}/search`
(startAt / maxResults / total), `GET /issue/{id}`, `GET /issue/{key}/comment`, bodies in ADF.

Field names copied from the docs. Not certain of: `comment.total` being present in every Data Center version's
search results (the connector tolerates its absence), and `jsdPublic` on comments outside Service Management.
"""

import base64
import copy
import json
import re
from datetime import datetime, timezone

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.jira import JiraConnector, adf_to_text

SITE = "https://acme.atlassian.net"
TOKEN = "JIRA-SECRET-77aa"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


# ── ADF builders (Atlassian Document Format) ──


def text(t, *marks):
    node = {"type": "text", "text": t}
    if marks:
        node["marks"] = [m if isinstance(m, dict) else {"type": m} for m in marks]
    return node


def para(*inline):
    return {"type": "paragraph", "content": list(inline)}


def adf(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def comment(cid, author, body, created="2026-03-02T09:00:00.000+0000", **extra):
    return {"self": f"{SITE}/rest/api/3/issue/10001/comment/{cid}", "id": str(cid),
            "author": {"accountId": "acc-1", "displayName": author, "active": True},
            "body": adf(para(text(body))) if isinstance(body, str) else body,
            "created": created, "updated": created, **extra}


def issue(iid, key, summary, *, updated="2026-03-04T10:11:12.345+0000", description="Something is broken.",
          comments=(), comment_total=None, project="PROJ", status="To Do", security=None, **fields):
    box = {"comments": list(comments), "self": f"{SITE}/rest/api/3/issue/{iid}/comment",
           "maxResults": 1048576, "total": len(comments) if comment_total is None else comment_total, "startAt": 0}
    return {
        "expand": "operations,versionedRepresentations,editmeta,changelog,renderedFields",
        "id": str(iid), "self": f"{SITE}/rest/api/3/issue/{iid}", "key": key,
        "fields": {
            "summary": summary,
            "description": adf(para(text(description))) if isinstance(description, str) else description,
            "issuetype": {"name": "Bug"}, "status": {"name": status, "statusCategory": {"key": "new"}},
            "priority": {"name": "High"}, "assignee": {"accountId": "a", "displayName": "Grace Hopper"},
            "reporter": {"accountId": "b", "displayName": "Ada Lovelace"}, "labels": ["backend", "urgent"],
            "components": [{"name": "API"}], "fixVersions": [{"name": "1.2"}], "resolution": None,
            "created": "2026-03-01T08:00:00.000+0000", "updated": updated,
            "project": {"id": "10000", "key": project, "name": {"PROJ": "Platform", "HR": "People"}.get(project, project)},
            "security": security, "comment": box, **fields,
        },
    }


class JiraSite:
    def __init__(self, issues, *, api="/rest/api/3", auth="Basic"):
        self.issues = {i["id"]: copy.deepcopy(i) for i in issues}
        self.api, self.auth = api, auth
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def searches(self):
        return [r for r in self.requests if r.url.path.endswith(("/search/jql", "/search"))]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        header = request.headers.get("authorization", "")
        want = ("Basic " + base64.b64encode(f"ada@acme.com:{TOKEN}".encode()).decode()) if self.auth == "Basic" else f"Bearer {TOKEN}"
        if header != want:
            return httpx.Response(401, json={"errorMessages": ["You do not have the permission to see the specified issue."]})
        path, q = request.url.path, dict(request.url.params)
        if path == f"{self.api}/search/jql":
            return self._search_token(q)
        if path == f"{self.api}/search":
            return self._search_legacy(q)
        m = re.fullmatch(re.escape(self.api) + r"/issue/(\w+-?\d*)/comment", path)
        if m:
            return self._comments(m.group(1), q)
        m = re.fullmatch(re.escape(self.api) + r"/issue/(\d+)", path)
        if m:
            found = self.issues.get(m.group(1))
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        return httpx.Response(404, json={"errorMessages": ["no endpoint " + path]})

    def _matching(self, jql):
        assert jql.count("ORDER BY") == 1 and jql.endswith("ORDER BY updated ASC, key ASC"), jql
        found = list(self.issues.values())
        m = re.search(r"project in \(([^)]*)\)", jql)
        if m:
            found = [i for i in found if i["fields"]["project"]["key"] in m.group(1).split(",")]
        m = re.search(r'updated >= "(\d{4}-\d\d-\d\d \d\d:\d\d)"', jql)
        if m:
            floor = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            found = [i for i in found if datetime.strptime(i["fields"]["updated"], "%Y-%m-%dT%H:%M:%S.%f%z") >= floor]
        return sorted(found, key=lambda i: (i["fields"]["updated"], i["key"]))

    def _search_token(self, q):
        found = self._matching(q["jql"])
        assert "startAt" not in q, "the token endpoint has no startAt"
        size = int(q["maxResults"])
        offset = int(q["nextPageToken"].split("-")[1]) if "nextPageToken" in q else 0
        page = found[offset:offset + size]
        body = {"issues": page}
        if offset + size < len(found):
            body["nextPageToken"] = f"tok-{offset + size}-ChZ0cm9ib"
            body["isLast"] = False
        else:
            body["isLast"] = True
        return httpx.Response(200, json=body)

    def _search_legacy(self, q):
        found = self._matching(q["jql"])
        start, size = int(q["startAt"]), int(q["maxResults"])
        return httpx.Response(200, json={"expand": "names,schema", "startAt": start, "maxResults": size,
                                         "total": len(found), "issues": found[start:start + size]})

    def _comments(self, key, q):
        issue_ = next(i for i in self.issues.values() if i["key"] == key)
        everything = issue_["_all_comments"] if "_all_comments" in issue_ else issue_["fields"]["comment"]["comments"]
        start, size = int(q["startAt"]), int(q["maxResults"])
        return httpx.Response(200, json={"startAt": start, "maxResults": size, "total": len(everything),
                                         "comments": everything[start:start + size]})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"base_url": SITE, "email": "ada@acme.com", "token_env": "JIRA_TOKEN", "projects": ["PROJ", "HR"],
              "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = JiraConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
    connector.sleeps = sleeps
    return connector


def run(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


def persisted(sf):
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


FIVE = [issue(10000 + n, f"PROJ-{n}", f"Issue number {n}", updated=f"2026-03-0{n}T10:00:00.000+0000") for n in range(1, 6)]


# ── pagination ──


def test_token_pagination_sends_the_server_token_back_and_stops_at_is_last():
    site = JiraSite(FIVE)
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == [f"jira:{10000 + n}" for n in range(1, 6)]
    searches = site.searches()
    assert [r.url.params.get("nextPageToken") for r in searches] == [None, "tok-2-ChZ0cm9ib", "tok-4-ChZ0cm9ib"]
    assert all(r.url.path == "/rest/api/3/search/jql" for r in searches)
    first = searches[0].url.params
    assert first["maxResults"] == "2" and "comment" in first["fields"] and "description" in first["fields"] and "security" in first["fields"]
    assert first["jql"] == "(project in (PROJ,HR)) ORDER BY updated ASC, key ASC"
    assert batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope == ""
    assert all(not b.done and b.complete_scope is None for b in batches[:-1])


def test_legacy_variant_pages_with_startat_and_total():
    site = JiraSite(FIVE)
    changes, batches = run(make(site, api_variant="legacy"))
    assert len(changes) == 5
    searches = site.searches()
    assert all(r.url.path == "/rest/api/3/search" for r in searches)
    assert [r.url.params["startAt"] for r in searches] == ["0", "2", "4"]      # stops because startAt reached total
    assert batches[-1].complete_scope == ""


def test_resume_continues_from_the_saved_token_and_never_declares_deletions():
    site = JiraSite(FIVE)
    mid = list(make(site).scan({}))[0]
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, batches = run(make(site), mid.cursor)
    assert [c.external_id for c in changes] == ["jira:10003", "jira:10004", "jira:10005"]
    assert site.searches()[0].url.params["nextPageToken"] == "tok-2-ChZ0cm9ib"
    assert batches[-1].complete_scope is None


def test_resume_in_legacy_mode_uses_startat():
    site = JiraSite(FIVE)
    mid = list(make(site, api_variant="legacy").scan({}))[0]
    site.reset()
    changes, _ = run(make(site, api_variant="legacy"), mid.cursor)
    assert site.searches()[0].url.params["startAt"] == "2" and len(changes) == 3


def test_a_cursor_saved_after_the_last_page_resumes_straight_to_the_end():
    site = JiraSite(FIVE)
    batches = list(make(site).scan({}))
    site.reset()
    changes, resumed = run(make(site), batches[-2].cursor)
    assert changes == [] and site.requests == [] and resumed[-1].done and resumed[-1].complete_scope is None


def test_is_last_wins_over_a_leftover_token_and_a_null_token_ends_the_scan():
    class Sloppy(JiraSite):
        def _search_token(self, q):
            r = super()._search_token(q)
            body = r.json()
            if body["isLast"]:
                body["nextPageToken"] = "stale-token" if q.get("nextPageToken") else None
            return httpx.Response(200, json=body)
    site = Sloppy(FIVE)
    changes, _ = run(make(site))
    assert len(changes) == 5 and len(site.searches()) == 3


def test_a_token_that_repeats_is_stopped():
    class Loop(JiraSite):
        def _search_token(self, q):
            return httpx.Response(200, json={"issues": [], "nextPageToken": "same", "isLast": False})
    with pytest.raises(HttpError, match="did not advance"):
        run(make(Loop(FIVE)))


# ── incremental ──


def test_user_jql_is_kept_its_order_by_replaced_and_updated_is_appended_when_incremental():
    site = JiraSite(FIVE)
    connector = make(site, projects=None, jql="project = PROJ AND status != Done ORDER BY created DESC")
    site.issues.clear()
    run(connector)
    assert site.searches()[0].url.params["jql"] == "(project = PROJ AND status != Done) ORDER BY updated ASC, key ASC"
    done = {"since": "2026-03-05T10:00:00Z", "last_full": NOW - 60}
    site.reset()
    _, batches = run(make(site, projects=None, jql="project = PROJ ORDER BY created DESC"), done)
    assert site.searches()[0].url.params["jql"] == '(project = PROJ) AND updated >= "2026-03-04 10:00" ORDER BY updated ASC, key ASC'
    assert batches[-1].complete_scope is None


def test_next_scan_after_a_finished_one_is_incremental_and_only_sees_what_changed():
    site = JiraSite(FIVE)
    _, batches = run(make(site))
    final = batches[-1].cursor
    assert final["since"] == "2026-03-05T10:00:00Z" and final["last_full"] == NOW
    json.dumps(final)
    site.issues["10002"]["fields"]["updated"] = "2026-03-09T12:00:00.000+0000"
    site.issues["10002"]["fields"]["summary"] = "Issue number 2 (reopened)"
    site.reset()
    changes, inc = run(make(site), final)
    ids = [c.external_id for c in changes]
    assert "jira:10002" in ids and "jira:10001" not in ids and "jira:10003" not in ids      # 03-01..03-03 are before the overlap floor
    assert changes[-1].file.name.startswith("PROJ-2-Issue number 2 (reopened)")
    assert inc[-1].cursor["since"] == "2026-03-09T12:00:00Z" and inc[-1].complete_scope is None


def test_full_resync_when_due_is_the_only_scan_that_can_delete():
    site = JiraSite(FIVE)
    stale = {"since": "2026-03-05T10:00:00Z", "last_full": NOW - 30 * 3600}
    _, batches = run(make(site), stale)
    assert batches[-1].complete_scope == "" and "updated >=" not in site.searches()[0].url.params["jql"]


def test_max_items_truncates_without_deleting_or_moving_the_watermark():
    site = JiraSite(FIVE)
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and batches[-1].complete_scope is None and "since" not in batches[-1].cursor
    site.reset()
    _, inc = run(make(site, max_pages=1), {"since": "2026-03-01T00:00:00Z", "last_full": NOW})
    assert len(site.searches()) == 1 and inc[-1].cursor["since"] == "2026-03-01T00:00:00Z"


# ── versions, moves ──


def test_version_follows_updated_and_content_and_is_stable_otherwise():
    def version(**kw):
        site = JiraSite([issue(10001, "PROJ-1", "Login fails", **kw)])
        return run(make(site))[0][0].file.version

    base = version()
    assert version() == base and base.startswith("2026-03-04T10:11:12.345+0000-")
    assert version(updated="2026-03-05T00:00:00.000+0000") != base
    assert version(description="Different text") != base
    assert version(comments=[comment(1, "Ada", "me too")], updated="2026-03-04T10:11:12.345+0000") != base   # a comment, same timestamp
    assert version(status="Done") != base


def test_moving_an_issue_keeps_the_external_id_but_changes_path_and_version():
    before = run(make(JiraSite([issue(10001, "PROJ-1", "Login fails")])))[0][0].file
    after = run(make(JiraSite([issue(10001, "HR-9", "Login fails", project="HR")])))[0][0].file
    assert before.external_id == after.external_id == "jira:10001"
    assert before.path == "jira/PROJ/PROJ-1-Login fails.md" and after.path == "jira/HR/HR-9-Login fails.md"
    assert before.version != after.version


# ── comments ──


def test_comments_are_included_and_restricted_or_internal_ones_are_not():
    comments = [
        comment(1, "Ada Lovelace", "Public comment visible to the reporter"),
        comment(2, "Grace Hopper", "Only for administrators", visibility={"type": "role", "value": "Administrators", "identifier": "10002"}),
        comment(3, "Linus", "Group restricted", visibility={"type": "group", "value": "jira-staff", "identifier": "g1"}),
        comment(4, "Agent", "JSM internal note", jsdPublic=False),
        comment(5, "Agent", "JSM customer reply", jsdPublic=True),
    ]
    site = JiraSite([issue(10001, "PROJ-1", "Login fails", comments=comments)])
    out = run(make(site))[0][0]
    with make(site).open(persisted(out.file)) as fh:
        body = fh.read().decode()
    assert "Public comment visible to the reporter" in body and "JSM customer reply" in body
    assert "Only for administrators" not in body and "Group restricted" not in body and "JSM internal note" not in body
    assert "### Ada Lovelace (2026-03-02T09:00:00.000+0000)" in body and "## Comments" in body
    everything = make(site, include_restricted_comments=True)
    with everything.open(persisted(out.file)) as fh:
        assert "Only for administrators" in fh.read().decode()
    plain = make(site, include_comments=False)
    with plain.open(persisted(out.file)) as fh:
        assert "Comments" not in fh.read().decode()


def test_comments_beyond_the_first_page_of_the_search_result_are_fetched_with_startat():
    first = [comment(1, "A", "one")]
    rest = [comment(n, "A", f"comment {n}") for n in range(2, 6)]
    item = issue(10001, "PROJ-1", "Chatty", comments=first, comment_total=5)
    item["_all_comments"] = first + rest
    site = JiraSite([item])
    changes, _ = run(make(site))
    asks = [r for r in site.requests if r.url.path.endswith("/comment")]
    assert [r.url.params["startAt"] for r in asks] == ["1"]
    with make(site).open(persisted(changes[0].file)) as fh:
        body = fh.read().decode()
    assert all(f"comment {n}" in body for n in range(2, 6)) and "one" in body


# ── security ──


def test_issues_with_a_security_level_are_skipped_and_reported_as_deleted():
    secret = issue(10002, "PROJ-2", "Salary dispute", security={"id": "10100", "name": "HR only", "description": "x"})
    site = JiraSite([issue(10001, "PROJ-1", "Public bug"), secret])
    changes, _ = run(make(site))
    assert [(c.kind, c.external_id) for c in changes] == [("upsert", "jira:10001"), ("delete", "jira:10002")]
    allowed, _ = run(make(site, include_secured_issues=True))
    assert [c.kind for c in allowed] == ["upsert", "upsert"]


def test_acl_rules_by_project_prefix_fail_closed():
    site = JiraSite([issue(10001, "PROJ-1", "A"), issue(10002, "HR-1", "B", project="HR"), issue(10003, "HR-2", "C", project="HR")])
    rules = [{"prefix": "jira/PROJ", "principals": ["group:eng"]}, {"prefix": "jira/HR/HR-2-*", "principals": ["group:hr-leads"]},
             {"prefix": "jira/HR", "principals": ["group:hr"]}]
    connector = make(site, acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["jira:10001"].acl == ["group:eng"] and by["jira:10002"].acl == ["group:hr"]
    assert by["jira:10003"].acl == ["group:hr-leads"]                                   # a glob rule on the file name wins by length
    assert connector.refresh_acl(persisted(by["jira:10002"])) == ["group:hr"]
    closed = make(site, acl_rules=[{"prefix": "jira/PROJ", "principals": ["group:eng"]}], default_acl=None)
    by = {c.external_id: c.file for c in run(closed)[0]}
    assert by["jira:10002"].acl == [] and by["jira:10001"].acl == ["group:eng"]


# ── open() ──


def test_open_returns_title_metadata_description_and_comments_from_persisted_columns_only():
    description = adf(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("Steps")]},
        para(text("Open "), text("the login page", {"type": "link", "attrs": {"href": "https://acme.io/login"}}), text(" and press "), text("Enter", "strong"), text(".")),
        {"type": "orderedList", "attrs": {"order": 1}, "content": [
            {"type": "listItem", "content": [para(text("enter user"))]}, {"type": "listItem", "content": [para(text("enter password"))]}]},
    )
    site = JiraSite([issue(10001, "PROJ-1", "Login fails", description=description, comments=[comment(1, "Grace Hopper", "Reproduced on Safari")])])
    scanned = run(make(site))[0][0].file
    assert scanned.path == "jira/PROJ/PROJ-1-Login fails.md" and scanned.name == "PROJ-1-Login fails.md" and scanned.mime == "text/markdown"
    assert scanned.modified_at == datetime(2026, 3, 4, 10, 11, 12, 345000, tzinfo=timezone.utc)
    with make(JiraSite(list(site.issues.values()))).open(persisted(scanned)) as fh:
        body = fh.read().decode()
    assert body.startswith("# PROJ-1: Login fails\n")
    for line in ("project: Platform (PROJ)", "type: Bug", "status: To Do", "priority: High", "assignee: Grace Hopper",
                 "reporter: Ada Lovelace", "labels: backend, urgent", "components: API", "url: https://acme.atlassian.net/browse/PROJ-1"):
        assert line in body
    assert "## Steps" in body and "[the login page](https://acme.io/login)" in body and "**Enter**" in body
    assert "1. enter user\n2. enter password" in body and "Reproduced on Safari" in body
    assert scanned.size == len(body.encode())
    assert [r.url.path for r in site.requests][-1] != ""                              # (open did not need scan state)


def test_open_of_a_missing_issue_or_a_bad_id_raises_without_a_wild_request():
    site = JiraSite([])
    connector = make(site)
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile("jira:10404", "p", "n", 0, None, "v", []))
    assert err.value.status == 404
    site.reset()
    for bad in ("jira:PROJ-1", "jira:1/../../x", "jira:"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []


# ── ADF ──


def test_adf_conversion_covers_the_node_types_that_appear_in_real_issues():
    doc = adf(
        {"type": "heading", "attrs": {"level": 3}, "content": [text("Title")]},
        para(text("plain "), text("bold", "strong"), text(" "), text("it", "em"), text(" "), text("code", "code"), text(" "), text("gone", "strike")),
        para(text("see "), {"type": "mention", "attrs": {"id": "u1", "text": "@Grace"}}, text(" "), {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}},
             {"type": "hardBreak"}, {"type": "inlineCard", "attrs": {"url": "https://acme.atlassian.net/browse/PROJ-9"}},
             text(" due "), {"type": "date", "attrs": {"timestamp": "1772668800000"}}, text(" "), {"type": "status", "attrs": {"text": "IN REVIEW", "color": "blue"}}),
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [para(text("a")), {"type": "bulletList", "content": [{"type": "listItem", "content": [para(text("a1"))]}]}]},
            {"type": "listItem", "content": [para(text("b"))]}]},
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("print('x')\nprint('y')")]},
        {"type": "blockquote", "content": [para(text("quoted"))]},
        {"type": "panel", "attrs": {"panelType": "warning"}, "content": [para(text("careful"))]},
        {"type": "rule"},
        {"type": "table", "attrs": {"isNumberColumnEnabled": False}, "content": [
            {"type": "tableRow", "content": [{"type": "tableHeader", "content": [para(text("H1"))]}, {"type": "tableHeader", "content": [para(text("H2"))]}]},
            {"type": "tableRow", "content": [{"type": "tableCell", "content": [para(text("c1"))]}, {"type": "tableCell", "content": [para(text("c|2"))]}]}]},
        {"type": "taskList", "attrs": {"localId": "t"}, "content": [
            {"type": "taskItem", "attrs": {"localId": "1", "state": "TODO"}, "content": [text("open task")]},
            {"type": "taskItem", "attrs": {"localId": "2", "state": "DONE"}, "content": [text("done task")]}]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m1", "type": "file", "alt": "screenshot.png"}}]},
        {"type": "somethingNew", "content": [para(text("unknown nodes keep their text"))]},
    )
    out = adf_to_text(doc)
    assert out.startswith("### Title\n\nplain **bold** *it* `code` ~~gone~~")
    assert "see @Grace 😄\nhttps://acme.atlassian.net/browse/PROJ-9 due 2026-03-05 [IN REVIEW]" in out
    assert "- a\n  - a1\n- b" in out
    assert "```python\nprint('x')\nprint('y')\n```" in out
    assert "> quoted" in out and "**Warning:** careful" in out and "\n---\n" in out
    assert "| H1 | H2 |\n| --- | --- |\n| c1 | c\\|2 |" in out
    assert "- [ ] open task\n- [x] done task" in out and "[attachment: screenshot.png]" in out
    assert "unknown nodes keep their text" in out
    assert adf_to_text(None) == "" and adf_to_text({}) == "" and adf_to_text("  wiki *markup*  ") == "wiki *markup*"


# ── http behaviour ──


def test_429_is_retried_with_retry_after():
    site = JiraSite(FIVE[:1])
    site.script = [httpx.Response(429, headers={"Retry-After": "11"})]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 1 and connector.sleeps == [11.0]


def test_401_is_a_clear_error_without_the_token(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "bad-token-value")
    site = JiraSite(FIVE)
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "401" in str(err.value) and "JIRA_TOKEN" in str(err.value)
    assert "bad-token-value" not in str(err.value) and TOKEN not in str(err.value)
    assert len(site.requests) == 1 and connector.sleeps == []


def test_cursors_hold_no_secrets_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(JiraSite(FIVE)))
    blob = json.dumps([b.cursor for b in batches])
    assert TOKEN not in blob and "ada@acme.com" not in blob
    monkeypatch.delenv("JIRA_TOKEN")
    with pytest.raises(HttpError, match="JIRA_TOKEN"):
        run(make(JiraSite(FIVE)))


# ── Data Center ──


def test_data_center_uses_api_2_legacy_search_a_bearer_pat_and_wiki_markup_strings():
    item = issue(10001, "PROJ-1", "DC issue", description="h2. Heading\n* item", comments=[comment(1, "Ada", "wiki *comment*")])
    item["fields"]["description"] = "h2. Heading\n* item"
    item["fields"]["comment"]["comments"][0]["body"] = "wiki *comment*"
    site = JiraSite([item], api="/rest/api/2", auth="Bearer")
    connector = JiraConnector(
        {"base_url": "https://jira.corp", "deployment": "datacenter", "token_env": "JIRA_TOKEN", "projects": ["PROJ"],
         "default_acl": ["*"]}, transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    changes, batches = run(connector)
    assert [r.url.path for r in site.searches()] == ["/rest/api/2/search"]
    with connector.open(persisted(changes[0].file)) as fh:
        body = fh.read().decode()
    assert "h2. Heading\n* item" in body and "wiki *comment*" in body and "url: https://jira.corp/browse/PROJ-1" in body
    assert batches[-1].complete_scope == ""


def test_configuration_errors_are_rejected_up_front():
    with pytest.raises(ValueError, match="base_url"):
        JiraConnector({"projects": ["A"]})
    with pytest.raises(ValueError, match="unbounded"):
        JiraConnector({"base_url": SITE})
    with pytest.raises(ValueError, match="project key"):
        JiraConnector({"base_url": SITE, "projects": ["A) OR (x"]})
    with pytest.raises(ValueError, match="api_variant"):
        JiraConnector({"base_url": SITE, "projects": ["A"], "api_variant": "graphql"})
