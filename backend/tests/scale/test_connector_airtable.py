"""Airtable connector against a stateful fake of the Airtable Web API: GET /v0/{base}/{table} with `offset` pagination
(the response carries `offset` while more records remain; the same filter / view / fields must be sent with it), `filterByFormula`,
GET /v0/{base}/{table}/{record}, and GET /v0/meta/bases/{base}/tables (the schema). Records are {id, createdTime, fields}: empty
cells are absent and NO modification time is exposed, as in the real API.

The fake enforces two documented constraints: an `offset` is only valid together with the query it came from (a changed filter is
HTTP 422), and the filter formula must be one of three exact shapes (anything else is 422 INVALID_FILTER_BY_FORMULA), so a connector
that builds a different formula, or forgets the filter on page two, fails here. `sparse=True` reproduces Airtable returning short or
empty pages that still carry an `offset`.

Written from memory of the reference, not certain of: that `IS_AFTER(LAST_MODIFIED_TIME(), '<ISO>')` is accepted as written (the
formula is a config template for that reason), the `error.type` strings, how long an `offset` stays valid (a rejected saved offset
is treated as "start the table again"), and the exact JSON of rarer field types (aiText, barcode, button, formula errors).
"""

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors._objects import short_hash
from app.scale.connectors.airtable import AirtableConnector, value_text
from app.scale.connectors.base import SourceFile

TOKEN = "patABCDEF.SECRET-airtable-token-77"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
BASE = "appAAAAAAAAAAAAAA"
BASE2 = "appBBBBBBBBBBBBBB"
OLD = "2026-03-01T10:00:00.000Z"


def rid(n):
    return f"rec{n:014d}"


def tid(n):
    return f"tbl{n:014d}"


def rec(n, name, *, modified=OLD, **fields):
    return {"id": rid(n), "createdTime": "2026-01-01T00:00:00.000Z", "fields": {"Name": name, **fields}, "modified": modified}


def table(n, name, records, *, primary="Name", fields=None, base=BASE, views=("Grid view",)):
    return {"base": base, "id": tid(n), "name": name, "primary": primary, "records": list(records), "views": list(views),
            "fields": fields if fields is not None else [(primary, "singleLineText"), ("Notes", "multilineText")]}


def error(kind, message="boom"):
    return {"error": {"type": kind, "message": message}}


FORMULAS = [
    (re.compile(r"^IS_AFTER\(LAST_MODIFIED_TIME\(\), '([^']+)'\)$"), lambda r, m: r["modified"] > m.group(1)),
    (re.compile(r"^IS_AFTER\(\{([^}]+)\}, '([^']+)'\)$"), lambda r, m: r["fields"].get(m.group(1), "") > m.group(2)),
    (re.compile(r"^LAST_MODIFIED_TIME\(\) > '([^']+)'$"), lambda r, m: r["modified"] > m.group(1)),
]


class Airtable:
    def __init__(self, tables, *, page=2, sparse=False):
        self.tables = copy.deepcopy(tables)
        self.page, self.sparse = page, sparse
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []
        self.schema_response: httpx.Response | None = None       # set to make the schema endpoint fail

    # ── state ──

    def find(self, base, ref):
        return next((t for t in self.tables if t["base"] == base and ref in (t["id"], t["name"])), None)

    def edit(self, ref, record_id, modified, **fields):
        row = next(r for r in self.find(BASE, ref)["records"] if r["id"] == record_id)
        row["fields"].update(fields)
        row["modified"] = modified

    def remove(self, ref, record_id):
        t = self.find(BASE, ref)
        t["records"] = [r for r in t["records"] if r["id"] != record_id]

    def add(self, ref, record, index=None):
        t = self.find(BASE, ref)
        t["records"].insert(len(t["records"]) if index is None else index, copy.deepcopy(record))

    # ── protocol ──

    def reset(self):
        self.requests.clear()

    def lists(self, ref=None):
        out = []
        for r in self.requests:
            parts = self._parts(r)
            if len(parts) == 3 and parts[0] == "v0" and parts[1].startswith("app") and (ref is None or parts[2] in (ref, tid_of(self, ref))):
                out.append(r)
        return out

    @staticmethod
    def _parts(request):
        raw = request.url.raw_path.split(b"?")[0].decode()
        return [unquote(p) for p in raw.strip("/").split("/")]

    @staticmethod
    def _sig(q):
        return hashlib.md5(json.dumps([q.get("filterByFormula"), q.get("view"), q.get_list("fields[]")]).encode()).hexdigest()[:8]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json=error("AUTHENTICATION_REQUIRED", "Authentication required"))
        assert "api_key" not in request.url.query.decode() and "access_token" not in request.url.query.decode(), "token in the URL"
        parts = self._parts(request)
        q = request.url.params
        if parts[:3] == ["v0", "meta", "bases"]:
            return self.schema_response or self._schema(parts[3])
        base, ref = parts[1], parts[2]
        t = self.find(base, ref)
        if t is None:
            return httpx.Response(404, json=error("TABLE_NOT_FOUND", f"Could not find table {ref} in application {base}"))
        if len(parts) == 4:
            row = next((r for r in t["records"] if r["id"] == parts[3]), None)
            return httpx.Response(200, json=self._public(row)) if row else httpx.Response(404, json=error("NOT_FOUND", "Record not found"))
        return self._list(t, q)

    @staticmethod
    def _public(row, only=()):
        fields = {k: v for k, v in row["fields"].items() if not only or k in only}
        return {"id": row["id"], "createdTime": row["createdTime"], "fields": fields}

    def _schema(self, base):
        tables = []
        for t in self.tables:
            if t["base"] != base:
                continue
            ids = {name: f"fld{i:014d}" for i, (name, _) in enumerate(t["fields"], 1)}
            tables.append({"id": t["id"], "name": t["name"], "primaryFieldId": ids[t["primary"]],
                           "fields": [{"id": ids[n], "name": n, "type": kind} for n, kind in t["fields"]],
                           "views": [{"id": f"viw{i:014d}", "name": v, "type": "grid"} for i, v in enumerate(t["views"], 1)]})
        return httpx.Response(200, json={"tables": tables})

    def _matches(self, row, formula):
        if not formula:
            return True
        for pattern, test in FORMULAS:
            m = pattern.match(formula)
            if m:
                return test(row, m)
        raise ValueError(formula)

    def _list(self, t, q):
        formula = q.get("filterByFormula")
        try:
            self._matches(t["records"][0] if t["records"] else {"modified": "", "fields": {}}, formula)
        except ValueError:
            return httpx.Response(422, json=error("INVALID_FILTER_BY_FORMULA", f"The formula for filtering records is invalid: {formula}"))
        if q.get("view") and q["view"] not in t["views"]:
            return httpx.Response(422, json=error("VIEW_NAME_NOT_FOUND", "View not found"))
        size = min(int(q.get("pageSize", 100)), self.page)
        order = {r["id"]: i for i, r in enumerate(t["records"])}
        start = 0
        if q.get("offset"):
            m = re.fullmatch(r"itr(\w{8})/(rec\w{14})", q["offset"])
            if not m or m.group(1) != self._sig(q) or m.group(2) not in order:
                return httpx.Response(422, json=error("INVALID_OFFSET", "Invalid offset"))
            start = order[m.group(2)] + 1
        only = q.get_list("fields[]")
        if self.sparse:                                              # chunks of the unfiltered table, filter applied inside each
            chunk = t["records"][start:start + size]
            rows = [r for r in chunk if self._matches(r, formula)]
            more, last = start + size < len(t["records"]), (chunk[-1]["id"] if chunk else None)
        else:
            rows = [r for r in t["records"][start:] if self._matches(r, formula)]
            page = rows[:size]
            more, last, rows = len(rows) > size, (page[-1]["id"] if page else None), page
        body = {"records": [self._public(r, only) for r in rows]}
        if more:
            body["offset"] = f"itr{self._sig(q)}/{last}"
        return httpx.Response(200, json=body)


def tid_of(site, ref):
    for t in site.tables:
        if ref == t["name"]:
            return t["id"]
    return ref


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "AIRTABLE_TOKEN", "base_id": BASE, "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = AirtableConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


def ids(changes):
    return [c.external_id.rsplit(":", 1)[1] for c in changes]


def five(**kw):
    return Airtable([table(1, "Contacts", [rec(n, f"Person {n}") for n in range(1, 6)])], **kw)


# ── pagination ──


def test_records_are_paged_by_offset_until_the_response_has_none():
    site = five()
    changes, batches = run(make(site))
    assert ids(changes) == [rid(n) for n in range(1, 6)]
    lists = site.lists()
    offsets = [r.url.params.get("offset") for r in lists]
    assert offsets[0] is None and len(offsets) == 3                              # five records, two per page
    assert offsets[1].startswith("itr") and offsets[1].endswith(f"/{rid(2)}") and offsets[2].endswith(f"/{rid(4)}")     # sent back untouched
    assert all(r.url.params["pageSize"] == "2" and "filterByFormula" not in r.url.params for r in lists)
    assert [len(b.changes) for b in batches] == [2, 2, 1, 0]
    assert batches[-1].done and batches[-1].complete_scope == "" and all(not b.done and b.complete_scope is None for b in batches[:-1])


def test_page_size_is_capped_at_the_api_maximum_of_100():
    site = five(page=1000)
    run(make(site, page_size=500, tables=["Contacts"], use_schema=False))
    assert site.lists()[0].url.params["pageSize"] == "100"


def test_a_page_that_is_empty_but_carries_an_offset_does_not_end_the_scan():
    site = Airtable([table(1, "Contacts", [rec(n, f"P{n}", modified=("2026-03-10T12:30:00.000Z" if n in (1, 6) else OLD)) for n in range(1, 7)])],
                    page=2, sparse=True)
    cursor = {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60}
    changes, batches = run(make(site, clock=lambda: NOW + 3600), cursor)
    assert ids(changes) == [rid(1), rid(6)]                                      # the middle page is empty, yet its offset leads on to record 6
    assert len(site.lists()) == 3 and batches[-1].complete_scope is None
    assert [len(b.changes) for b in batches] == [1, 0, 1, 0]


def test_a_server_that_returns_the_same_offset_forever_is_an_error_not_a_loop():
    class Stuck(Airtable):
        def _list(self, t, q):
            response = super()._list(t, q)
            body = response.json()
            body["offset"] = f"itr{self._sig(q)}/{rid(2)}"
            return httpx.Response(200, json=body)

    with pytest.raises(HttpError, match="did not advance"):
        run(make(Stuck([table(1, "Contacts", [rec(n, f"P{n}") for n in range(1, 9)])])))


def test_pagination_is_stable_when_a_record_is_inserted_between_pages():
    site = five()
    batches = []
    scan = make(site).scan({})
    batches.append(next(scan))
    site.add("Contacts", rec(9, "Inserted early"), index=0)                       # an offset is a record position, not a row number
    batches += list(scan)
    got = [i for b in batches for i in ids(b.changes)]
    assert got == [rid(n) for n in range(1, 6)]                                  # nothing repeated, nothing skipped (9 sits before the cursor)


def test_every_table_of_every_base_is_scanned_in_configuration_order():
    site = Airtable([table(1, "Contacts", [rec(n, f"C{n}") for n in (1, 2, 3)]), table(2, "Deals", [rec(n, f"D{n}") for n in (11, 12, 13)]),
                     table(3, "Other", [rec(21, "O1")], base=BASE2)])
    changes, batches = run(make(site, base_id=None, bases=[{"id": BASE, "name": "CRM", "tables": ["Deals", "Contacts"]},
                                                          {"id": BASE2, "tables": [tid(3)]}]))
    assert [c.file.path.split("/")[1:3] for c in changes] == [["CRM", "Deals"]] * 3 + [["CRM", "Contacts"]] * 3 + [[BASE2, "Other"]]
    assert {c.external_id.split(":")[1] for c in changes} == {BASE, BASE2} and batches[-1].complete_scope == ""


# ── tables, schema ──


def test_without_a_tables_list_every_table_comes_from_the_schema_and_is_addressed_by_id():
    site = Airtable([table(1, "Contacts", [rec(1, "A")]), table(2, "Deals", [rec(2, "B")]), table(3, "Elsewhere", [rec(3, "C")], base=BASE2)])
    changes, _ = run(make(site))
    assert [r.url.path for r in site.requests] == [f"/v0/meta/bases/{BASE}/tables", f"/v0/{BASE}/{tid(1)}", f"/v0/{BASE}/{tid(2)}"]
    assert [c.external_id for c in changes] == [f"airtable:{BASE}:{tid(1)}:{rid(1)}", f"airtable:{BASE}:{tid(2)}:{rid(2)}"]
    assert [c.file.path.split("/")[2] for c in changes] == ["Contacts", "Deals"]


def test_the_schema_is_optional_when_tables_are_configured_even_if_the_token_may_not_read_it():
    site = five()
    site.schema_response = httpx.Response(403, json=error("INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "Invalid permissions"))
    changes, _ = run(make(site, tables=["Contacts"]))
    assert len(changes) == 5 and changes[0].file.path.startswith("airtable/" + BASE + "/Contacts/")
    off = five()
    run(make(off, tables=["Contacts"], use_schema=False))
    assert not any("/meta/" in r.url.path for r in off.requests)
    with pytest.raises(ValueError, match="needs a `tables` list"):
        make(off, use_schema=False)


def test_a_table_given_by_id_is_named_after_the_schema_and_a_table_given_by_name_keeps_its_name():
    site = five()
    by_id, _ = run(make(site, tables=[tid(1)]))
    assert by_id[0].file.path.split("/")[2] == "Contacts" and by_id[0].external_id.startswith(f"airtable:{BASE}:{tid(1)}:")
    site.schema_response = httpx.Response(404, json=error("NOT_FOUND"))
    bare, _ = run(make(site, tables=[tid(1)]))
    assert bare[0].file.path.split("/")[2] == tid(1)                                  # no schema: the id is all there is


def test_a_schema_that_is_needed_but_refused_is_a_clear_error_naming_the_scope():
    site = five()
    site.schema_response = httpx.Response(403, json=error("INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "Invalid permissions"))
    with pytest.raises(AuthError, match="schema.bases:read"):
        run(make(site))


def test_table_names_with_spaces_slashes_colons_and_url_characters_round_trip():
    names = ["Q1 Plan & Roadmap #2?", "Plan: 2026/H1"]
    site = Airtable([table(1, names[0], [rec(1, "x")]), table(2, names[1], [rec(2, "y")])])
    connector = make(site, tables=names, use_schema=False)
    changes, _ = run(connector)
    for change in changes:
        assert change.external_id.count(":") == 3                                         # ':' inside a name is escaped
    assert [Airtable._parts(r)[2] for r in site.lists()] == names                          # and the server sees the real names
    assert any(b"%23" in r.url.raw_path for r in site.lists())                              # '#' and '?' cannot leak into the URL structure
    assert read(make(site, tables=names, use_schema=False), changes[1].file).startswith("# y\n")


def test_view_and_field_restrictions_are_sent_on_every_page():
    site = Airtable([table(1, "Contacts", [rec(n, f"P{n}", Notes=f"n{n}", Secret="s") for n in range(1, 4)])])
    changes, _ = run(make(site, tables=[{"table": "Contacts", "view": "Grid view", "fields": ["Name", "Notes"]}]))
    assert all(r.url.params["view"] == "Grid view" and r.url.params.get_list("fields[]") == ["Name", "Notes"] for r in site.lists())
    assert len(site.lists()) == 2 and len(changes) == 3
    with pytest.raises(HttpError, match="VIEW_NAME_NOT_FOUND"):
        run(make(site, tables=[{"table": "Contacts", "view": "Nope"}]))


def test_configuration_errors_are_raised_early():
    for bad in ({"base_id": None}, {"base_id": "base123"}, {"base_id": BASE, "tables": [{"view": "x"}]}):
        with pytest.raises(ValueError):
            make(five(), **bad)


# ── rendering ──


CONTACT_FIELDS = [("Name", "singleLineText"), ("Email", "email"), ("Notes", "multilineText"), ("Status", "singleSelect"),
                  ("Tags", "multipleSelects"), ("Owner", "singleCollaborator"), ("Files", "multipleAttachments"),
                  ("Company", "multipleRecordLinks"), ("Active", "checkbox"), ("Score", "number"), ("Revenue", "currency"),
                  ("Edited", "lastModifiedTime"), ("Open", "button"), ("Summary", "aiText"), ("Code", "barcode"),
                  ("Total", "formula"), ("Broken", "formula"), ("Cities", "multipleLookupValues"), ("Empty", "singleLineText")]


def rich_record(**over):
    fields = {
        "Score": 42, "Notes": "line one\nline two", "Email": "ada@acme.com", "Status": "Customer", "Tags": ["vip", "emea"],
        "Owner": {"id": "usrAAAAAAAAAAAAAA", "email": "sam@acme.com", "name": "Sam Owner"},
        "Files": [{"id": "attAAAAAAAAAAAAAA", "url": "https://v5.airtableusercontent.com/v3/u/abc/SIGNED?x=1", "filename": "contract.pdf",
                   "size": 1000, "type": "application/pdf", "thumbnails": {"small": {"url": "https://v5.airtableusercontent.com/SIGNED2"}}}],
        "Company": [rid(77)], "Active": True, "Revenue": 1234.5, "Edited": "2026-03-01T10:00:00.000Z",
        "Open": {"label": "Open record", "url": "https://airtable.com/x"},
        "Summary": {"state": "generated", "value": "Ada is a long-standing customer.", "isStale": False},
        "Code": {"text": "AB-123", "type": "code128"}, "Total": 7, "Broken": {"error": "#ERROR!"}, "Cities": ["London", "Paris"], "Empty": "",
    }
    fields.update(over)
    return rec(1, "Ada Lovelace", **fields)


def contacts(*records, fields=CONTACT_FIELDS):
    return Airtable([table(1, "Contacts", list(records) or [rich_record()], fields=fields)])


def test_a_record_is_rendered_as_labelled_fields_in_schema_order_with_the_primary_field_as_title():
    site = contacts()
    connector = make(site, base_id=None, bases=[{"id": BASE, "name": "CRM"}])
    change = run(connector)[0][0]
    text = read(connector, change.file)
    assert text == (
        "# Ada Lovelace\n\n"
        f"base: CRM\ntable: Contacts\nrecord: {rid(1)}\ncreated: 2026-01-01T00:00:00.000Z\n\n"
        "Email: ada@acme.com\n"
        "Notes:\nline one\nline two\n\n"
        "Status: Customer\nTags: vip, emea\nOwner: Sam Owner (sam@acme.com)\nFiles: contract.pdf\nActive: yes\n"
        "Score: 42\nRevenue: 1234.5\nEdited: 2026-03-01T10:00:00.000Z\nSummary: Ada is a long-standing customer.\n"
        "Code: AB-123\nTotal: 7\nCities: London, Paris\n")
    assert "Company" not in text and "Open" not in text and "Broken" not in text and "Empty" not in text   # links, buttons, errors, blanks
    assert "SIGNED" not in text and "airtableusercontent" not in text                  # expiring attachment URLs never reach the index
    assert change.file.name == f"Ada Lovelace-{rid(1)}.md" and change.file.path == f"airtable/CRM/Contacts/Ada Lovelace-{rid(1)}.md"
    assert change.file.mime == "text/markdown" and change.file.modified_at is None


def test_size_and_version_describe_exactly_the_bytes_open_returns():
    site = contacts()
    connector = make(site)
    change = run(connector)[0][0]
    data = read(make(contacts()), change.file).encode()
    assert change.file.size == len(data) and change.file.version == short_hash(data.decode(), size=16)


def test_without_a_schema_fields_keep_the_order_airtable_sent_and_the_title_is_the_configured_or_first_text_field():
    site = contacts()
    connector = make(site, tables=["Contacts"], use_schema=False)
    change = run(connector)[0][0]
    text = read(connector, change.file)
    assert text.startswith("# Ada Lovelace\n") and text.index("Score: 42") < text.index("Email: ada@acme.com")
    assert "Company" not in text                                                      # a list of record ids is recognised without the schema
    assert "table: Contacts" in text
    titled = make(site, tables=[{"table": "Contacts", "title_field": "Email"}], use_schema=False)
    other = read(titled, run(titled)[0][0].file)
    assert other.startswith("# ada@acme.com\n") and "Email:" not in other and "Name: Ada Lovelace" in other


def test_untitled_records_fall_back_to_the_record_id_and_long_or_multiline_titles_are_cut():
    site = Airtable([table(1, "Contacts", [{"id": rid(1), "createdTime": OLD, "fields": {"Notes": "only notes"}, "modified": OLD},
                                          rec(2, "First line\nsecond line " + "x" * 300)])])
    changes, _ = run(make(site))
    first = read(make(site), changes[0].file)
    assert first.startswith(f"# {rid(1)}\n") and "Notes: only notes" in first and changes[0].file.name == f"{rid(1)}-{rid(1)}.md"
    second = changes[1].file
    assert read(make(site), second).startswith("# First line\n") and len(second.name) < 200


def test_value_text_handles_every_documented_value_shape():
    assert value_text(None) == "" and value_text(True) == "yes" and value_text(False) == "no"
    assert value_text(3) == "3" and value_text(3.0) == "3" and value_text(2.5) == "2.5" and value_text(0) == "0"
    assert value_text(["a", "", "b"]) == "a, b" and value_text([]) == ""
    assert value_text([rid(1), rid(2)]) == "" and value_text([rid(1), "x"]) == f"{rid(1)}, x"
    assert value_text({"id": "usr", "email": "a@b.c"}) == "a@b.c" and value_text({"id": "usr", "name": "Ada", "email": "a@b.c"}) == "Ada (a@b.c)"
    assert value_text({"specialValue": "NaN"}) == "" and value_text({"error": "#ERROR!"}) == "" and value_text({"unknown": 1}) == ""
    assert value_text([{"filename": "a.pdf", "url": "https://signed"}, {"filename": "b.png"}]) == "a.pdf, b.png"
    assert value_text([[1, 2], [3]]) == "1, 2, 3"


def test_version_changes_with_the_content_only():
    site = contacts()
    first = run(make(site))[0][0].file.version
    assert run(make(site))[0][0].file.version == first
    site.edit("Contacts", rid(1), "2026-03-02T00:00:00.000Z", Score=43)
    assert read(make(site), run(make(site))[0][0].file) != ""
    assert run(make(site))[0][0].file.version != first
    rotated = contacts()
    rotated.tables[0]["records"][0]["fields"]["Files"][0]["url"] = "https://v5.airtableusercontent.com/v3/u/abc/ROTATED?x=2"
    assert run(make(rotated))[0][0].file.version == first                              # a new signed URL is not a new version


def test_modified_field_gives_modified_at_and_replaces_last_modified_time_in_the_incremental_filter():
    site = contacts()
    connector = make(site, modified_field="Edited")
    change = run(connector)[0][0]
    assert change.file.modified_at == datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    site.reset()
    run(make(site, modified_field="Edited", clock=lambda: NOW + 3600), {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})
    assert site.lists()[0].url.params["filterByFormula"] == "IS_AFTER({Edited}, '2026-03-10T11:50:00.000Z')"


# ── open() ──


def test_open_fetches_the_single_record_and_reproduces_what_the_scan_described():
    site = contacts(rich_record(), rec(2, "Grace Hopper", Score=1))
    scanned = {c.external_id: c.file for c in run(make(site))[0]}
    fresh_site = contacts(rich_record(), rec(2, "Grace Hopper", Score=1))
    fresh = make(fresh_site)
    for ext, sf in scanned.items():
        text = read(fresh, sf)
        assert len(text.encode()) == sf.size and text.startswith("# ")
    gets = [r for r in fresh_site.requests if len(Airtable._parts(r)) == 4]
    assert [Airtable._parts(r)[3] for r in gets] == [rid(1), rid(2)]
    assert len([r for r in fresh_site.requests if "/meta/" in r.url.path]) == 1       # the schema is fetched once and cached


def test_open_rejects_ids_that_are_not_record_ids_and_reports_a_record_that_is_gone():
    site = five()
    connector = make(site)
    site.reset()
    for bad in ("airtable:x", f"airtable:{BASE}:Contacts", f"airtable:{BASE}:Contacts:rec1", "airtable:app1:Contacts:" + rid(1),
                f"airtable:{BASE}::{rid(1)}", f"other:{BASE}:Contacts:{rid(1)}", f"airtable:{BASE}:../x:{rid(1)}:extra"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile(f"airtable:{BASE}:Contacts:{rid(99)}", "p", "n", 0, None, "v", []))
    assert err.value.status == 404
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile(f"airtable:{BASE}:Nope:{rid(1)}", "p", "n", 0, None, "v", []))
    assert err.value.status == 404 and "Nope" in str(err.value) and TOKEN not in str(err.value)


# ── incremental ──


def edited_site():
    records = [rec(n, f"Person {n}", modified=OLD) for n in range(1, 8)]
    return Airtable([table(1, "Contacts", records)])


def test_incremental_scan_asks_only_for_records_modified_since_the_watermark_minus_the_overlap_on_every_page():
    site = edited_site()
    _, batches = run(make(site))
    cursor = batches[-1].cursor
    assert cursor["since"] == "2026-03-10T12:00:00Z" and json.loads(json.dumps(cursor)) == cursor and TOKEN not in json.dumps(cursor)
    for n, when in ((2, "2026-03-10T12:30:00.000Z"), (4, "2026-03-10T12:45:00.000Z"), (6, "2026-03-10T11:55:00.000Z")):      # 11:55 is inside the overlap
        site.edit("Contacts", rid(n), when, Notes="changed")
    site.edit("Contacts", rid(7), "2026-03-10T11:40:00.000Z", Notes="older than the window")
    site.reset()
    changes, inc = run(make(site, clock=lambda: NOW + 3600), cursor)
    assert ids(changes) == [rid(2), rid(4), rid(6)]
    formulas = {r.url.params["filterByFormula"] for r in site.lists()}
    assert formulas == {"IS_AFTER(LAST_MODIFIED_TIME(), '2026-03-10T11:50:00.000Z')"} and len(site.lists()) == 2     # the same filter on page two
    assert site.lists()[1].url.params["offset"]
    assert inc[-1].done and inc[-1].complete_scope is None and inc[-1].cursor["since"] == "2026-03-10T13:00:00Z"
    assert inc[-1].cursor["last_full"] == cursor["last_full"]


def test_incremental_scan_cannot_see_deletions_but_a_full_scan_is_authoritative_and_lists_only_survivors():
    site = edited_site()
    _, batches = run(make(site))
    cursor = batches[-1].cursor
    site.remove("Contacts", rid(3))
    site.reset()
    changes, inc = run(make(site, clock=lambda: NOW + 3600), cursor)
    assert changes == [] and all(c.kind == "upsert" for c in changes) and inc[-1].complete_scope is None
    site.reset()
    changes, full = run(make(site, clock=lambda: NOW + 30 * 3600), cursor)               # full_resync_hours (24) has passed
    assert rid(3) not in ids(changes) and len(changes) == 6 and full[-1].complete_scope == ""
    assert all("filterByFormula" not in r.url.params for r in site.lists())
    assert full[-1].cursor["last_full"] == NOW + 30 * 3600


def test_the_incremental_formula_is_a_template_for_tenants_where_the_default_comparison_is_not_accepted():
    site = edited_site()
    site.edit("Contacts", rid(5), "2026-03-10T12:30:00.000Z", Notes="x")
    changes, _ = run(make(site, clock=lambda: NOW + 3600, incremental_formula="LAST_MODIFIED_TIME() > '{since}'"),
                     {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})
    assert ids(changes) == [rid(5)]
    assert site.lists()[0].url.params["filterByFormula"] == "LAST_MODIFIED_TIME() > '2026-03-10T11:50:00.000Z'"
    bad = edited_site()
    with pytest.raises(HttpError, match="INVALID_FILTER_BY_FORMULA"):
        run(make(bad, clock=lambda: NOW + 3600, incremental_formula="WHEN_MODIFIED() > '{since}'"), {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})


def test_overlap_minutes_is_configurable():
    site = edited_site()
    run(make(site, clock=lambda: NOW + 3600, overlap_minutes=30), {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})
    assert "'2026-03-10T11:30:00.000Z'" in site.lists()[0].url.params["filterByFormula"]


# ── resume ──


def test_resume_in_the_middle_of_a_table_continues_at_the_saved_offset():
    site = five()
    batches = list(make(site).scan({}))
    first = batches[0]
    assert first.cursor["pos"]["table"] == f"{BASE}/{tid(1)}" and first.cursor["pos"]["offset"].endswith(f"/{rid(2)}")
    assert json.loads(json.dumps(first.cursor)) == first.cursor and TOKEN not in json.dumps(first.cursor)
    site.reset()
    changes, resumed = run(make(site), first.cursor)
    assert ids(changes) == [rid(3), rid(4), rid(5)] and resumed[-1].done and resumed[-1].complete_scope is None
    assert site.lists()[0].url.params["offset"] == first.cursor["pos"]["offset"] and len(site.lists()) == 2


def test_resume_between_tables_skips_the_ones_that_are_finished():
    site = Airtable([table(1, "Contacts", [rec(n, f"C{n}") for n in (1, 2)]), table(2, "Deals", [rec(n, f"D{n}") for n in (11, 12)])])
    batches = list(make(site).scan({}))
    boundary = next(b for b in batches if b.cursor.get("pos", {}).get("table") == f"{BASE}/{tid(2)}")
    assert "offset" not in boundary.cursor["pos"]
    site.reset()
    changes, _ = run(make(site), boundary.cursor)
    assert ids(changes) == [rid(11), rid(12)] and [Airtable._parts(r)[2] for r in site.lists()] == [tid(2)]


def test_a_cursor_saved_after_the_last_page_goes_straight_to_the_end_without_deleting():
    site = five()
    batches = list(make(site).scan({}))
    assert batches[-2].cursor["pos"] == {"finished": True}
    site.reset()
    changes, resumed = run(make(site), batches[-2].cursor)
    assert changes == [] and site.lists() == [] and resumed[-1].done and resumed[-1].complete_scope is None
    assert resumed[-1].cursor["since"] == "2026-03-10T12:00:00Z"


def test_a_saved_offset_that_airtable_no_longer_accepts_restarts_that_table_without_deleting():
    site = five()
    first = list(make(site).scan({}))[0]
    stale = dict(first.cursor, pos=dict(first.cursor["pos"], offset="itr00000000/" + rid(2)))        # UNSURE how Airtable words this (422)
    site.reset()
    changes, resumed = run(make(site), stale)
    assert ids(changes) == [rid(n) for n in range(1, 6)] and resumed[-1].complete_scope is None
    assert [r.url.params.get("offset") is None for r in site.lists()][:2] == [False, True]


def test_a_422_that_is_not_about_a_resumed_offset_is_not_swallowed():
    site = five()
    site.script = [httpx.Response(422, json=error("INVALID_REQUEST_UNKNOWN", "nope"))]
    with pytest.raises(HttpError) as err:
        run(make(site, tables=["Contacts"], use_schema=False))
    assert err.value.status == 422


def test_a_cursor_for_a_table_that_is_no_longer_configured_starts_from_the_first_table():
    site = five()
    changes, _ = run(make(site), {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"table": f"{BASE}/tblGONEGONEGONEXX", "offset": "itrXXXXXXXX/recX"}})
    assert ids(changes) == [rid(n) for n in range(1, 6)] and site.lists()[0].url.params.get("offset") is None       # the stale offset was ignored


def test_max_items_and_max_pages_truncate_without_deleting_or_advancing_the_watermark():
    site = five()
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and batches[-1].complete_scope is None and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=2, tables=["Contacts"], use_schema=False))
    assert len(site.lists()) == 2 and len(changes) == 4 and batches[-1].complete_scope is None


def test_resuming_from_a_batch_that_was_cut_short_re_reads_that_page():
    site = five()
    batches = list(make(site, max_items=3).scan({}))
    cut = batches[1]                                                                   # page two held records 3 and 4; only 3 fit
    assert ids(cut.changes) == [rid(3)] and cut.cursor["pos"]["offset"].endswith(f"/{rid(2)}")
    site.reset()
    changes, resumed = run(make(site), cut.cursor)
    assert ids(changes) == [rid(3), rid(4), rid(5)] and resumed[-1].complete_scope is None


def test_max_items_that_ends_exactly_at_the_last_record_is_still_an_authoritative_scan():
    changes, batches = run(make(five(), max_items=5))
    assert len(changes) == 5 and batches[-1].complete_scope == "" and "since" in batches[-1].cursor


# ── permissions ──


def test_acl_rules_by_base_and_table_prefix_longest_wins_and_unmatched_content_fails_closed():
    site = Airtable([table(1, "Contacts", [rec(1, "A")]), table(2, "Salaries", [rec(2, "B")]), table(3, "Notes", [rec(3, "C")])])
    rules = [{"prefix": "airtable/CRM", "principals": ["group:sales"]}, {"prefix": "airtable/CRM/Salaries", "principals": ["group:hr"]}]
    connector = make(site, base_id=None, bases=[{"id": BASE, "name": "CRM", "tables": ["Contacts", "Salaries"]}], acl_rules=rules, default_acl=None)
    by = {c.file.path.split("/")[2]: c.file for c in run(connector)[0]}
    assert by["Contacts"].acl == ["group:sales"] and by["Salaries"].acl == ["group:hr"]
    assert connector.refresh_acl(persisted(by["Salaries"])) == ["group:hr"]
    other = make(site, base_id=None, bases=[{"id": BASE, "name": "Private", "tables": ["Notes"]}], acl_rules=rules, default_acl=None)
    note = run(other)[0][0].file
    assert note.acl == [] and other.refresh_acl(persisted(note)) == []                    # no rule, no default: nobody can read it
    assert run(make(site, tables=["Notes"], default_acl=["group:ops"]))[0][0].file.acl == ["group:ops"]


# ── http behaviour ──


def test_429_is_retried_honouring_retry_after_and_backing_off_without_it():
    site = five()
    site.script = [httpx.Response(429, headers={"Retry-After": "30"}, json=error("RATE_LIMIT_REACHED", "Rate limit reached")),
                   httpx.Response(429, json=error("RATE_LIMIT_REACHED", "Rate limit reached"))]
    connector = make(site, tables=["Contacts"], use_schema=False)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [30.0, 2]            # Retry-After first, then the exponential default for attempt 1


def test_401_is_a_clear_error_that_names_the_variable_and_never_shows_the_token(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "patWRONG.value-1234567")
    site = five()
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert "401" in message and "AIRTABLE_TOKEN" in message and "AUTHENTICATION_REQUIRED" in message
    assert "patWRONG.value-1234567" not in message and TOKEN not in message
    assert len(site.requests) == 1 and connector.sleeps == []


def test_a_server_that_echoes_the_credential_does_not_get_it_into_the_error(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", "patECHO.secret-7654321")
    site = five()
    site.script = [httpx.Response(401, json=error("AUTHENTICATION_REQUIRED", "bad Bearer patECHO.secret-7654321"))]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert "patECHO.secret-7654321" not in str(err.value) and "***" in str(err.value)
    site = five()
    site.script = [httpx.Response(500, json=error("SERVER", "boom patECHO.secret-7654321"))] * 10
    with pytest.raises(HttpError) as err:
        run(make(site, tables=["Contacts"], use_schema=False, ))
    assert "patECHO.secret-7654321" not in str(err.value)


def test_403_explains_the_scopes_and_is_not_retried():
    site = five()
    site.script = [httpx.Response(403, json=error("INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND", "Invalid permissions"))]
    connector = make(site, tables=["Contacts"], use_schema=False)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert err.value.status == 403 and "data.records:read" in str(err.value) and connector.sleeps == [] and len(site.requests) == 1


def test_a_table_that_cannot_be_found_stops_the_scan_naming_it_and_deletes_nothing():
    site = five()
    with pytest.raises(HttpError) as err:
        run(make(site, tables=["Contacts", "Vanished"], use_schema=False))
    assert err.value.status == 404 and "Vanished" in str(err.value) and "TABLE_NOT_FOUND" in str(err.value)


def test_a_missing_token_variable_is_named_and_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("AIRTABLE_TOKEN")
    site = five()
    with pytest.raises(HttpError, match="AIRTABLE_TOKEN"):
        run(make(site))
    assert site.requests == []


def test_cursors_never_contain_the_secret_and_the_token_never_travels_in_the_url():
    site = five()
    _, batches = run(make(site))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    assert all(TOKEN not in str(r.url) for r in site.requests) and all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in site.requests)
