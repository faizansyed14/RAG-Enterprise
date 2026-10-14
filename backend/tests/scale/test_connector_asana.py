"""Asana connector against a fake API shaped like the Asana documentation: `{"data": ..., "next_page": {...}|null}` envelopes,
`offset` pagination with tokens the server issued (anything else is a 400), `opt_fields` (the fake returns only the fields
asked for), `modified_since` only on GET /tasks (a task counts as modified when a story is added to it), `completed_since=now`.

Not certain of (written from memory of the docs): the wording of error bodies, that an expired offset is a 400, and that
`completed_since=now` behaves the same on both list endpoints.
"""

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.asana import AsanaConnector
from app.scale.connectors.base import SourceFile

TOKEN = "2/1200000000/1234567890:SeCrEt0123456789abcdef"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
PROJECTS = {"1001": "Website Relaunch", "1002": "Payroll"}


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def task(gid, name, *, project="1001", section="To do", notes="", completed=False, modified="2026-03-01T10:00:00.000Z", **kw):
    return {
        "gid": str(gid), "resource_type": "task", "resource_subtype": "default_task", "name": name, "notes": notes, "completed": completed,
        "completed_at": None, "created_at": "2026-02-01T09:00:00.000Z", "modified_at": modified, "due_on": None, "due_at": None, "start_on": None,
        "assignee": {"gid": "7", "name": "Ada Lovelace", "resource_type": "user"}, "created_by": {"gid": "8", "name": "Linus Torvalds", "resource_type": "user"},
        "tags": [{"gid": "t1", "name": "urgent", "resource_type": "tag"}],
        "memberships": [{"project": {"gid": project, "name": PROJECTS[project]}, "section": {"gid": "s1", "name": section}}],
        "parent": None, "permalink_url": f"https://app.asana.com/0/{project}/{gid}",
        "custom_fields": [{"gid": "c1", "name": "Priority", "display_value": "High"}, {"gid": "c2", "name": "Estimate", "display_value": None}], **kw,
    }


def story(gid, text, *, who="Grace Hopper", created="2026-03-02T08:00:00.000Z", subtype="comment_added", **kw):
    return {"gid": str(gid), "resource_type": "story", "created_at": created, "created_by": {"gid": "9", "name": who, "resource_type": "user"},
            "resource_subtype": subtype, "type": "comment" if subtype == "comment_added" else "system", "text": text, "html_text": f"<body>{text}</body>", **kw}


class Asana:
    sessions = 0

    def __init__(self, tasks=(), stories=None, *, projects=None, cap=2, expire_offsets=False):
        self.projects = dict(PROJECTS if projects is None else projects)
        self.tasks = copy.deepcopy(list(tasks))
        self.stories = copy.deepcopy(stories or {})
        self.cap, self.expire_offsets = cap, expire_offsets
        self.issued: set[str] = set()
        self.denied: dict[str, int] = {}                          # project gid -> status of its project / task list
        Asana.sessions += 1
        self.nonce = f"s{Asana.sessions}-"
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def calls(self, pattern):
        return [r for r in self.requests if re.search(pattern, r.url.path)]

    def touched(self, t):
        times = [t["modified_at"]] + [s["created_at"] for s in self.stories.get(t["gid"], [])]
        return max(epoch(x) for x in times)

    @staticmethod
    def pick(record, fields):
        """The compact record plus the requested fields (nested names such as assignee.name keep the whole `assignee`)."""
        wanted = {f.split(".")[0] for f in fields.split(",")} if fields else set()
        return {k: v for k, v in record.items() if k in wanted or k in ("gid", "resource_type", "resource_subtype", "name")}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"errors": [{"message": "Not Authorized"}]})
        q = dict(request.url.params)
        path = request.url.path.removeprefix("/api/1.0")
        if "limit" in q and not 1 <= int(q["limit"]) <= 100:
            return httpx.Response(400, json={"errors": [{"message": "limit: must be between 1 and 100"}]})
        m = re.fullmatch(r"/projects/(\d+)", path)
        if m:
            gid = m.group(1)
            if gid in self.denied:
                return httpx.Response(self.denied[gid], json={"errors": [{"message": "project: Not a recognized ID" if self.denied[gid] == 404 else "Forbidden"}]})
            if gid not in self.projects:
                return httpx.Response(404, json={"errors": [{"message": f"project: Not a recognized ID: {gid}"}]})
            return httpx.Response(200, json={"data": self.pick({"gid": gid, "resource_type": "project", "name": self.projects[gid], "archived": False}, q.get("opt_fields"))})
        m = re.fullmatch(r"/projects/(\d+)/tasks", path)
        if m:
            assert "modified_since" not in q, "modified_since belongs to GET /tasks, not to GET /projects/{gid}/tasks"
            if m.group(1) in self.denied:
                return httpx.Response(self.denied[m.group(1)], json={"errors": [{"message": "Forbidden"}]})
            return self.task_page(m.group(1), q, path)
        if path == "/tasks":
            assert "project" in q, "GET /tasks needs a project (or an assignee + workspace)"
            return self.task_page(q["project"], q, path, since=q.get("modified_since"))
        m = re.fullmatch(r"/tasks/(\d+)", path)
        if m:
            found = [t for t in self.tasks if t["gid"] == m.group(1)]
            return httpx.Response(200, json={"data": self.pick(found[0], q.get("opt_fields"))}) if found else httpx.Response(404, json={"errors": [{"message": "task: Unknown object: " + m.group(1)}]})
        m = re.fullmatch(r"/tasks/(\d+)/stories", path)
        if m:
            if not [t for t in self.tasks if t["gid"] == m.group(1)]:
                return httpx.Response(404, json={"errors": [{"message": "task: Unknown object"}]})
            rows = sorted(self.stories.get(m.group(1), []), key=lambda s: s["created_at"])
            return self.page([self.pick(s, q.get("opt_fields")) for s in rows], q, path)
        return httpx.Response(404, json={"errors": [{"message": "Unknown path"}]})

    def task_page(self, project, q, path, since=None):
        rows = [t for t in self.tasks if any(mm["project"]["gid"] == project for mm in t["memberships"])]
        if q.get("completed_since") == "now":
            rows = [t for t in rows if not t["completed"]]
        if since:
            rows = [t for t in rows if self.touched(t) >= epoch(since)]
        rows.sort(key=lambda t: int(t["gid"]))
        return self.page([self.pick(t, q.get("opt_fields")) for t in rows], q, path)

    def page(self, rows, q, path):
        if "offset" in q:
            if q["offset"] not in self.issued or self.expire_offsets and not q["offset"].startswith(self.nonce):
                return httpx.Response(400, json={"errors": [{"message": "offset: Invalid offset"}]})
            start = int(q["offset"].rsplit("o", 1)[1])
        else:
            start = 0
        size = min(int(q.get("limit", 30)), self.cap)
        part = rows[start:start + size]
        if start + size >= len(rows):
            return httpx.Response(200, json={"data": part, "next_page": None})
        token = f"{self.nonce}o{start + size}"
        self.issued.add(token)
        query = urlencode({**{k: v for k, v in q.items() if k != "offset"}, "offset": token})
        return httpx.Response(200, json={"data": part, "next_page": {"offset": token, "path": f"{path}?{query}", "uri": f"https://app.asana.com/api/1.0{path}?{query}"}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASANA_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "ASANA_TOKEN", "projects": ["1001"], "default_acl": ["group:pm"], **cfg}
    connector = AsanaConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


def ids(changes):
    return [c.external_id for c in changes]


TASKS = [
    task(2001, "Design the landing page", notes="Hero, pricing and FAQ.", modified="2026-03-01T10:00:00.000Z", due_on="2026-03-20"),
    task(2002, "Write copy", section="Doing", notes="Short and friendly.", modified="2026-03-02T10:00:00.000Z"),
    task(2003, "Set up analytics", completed=True, completed_at="2026-03-03T11:00:00.000Z", modified="2026-03-03T11:00:00.000Z"),
    task(2004, "Launch checklist", modified="2026-03-04T10:00:00.000Z"),
    task(2005, "Post-launch review", notes="", modified="2026-03-05T10:00:00.000Z"),
    task(3001, "Run March payroll", project="1002", notes="Cut-off is the 25th.", modified="2026-03-02T10:00:00.000Z"),
    task(3002, "File tax forms", project="1002", modified="2026-03-04T10:00:00.000Z"),
]
STORIES = {
    "2001": [story(1, "We should ship on Friday", created="2026-03-02T08:00:00.000Z"),
             story(2, "Grace assigned this to Ada", subtype="assigned", created="2026-03-01T10:00:00.000Z"),
             story(3, "", who="Ada Lovelace", created="2026-03-03T09:00:00.000Z", html_text="<body>Agreed, <b>Friday</b> it is</body>")],
    "3001": [story(4, "Remember the bonus run", who="Ada Lovelace", created="2026-03-02T09:00:00.000Z")],
}
FIRST = "asana:1001/"


def standard(**kw):
    return Asana(TASKS, STORIES, **kw)


def key(change):
    return change.external_id.removeprefix("asana:")


# ── pagination and paths ──


def test_tasks_are_listed_by_offset_until_next_page_is_null_and_named_after_project_and_task():
    site = standard()
    changes, batches = run(make(site))
    assert ids(changes) == [f"asana:1001/{n}" for n in (2001, 2002, 2003, 2004, 2005)]
    listing = site.calls(r"/projects/1001/tasks$")
    assert [r.url.params.get("offset") for r in listing] == [None, f"{site.nonce}o2", f"{site.nonce}o4"]
    assert all(r.url.params["limit"] == "100" and "modified_since" not in r.url.params and "completed_since" not in r.url.params for r in listing)
    assert "assignee.name" in listing[0].url.params["opt_fields"] and "memberships.section.name" in listing[0].url.params["opt_fields"]
    by = {c.external_id: c.file for c in changes}
    first = by["asana:1001/2001"]
    assert first.path == "asana/Website Relaunch/2001-Design the landing page.md" and first.name == "2001-Design the landing page.md"
    assert first.mime == "text/markdown" and first.acl == ["group:pm"] and first.modified_at == datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
    assert len(site.calls(r"/projects/1001$")) == 1 and len(site.calls(r"/stories$")) == 6
    assert scopes(batches) == [FIRST] and batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope is None
    assert [b.complete_scope for b in batches[:-1]] == [None, None, FIRST]


def test_page_size_is_clamped_to_the_maximum_and_the_token_goes_in_the_header_only():
    site = standard()
    run(make(site, page_size=500, include_comments=False))
    assert {r.url.params["limit"] for r in site.calls(r"/projects/1001/tasks$")} == {"100"} and site.calls(r"/stories$") == []
    assert all("SeCrEt" not in str(r.url) for r in site.requests)


def test_each_project_is_listed_on_its_own_with_its_own_scope_and_folder():
    site = standard()
    changes, batches = run(make(site, projects=["1001", "1002", "1001"]))                           # a repeated gid is read once
    assert ids(changes)[-2:] == ["asana:1002/3001", "asana:1002/3002"] and len(changes) == 7
    assert scopes(batches) == ["asana:1001/", "asana:1002/"]
    assert {c.file.path.split("/")[1] for c in changes} == {"Website Relaunch", "Payroll"}


def test_a_task_in_two_projects_is_a_file_in_each_with_its_own_acl():
    both = task(2001, "Shared task")
    both["memberships"].append({"project": {"gid": "1002", "name": "Payroll"}, "section": {"gid": "s2", "name": "Inbox"}})
    site = Asana([both])
    rules = [{"prefix": "asana/Website Relaunch", "principals": ["group:web"]}, {"prefix": "asana/Payroll", "principals": ["group:finance"]}]
    changes, _ = run(make(site, projects=["1001", "1002"], acl_rules=rules, default_acl=None))
    by = {c.external_id: c.file for c in changes}
    assert by["asana:1001/2001"].acl == ["group:web"] and by["asana:1002/2001"].acl == ["group:finance"]
    assert "section: To do" in read(make(site), by["asana:1001/2001"]) and "section: Inbox" in read(make(site), by["asana:1002/2001"])


def test_section_pseudo_tasks_are_not_indexed_and_completed_tasks_can_be_left_out():
    rows = TASKS[:3] + [task(2999, "Backlog:", resource_subtype="section")]
    site = Asana(rows, STORIES)
    changes, _ = run(make(site))
    assert ids(changes) == ["asana:1001/2001", "asana:1001/2002", "asana:1001/2003"]
    site.reset()
    changes, _ = run(make(site, include_completed=False))
    assert ids(changes) == ["asana:1001/2001", "asana:1001/2002"]
    assert all(r.url.params["completed_since"] == "now" for r in site.calls(r"/projects/1001/tasks$"))


# ── rendering ──


def test_a_task_file_has_metadata_notes_fields_and_comments_but_no_system_stories_by_default():
    site = standard()
    connector = make(site)
    by = {c.external_id: c.file for c in run(connector)[0]}
    text = read(connector, by["asana:1001/2001"])
    assert text.startswith("# Design the landing page\n")
    for line in ("type: task", "project: Website Relaunch", "section: To do", "status: open", "assignee: Ada Lovelace", "created by: Linus Torvalds",
                 "due: 2026-03-20", "tags: urgent", "created: 2026-02-01T09:00:00.000Z", "url: https://app.asana.com/0/1001/2001"):
        assert line in text, line
    assert "Hero, pricing and FAQ." in text and "- Priority: High" in text and "Estimate" not in text           # fields without a value are dropped
    assert "## Comments" in text and "### Grace Hopper (2026-03-02T08:00:00.000Z)\n\nWe should ship on Friday" in text
    assert "### Ada Lovelace (2026-03-03T09:00:00.000Z)\n\nAgreed, **Friday** it is" in text                         # html_text when text is empty
    assert text.index("We should ship") < text.index("Agreed")                                                        # oldest first
    assert "assigned this" not in text and "## Activity" not in text
    assert "status: completed (2026-03-03T11:00:00.000Z)" in read(connector, by["asana:1001/2003"])
    assert by["asana:1001/2001"].size == len(text.encode())
    with_system = read(make(site, include_system_stories=True), by["asana:1001/2001"])
    assert "## Activity" in with_system and "- 2026-03-01T10:00:00.000Z: Grace assigned this to Ada" in with_system
    only_system = read(make(site, include_comments=False, include_system_stories=True), by["asana:1001/2001"])
    assert "## Comments" not in only_system and "assigned this to Ada" in only_system


def test_stories_are_paged_empty_ones_are_skipped_and_comments_can_be_switched_off():
    many = {"2001": [story(n, f"comment number {n}", created=f"2026-03-02T08:0{n}:00.000Z") for n in range(1, 6)] + [story(9, "   ", created="2026-03-02T09:00:00.000Z")]}
    site = Asana([task(2001, "Chatty")], many)
    connector = make(site)
    changes, _ = run(connector)
    stories = site.calls(r"/tasks/2001/stories$")
    assert len(stories) == 3 and [r.url.params.get("offset") for r in stories] == [None, f"{site.nonce}o2", f"{site.nonce}o4"]
    text = read(connector, changes[0].file)
    assert all(f"comment number {n}" in text for n in range(1, 6)) and text.count("### ") == 5
    quiet = Asana([task(2001, "Chatty")], many)
    run(make(quiet, include_comments=False))
    assert quiet.calls(r"/stories$") == []


def test_version_follows_the_rendered_text_and_path_but_not_modification_times():
    def versions(site, **cfg):
        return {c.external_id: c.file.version for c in run(make(site, **cfg))[0]}

    base = versions(standard())
    assert versions(standard()) == base
    touched = standard()
    touched.tasks[0]["modified_at"] = "2026-03-09T10:00:00.000Z"
    assert versions(touched) == base                                                  # modified_at is not content
    edited = standard()
    edited.tasks[0]["notes"] = "Hero, pricing, FAQ and a footer."
    after = versions(edited)
    assert after["asana:1001/2001"] != base["asana:1001/2001"] and after["asana:1001/2002"] == base["asana:1001/2002"]
    commented = standard()
    commented.stories["2002"] = [story(50, "A new remark", created="2026-03-06T08:00:00.000Z")]
    assert versions(commented)["asana:1001/2002"] != base["asana:1001/2002"]
    renamed = standard()
    renamed.projects["1001"] = "Website v2"
    assert versions(renamed)["asana:1001/2001"] != base["asana:1001/2001"]            # the folder (and the project line) moved
    moved = standard()
    moved.tasks[0]["memberships"][0]["section"]["name"] = "Done"
    assert versions(moved)["asana:1001/2001"] != base["asana:1001/2001"]


# ── resume ──


def test_a_listing_resumes_at_the_saved_offset_and_does_not_claim_completeness():
    site = standard()
    batches = list(make(site).scan({}))
    mid = batches[0]
    assert mid.cursor["pos"] == {"project": 0, "next": f"{site.nonce}o2"} and mid.cursor["mode"] == "full"
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, resumed = run(make(site), mid.cursor)
    assert ids(changes) == [f"asana:1001/{n}" for n in (2003, 2004, 2005)] and scopes(resumed) == []
    assert site.calls(r"/projects/1001/tasks$")[0].url.params["offset"] == f"{site.nonce}o2" and resumed[-1].done
    assert resumed[-1].cursor.get("last_full") is None                              # a resumed scan is not a full pass


def test_a_resume_in_the_second_project_skips_the_first_and_a_refused_offset_restarts_the_project():
    site = standard()
    batches = list(make(site, projects=["1001", "1002"]).scan({}))
    boundary = next(b for b in batches if b.complete_scope == FIRST)
    assert boundary.cursor["pos"] == {"project": 1}
    site.reset()
    changes, resumed = run(make(site, projects=["1001", "1002"]), boundary.cursor)
    assert ids(changes) == ["asana:1002/3001", "asana:1002/3002"] and not site.calls("/1001") and scopes(resumed) == ["asana:1002/"]       # that project was listed from its first page
    expiring = standard(expire_offsets=True)
    mid = list(make(expiring).scan({}))[0]
    stale = {**mid.cursor, "pos": {"project": 0, "next": "s0-o2"}}                     # an offset from an earlier session
    expiring.reset()
    changes, batches = run(make(expiring), stale)
    assert ids(changes) == [f"asana:1001/{n}" for n in (2001, 2002, 2003, 2004, 2005)] and scopes(batches) == [FIRST]       # it ran from the start after all
    assert [r.url.params.get("offset") for r in expiring.calls(r"/projects/1001/tasks$")][:2] == ["s0-o2", None]


def test_a_cursor_past_the_last_project_goes_straight_to_the_end():
    site = standard()
    changes, batches = run(make(site), {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"project": 5}})
    assert changes == [] and site.requests == [] and batches[-1].done


# ── incremental ──


def test_next_scan_asks_for_tasks_modified_since_the_start_of_the_last_scan_and_sees_new_comments():
    site = standard()
    _, batches = run(make(site))
    final = batches[-1].cursor
    assert final == {"since": "2026-03-10T12:00:00Z", "last_full": NOW}                    # when the scan began, not the newest modified_at
    site.stories["2002"] = [story(60, "Late remark", created="2026-03-10T12:30:00.000Z")]  # a comment: modified_at of the task does not move
    site.tasks[3]["name"] = "Launch checklist v2"
    site.tasks[3]["modified_at"] = "2026-03-10T12:10:00.000Z"
    site.reset()
    later = NOW + 3600
    changes, inc = run(make(site, clock=lambda: later), final)
    assert not site.calls(r"/projects/1001/tasks$")
    listing = site.calls(r"^/api/1.0/tasks$")
    assert listing[0].url.params["project"] == "1001" and listing[0].url.params["modified_since"] == "2026-03-10T11:55:00Z"       # minus the 5 minutes of overlap
    assert sorted(ids(changes)) == ["asana:1001/2002", "asana:1001/2004"]
    assert "Late remark" in read(make(site), next(c.file for c in changes if c.external_id == "asana:1001/2002"))
    assert scopes(inc) == [] and inc[-1].cursor == {"since": "2026-03-10T13:00:00Z", "last_full": NOW}


def test_incremental_scans_use_overlap_minutes_and_a_floor_from_tasks_since():
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW - 60}
    site = standard()
    run(make(site, overlap_minutes=60), cursor)
    assert site.calls(r"^/api/1.0/tasks$")[0].url.params["modified_since"] == "2026-03-05T09:00:00Z"
    site = standard()
    changes, batches = run(make(site, tasks_since="2026-03-03"))                                # a full scan limited to recent tasks
    first = site.calls(r"^/api/1.0/tasks$")[0].url.params
    assert first["modified_since"] == "2026-03-03T00:00:00Z" and first["project"] == "1001"
    assert ids(changes) == ["asana:1001/2001", "asana:1001/2003", "asana:1001/2004", "asana:1001/2005"] and scopes(batches) == [FIRST]       # 2001 has a comment from 03-03
    site = standard()
    run(make(site, tasks_since="2026-03-08"), cursor)                                             # the floor is newer than since - overlap
    assert site.calls(r"^/api/1.0/tasks$")[0].url.params["modified_since"] == "2026-03-08T00:00:00Z"
    site = standard()
    run(make(site, tasks_since="2026-01-01", include_completed=False), cursor)                    # the watermark is newer than the floor
    params = site.calls(r"^/api/1.0/tasks$")[0].url.params
    assert params["modified_since"] == "2026-03-05T09:55:00Z" and params["completed_since"] == "now"


def test_full_resync_when_due_lists_the_whole_project_and_declares_the_scope():
    site = standard()
    changes, batches = run(make(site, clock=lambda: NOW + 25 * 3600), {"since": "2026-03-10T12:00:00Z", "last_full": NOW})
    assert len(changes) == 5 and scopes(batches) == [FIRST] and not site.calls(r"^/api/1.0/tasks$")
    assert batches[-1].cursor["last_full"] == NOW + 25 * 3600


def test_max_items_and_max_pages_truncate_and_never_declare_completeness():
    site = standard()
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=2))
    assert len(site.calls(r"/projects/1001/tasks$")) == 1 and len(changes) == 2 and scopes(batches) == [] and "since" not in batches[-1].cursor      # the project lookup is a page
    wide = Asana(TASKS, STORIES, cap=10)
    changes, batches = run(make(wide, max_items=3))                                               # a final page that was cut
    assert len(changes) == 3 and scopes(batches) == []
    changes, batches = run(make(Asana(TASKS, STORIES, cap=10), max_items=5))
    assert len(changes) == 5 and scopes(batches) == [FIRST]
    two = standard()
    changes, batches = run(make(two, projects=["1001", "1002"], max_items=5))
    assert len(changes) == 5 and two.calls(r"/projects/1002") == [] and scopes(batches) == ["asana:1001/"]       # the first project was complete; nothing is requested for the second


# ── projects that cannot be read, vanished tasks ──


def test_a_project_the_token_cannot_read_is_treated_as_gone_in_a_full_scan_only():
    for status in (403, 404):
        site = standard()
        site.denied["1002"] = status
        changes, batches = run(make(site, projects=["1001", "1002"]))
        assert len(changes) == 5 and scopes(batches) == ["asana:1001/", "asana:1002/"], status        # 1002's old files are removed by the sync layer
    site = standard()
    site.denied["1002"] = 403
    changes, batches = run(make(site, projects=["1001", "1002"]), {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})
    assert scopes(batches) == []
    site = standard()
    site.projects.pop("1002")
    assert len(run(make(site, projects=["1002"]))[0]) == 0                                       # an unknown gid (404) is also just empty


def test_a_task_that_vanishes_while_its_comments_are_fetched_becomes_a_delete():
    site = standard()
    original = site.handler

    def handler(request):
        if request.url.path.endswith("/tasks/2002/stories"):
            site.requests.append(request)
            return httpx.Response(404, json={"errors": [{"message": "task: Unknown object: 2002"}]})
        return original(request)
    connector = AsanaConnector({"token_env": "ASANA_TOKEN", "projects": ["1001"], "default_acl": ["group:pm"]}, transport=httpx.MockTransport(handler), sleep=lambda s: None, clock=lambda: NOW)
    changes, _ = run(connector)
    assert [(c.kind, c.external_id) for c in changes][1] == ("delete", "asana:1001/2002") and len(changes) == 5


def test_a_next_page_pointing_elsewhere_is_never_followed():
    site = standard()
    original = site.page

    def evil(rows, q, path):
        body = original(rows, q, path).json()
        if body["next_page"]:
            body["next_page"]["uri"] = "https://evil.example/steal?offset=1"
            body["next_page"]["path"] = "https://evil.example/steal?offset=1"
        return httpx.Response(200, json=body)
    site.page = evil
    changes, _ = run(make(site))
    assert len(changes) == 5 and {r.url.host for r in site.requests} == {"app.asana.com"}


def test_an_offset_that_never_changes_is_an_error_for_task_lists_and_for_stories():
    class Stuck(Asana):
        def page(self, rows, q, path):
            body = super().page(rows, q, path).json()
            if body["next_page"]:
                body["next_page"]["offset"] = "stuck-o1"                      # the same token again and again
                self.issued.add("stuck-o1")
            return httpx.Response(200, json=body)

    with pytest.raises(HttpError, match="did not advance"):
        run(make(Stuck(TASKS, {}, cap=1)))
    chatty = {"2001": [story(n, "x", created=f"2026-03-02T08:0{n}:00.000Z") for n in range(1, 5)]}
    with pytest.raises(HttpError, match="did not advance"):
        run(make(Stuck([TASKS[0]], chatty, cap=1)))


# ── errors and rate limits ──


def test_429_is_retried_after_retry_after_for_lists_and_comments():
    site = standard()
    site.script = [httpx.Response(429, headers={"Retry-After": "13"}, json={"errors": [{"message": "Rate limit"}]})]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [13.0]
    flaky = standard()
    original, state, slept = flaky.handler, {"hit": False}, []

    def handler(request):
        if request.url.path.endswith("/stories") and not state["hit"]:
            state["hit"] = True
            flaky.requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"errors": [{"message": "Rate limit"}]})
        return original(request)
    connector = AsanaConnector({"token_env": "ASANA_TOKEN", "projects": ["1001"], "default_acl": ["group:pm"]}, transport=httpx.MockTransport(handler), sleep=slept.append, clock=lambda: NOW)
    assert len(run(connector)[0]) == 5 and slept == [2.0]


def test_401_never_shows_the_token_and_403_on_a_task_page_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("ASANA_TOKEN", "2/999/wrong-token-value-0000")
    site = standard()
    site.script = [httpx.Response(401, json={"errors": [{"message": "Not Authorized: 2/999/wrong-token-value-0000"}]})]       # a server that echoes the credential
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert "401" in message and "wrong-token-value" not in message and TOKEN not in message and "ASANA_TOKEN" in message
    assert len(site.requests) == 1 and connector.sleeps == []
    monkeypatch.setenv("ASANA_TOKEN", TOKEN)
    site = standard()
    site.script = [httpx.Response(200, json={"data": {"gid": "1001", "name": "Website Relaunch"}}), httpx.Response(401, json={"errors": [{"message": "expired"}]})]
    with pytest.raises(AuthError):
        run(make(site))                                                                           # a token revoked in the middle of a scan is not "project gone"


def test_server_errors_are_raised_and_never_echo_the_token():
    site = standard()
    site.script = [httpx.Response(200, json={"data": {"gid": "1001", "name": "Website Relaunch"}}), httpx.Response(500, text=f"boom {TOKEN}")]
    with pytest.raises(HttpError) as err:
        run(make(site))
    assert "500" in str(err.value) and TOKEN not in str(err.value) and "***" in str(err.value)
    site = standard()
    site.script = [httpx.Response(200, json={"data": {"gid": "1001", "name": "Website Relaunch"}}), httpx.Response(400, json={"errors": [{"message": "limit: bad"}]})]
    with pytest.raises(HttpError, match="400"):
        run(make(site))                                                                           # a 400 without a saved offset is a real error


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(standard()))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("ASANA_TOKEN")
    with pytest.raises(HttpError, match="ASANA_TOKEN"):
        run(make(standard()))


def test_configuration_errors_are_rejected_up_front():
    for bad in ({}, {"projects": []}, {"projects": ["Website"]}, {"projects": ["12 34"]}, {"projects": "1001"}, {"projects": ["../1"]}):
        with pytest.raises(ValueError, match="projects"):
            AsanaConnector({"token_env": "ASANA_TOKEN", **bad})
    assert AsanaConnector({"token_env": "ASANA_TOKEN", "projects": [1001, "1001", 1002]}).projects == ["1001", "1002"]


# ── permissions and open() ──


def test_acl_rules_by_project_folder_fail_closed():
    site = standard()
    rules = [{"prefix": "asana/Website Relaunch", "principals": ["group:web"]}]
    connector = make(site, projects=["1001", "1002"], acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["asana:1001/2001"].acl == ["group:web"] and by["asana:1002/3001"].acl == []
    assert connector.refresh_acl(persisted(by["asana:1001/2002"])) == ["group:web"] and connector.refresh_acl(persisted(by["asana:1002/3002"])) == []


def test_open_rebuilds_exactly_the_file_that_was_scanned_from_persisted_columns_only():
    scanned = {c.external_id: c.file for c in run(make(standard(), projects=["1001", "1002"]))[0]}
    site = standard()
    fresh = make(site)
    for ext, sf in scanned.items():
        text = read(fresh, sf)
        assert len(text.encode()) == sf.size and text.startswith(f"# {sf.name.split('-', 1)[1][:-3]}\n"), ext
    site.reset()
    read(fresh, scanned["asana:1001/2001"])
    assert [r.url.path.removeprefix("/api/1.0") for r in site.requests][:2] == ["/tasks/2001", "/tasks/2001/stories"]
    assert "project: Website Relaunch" in read(fresh, scanned["asana:1001/2001"])
    lonely = SourceFile("asana:1001/2002", "weird", "n", 0, None, "v", [])
    assert "project: 1001" in read(fresh, lonely)                                                  # no folder in the path: the project gid stands in


def test_open_of_a_task_that_is_gone_is_a_404_and_bad_ids_are_rejected_without_a_request():
    site = standard()
    connector = make(site)
    for ext in ("asana:1001/9999",):
        with pytest.raises(HttpError) as err:
            connector.open(SourceFile(ext, "asana/Website Relaunch/x.md", "n", 0, None, "v", []))
        assert err.value.status == 404
    empty = Asana()
    empty.script = [httpx.Response(200, json={"data": None})]
    with pytest.raises(HttpError) as err:
        make(empty).open(SourceFile("asana:1001/2001", "p", "n", 0, None, "v", []))
    assert err.value.status == 404
    site.reset()
    for bad in ("asana:1001", "asana:abc/2001", "asana:1001/2001/../1", "asana:1001/x", "github:1001/2001", "asana:1001/2001 "):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []
