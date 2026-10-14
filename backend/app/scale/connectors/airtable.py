"""Airtable records as virtual files: one Markdown file per record, the fields as labelled lines.

Endpoints (Airtable Web API reference, airtable.com/developers/web/api: "List records", "Get record", "Get base schema",
"Field model", "Rate limits", "Errors"; every request carries `Authorization: Bearer <token>`):

  GET /v0/{baseId}/{tableIdOrName}?pageSize=100&offset=O&filterByFormula=F&view=V&fields[]=A&fields[]=B
        records of a table, in pages. `offset` pagination: a response that has more records carries an `offset` string, which is
        sent back unchanged (together with the same filter / view / fields) to get the next page; the last page has no `offset`.
        A page may hold fewer records than `pageSize` (or none) and still carry an `offset`. Each record is
        {"id": "rec...", "createdTime": "...", "fields": {...}}: empty cells are absent from `fields`, and the record's own
        last-modified time is NOT part of it (only a "Last modified time" field that the base owner added shows it).
  GET /v0/{baseId}/{tableIdOrName}/{recordId}       one record (the same object), used by open().
  GET /v0/meta/bases/{baseId}/tables                the table schema: table ids and names, `primaryFieldId`, fields with id /
        name / type. OPTIONAL: it needs the `schema.bases:read` scope. It supplies the table list (when `tables` is not
        configured), the folder name of a table given by id, the field order and the primary field (the record's title).
        Without it (use_schema false, or the token lacks the scope) records are rendered in the order Airtable returns the fields
        and titled by `title_field` or the first text field.

Auth: a personal access token (or an OAuth access token) in the environment variable named by `token_env`, read at every request.
Scopes: `data.records:read` always, `schema.bases:read` for the schema. The token must also be granted the base. A 401 names the
variable; a 403 (Airtable also answers 403 / 404 for a base or table the token cannot see) names the scopes.
Rate limit: 5 requests per second per base; a 429 is retried with backoff (Airtable asks clients to wait 30 seconds; `Retry-After`
is honoured when sent, otherwise the backoff reaches 16 s on the fifth attempt).

Config: token_env, base_url (`https://api.airtable.com`), EITHER base_id + tables OR bases: [{"id", "name" (label used in the path;
default the base id), "tables": [...]}]. A table is a name or id string, or {"table", "view", "fields" (only these fields),
"title_field"}; no tables => every table of the base from the schema. use_schema (true), page_size (100, the API maximum),
modified_field (the name of a "Last modified time" field: used for `modified_at` and as the incremental filter instead of
LAST_MODIFIED_TIME()), incremental_formula (template with `{since}`; default `IS_AFTER(LAST_MODIFIED_TIME(), '{since}')`),
overlap_minutes (10), full_resync_hours (24), max_items, max_pages, acl_rules, default_acl.

Objects: external_id `airtable:<base id>:<table as configured or its id, percent-encoded>:<record id>` (use table ids in the config:
a table renamed in Airtable keeps its id), path `airtable/<base label>/<table name>/<title>-<record id>.md`, version = a hash of the
rendered text (Airtable gives no record version; edits change it, the expiring attachment URLs are never rendered so they do not).
Rendering: `# <primary field>`, then `base`, `table`, `record`, `created` lines, then `Field: value` per non-empty field (multi-line
text goes on the lines below its label). Collaborators show name and e-mail, attachments their file names, select fields their
names, formulas / lookups / rollups their results (lists joined with commas), buttons and linked-record fields are left out.

Incremental: after a finished scan the cursor holds the scan's start time. The next scan asks Airtable only for records modified
after it (minus `overlap_minutes`), on every page. Airtable has NO delete feed in this API: a deleted record is invisible to an
incremental scan. A full scan (the first, and one every `full_resync_hours`) is authoritative for the whole connection
(`complete_scope=""`), which is how deletions are noticed. (Until the sync layer persists the cursor of a finished scan, every scan
is a full one.) A table that cannot be read stops the scan with an error and deletes nothing.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `airtable/CRM/Contacts` readable by
`group:sales`; no match and no `default_acl` => nobody). It is NOT Airtable's share model: the token's reach is the first line of
control, and every record of a table it can read becomes readable by whoever the rules name.

What this does NOT do: linked-record names (only ids exist in the response), comments, attachment contents, record history, the
Webhooks API (the one source of deletions), Airtable Enterprise audit data, sync tables' remote data beyond what the fields show.
LAST_MODIFIED_TIME() ignores computed fields, so an edit that only changes a formula / rollup is picked up by the next full scan.

Not exercised against a live Airtable; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): that `IS_AFTER(LAST_MODIFIED_TIME(), '<ISO>')` is accepted as written (the template is configurable),
the `error.type` strings of the error bodies, how long an `offset` stays valid (a rejected saved offset restarts that table), and the
exact JSON of the rarer field types (aiText, barcode, button).
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable, Iterator
from urllib.parse import quote, unquote

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

_BASE = re.compile(r"^app[A-Za-z0-9]{14}$")
_RECORD = re.compile(r"^rec[A-Za-z0-9]{14}$")
_TABLE_ID = re.compile(r"^tbl[A-Za-z0-9]{14}$")
SKIP_TYPES = {"multipleRecordLinks", "button"}
DEFAULT_FORMULA = "IS_AFTER(LAST_MODIFIED_TIME(), '{since}')"


# ───────────────────────────── values ─────────────────────────────


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def value_text(value) -> str:
    """One cell as plain text. Lists are joined with commas; objects by their most meaningful member."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if value and all(isinstance(v, str) and _RECORD.match(v) for v in value):
            return ""                                              # linked records: only ids exist here
        return ", ".join(t for t in (value_text(v).strip() for v in value) if t)
    if isinstance(value, dict):
        if "error" in value or "specialValue" in value:           # formula errors: #ERROR!, NaN
            return ""
        name, email = value.get("name"), value.get("email")
        if name and email:                                          # a collaborator
            return f"{name} ({email})"
        for key in ("name", "email", "filename", "text", "value", "label"):    # collaborator / attachment / barcode / aiText / button
            if value.get(key) not in (None, ""):
                return value_text(value[key])
    return ""


def _iso_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class _Table:
    base: str
    base_label: str
    ref: str                       # what the URL and the external id use: the configured string, or the id from the schema
    name: str                      # folder name
    fields: list[dict] = field(default_factory=list)       # schema fields in order; empty without a schema
    primary: str | None = None
    view: str | None = None
    only: list[str] = field(default_factory=list)
    title_field: str | None = None

    @property
    def key(self) -> str:
        return f"{self.base}/{self.ref}"


# ───────────────────────────── connector ─────────────────────────────


class AirtableConnector:
    kind = "airtable"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.bases = self._bases(config)
        self.use_schema = bool(config.get("use_schema", True))
        self.page_size = max(1, min(int(config.get("page_size", 100)), 100))
        self.modified_field = config.get("modified_field")
        default = f"IS_AFTER({{{self.modified_field}}}, '{{since}}')" if self.modified_field else DEFAULT_FORMULA
        self.formula = str(config.get("incremental_formula") or default)
        self.overlap = timedelta(minutes=float(config.get("overlap_minutes", 10)))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(config.get("base_url", "https://api.airtable.com"), transport=transport, client=client, sleep=sleep)
        self._schemas: dict[str, list[dict] | None] = {}
        if not self.use_schema and any(not b["tables"] for b in self.bases):
            raise ValueError("airtable: without the schema (use_schema false) every base needs a `tables` list")

    @staticmethod
    def _bases(config: dict) -> list[dict]:
        raw = config.get("bases") or ([{"id": config["base_id"], "tables": config.get("tables")}] if config.get("base_id") else [])
        if not raw:
            raise ValueError("airtable: configure `base_id` (and `tables`) or `bases`")
        out = []
        for entry in raw:
            base_id = str(entry.get("id", ""))
            if not _BASE.match(base_id):
                raise ValueError(f"airtable: {base_id!r} is not a base id (app + 14 characters)")
            tables = []
            for table in entry.get("tables") or []:
                spec = {"table": table} if isinstance(table, str) else dict(table)
                if not spec.get("table"):
                    raise ValueError("airtable: a table entry needs `table`")
                tables.append(spec)
            out.append({"id": base_id, "label": str(entry.get("name") or base_id), "tables": tables})
        return out

    # ── http ──

    def _call(self, path: str, params: list[tuple[str, str]] | None = None, *, context: str = "") -> dict:
        token = env_secret(self.cfg.get("token_env"), "Airtable access token")
        try:
            return self.http.get_json(path, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})  # type: ignore[return-value]
        except AuthError as exc:
            hint = (f"check the token in environment variable {self.cfg.get('token_env')}" if exc.status == 401 else
                    f"the token needs the data.records:read scope (schema.bases:read for the schema) and access to the base{context}")
            raise redacted(exc, token, hint=hint) from None
        except HttpError as exc:
            raise redacted(exc, token, hint=f"Airtable answers 404 for a base or table that does not exist or that the token cannot see{context}" if exc.status == 404 else "") from None

    # ── schema and tables ──

    def _schema(self, base_id: str, *, required: bool = False) -> list[dict] | None:
        if base_id in self._schemas:
            return self._schemas[base_id]
        found: list[dict] | None = None
        if self.use_schema or required:
            try:
                found = list(self._call(f"/v0/meta/bases/{base_id}/tables", context=f" (base {base_id})").get("tables") or [])
            except HttpError as exc:
                if required or exc.status not in (403, 404, 422):          # 401 and server errors still stop the scan
                    raise
                log.warning("airtable: the schema of %s is not readable (%s); records are rendered without it", base_id, exc.status)
        self._schemas[base_id] = found
        return found

    def _table(self, base: dict | str, spec: dict) -> _Table:
        base_id, label = (base["id"], base["label"]) if isinstance(base, dict) else (base, base)
        ref = str(spec["table"])
        match = next((t for t in self._schema(base_id) or [] if ref in (t.get("id"), t.get("name"))), None)
        fields = list((match or {}).get("fields") or [])
        primary_id = (match or {}).get("primaryFieldId")
        return _Table(
            base=base_id, base_label=label, ref=ref, name=str((match or {}).get("name") or ref), fields=fields,
            primary=next((f["name"] for f in fields if f.get("id") == primary_id), None),
            view=spec.get("view"), only=[str(x) for x in spec.get("fields") or []], title_field=spec.get("title_field"),
        )

    def _tables(self) -> list[_Table]:
        tables: list[_Table] = []
        for base in self.bases:
            if base["tables"]:
                tables.extend(self._table(base, spec) for spec in base["tables"])
            else:
                schema = self._schema(base["id"], required=True) or []
                tables.extend(self._table(base, {"table": t["id"]}) for t in schema)
        return tables

    @staticmethod
    def _url(table: _Table) -> str:
        return f"/v0/{table.base}/{quote(table.ref, safe='')}"

    # ── rendering ──

    def _title(self, table: _Table, record: dict) -> tuple[str, str | None]:
        """(title, the field it came from). `title_field` wins, then the schema's primary field, then (with neither
        configured) the first text cell; an untitled record is named by its id."""
        fields = record.get("fields") or {}
        names = [n for n in (table.title_field, table.primary) if n]
        if not names:
            names = [n for n, v in fields.items() if isinstance(v, str) and v.strip()][:1]
        for name in names:
            text = value_text(fields.get(name)).strip()
            if text:
                return text.splitlines()[0][:120], name
        return str(record.get("id", "Untitled")), None

    def _order(self, table: _Table, fields: dict) -> list[str]:
        known = [f["name"] for f in table.fields if f.get("name") in fields and f.get("type") not in SKIP_TYPES]
        skipped = {f["name"] for f in table.fields if f.get("type") in SKIP_TYPES}
        return known + [n for n in fields if n not in known and n not in skipped]

    def _render(self, table: _Table, record: dict) -> tuple[bytes, str]:
        fields = record.get("fields") or {}
        title, used = self._title(table, record)
        body: list[str] = []
        for name in self._order(table, fields):
            text = value_text(fields[name]).strip()
            if name == used or not text:
                continue
            body.append(f"{name}:\n{text}\n" if "\n" in text else f"{name}: {text}")
        meta = {"base": table.base_label, "table": table.name, "record": record.get("id"), "created": record.get("createdTime")}
        return render_document(title, meta, "\n".join(body)), title

    def _external_id(self, table: _Table, record_id: str) -> str:
        return f"airtable:{table.base}:{quote(table.ref, safe='')}:{record_id}"

    def _change(self, table: _Table, record: dict) -> Change | None:
        record_id = str(record.get("id", ""))
        if not _RECORD.match(record_id):
            return None
        text, title = self._render(table, record)
        path = f"airtable/{safe_name(table.base_label, table.base, 80)}/{safe_name(table.name, 'table', 80)}/{safe_name(title, record_id, 90)}-{record_id}.md"
        ext = self._external_id(table, record_id)
        modified = parse_dt((record.get("fields") or {}).get(self.modified_field)) if self.modified_field else None
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=modified,
            version=short_hash(text, size=16), acl=self.rules.for_path(path), mime="text/markdown",
            extra={"base": table.base, "table": table.ref, "record": record_id},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    def _list_params(self, table: _Table, offset: str | None, formula: str | None) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [("pageSize", str(self.page_size))]
        if offset:
            params.append(("offset", offset))
        if formula:
            params.append(("filterByFormula", formula))
        if table.view:
            params.append(("view", table.view))
        params += [("fields[]", name) for name in table.only]
        return params

    def _page(self, table: _Table, offset: str | None, formula: str | None, *, resumed: bool) -> tuple[dict, bool]:
        """(response, restarted). A saved offset that Airtable no longer accepts restarts the table from its first page."""
        context = f" (table {table.name!r} of base {table.base})"
        try:
            return self._call(self._url(table), self._list_params(table, offset, formula), context=context), False
        except HttpError as exc:
            if not (resumed and offset and exc.status == 422):
                raise
        log.warning("airtable: the saved offset for %s was rejected (422); reading the table from its first page", table.name)
        return self._call(self._url(table), self._list_params(table, None, formula), context=context), True

    def _walk(self, state: ScanState, budget: Budget) -> Iterator[ScanBatch]:
        tables = self._tables()
        floor = (parse_dt(state.since) - self.overlap) if state.incremental and state.since else None
        formula = self.formula.replace("{since}", _iso_ms(floor)) if floor else None
        found = next((i for i, t in enumerate(tables) if t.key == state.pos.get("table")), None)
        start = found or 0
        offset = state.pos.get("offset") if found is not None else None
        for index in range(start, len(tables)):
            table = tables[index]
            resumed = bool(offset)
            while True:
                if not budget.allow_page(("page", table.key, offset)):
                    return
                body, restarted = self._page(table, offset, formula, resumed=resumed)
                resumed = False
                if restarted:
                    offset = None
                rows = budget.clip(list(body.get("records") or []))
                changes = [c for c in (self._change(table, r) for r in rows) if c]
                following = body.get("offset") or None
                if budget.truncated:                                  # part of this page was left out: a resume must read it again
                    cursor = state.cursor(table=table.key, offset=offset)
                elif following:
                    cursor = state.cursor(table=table.key, offset=following)
                elif index + 1 < len(tables):
                    cursor = state.cursor(table=tables[index + 1].key)
                else:
                    cursor = state.cursor(finished=True)
                yield ScanBatch(changes, cursor)
                if budget.truncated:
                    return
                if not following:
                    break
                offset = following
            offset = None

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        if not state.resumed:
            state.observe(datetime.fromtimestamp(state.started, tz=timezone.utc))
        if not state.pos.get("finished"):
            yield from self._walk(state, budget)
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True,
                        complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        parts = file.external_id.split(":")
        if len(parts) != 4 or parts[0] != "airtable" or not _BASE.match(parts[1]) or not _RECORD.match(parts[3]) or not parts[2]:
            raise HttpError(f"airtable: {file.external_id!r} is not a record id")
        base_id, ref, record_id = parts[1], unquote(parts[2]), parts[3]
        base = next((b for b in self.bases if b["id"] == base_id), None)
        spec = next((s for s in (base or {}).get("tables") or [] if s["table"] == ref), {"table": ref})
        table = self._table(base or base_id, spec)
        record = self._call(f"{self._url(table)}/{record_id}", context=f" (table {table.name!r} of base {base_id})")
        return io.BytesIO(self._render(table, record)[0])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
