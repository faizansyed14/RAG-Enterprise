"""Confluence connector against a fake site whose JSON is shaped like the Confluence REST API v1 documentation
(Cloud: /wiki/rest/api/content, /content/search, /content/{id}; Data Center: /rest/api/content).

The fake enforces the things a real server enforces: credentials, `_links.next` paging (a page after the first
is only served when the connector sends the opaque `cursor` the previous page handed out), CQL filtering.
Field names copied from the docs; the `restrictions` expansion is the one shape written from memory.
"""

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.confluence import ConfluenceConnector

SITE = "https://acme.atlassian.net"
TOKEN = "SECRET-TOKEN-9f3a"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
SPACES = {"ENG": "Engineering", "HR": "People Ops"}


def page(i, title, space="ENG", body="<p>hello</p>", when="2026-03-01T10:00:00.000Z", number=1, ancestors=(),
         status="current", type="page", restrictions=None):
    item = {
        "id": str(i), "type": type, "status": status, "title": title,
        "space": {"id": 100, "key": space, "name": SPACES[space], "type": "global", "status": "current"},
        "version": {"by": {"type": "known", "displayName": "Ada Lovelace"}, "when": when,
                    "friendlyWhen": "a while ago", "message": "", "number": number, "minorEdit": False},
        "ancestors": [{"id": str(900 + n), "type": "page", "status": "current", "title": a} for n, a in enumerate(ancestors)],
        "body": {"storage": {"value": body, "representation": "storage", "embeddedContent": []}},
        "_links": {"webui": f"/spaces/{space}/pages/{i}/{title.replace(' ', '+')}", "tinyui": "/x/abc"},
    }
    if restrictions is not None:
        item["restrictions"] = restrictions
    return item


class Site:
    """A Confluence Cloud site (or Data Center when `prefix`/`context` are changed)."""

    def __init__(self, pages, *, prefix="/wiki/rest/api", context="/wiki", auth="Basic"):
        self.pages = {p["id"]: copy.deepcopy(p) for p in pages}
        self.prefix, self.context, self.auth = prefix, context, auth
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def query(self, n=-1):
        return dict(self.requests[n].url.params)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        header = request.headers.get("authorization", "")
        if self.auth == "Basic":
            import base64
            ok = header == "Basic " + base64.b64encode(f"ada@acme.com:{TOKEN}".encode()).decode()
        else:
            ok = header == f"Bearer {TOKEN}"
        if not ok:
            return httpx.Response(401, json={"statusCode": 401, "message": "Unauthorized"})
        path, q = request.url.path, dict(request.url.params)
        if path == f"{self.prefix}/content/search":
            return self._listing(q, self._search(q), "/content/search")
        if path == f"{self.prefix}/content":
            items = [p for p in self.pages.values() if p["type"] == q.get("type", "page")
                     and p["status"] == q.get("status", "current")
                     and (not q.get("spaceKey") or p["space"]["key"] == q["spaceKey"])]
            return self._listing(q, items, "/content")
        m = re.fullmatch(re.escape(self.prefix) + r"/content/(\d+)", path)
        if m:
            found = self.pages.get(m.group(1))
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "No content found"})
        return httpx.Response(404, json={"message": "no such endpoint " + path})

    def _search(self, q):
        cql = q["cql"]
        assert cql.endswith("order by lastmodified asc"), cql
        since = re.search(r'lastmodified >= "(\d{4}-\d\d-\d\d \d\d:\d\d)"', cql)
        spaces = re.search(r"space in \(([^)]*)\)", cql)
        types = re.search(r"type in \(([^)]*)\)", cql).group(1).replace('"', "").split(",")
        keys = spaces.group(1).replace('"', "").split(",") if spaces else None
        floor = datetime.strptime(since.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        out = [p for p in self.pages.values()
               if p["status"] == "current" and p["type"] in types and (keys is None or p["space"]["key"] in keys)
               and datetime.fromisoformat(p["version"]["when"].replace("Z", "+00:00")) >= floor]
        return sorted(out, key=lambda p: p["version"]["when"])

    def _listing(self, q, items, endpoint):
        limit = int(q.get("limit", 25))
        offset = int(q.get("start", 0))
        if "cursor" in q:
            offset = int(q["cursor"].split("-")[1])
        elif offset:
            return httpx.Response(400, json={"message": "start is not supported for paging here, follow _links.next"})
        results = items[offset:offset + limit]
        body = {"results": results, "start": offset, "limit": limit, "size": len(results),
                "_links": {"base": SITE + self.context, "context": self.context, "self": SITE + "/x"}}
        if offset + limit < len(items):
            nq = {k: v for k, v in q.items() if k not in ("start", "cursor")}
            nq.update({"start": offset + limit, "cursor": f"c-{offset + limit}"})
            body["_links"]["next"] = f"/rest/api{endpoint}?{urlencode(nq)}"
        return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CONF_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"base_url": SITE, "email": "ada@acme.com", "token_env": "CONF_TOKEN", "page_size": 2,
              "default_acl": ["group:all"], **cfg}
    connector = ConfluenceConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
    connector.sleeps = sleeps
    return connector


def run(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


def persisted(sf: SourceFile) -> SourceFile:
    """What the pipeline hands back to open() / refresh_acl(): only the columns stored in source_items."""
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


FIVE = [page(i, f"Page {i}", when=f"2026-03-0{i}T10:00:00.000Z") for i in range(1, 6)]


# ── pagination, scope, resume ──


def test_full_scan_follows_links_next_across_pages_and_is_authoritative():
    site = Site(FIVE)
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == [f"confluence:{i}" for i in range(1, 6)]
    assert len(site.requests) == 3                                   # 2 + 2 + 1, then no `next`
    assert [r.url.params.get("cursor") for r in site.requests] == [None, "c-2", "c-4"]   # the server's own link was followed
    assert all(not b.done for b in batches[:-1]) and batches[-1].done and batches[-1].changes == []
    assert batches[-1].complete_scope == ""                          # complete enumeration => the sync layer may delete unseen
    assert all(b.complete_scope is None for b in batches[:-1])
    first = site.requests[0].url.params
    assert first["type"] == "page" and "body.storage" in first["expand"] and "ancestors" in first["expand"]


def test_resume_continues_at_the_saved_page_and_is_not_authoritative():
    site = Site(FIVE)
    batches = list(make(site).scan({}))
    mid = batches[0]                                                 # crash after the first page was recorded
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor           # JSON-serialisable
    site.reset()
    changes, resumed = run(make(site), mid.cursor)
    assert [c.external_id for c in changes] == ["confluence:3", "confluence:4", "confluence:5"]
    assert site.requests[0].url.params["cursor"] == "c-2"            # did not start over
    assert resumed[-1].done and resumed[-1].complete_scope is None   # it only saw the tail, so it deletes nothing


def test_one_space_at_a_time_when_space_keys_are_configured():
    pages = [page(1, "A", "ENG"), page(2, "B", "HR"), page(3, "C", "ENG")]
    site = Site(pages)
    changes, _ = run(make(site, space_keys=["HR", "ENG"]))
    assert [r.url.params["spaceKey"] for r in site.requests] == ["HR", "ENG"]
    assert [c.external_id for c in changes] == ["confluence:2", "confluence:1", "confluence:3"]


def test_blogposts_only_when_asked():
    pages = [page(1, "A"), page(2, "Post", type="blogpost")]
    assert [c.external_id for c in run(make(Site(pages)))[0]] == ["confluence:1"]
    site = Site(pages)
    changes, _ = run(make(site, content_types=["page", "blogpost"]))
    assert sorted(c.external_id for c in changes) == ["confluence:1", "confluence:2"]
    assert sorted(r.url.params["type"] for r in site.requests) == ["blogpost", "page"]


# ── incremental ──


def test_finished_scan_leaves_a_watermark_and_the_next_scan_uses_cql_with_overlap():
    site = Site(FIVE)
    connector = make(site)
    _, batches = run(connector)
    final = batches[-1].cursor
    assert final["since"] == "2026-03-05T10:00:00Z" and final["last_full"] == NOW   # newest page seen, not "now"
    json.dumps(final)

    site.pages["3"] = page(3, "Page 3", body="<p>edited body</p>", when="2026-03-09T08:30:00.000Z", number=2)
    site.pages["6"] = page(6, "Page 6", when="2026-03-09T09:00:00.000Z")
    site.pages["4"]["status"] = "trashed"                            # moved to the trash in the meantime
    site.reset()
    changes, inc = run(make(site), final)
    cql = [r.url.params["cql"] for r in site.requests if r.url.path.endswith("/content/search")][0]
    assert 'lastmodified >= "2026-03-04 10:00"' in cql                # watermark minus the 24h overlap (time-zone safety)
    assert cql.startswith('type in ("page")') and cql.endswith("order by lastmodified asc")
    kinds = {(c.kind, c.external_id) for c in changes}
    assert ("upsert", "confluence:3") in kinds and ("upsert", "confluence:6") in kinds
    assert ("delete", "confluence:4") in kinds                        # trash => delete, even though its version.when is old
    assert ("upsert", "confluence:1") not in kinds                    # untouched pages are not re-listed
    assert inc[-1].done and inc[-1].complete_scope is None           # incremental => never authoritative
    assert inc[-1].cursor["since"] == "2026-03-09T09:00:00Z" and inc[-1].cursor["last_full"] == NOW


def test_watermark_never_moves_backwards_when_only_overlap_pages_come_back():
    site = Site(FIVE)
    _, batches = run(make(site))
    changes, inc = run(make(site), batches[-1].cursor)               # the overlap window re-returns old pages
    assert changes                                                    # (they are re-sent; versions are identical)
    assert inc[-1].cursor["since"] == "2026-03-05T10:00:00Z"


def test_full_resync_happens_when_the_last_full_scan_is_old_and_only_it_can_delete():
    site = Site(FIVE)
    stale = {"since": "2026-03-05T10:00:00Z", "last_full": NOW - 25 * 3600}
    _, batches = run(make(site), stale)
    assert batches[-1].complete_scope == ""
    assert not any("cql" in r.url.params for r in site.requests)
    fresh = {"since": "2026-03-05T10:00:00Z", "last_full": NOW - 3600}
    _, batches = run(make(site), fresh)
    assert batches[-1].complete_scope is None


def test_unfinished_full_scan_that_is_resumed_does_not_become_an_incremental_watermark_without_a_full_run():
    site = Site(FIVE)
    mid = list(make(site).scan({}))[0].cursor
    _, resumed = run(make(site), mid)
    # last_full stays unset: the next scan is therefore a full one again (an authoritative pass has not happened yet)
    assert resumed[-1].cursor["last_full"] is None
    _, nxt = run(make(site), resumed[-1].cursor)
    assert nxt[-1].complete_scope == ""


def test_trashed_detection_can_be_switched_off():
    site = Site(FIVE)
    run(make(site, detect_trashed=False), {"since": "2026-03-05T10:00:00Z", "last_full": NOW})
    assert not any(r.url.params.get("status") == "trashed" for r in site.requests)


# ── bounded work ──


def test_max_items_truncates_and_a_truncated_scan_never_deletes_or_advances():
    site = Site(FIVE)
    changes, batches = run(make(site, max_items=3))
    assert [c.external_id for c in changes] == ["confluence:1", "confluence:2", "confluence:3"]
    assert batches[-1].done and batches[-1].complete_scope is None
    assert "since" not in batches[-1].cursor                          # next scan starts over, nothing was skipped silently


def test_max_pages_limits_requests():
    site = Site(FIVE)
    changes, batches = run(make(site, max_pages=2))
    assert len(site.requests) == 2 and len(changes) == 4 and batches[-1].complete_scope is None


def test_truncated_incremental_scan_keeps_the_old_watermark():
    site = Site(FIVE)
    start = {"since": "2026-03-01T00:00:00Z", "last_full": NOW}
    _, batches = run(make(site, max_items=1, detect_trashed=False), start)
    assert batches[-1].cursor["since"] == "2026-03-01T00:00:00Z"


def test_a_server_that_repeats_the_same_next_page_is_stopped():
    class Loop(Site):
        def _listing(self, q, items, endpoint):
            r = super()._listing(q, items, endpoint)
            body = r.json()
            body["_links"]["next"] = f"/rest/api{endpoint}?{urlencode({**q, 'cursor': 'c-0'})}"
            return httpx.Response(200, json=body)
    with pytest.raises(HttpError, match="did not advance"):
        run(make(Loop(FIVE)))


# ── versions ──


def test_version_is_stable_when_nothing_changed_and_changes_with_content_title_or_position():
    def version_of(**kw):
        site = Site([page(1, **{"title": "Doc", **kw})])
        return run(make(site))[0][0].file.version

    base = version_of()
    assert version_of() == base
    assert version_of(body="<p>hello again</p>") != base              # body edit
    assert version_of(title="Doc renamed") != base                    # rename
    assert version_of(ancestors=("Home",)) != base                    # move under another parent (version.number unchanged)
    assert version_of(number=2).startswith("2-") and base.startswith("1-")


# ── permissions ──


def test_acl_comes_from_prefix_rules_longest_wins_and_defaults_fail_closed():
    pages = [page(1, "Salaries", "HR"), page(2, "Runbook", "ENG"), page(3, "Public handbook", "HR", ancestors=("Public",))]
    rules = [{"prefix": "confluence/HR", "principals": ["group:hr"]},
             {"prefix": "confluence/ENG", "principals": ["group:eng", "user:Ada@acme.com"]},
             {"prefix": "confluence/HR/Public", "principals": ["*"]}]
    connector = make(Site(pages), acl_rules=rules, default_acl=[])
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["confluence:1"].acl == ["group:hr"]
    assert by["confluence:2"].acl == ["group:eng", "user:ada@acme.com"]
    assert by["confluence:3"].acl == ["*"]
    assert connector.refresh_acl(persisted(by["confluence:1"])) == ["group:hr"]

    closed = make(Site(pages), acl_rules=[{"prefix": "confluence/ENG", "principals": ["group:eng"]}], default_acl=None)
    by = {c.external_id: c.file for c in run(closed)[0]}
    assert by["confluence:1"].acl == [] and by["confluence:2"].acl == ["group:eng"]   # no rule, no default => nobody
    nothing = make(Site(pages), default_acl=None)
    assert all(c.file.acl == [] for c in run(nothing)[0])


def test_pages_with_direct_read_restrictions_are_left_out_and_deleted():
    # shape of the `restrictions` expansion written from memory of the docs (read.restrictions.user|group.results)
    restricted = {"read": {"operation": "read", "restrictions": {
        "user": {"results": [{"type": "known", "accountId": "abc"}], "start": 0, "limit": 200, "size": 1},
        "group": {"results": [], "start": 0, "limit": 200, "size": 0}}}}
    open_to_all = {"read": {"operation": "read", "restrictions": {"user": {"results": []}, "group": {"results": []}}}}
    site = Site([page(1, "Board minutes", restrictions=restricted), page(2, "Handbook", restrictions=open_to_all)])
    changes, _ = run(make(site))
    assert [(c.kind, c.external_id) for c in changes] == [("delete", "confluence:1"), ("upsert", "confluence:2")]
    assert "restrictions.read.restrictions.user" in site.requests[0].url.params["expand"]
    allowed = make(Site([page(1, "Board minutes", restrictions=restricted)]), skip_restricted_pages=False)
    assert run(allowed)[0][0].kind == "upsert"


# ── open() ──


def test_open_renders_title_metadata_and_converted_body_from_persisted_columns_only():
    body = ("<h2>Deploy</h2><p>Run <strong>make deploy</strong> then check <a href=\"https://status.acme.io\">status</a>.</p>"
            "<ul><li>first</li><li>second</li></ul>")
    site = Site([page(7, "Deploy guide", "ENG", body=body, ancestors=("Home", "Runbooks"))])
    scanned = run(make(site))[0][0].file
    assert scanned.path == "confluence/ENG/Home/Runbooks/Deploy guide.md" and scanned.name == "Deploy guide.md"
    assert scanned.mime == "text/markdown" and scanned.modified_at == datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
    fresh = make(Site(list(site.pages.values())))                     # a different process: no scan state
    with fresh.open(persisted(scanned)) as fh:
        text = fh.read().decode("utf-8")
    assert text.startswith("# Deploy guide\n")
    assert "space: Engineering (ENG)" in text and "breadcrumb: Home > Runbooks" in text
    assert "updated by: Ada Lovelace" in text and "url: https://acme.atlassian.net/wiki/spaces/ENG/pages/7/Deploy+guide" in text
    assert "## Deploy" in text and "**make deploy**" in text and "[status](https://status.acme.io)" in text
    assert "- first\n- second" in text and "<p>" not in text and "<strong>" not in text
    assert scanned.size == len(text.encode("utf-8"))                  # size is the size of what open() returns


def test_open_of_a_deleted_page_raises_a_status_carrying_error():
    connector = make(Site([]))
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile("confluence:404", "confluence/ENG/x.md", "x.md", 0, None, "1-x", []))
    assert err.value.status == 404


def test_open_refuses_an_external_id_that_is_not_a_content_id_without_calling_the_server():
    site = Site([page(1, "A")])
    connector = make(site)
    for bad in ("confluence:1/../../../user/current", "confluence:1?expand=x", "confluence:", "confluence:abc"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []


# ── storage format -> text ──


def test_storage_format_macros_tables_and_entities_become_readable_text():
    body = (
        "<h1>Overview</h1><p>Caf&eacute; &amp; bar &lt;ok&gt;</p>"
        '<ac:structured-macro ac:name="code" ac:schema-version="1"><ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[if a < b:\n    print(\"x\")]]></ac:plain-text-body></ac:structured-macro>"
        '<ac:structured-macro ac:name="warning"><ac:rich-text-body><p>Do not run in prod</p></ac:rich-text-body></ac:structured-macro>'
        "<table><tbody><tr><th>Env</th><th>URL</th></tr><tr><td>prod</td><td>https://p.acme.io</td></tr></tbody></table>"
        '<p>See <ac:link><ri:page ri:content-title="Incident process"/></ac:link>.</p>'
        "<ac:task-list><ac:task><ac:task-id>1</ac:task-id><ac:task-status>incomplete</ac:task-status><ac:task-body>write tests</ac:task-body></ac:task>"
        "<ac:task><ac:task-id>2</ac:task-id><ac:task-status>complete</ac:task-status><ac:task-body>ship it</ac:task-body></ac:task></ac:task-list>"
        '<script>alert("x")</script><style>.a{}</style>'
    )
    site = Site([page(1, "Mixed", body=body)])
    with make(site).open(SourceFile("confluence:1", "p", "n", 0, None, "v", [])) as fh:
        text = fh.read().decode()
    assert "# Overview" in text and "Café & bar <ok>" in text
    assert '```\nif a < b:\n    print("x")\n```' in text
    assert "**Warning:**" in text and "Do not run in prod" in text
    assert "| Env | URL |\n| --- | --- |\n| prod | https://p.acme.io |" in text
    assert "See Incident process." in text
    assert "- [ ] write tests" in text and "- [x] ship it" in text
    assert "alert" not in text and ".a{}" not in text and "ac:" not in text and "CDATA" not in text


# ── http behaviour ──


def test_429_is_retried_honouring_retry_after():
    site = Site(FIVE[:1])
    site.script = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503)]
    connector = make(site)
    changes, _ = run(connector)
    assert [c.external_id for c in changes] == ["confluence:1"]
    assert connector.sleeps[0] == 7.0 and len(connector.sleeps) == 2  # the 503 was retried with backoff too


def test_401_is_a_clear_auth_error_that_never_contains_the_token_and_is_not_retried(monkeypatch):
    monkeypatch.setenv("CONF_TOKEN", "WRONG-TOKEN-1234")
    site = Site(FIVE)
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert "401" in message and "CONF_TOKEN" in message               # tells the operator which variable to look at
    assert "WRONG-TOKEN-1234" not in message and TOKEN not in message
    assert len(site.requests) == 1 and connector.sleeps == []


def test_a_server_that_echoes_the_credential_in_an_error_page_is_scrubbed():
    site = Site(FIVE)
    site.script = [httpx.Response(403, text=f"forbidden for token {TOKEN}")]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert TOKEN not in str(err.value) and "***" in str(err.value)


def test_missing_secret_variable_is_reported_by_name(monkeypatch):
    monkeypatch.delenv("CONF_TOKEN")
    with pytest.raises(HttpError, match="CONF_TOKEN"):
        run(make(Site(FIVE)))


def test_cursors_never_contain_the_secret_or_the_email():
    site = Site(FIVE)
    _, batches = run(make(site))
    blob = json.dumps([b.cursor for b in batches])
    assert TOKEN not in blob and "ada@acme.com" not in blob


def test_credentials_are_only_ever_sent_to_the_configured_host_even_if_next_points_elsewhere():
    class Evil(Site):
        def _listing(self, q, items, endpoint):
            r = super()._listing(q, items, endpoint)
            body = r.json()
            if "next" in body["_links"]:
                body["_links"]["next"] = "https://evil.example/wiki" + body["_links"]["next"]
            return httpx.Response(200, json=body)
    site = Evil(FIVE)
    run(make(site))
    assert {r.url.host for r in site.requests} == {"acme.atlassian.net"}


# ── Data Center ──


def test_data_center_uses_rest_api_without_wiki_prefix_and_a_bearer_pat():
    site = Site(FIVE, prefix="/confluence/rest/api", context="/confluence", auth="Bearer")
    connector = ConfluenceConnector(
        {"base_url": "https://wiki.corp/confluence", "deployment": "datacenter", "token_env": "CONF_TOKEN", "page_size": 2,
         "default_acl": ["*"]},
        transport=httpx.MockTransport(lambda r: site.handler(r)), sleep=lambda s: None, clock=lambda: NOW)
    changes, batches = run(connector)
    assert len(changes) == 5 and batches[-1].complete_scope == ""
    assert {r.url.host for r in site.requests} == {"wiki.corp"}
    assert all(r.url.path.startswith("/confluence/rest/api/content") for r in site.requests)   # no doubled context path
    assert changes[0].file.path.startswith("confluence/ENG/")


def test_unusable_configuration_is_rejected_up_front():
    with pytest.raises(ValueError):
        ConfluenceConnector({"token_env": "X"})
    with pytest.raises(ValueError):
        ConfluenceConnector({"base_url": SITE, "space_keys": ['ENG" OR space = "HR']})
    with pytest.raises(ValueError):
        ConfluenceConnector({"base_url": SITE, "content_types": ["attachment"]})
