"""Notion connector against a fake API shaped like the Notion API reference (version 2022-06-28):
POST /v1/search, POST /v1/databases/{id}/query, GET /v1/blocks/{id}/children, GET /v1/pages/{id}.
List responses: {"object": "list", "results": [...], "next_cursor": ..., "has_more": ...}.

Written from memory of the reference, not certain of: the `parent` object for pages inside synced/toggle blocks and
`type`/`page_or_database` on search responses (the connector does not read them).
"""

import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.notion import NotionConnector, property_text, rich_text

TOKEN = "secret_notion_TOKEN_55bb"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def uid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def rt(text, *, bold=False, italic=False, code=False, strike=False, href=None, kind="text"):
    item = {"type": kind, "annotations": {"bold": bold, "italic": italic, "strikethrough": strike, "underline": False,
                                          "code": code, "color": "default"}, "plain_text": text, "href": href}
    if kind == "text":
        item["text"] = {"content": text, "link": {"url": href} if href else None}
    return item


def npage(n, title, *, parent=None, edited="2026-03-01T10:00:00.000Z", props=None, archived=False, title_prop="title"):
    return {
        "object": "page", "id": uid(n), "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": edited,
        "created_by": {"object": "user", "id": "u1"}, "last_edited_by": {"object": "user", "id": "u1"},
        "cover": None, "icon": None, "parent": parent or {"type": "workspace", "workspace": True}, "archived": archived,
        "properties": {title_prop: {"id": "title", "type": "title", "title": [rt(title)]}, **(props or {})},
        "url": f"https://www.notion.so/{title.replace(' ', '-')}-{uid(n).replace('-', '')}",
    }


def ndb(n, title, *, parent=None, edited="2026-02-01T00:00:00.000Z"):
    return {"object": "database", "id": uid(n), "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": edited,
            "title": [rt(title)], "parent": parent or {"type": "workspace", "workspace": True}, "archived": False,
            "properties": {"Name": {"id": "title", "type": "title", "title": {}}}}


def under(n):
    return {"type": "page_id", "page_id": uid(n)}


def in_db(n):
    return {"type": "database_id", "database_id": uid(n)}


def blk(kind, text="", *, bid, has_children=False, **extra):
    data = {"rich_text": [rt(text)] if text else [], "color": "default", **extra}
    return {"object": "block", "id": bid, "parent": {"type": "page_id", "page_id": "x"}, "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-01-01T00:00:00.000Z", "has_children": has_children, "archived": False, "type": kind, kind: data}


class Notion:
    def __init__(self, pages=(), databases=(), blocks=None, version="2022-06-28"):
        self.pages = {p["id"]: copy.deepcopy(p) for p in pages}
        self.dbs = {d["id"]: copy.deepcopy(d) for d in databases}
        self.blocks = copy.deepcopy(blocks or {})
        self.version = version
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.method == "POST" and r.url.path == path]

    def paths(self):
        return [r.url.path for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"object": "error", "status": 401, "code": "unauthorized", "message": "API token is invalid."})
        assert request.headers.get("notion-version") == self.version
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/search":
            kind = body["filter"]["value"]
            items = list((self.pages if kind == "page" else self.dbs).values())
            items = [i for i in items if not i.get("archived")]
            if "sort" in body:
                assert body["sort"] == {"direction": "descending", "timestamp": "last_edited_time"}
                items.sort(key=lambda i: i["last_edited_time"], reverse=True)
            return self._list(items, body)
        if path.startswith("/v1/databases/") and path.endswith("/query"):
            db_id = path.split("/")[3]
            rows = [p for p in self.pages.values() if p["parent"].get("database_id") == db_id]
            if "filter" in body:
                floor = body["filter"]["last_edited_time"]["on_or_after"]
                rows = [r for r in rows if r["last_edited_time"] >= floor]
            return self._list(rows, body)
        if path.startswith("/v1/blocks/") and path.endswith("/children"):
            block_id = path.split("/")[3]
            q = dict(request.url.params)
            return self._list(self.blocks.get(block_id, []), {"start_cursor": q.get("start_cursor"), "page_size": int(q.get("page_size", 100))})
        if path.startswith("/v1/pages/"):
            found = self.pages.get(path.split("/")[3])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found", "message": "Could not find page"})
        if path.startswith("/v1/databases/"):
            found = self.dbs.get(path.split("/")[3])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found"})
        return httpx.Response(404)

    @staticmethod
    def _list(items, body):
        size = min(int(body.get("page_size", 100)), 100)
        offset = int(body["start_cursor"].split("-")[1]) if body.get("start_cursor") else 0
        part = items[offset:offset + size]
        more = offset + size < len(items)
        return httpx.Response(200, json={"object": "list", "results": part, "next_cursor": f"c-{offset + size}-aGVsbG8" if more else None,
                                         "has_more": more, "type": "page_or_database", "page_or_database": {}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "NOTION_TOKEN", "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = NotionConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


FIVE = [npage(n, f"Page {n}", edited=f"2026-03-0{n}T10:00:00.000Z") for n in range(1, 6)]


# ── pagination ──


def test_search_pages_newest_first_with_start_cursor_until_has_more_is_false():
    site = Notion(FIVE)
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == [f"notion:{uid(n)}" for n in (5, 4, 3, 2, 1)]       # newest first
    searches = site.bodies("/v1/search")
    page_searches = [b for b in searches if b["filter"]["value"] == "page"]
    assert [b.get("start_cursor") for b in page_searches] == [None, "c-2-aGVsbG8", "c-4-aGVsbG8"]
    assert all(b["page_size"] == 2 for b in searches)
    assert batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope == ""
    assert all(b.complete_scope is None and not b.done for b in batches[:-1])
    assert changes[0].file.version == "2026-03-05T10:00:00.000Z" and changes[0].file.size == 0


def test_database_rows_are_queried_with_pagination_and_not_duplicated_by_the_page_search():
    rows = [npage(10 + n, f"Task {n}", parent=in_db(100), title_prop="Name", edited=f"2026-03-0{n}T09:00:00.000Z",
                  props={"Status": {"id": "s", "type": "select", "select": {"id": "1", "name": "Done", "color": "green"}}}) for n in range(1, 6)]
    site = Notion(rows + [npage(1, "Plain page")], [ndb(100, "Tasks")])
    changes, batches = run(make(site))
    ids = [c.external_id for c in changes]
    assert len(ids) == len(set(ids)) == 6                                           # rows also come back from search; emitted once
    queries = site.bodies(f"/v1/databases/{uid(100)}/query")
    assert [q.get("start_cursor") for q in queries] == [None, "c-2-aGVsbG8", "c-4-aGVsbG8"]
    assert all("filter" not in q for q in queries)                                    # full scan: no time filter
    assert batches[-1].complete_scope == ""
    by = {c.external_id: c.file for c in changes}
    assert by[f"notion:{uid(11)}"].path.startswith("notion/Tasks/Task 1-")


def test_databases_can_be_left_out():
    site = Notion([npage(1, "A")], [ndb(100, "Tasks")])
    run(make(site, include_databases=False))
    assert not any(b["filter"]["value"] == "database" for b in site.bodies("/v1/search"))
    assert not any("/query" in p for p in site.paths())


def test_resume_in_the_page_phase_and_in_the_middle_of_a_database():
    site = Notion(FIVE)
    batches = list(make(site).scan({}))
    mid = batches[0]
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, resumed = run(make(site), mid.cursor)
    assert [c.external_id for c in changes] == [f"notion:{uid(n)}" for n in (3, 2, 1)]
    assert site.bodies("/v1/search")[0]["start_cursor"] == "c-2-aGVsbG8" and resumed[-1].complete_scope is None

    rows = [npage(10 + n, f"Task {n}", parent=in_db(100), title_prop="Name") for n in range(1, 6)]
    site = Notion(rows, [ndb(100, "Tasks")])
    first = list(make(site).scan({}))[0]
    assert first.cursor["pos"]["phase"] == "db" and first.cursor["pos"]["cursor"] == "c-2-aGVsbG8"
    site.reset()
    changes, _ = run(make(site), first.cursor)
    assert site.bodies(f"/v1/databases/{uid(100)}/query")[0]["start_cursor"] == "c-2-aGVsbG8"
    assert not any(b["filter"]["value"] == "database" for b in site.bodies("/v1/search"))           # database list came from the cursor
    assert len(changes) >= 3


def test_a_cursor_saved_after_the_last_page_goes_straight_to_the_final_batch():
    site = Notion(FIVE)
    batches = list(make(site).scan({}))
    site.reset()
    changes, resumed = run(make(site), batches[-2].cursor)
    assert changes == [] and site.requests == [] and resumed[-1].done


# ── incremental ──


def test_incremental_scan_stops_at_the_first_page_older_than_the_watermark_and_filters_databases():
    rows = [npage(10 + n, f"Task {n}", parent=in_db(100), title_prop="Name", edited=f"2026-03-0{n}T09:00:00.000Z") for n in range(1, 4)]
    pages = [npage(n, f"Page {n}", edited=f"2026-03-{n:02d}T10:00:00.000Z") for n in (1, 2, 3, 4, 5, 6, 7, 8)]
    site = Notion(pages + rows, [ndb(100, "Tasks")])
    done = {"since": "2026-03-06T00:00:00Z", "last_full": NOW - 60}
    changes, batches = run(make(site), done)
    page_ids = {c.external_id for c in changes}
    assert {f"notion:{uid(n)}" for n in (6, 7, 8)} <= page_ids and f"notion:{uid(5)}" not in page_ids
    page_searches = [b for b in site.bodies("/v1/search") if b["filter"]["value"] == "page"]
    assert len(page_searches) == 2                                                    # the 2nd result page already held older pages: no 3rd call
    flt = site.bodies(f"/v1/databases/{uid(100)}/query")[0]["filter"]
    assert flt == {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": "2026-03-05T23:50:00.000Z"}}   # 10 min overlap
    assert batches[-1].complete_scope is None and batches[-1].cursor["since"] == "2026-03-08T10:00:00Z"
    assert batches[-1].cursor["last_full"] == NOW - 60


def test_full_resync_when_due_and_truncation_never_deletes():
    site = Notion(FIVE)
    _, batches = run(make(site), {"since": "2026-03-05T00:00:00Z", "last_full": NOW - 48 * 3600})
    assert batches[-1].complete_scope == ""
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and batches[-1].complete_scope is None and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=1, include_databases=False))
    assert len(site.bodies("/v1/search")) == 1 and len(changes) == 2 and batches[-1].complete_scope is None


def test_archived_pages_become_deletes():
    class Leaky(Notion):             # a server that still lists archived pages (e.g. a database query does)
        def handler(self, request):
            response = super().handler(request)
            if request.url.path == "/v1/search" and json.loads(request.content)["filter"]["value"] == "page":
                body = response.json()
                body["results"].append(self.pages[uid(2)])
                return httpx.Response(200, json=body)
            return response
    changes, _ = run(make(Leaky([npage(1, "Live"), npage(2, "Gone", archived=True)])))
    assert sorted((c.kind, c.external_id) for c in changes) == [("delete", f"notion:{uid(2)}"), ("upsert", f"notion:{uid(1)}")]


# ── versions and paths ──


def test_version_is_last_edited_time_and_changes_when_the_page_is_edited():
    site = Notion([npage(1, "Doc", edited="2026-03-01T10:00:00.000Z")])
    v1 = run(make(site))[0][0].file.version
    assert run(make(site))[0][0].file.version == v1 == "2026-03-01T10:00:00.000Z"
    site.pages[uid(1)]["last_edited_time"] = "2026-03-02T11:30:00.000Z"
    assert run(make(site))[0][0].file.version == "2026-03-02T11:30:00.000Z"


def test_paths_follow_the_parent_chain_and_top_level_pages_get_their_own_folder():
    pages = [npage(1, "Engineering Wiki"), npage(2, "Runbooks", parent=under(1)), npage(3, "Deploy", parent=under(2), edited="2026-03-09T10:00:00.000Z"),
             npage(4, "Stray", parent={"type": "page_id", "page_id": uid(99)}),             # parent not shared with the integration
             npage(5, "In a toggle", parent={"type": "block_id", "block_id": uid(77)})]
    site = Notion(pages)
    by = {c.external_id: c.file for c in run(make(site))[0]}
    short = lambda n: uid(n).replace("-", "")[:8]                                          # noqa: E731
    assert by[f"notion:{uid(1)}"].path == f"notion/Engineering Wiki/Engineering Wiki-{short(1)}.md"
    assert by[f"notion:{uid(2)}"].path == f"notion/Engineering Wiki/Runbooks-{short(2)}.md"
    assert by[f"notion:{uid(3)}"].path == f"notion/Engineering Wiki/Runbooks/Deploy-{short(3)}.md"
    assert by[f"notion:{uid(4)}"].path == f"notion/Stray/Stray-{short(4)}.md"             # the missing ancestor ends the chain, no crash
    assert by[f"notion:{uid(5)}"].path == f"notion/In a toggle/In a toggle-{short(5)}.md"
    assert by[f"notion:{uid(3)}"].name == f"Deploy-{short(3)}.md"
    fetched = [p for p in site.paths() if p.startswith("/v1/pages/")]
    assert len(fetched) == len(set(fetched))                                               # each unknown ancestor is fetched once


def test_resolve_paths_off_makes_no_ancestor_requests():
    site = Notion([npage(1, "Top"), npage(2, "Child", parent=under(1))])
    by = {c.external_id: c.file for c in run(make(site, resolve_paths=False))[0]}
    assert by[f"notion:{uid(2)}"].path.startswith("notion/Child-") and "/" not in by[f"notion:{uid(2)}"].path[7:]
    assert not any(p.startswith("/v1/pages/") for p in site.paths())


def test_acl_rules_by_top_level_folder_longest_wins_glob_covers_the_page_itself_and_fail_closed():
    pages = [npage(1, "Engineering Wiki"), npage(2, "Runbooks", parent=under(1)), npage(3, "HR Space"), npage(4, "Offers", parent=under(3))]
    rules = [{"prefix": "notion/Engineering Wiki", "principals": ["group:eng"]},
             {"prefix": "notion/HR Space", "principals": ["group:hr"]},
             {"prefix": "notion/Engineering Wiki/Runbooks*", "principals": ["*"]}]
    connector = make(Notion(pages), acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by[f"notion:{uid(1)}"].acl == ["group:eng"] and by[f"notion:{uid(4)}"].acl == ["group:hr"]
    assert by[f"notion:{uid(2)}"].acl == ["*"]
    assert connector.refresh_acl(persisted(by[f"notion:{uid(4)}"])) == ["group:hr"]
    closed = make(Notion(pages), acl_rules=[{"prefix": "notion/HR Space", "principals": ["group:hr"]}], default_acl=None)
    by = {c.external_id: c.file for c in run(closed)[0]}
    assert by[f"notion:{uid(1)}"].acl == [] and by[f"notion:{uid(3)}"].acl == ["group:hr"]


# ── open(): blocks to Markdown ──


def test_open_renders_title_properties_and_nested_blocks_with_paginated_children():
    page = npage(1, "Release plan", parent=in_db(100), title_prop="Name", props={
        "Status": {"id": "s", "type": "select", "select": {"id": "1", "name": "In progress", "color": "blue"}},
        "Tags": {"id": "t", "type": "multi_select", "multi_select": [{"name": "q1"}, {"name": "infra"}]},
        "Due": {"id": "d", "type": "date", "date": {"start": "2026-04-01", "end": None}},
        "Owner": {"id": "o", "type": "people", "people": [{"object": "user", "id": "u", "name": "Ada Lovelace"}]},
        "Done": {"id": "c", "type": "checkbox", "checkbox": False}, "Empty": {"id": "e", "type": "rich_text", "rich_text": []},
        "Related": {"id": "r", "type": "relation", "relation": [{"id": uid(5)}]}})
    top = [
        blk("heading_1", "Goals", bid="b1"),
        blk("paragraph", "Ship it", bid="b2"),
        blk("bulleted_list_item", "first", bid="b3", has_children=True),
        blk("numbered_list_item", "one", bid="b4"), blk("numbered_list_item", "two", bid="b5"),
        blk("paragraph", "break", bid="b6"),
        blk("numbered_list_item", "restart", bid="b7"),
        blk("to_do", "write docs", bid="b8", checked=True), blk("to_do", "write tests", bid="b9", checked=False),
        blk("toggle", "More", bid="b10", has_children=True),
        blk("quote", "wise words", bid="b11"),
        blk("callout", "heads up", bid="b12", icon={"type": "emoji", "emoji": "💡"}),
        blk("code", "print(1)", bid="b13", language="python", caption=[]),
        blk("divider", bid="b14"),
        blk("child_page", bid="b15", has_children=True, title="Sub page"),
        blk("image", bid="b16", type="file", file={"url": "https://s3.amazonaws.com/secure.notion-static.com/x.png?X-Amz-Signature=SIGNED", "expiry_time": "2026-03-10T13:00:00Z"}, caption=[rt("architecture")]),
        blk("image", bid="b17", type="external", external={"url": "https://img.acme.io/logo.png?size=big"}, caption=[]),
        blk("bookmark", bid="b18", url="https://acme.io/spec", caption=[]),
        blk("equation", bid="b19", expression="E=mc^2"),
        blk("table", bid="b20", has_children=True, table_width=2, has_column_header=True, has_row_header=False),
        blk("column_list", bid="b21", has_children=True),
        blk("synced_block", bid="b22", synced_from={"block_id": "orig"}),
        blk("table_of_contents", bid="b23"), blk("unsupported", bid="b24"),
        blk("image", bid="b25", type="file", file={"url": "https://s3.amazonaws.com/x/y.png?X-Amz-Signature=SIGNED2", "expiry_time": "2026-03-10T13:00:00Z"}, caption=[]),
        blk("file", bid="b26", type="file", file={"url": "https://s3.amazonaws.com/x/z.pdf?X-Amz-Signature=SIGNED3", "expiry_time": "2026-03-10T13:00:00Z"}, name="plan.pdf", caption=[]),
    ]
    row = lambda *cells: {"object": "block", "id": "r" + cells[0], "type": "table_row", "has_children": False, "table_row": {"cells": [[rt(c)] for c in cells]}}   # noqa: E731
    blocks = {
        uid(1): top,
        "b3": [blk("bulleted_list_item", "nested a", bid="n1", has_children=True), blk("paragraph", "nested para", bid="n2")],
        "n1": [blk("bulleted_list_item", "deep", bid="n3")],
        "b10": [blk("paragraph", "hidden inside toggle", bid="n4")],
        "b15": [blk("paragraph", "SUBPAGE BODY MUST NOT APPEAR", bid="n5")],
        "b20": [row("H1", "H2"), row("a", "b|c")],
        "b21": [blk("column", bid="c1", has_children=True), blk("column", bid="c2", has_children=True)],
        "c1": [blk("paragraph", "left column", bid="n6")], "c2": [blk("paragraph", "right column", bid="n7")],
        "orig": [blk("paragraph", "synced content", bid="n8")],
    }
    site = Notion([page], [ndb(100, "Roadmap")], blocks)
    scanned = run(make(site))[0][0].file
    assert scanned.path.startswith("notion/Roadmap/Release plan-")
    site.reset()
    text = read(make(site), scanned)
    assert text.startswith("# Release plan\n")
    for line in ("Status: In progress", "Tags: q1, infra", "Due: 2026-04-01", "Owner: Ada Lovelace", "Done: no",
                 "url: https://www.notion.so/Release-plan-", "last edited: 2026-03-01T10:00:00.000Z"):
        assert line in text
    assert "Empty:" not in text and "Related:" not in text
    assert "# Goals" in text and "Ship it" in text
    assert "- first\n  - nested a\n    - deep\n  nested para" in text
    assert "1. one\n2. two" in text and "1. restart" in text
    assert "- [x] write docs\n- [ ] write tests" in text
    assert "- More\n  hidden inside toggle" in text
    assert "> wise words" in text and "> 💡 heads up" in text
    assert "```python\nprint(1)\n```" in text and "\n---\n" in text and "$$E=mc^2$$" in text
    assert "(subpage: Sub page)" in text and "SUBPAGE BODY" not in text
    assert "[image: architecture]" in text and "[image: https://img.acme.io/logo.png](https://img.acme.io/logo.png)" in text
    assert "SIGNED" not in text and "X-Amz" not in text and "size=big" not in text            # signed URLs never reach the index
    assert "[image: file]" in text and "[file: plan.pdf]" in text
    assert "https://acme.io/spec" in text
    assert "| H1 | H2 |\n| --- | --- |\n| a | b\\|c |" in text
    assert "left column" in text and "right column" in text and "synced content" in text
    assert "table_of_contents" not in text and "unsupported" not in text
    children_calls = [r for r in site.requests if r.url.path == f"/v1/blocks/{uid(1)}/children"]
    assert [r.url.params.get("start_cursor") for r in children_calls] == [None, "c-2-aGVsbG8", "c-4-aGVsbG8"] + [f"c-{n}-aGVsbG8" for n in range(6, 26, 2)]
    assert scanned.version == "2026-03-01T10:00:00.000Z"


def test_deep_or_huge_pages_are_cut_off_with_a_marker_instead_of_running_away():
    blocks = {uid(1): [blk("paragraph", f"line {n}", bid=f"p{n}") for n in range(30)]}
    site = Notion([npage(1, "Big")], blocks=blocks)
    text = read(make(site, max_blocks_per_page=10), run(make(site))[0][0].file)
    assert "line 9" in text and "line 10" not in text and "truncated after 10 blocks" in text

    nest = {uid(1): [blk("bulleted_list_item", "l0", bid="d0", has_children=True)],
            **{f"d{i}": [blk("bulleted_list_item", f"l{i + 1}", bid=f"d{i + 1}", has_children=True)] for i in range(12)}}
    text = read(make(Notion([npage(1, "Deep")], blocks=nest), max_block_depth=3), run(make(Notion([npage(1, "Deep")])))[0][0].file)
    assert "l0" in text and "l3" in text and "l4" not in text


def test_rich_text_annotations_links_and_equations():
    items = [rt("plain "), rt("bold", bold=True), rt(" "), rt("it", italic=True), rt(" "), rt("code", code=True), rt(" "),
             rt("gone", strike=True), rt(" "), rt("site", href="https://acme.io"), rt(" x^2", kind="equation"),
             rt(" bold and spaced ", bold=True), rt("@Ada", kind="mention")]
    assert rich_text(items) == "plain **bold** *it* `code` ~~gone~~ [site](https://acme.io)$ x^2$ **bold and spaced** @Ada"
    assert rich_text(None) == "" and rich_text([{"plain_text": ""}]) == ""
    assert property_text({"type": "formula", "formula": {"type": "number", "number": 3}}) == "3"
    assert property_text({"type": "unique_id", "unique_id": {"prefix": "TASK", "number": 12}}) == "TASK-12"
    assert property_text({"type": "date", "date": {"start": "2026-01-01", "end": "2026-01-05"}}) == "2026-01-01 -> 2026-01-05"
    assert property_text({"type": "rollup", "rollup": {}}) == ""


def test_open_validates_the_id_and_reports_a_page_that_is_no_longer_shared():
    site = Notion([])
    connector = make(site)
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile(f"notion:{uid(404)}", "p", "n", 0, None, "v", []))
    assert err.value.status == 404
    site.reset()
    for bad in ("notion:../users/me", "notion:", "notion:abc"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []


# ── http behaviour ──


def test_429_is_retried_and_the_notion_version_header_is_configurable():
    site = Notion([npage(1, "A")], version="2025-09-03")
    site.script = [httpx.Response(429, headers={"Retry-After": "3"}, json={"object": "error", "code": "rate_limited"})]
    connector = make(site, notion_version="2025-09-03")
    changes, _ = run(connector)
    assert len(changes) == 1 and connector.sleeps == [3.0]


def test_401_is_a_clear_error_without_the_secret_and_not_retried(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret_wrong_value_123")
    site = Notion(FIVE)
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "401" in str(err.value) and "NOTION_TOKEN" in str(err.value)
    assert "secret_wrong_value_123" not in str(err.value) and TOKEN not in str(err.value)
    assert len(site.requests) == 1 and connector.sleeps == []


def test_cursors_never_contain_the_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(Notion(FIVE)))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("NOTION_TOKEN")
    with pytest.raises(HttpError, match="NOTION_TOKEN"):
        run(make(Notion(FIVE)))
