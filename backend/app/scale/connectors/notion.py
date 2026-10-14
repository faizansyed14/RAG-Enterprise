"""Notion pages and database rows as virtual files, one Markdown file per page.

Endpoints (Notion API reference: "Search", "Query a database", "Retrieve block children", "Retrieve a page",
"Pagination", "Versioning"; every request carries `Notion-Version`, pinned to 2022-06-28 by default):

  POST /v1/search  {"filter": {"property": "object", "value": "database"}, "page_size": 100, "start_cursor": c}
        lists the databases shared with the integration (cursor pagination: `has_more` / `next_cursor`).
  POST /v1/databases/{id}/query  {"page_size": 100, "start_cursor": c, "filter": {"timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": ...}}}      rows of each database; the filter is used for incremental scans.
  POST /v1/search  {"filter": {"property": "object", "value": "page"}, "sort": {"direction": "descending",
        "timestamp": "last_edited_time"}, "page_size": 100, "start_cursor": c}
        every other page. Newest first, so an incremental scan simply stops at the first page older than the watermark.
  GET  /v1/blocks/{id}/children?page_size=100&start_cursor=c      recursively, on open() only (a scan stays cheap).
  GET  /v1/pages/{id} | /v1/databases/{id}                        titles of ancestors (path) and the page itself on open().

Config: token_env (the integration secret), notion_version, page_size (100), include_databases (true),
resolve_paths (true), max_path_depth (6), max_block_depth (8), max_blocks_per_page (5000), max_items, max_pages,
full_resync_hours (24), overlap_minutes (10), base_url, acl_rules, default_acl.

Objects: external_id `notion:<page id>`, version = the page's `last_edited_time` (Notion rounds it to the minute),
path `notion/<top-level page or database>/<...>/<Title>-<first 8 of id>.md` (titles are not unique in Notion, ids
are). A page sits in the folder of its parent, and its own children live in a folder named after it; a top-level
page is its own folder. So `notion/Wiki` covers the whole tree of the top-level page "Wiki", and `notion/Wiki/Runbooks`
covers the children of "Runbooks" (use the glob `notion/Wiki/Runbooks*` to include the page itself). Rich text is converted to Markdown; properties of database rows become `name: value` lines; headings, lists,
to-dos, toggles, quotes, callouts, code, tables, columns and synced blocks keep their structure. Signed file URLs
(which expire and carry credentials in the query string) are never written into the text.

Deletions: a page that is archived or whose access was revoked disappears from search. A full scan (the first one,
and one every `full_resync_hours`) is authoritative for the whole connection; an incremental one is not. (Until the
sync layer persists the cursor of a finished scan, every scan is a full one.)

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path; the first folder is the
top-level page or database the content lives under; no match and no `default_acl` => nobody). This is not Notion's
own sharing model. The integration sees only what was shared with it, which is the first line of control.

What this does NOT do: comments, page history, file/image contents, relations / rollups (the related titles),
Notion-Version 2025-09-03 "data sources" (databases are queried the classic way), webhooks.

Not exercised against a live Notion workspace; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): the exact `parent` object of pages inside synced / toggle blocks.
"""

from __future__ import annotations

import io
import re
import time
from datetime import timedelta
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, parse_dt, redacted
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")


# ───────────────────────────── rich text, properties, blocks ─────────────────────────────


def _wrap(text: str, mark: str) -> str:
    lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    core = text.strip()
    return f"{lead}{mark}{core}{mark}{trail}" if core else text


def rich_text(items: list[dict] | None) -> str:
    out: list[str] = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        if not text:
            continue
        ann = item.get("annotations") or {}
        if item.get("type") == "equation":
            text = f"${text}$"
        if ann.get("code"):
            text = _wrap(text, "`")
        if ann.get("bold"):
            text = _wrap(text, "**")
        if ann.get("italic"):
            text = _wrap(text, "*")
        if ann.get("strikethrough"):
            text = _wrap(text, "~~")
        href = item.get("href") or ((item.get("text") or {}).get("link") or {}).get("url")
        if href and href.startswith(("http://", "https://", "mailto:")) and text.strip():
            core = text.strip()
            text = text.replace(core, f"[{core}]({href})", 1)
        out.append(text)
    return "".join(out)


def _date(value: dict | None) -> str:
    if not value or not value.get("start"):
        return ""
    return value["start"] + (f" -> {value['end']}" if value.get("end") else "")


def property_text(prop: dict) -> str:
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("rich_text", "title"):
        return rich_text(value)
    if kind in ("number", "url", "email", "phone_number", "checkbox", "created_time", "last_edited_time"):
        return "" if value is None else ("yes" if value is True else "no" if value is False else str(value))
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(v.get("name", "") for v in value or [])
    if kind == "date":
        return _date(value)
    if kind == "people":
        return ", ".join(p.get("name") or p.get("id", "") for p in value or [])
    if kind in ("created_by", "last_edited_by"):
        return (value or {}).get("name", "")
    if kind == "files":
        return ", ".join(f.get("name", "") for f in value or [])
    if kind == "formula":
        inner = value or {}
        result = inner.get(inner.get("type"))
        return _date(result) if inner.get("type") == "date" else ("" if result is None else str(result))
    if kind == "unique_id":
        return f"{(value or {}).get('prefix') or ''}{'-' if (value or {}).get('prefix') else ''}{(value or {}).get('number', '')}"
    return ""        # relation, rollup, verification, button: ids or computed values we cannot show faithfully


def _title_of(obj: dict) -> str:
    if isinstance(obj.get("title"), list):             # a database object carries its title at the top level
        return rich_text(obj["title"]).strip()
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return rich_text(prop.get("title")).strip()
    return ""


def _plain_url(block: dict, kind: str) -> str:
    data = block.get(kind) or {}
    caption = rich_text(data.get("caption")).strip()
    if data.get("type") == "external":
        url = (data.get("external") or {}).get("url", "").split("?", 1)[0]
        return f"[{kind}: {caption or url}]({url})" if url else f"[{kind}: {caption}]"
    name = data.get("name") or ""
    return f"[{kind}: {caption or name or 'file'}]"      # Notion-hosted files have expiring signed URLs: keep out


# ───────────────────────────── connector ─────────────────────────────


class NotionConnector:
    kind = "notion"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.version = config.get("notion_version", "2022-06-28")
        self.page_size = min(int(config.get("page_size", 100)), 100)
        self.include_databases = bool(config.get("include_databases", True))
        self.resolve_paths = bool(config.get("resolve_paths", True))
        self.max_path_depth = int(config.get("max_path_depth", 6))
        self.max_block_depth = int(config.get("max_block_depth", 8))
        self.max_blocks = int(config.get("max_blocks_per_page", 5000))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = timedelta(minutes=float(config.get("overlap_minutes", 10)))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(config.get("base_url", "https://api.notion.com"), transport=transport, client=client, sleep=sleep)
        self._known: dict[str, dict | None] = {}

    # ── http ──

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        token = env_secret(self.cfg.get("token_env"), "Notion integration secret")
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": self.version}
        try:
            if method == "POST":
                return self.http.post_json(path, json=body or {}, headers=headers)
            return self.http.get_json(path, params=params, headers=headers)
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the integration secret in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    # ── titles and paths ──

    def _remember(self, kind: str, obj: dict) -> None:
        self._known[obj["id"]] = {"title": _title_of(obj) or "Untitled", "parent": obj.get("parent") or {}}

    def _lookup(self, kind: str, object_id: str) -> dict | None:
        if object_id not in self._known:
            try:
                self._remember(kind, self._call("GET", f"/v1/{kind}s/{object_id}"))
            except HttpError as exc:
                if exc.status not in (400, 403, 404):          # 401 and server errors still stop the scan
                    raise
                self._known[object_id] = None               # not shared with the integration: the path simply stops here
        return self._known.get(object_id)

    def _folders(self, page: dict) -> list[str]:
        chain: list[str] = []
        parent = page.get("parent") or {}
        for _ in range(self.max_path_depth):
            kind = {"page_id": "page", "database_id": "database"}.get(parent.get("type", ""))
            node = self._lookup(kind, parent[parent["type"]]) if kind else None
            if not node:
                break
            chain.append(node["title"])
            parent = node["parent"]
        return list(reversed(chain))

    def _path(self, page: dict, title: str) -> str:
        leaf = f"{safe_name(title, 'Untitled', 90)}-{page['id'].replace('-', '')[:8]}.md"
        if not self.resolve_paths:
            return f"notion/{leaf}"
        folders = self._folders(page) or [title or "Untitled"]
        return "/".join(["notion"] + [safe_name(f, "Untitled", 80) for f in folders] + [leaf])

    # ── blocks -> Markdown ──

    def _children(self, block_id: str) -> Iterator[dict]:
        cursor = None
        while True:
            params = {"page_size": self.page_size, **({"start_cursor": cursor} if cursor else {})}
            page = self._call("GET", f"/v1/blocks/{block_id}/children", params=params)
            yield from page.get("results") or []
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if not cursor:
                return

    def _render_blocks(self, block_id: str, depth: int, counter: list[int]) -> list[str]:
        lines: list[str] = []
        numbered = 0
        indent = "  " * depth
        for block in self._children(block_id):
            if counter[0] >= self.max_blocks:
                lines.append(f"{indent}(... truncated after {self.max_blocks} blocks)")
                break
            counter[0] += 1
            kind = block.get("type", "")
            data = block.get(kind) or {}
            text = rich_text(data.get("rich_text")) if isinstance(data, dict) else ""
            numbered = numbered + 1 if kind == "numbered_list_item" else 0
            nested_ok = block.get("has_children") and depth < self.max_block_depth
            child_depth = depth + 1
            line: str | None
            if kind in ("heading_1", "heading_2", "heading_3"):
                line = f"\n{'#' * int(kind[-1])} {text}\n"
            elif kind == "paragraph":
                line = text
            elif kind == "bulleted_list_item":
                line = f"- {text}"
            elif kind == "numbered_list_item":
                line = f"{numbered}. {text}"
            elif kind == "to_do":
                line = f"- [{'x' if data.get('checked') else ' '}] {text}"
            elif kind == "toggle":
                line = f"- {text}"
            elif kind == "quote":
                line = f"> {text}"
            elif kind == "callout":
                icon = (data.get("icon") or {}).get("emoji", "")
                line = f"> {icon + ' ' if icon else ''}{text}"
            elif kind == "code":
                line = f"```{data.get('language', '')}\n{text}\n```"
            elif kind == "equation":
                line = f"$${data.get('expression', '')}$$"
            elif kind == "divider":
                line = "---"
            elif kind in ("image", "file", "pdf", "video", "audio"):
                line = _plain_url(block, kind)
            elif kind in ("bookmark", "embed", "link_preview"):
                line = data.get("url", "")
            elif kind == "child_page":
                line, nested_ok = f"(subpage: {data.get('title', '')})", False
            elif kind == "child_database":
                line, nested_ok = f"(database: {data.get('title', '')})", False
            elif kind == "link_to_page":
                line = "(link to another page)"
            elif kind == "table":
                line, nested_ok = self._table(block, data), False
            elif kind in ("column_list", "column", "synced_block", "template"):
                line, child_depth = None, depth
                source = ((data.get("synced_from") or {}) if kind == "synced_block" else {}).get("block_id")
                if nested_ok or source:
                    lines.extend(self._render_blocks(source or block["id"], depth, counter))
                nested_ok = False
            else:
                line = text or None                            # breadcrumb, table_of_contents, unsupported, ...
            if line:
                lines.append("\n".join(indent + part if part.strip() else part for part in line.split("\n")))
            if nested_ok:
                lines.extend(self._render_blocks(block["id"], child_depth if kind != "quote" else depth, counter))
        return lines

    def _table(self, block: dict, data: dict) -> str:
        rows = []
        for number, row in enumerate(self._children(block["id"])):
            cells = [rich_text(cell).replace("|", "\\|").replace("\n", " ") for cell in (row.get("table_row") or {}).get("cells") or []]
            rows.append("| " + " | ".join(cells) + " |")
            if number == 0 and data.get("has_column_header"):
                rows.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n".join(rows)

    def _render(self, page: dict, *, with_body: bool = True) -> bytes:
        title = _title_of(page) or "Untitled"
        meta: dict[str, object] = {}
        for name, prop in (page.get("properties") or {}).items():
            if prop.get("type") == "title":
                continue
            value = property_text(prop)
            if value:
                meta[name] = value
        meta.update({"created": page.get("created_time"), "last edited": page.get("last_edited_time"), "url": page.get("url")})
        body = "\n".join(self._render_blocks(page["id"], 0, [0])) if with_body else ""
        return render_document(title, meta, re.sub(r"\n{3,}", "\n\n", body))

    # ── scan ──

    def _change(self, page: dict, state: ScanState) -> Change | None:
        page_id = page.get("id", "")
        if page.get("object") != "page" or not page_id:
            return None
        ext = f"notion:{page_id}"
        if page.get("archived") or page.get("in_trash"):
            return Change("delete", ext)
        self._remember("page", page)
        title = _title_of(page) or "Untitled"
        path = self._path(page, title)
        state.observe(page.get("last_edited_time"))
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=0,
            modified_at=parse_dt(page.get("last_edited_time")), version=str(page.get("last_edited_time") or ""),
            acl=self.rules.for_path(path), mime="text/markdown", extra={"url": page.get("url", "")},
        )
        return Change("upsert", ext, sf)

    def _search(self, kind: str, cursor: str | None, descending: bool = False) -> dict:
        body: dict = {"filter": {"property": "object", "value": kind}, "page_size": self.page_size}
        if cursor:
            body["start_cursor"] = cursor
        if descending:
            body["sort"] = {"direction": "descending", "timestamp": "last_edited_time"}
        return self._call("POST", "/v1/search", body=body)

    def _walk(self, state: ScanState, budget: Budget) -> Iterator[ScanBatch]:
        floor = (parse_dt(state.since) - self.overlap) if state.incremental and state.since else None
        seen: set[str] = set()
        phase = state.pos.get("phase", "db" if self.include_databases else "pages")
        if phase == "db":
            databases = state.pos.get("dbs")
            if databases is None:
                databases, cursor = [], None
                while True:
                    if not budget.allow_page(("dblist", cursor)):
                        return
                    found = self._search("database", cursor)
                    for db in found.get("results") or []:
                        if db.get("object") == "database" and not db.get("archived") and not db.get("in_trash"):
                            self._remember("database", db)
                            databases.append([db["id"], _title_of(db), db.get("parent") or {}])
                    cursor = found.get("next_cursor") if found.get("has_more") else None
                    if not cursor:
                        break
            index, cursor = int(state.pos.get("i", 0)), state.pos.get("cursor")
            while index < len(databases):
                db_id = databases[index][0]
                if not self._known.get(db_id):
                    self._known[db_id] = {"title": databases[index][1] or "Untitled", "parent": databases[index][2]}
                while True:
                    if not budget.allow_page(("db", db_id, cursor)):
                        return
                    body: dict = {"page_size": self.page_size}
                    if cursor:
                        body["start_cursor"] = cursor
                    if floor:
                        body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": floor.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
                    page = self._call("POST", f"/v1/databases/{db_id}/query", body=body)
                    rows = budget.clip(page.get("results") or [])
                    changes = [c for c in (self._change(r, state) for r in rows if r.get("id") not in seen) if c]
                    seen.update(r.get("id") for r in rows)
                    cursor = page.get("next_cursor") if page.get("has_more") else None
                    yield ScanBatch(changes, state.cursor(phase="db", dbs=databases, i=index if cursor else index + 1, cursor=cursor))
                    if not cursor:
                        break
                index += 1
            cursor = None
        else:
            cursor = state.pos.get("cursor")
        while True:
            if not budget.allow_page(("pages", cursor)):
                return
            page = self._search("page", cursor, descending=True)
            results = page.get("results") or []
            stop = False
            if floor:
                fresh = [r for r in results if (parse_dt(r.get("last_edited_time")) or floor) >= floor]
                stop = len(fresh) < len(results)               # newest first: everything after this is older still
                results = fresh
            rows = budget.clip(results)
            changes = [c for c in (self._change(r, state) for r in rows if r.get("id") not in seen) if c]
            seen.update(r.get("id") for r in rows)
            cursor = None if stop else (page.get("next_cursor") if page.get("has_more") else None)
            yield ScanBatch(changes, state.cursor(phase="pages", cursor=cursor) if cursor else state.cursor(phase="pages", finished=True))
            if not cursor:
                return

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        if not state.pos.get("finished"):
            yield from self._walk(state, budget)
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True,
                        complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        page_id = file.external_id.split(":", 1)[-1]
        if not _ID.match(page_id):
            raise HttpError(f"notion: {file.external_id!r} is not a page id")
        return io.BytesIO(self._render(self._call("GET", f"/v1/pages/{page_id}")))

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
