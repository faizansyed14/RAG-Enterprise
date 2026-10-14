"""Jira issues (Cloud and Data Center) with their comments as virtual files, one Markdown file per issue.

Endpoints (Jira Cloud platform REST API v3, "Issue search" and "Issue comments" groups; Data Center: the Jira
REST API `search` resource; Atlassian's "Advanced searching - JQL" for the query language):

  GET /rest/api/3/search/jql?jql=...&fields=...&maxResults=N&nextPageToken=T     (api_variant "token", Cloud default)
        token pagination: the response carries `nextPageToken` (absent / `isLast: true` on the last page); the
        token is sent back unchanged. The endpoint has no `total` and no `startAt`.
  GET /rest/api/{3|2}/search?jql=...&fields=...&startAt=S&maxResults=N           (api_variant "legacy"; Data Center default)
        classic pagination: `startAt` / `maxResults` / `total`.
  GET /rest/api/{v}/issue/{id}?fields=...                                        open(): re-render one issue.
  GET /rest/api/{v}/issue/{key}/comment?startAt=S&maxResults=N&orderBy=created   only when the search result holds
        fewer comments than `comment.total`.

Config: base_url, deployment ("cloud" | "datacenter"), api_variant, api_version ("3" Cloud, "2" Data Center),
auth ("basic" | "bearer"), email | email_env, token_env, jql and/or projects (one of them is required: the token
endpoint refuses unbounded queries), page_size (50), include_comments (true), include_restricted_comments (false),
include_secured_issues (false), extra_fields, max_items, max_pages, full_resync_hours (24), overlap_minutes (1440),
acl_rules, default_acl.

Objects: external_id `jira:<numeric issue id>` (an id survives moving the issue to another project), path
`jira/<PROJECT>/<KEY>-<summary>.md`, version `<fields.updated>-<hash of the rendered text>`.
ADF (Atlassian Document Format) descriptions and comments are converted to Markdown text by `adf_to_text`;
Data Center / API v2 bodies are wiki-markup strings and are indexed as written.

Incremental: after a finished scan the cursor holds the newest `updated` seen; the next scan appends
`updated >= "yyyy-MM-dd HH:mm"` (minus `overlap_minutes`, because JQL date literals are read in the searching
user's time zone) to the JQL. Jira has no feed of deleted issues, so every `full_resync_hours` the scan is a full
enumeration; only that one declares unseen issues deleted. (Until the sync layer persists the cursor of a finished
scan, every scan is a full one.)

Fail-closed choices that go beyond the path rules: comments with a `visibility` restriction (role / group) and
Jira Service Management internal comments (`jsdPublic: false`) are NOT indexed, and issues that carry an issue
security level are skipped (and reported as deleted), unless the matching `include_*` option is set.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. everything under
`jira/HR` is readable by `group:hr`; no match and no `default_acl` => nobody). This is not Jira's permission
scheme: a project whose issues are visible to different people needs one connection per audience or has to stay
out of the index.

What this does NOT do: attachments, worklogs, changelog/history, issue links, custom fields (use `extra_fields`
for simple ones; they are listed as `name: value` only when they are strings or numbers).

Not exercised against a live Jira; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): whether every Data Center version returns `comment.total` in search results.
"""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, basic_auth, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

_PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ORDER_BY = re.compile(r"\border\s+by\b.*$", re.I | re.S)
_BASE_FIELDS = ["summary", "description", "issuetype", "status", "priority", "assignee", "reporter", "labels",
                "components", "fixVersions", "created", "updated", "resolution", "parent", "project", "security",
                "comment"]


# ───────────────────────────── ADF -> text ─────────────────────────────


def _marked(text: str, marks: list[dict]) -> str:
    for mark in marks or []:
        kind = mark.get("type")
        if kind == "code":
            text = f"`{text}`"
        elif kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "strike":
            text = f"~~{text}~~"
        elif kind == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            if href.startswith(("http://", "https://", "mailto:")) and text.strip() != href:
                text = f"[{text}]({href})"
    return text


def _inline(nodes: list[dict]) -> str:
    out: list[str] = []
    for node in nodes or []:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            out.append(_marked(node.get("text", ""), node.get("marks") or []))
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            out.append(attrs.get("text") or "@user")
        elif kind == "emoji":
            out.append(attrs.get("text") or attrs.get("shortName") or "")
        elif kind in ("inlineCard", "blockCard"):
            out.append(attrs.get("url") or "")
        elif kind == "date":
            try:
                out.append(datetime.fromtimestamp(int(attrs["timestamp"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
            except (KeyError, ValueError, TypeError):
                pass
        elif kind == "status":
            out.append(f"[{attrs.get('text', '')}]")
        elif kind in ("media", "mediaInline"):
            out.append(f"[attachment: {attrs.get('alt') or attrs.get('id') or 'file'}]")
        else:
            out.append(_inline(node.get("content") or []))
    return "".join(out)


def _list_lines(node: dict, depth: int) -> list[str]:
    ordered = node.get("type") == "orderedList"
    start = int((node.get("attrs") or {}).get("order", 1))
    lines: list[str] = []
    for index, item in enumerate(node.get("content") or []):
        marker = f"{start + index}. " if ordered else "- "
        first = True
        for child in item.get("content") or []:
            if child.get("type") in ("bulletList", "orderedList"):
                lines.extend(_list_lines(child, depth + 1))
                continue
            text = _block(child, depth + 1).replace("\n", " ") if child.get("type") != "codeBlock" else _block(child, depth + 1)
            lines.append(("  " * depth + marker + text) if first else ("  " * (depth + 1) + text))
            first = False
        if first:
            lines.append("  " * depth + marker.rstrip())
    return lines


def _block(node: dict, depth: int = 0) -> str:
    kind, attrs, content = node.get("type"), node.get("attrs") or {}, node.get("content") or []
    if kind == "paragraph":
        return _inline(content).strip()
    if kind == "heading":
        return "#" * max(1, min(int(attrs.get("level", 1)), 6)) + " " + _inline(content).strip()
    if kind in ("bulletList", "orderedList"):
        return "\n".join(_list_lines(node, depth))
    if kind == "codeBlock":
        return f"```{attrs.get('language', '')}\n{_inline(content)}\n```"
    if kind == "blockquote":
        return "\n".join("> " + line for line in _blocks(content, depth).splitlines())
    if kind == "panel":
        label = {"info": "Info", "note": "Note", "warning": "Warning", "error": "Error", "success": "Success"}.get(
            attrs.get("panelType", ""), "")
        body = _blocks(content, depth)
        return f"**{label}:** {body}" if label else body
    if kind == "rule":
        return "---"
    if kind == "table":
        rows: list[str] = []
        for number, row in enumerate(content):
            cells = [_blocks(cell.get("content") or [], depth).replace("\n", " ").replace("|", "\\|")
                     for cell in row.get("content") or []]
            rows.append("| " + " | ".join(cells) + " |")
            if number == 0 and any(c.get("type") == "tableHeader" for c in row.get("content") or []):
                rows.append("| " + " | ".join("---" for _ in cells) + " |")
        return "\n".join(rows)
    if kind == "taskList":
        return "\n".join(
            ("- [x] " if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ] ") + _inline(item.get("content") or []).strip()
            for item in content if item.get("type") == "taskItem")
    if kind in ("expand", "nestedExpand"):
        title = attrs.get("title")
        body = _blocks(content, depth)
        return f"**{title}**\n{body}" if title else body
    if kind in ("mediaSingle", "mediaGroup"):
        return _inline(content)
    if kind in ("decisionList", "layoutSection", "layoutColumn", "listItem", "doc", "extension", "bodiedExtension"):
        return _blocks(content, depth)
    if kind in ("text", "mention", "emoji", "inlineCard", "blockCard", "hardBreak", "date", "status", "media"):
        return _inline([node])
    return _blocks(content, depth) if content else ""


def _blocks(nodes: list[dict], depth: int = 0) -> str:
    return "\n\n".join(text for text in (_block(n, depth) for n in nodes or []) if text.strip())


def adf_to_text(body) -> str:
    """Atlassian Document Format (a dict), or a plain / wiki-markup string, as readable Markdown text."""
    if not body:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        return (_blocks(body.get("content") or []) if body.get("type") == "doc" else _block(body)).strip()
    return ""


# ───────────────────────────── connector ─────────────────────────────


def _name(value: dict | None) -> str | None:
    return (value or {}).get("name")


def _who(value: dict | None) -> str | None:
    return (value or {}).get("displayName")


class JiraConnector:
    kind = "jira"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        if not config.get("base_url"):
            raise ValueError("jira: config needs base_url (e.g. https://acme.atlassian.net)")
        self.cfg = config
        self.site = config["base_url"].rstrip("/")
        cloud = config.get("deployment", "cloud") == "cloud"
        self.variant = config.get("api_variant", "token" if cloud else "legacy")
        if self.variant not in ("token", "legacy"):
            raise ValueError("jira: api_variant must be 'token' or 'legacy'")
        self.api = f"/rest/api/{config.get('api_version', '3' if cloud else '2')}"
        self.auth = config.get("auth", "basic" if cloud else "bearer")
        projects = list(config.get("projects") or [])
        for key in projects:
            if not _PROJECT_KEY.match(key):
                raise ValueError(f"jira: unusable project key {key!r}")
        if not config.get("jql") and not projects:
            raise ValueError("jira: config needs `jql` or `projects` (Jira refuses unbounded searches)")
        self.base_jql = _ORDER_BY.sub("", config.get("jql") or "").strip() or (
            f"project in ({','.join(projects)})" if projects else "")
        self.fields = _BASE_FIELDS + [f for f in config.get("extra_fields") or [] if f not in _BASE_FIELDS]
        self.page_size = int(config.get("page_size", 50))
        self.include_comments = bool(config.get("include_comments", True))
        self.include_restricted = bool(config.get("include_restricted_comments", False))
        self.include_secured = bool(config.get("include_secured_issues", False))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = timedelta(minutes=float(config.get("overlap_minutes", 1440)))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(self.site, transport=transport, client=client, sleep=sleep)

    # ── http ──

    def _get(self, path: str, params: dict | None = None) -> dict:
        token = env_secret(self.cfg.get("token_env"), "Jira API token / personal access token")
        if self.auth == "bearer":
            authorization = f"Bearer {token}"
        else:
            user = self.cfg.get("email") or env_secret(self.cfg.get("email_env"), "Jira account email")
            authorization = basic_auth(user, token)
        try:
            return self.http.get_json(path, params=params, headers={"Authorization": authorization, "Accept": "application/json"})
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the credential in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    # ── rendering ──

    def _comments(self, issue: dict) -> list[dict]:
        box = (issue.get("fields") or {}).get("comment") or {}
        comments = list(box.get("comments") or [])
        total = int(box.get("total", len(comments)) or 0)
        key = issue.get("key") or issue.get("id")
        while len(comments) < total:
            page = self._get(f"{self.api}/issue/{key}/comment",
                             {"startAt": len(comments), "maxResults": 100, "orderBy": "created"}).get("comments") or []
            if not page:
                break
            comments.extend(page)
        return comments

    def _render(self, issue: dict) -> tuple[str, str, bytes]:
        fields = issue.get("fields") or {}
        key = issue.get("key") or f"ID-{issue.get('id')}"
        summary = fields.get("summary") or "(no summary)"
        project = fields.get("project") or {}
        project_key = project.get("key") or key.split("-")[0]
        parent = fields.get("parent") or {}
        meta = {
            "project": f"{project.get('name') or project_key} ({project_key})",
            "type": _name(fields.get("issuetype")), "status": _name(fields.get("status")),
            "priority": _name(fields.get("priority")), "resolution": _name(fields.get("resolution")),
            "assignee": _who(fields.get("assignee")), "reporter": _who(fields.get("reporter")),
            "labels": ", ".join(fields.get("labels") or []) or None,
            "components": ", ".join(c.get("name", "") for c in fields.get("components") or []) or None,
            "fix versions": ", ".join(v.get("name", "") for v in fields.get("fixVersions") or []) or None,
            "parent": f"{parent['key']} {(parent.get('fields') or {}).get('summary', '')}".strip() if parent.get("key") else None,
            "created": fields.get("created"), "updated": fields.get("updated"),
            "url": f"{self.site}/browse/{key}",
        }
        for extra in self.cfg.get("extra_fields") or []:
            value = fields.get(extra)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                meta[extra] = value
        sections = [adf_to_text(fields.get("description"))]
        if self.include_comments:
            blocks = []
            for comment in self._comments(issue):
                if not self.include_restricted and (comment.get("visibility") or comment.get("jsdPublic") is False):
                    continue
                body = adf_to_text(comment.get("body"))
                if body:
                    blocks.append(f"### {_who(comment.get('author')) or 'Unknown'} ({comment.get('created', '')})\n\n{body}")
            if blocks:
                sections.append("## Comments\n\n" + "\n\n".join(blocks))
        text = render_document(f"{key}: {summary}", meta, "\n\n".join(s for s in sections if s))
        path = f"jira/{safe_name(project_key, '_', 40)}/{safe_name(f'{key}-{summary}', key, 100)}.md"
        return path, f"{key}: {summary}", text

    def _to_change(self, issue: dict, state: ScanState) -> Change | None:
        issue_id = str(issue.get("id", ""))
        if not issue_id:
            return None
        ext = f"jira:{issue_id}"
        fields = issue.get("fields") or {}
        if fields.get("security") and not self.include_secured:
            return Change("delete", ext)                  # issue security level: never searchable by path rules
        path, _, text = self._render(issue)
        state.observe(fields.get("updated"))
        key = issue.get("key") or issue_id
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text),
            modified_at=parse_dt(fields.get("updated")), version=f"{fields.get('updated', '')}-{short_hash(text)}",
            acl=self.rules.for_path(path), mime="text/markdown", extra={"key": key},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    def _jql(self, state: ScanState) -> str:
        clauses = [f"({self.base_jql})"] if self.base_jql else []
        if state.incremental:
            since = (parse_dt(state.since) or parse_dt(0)) - self.overlap
            clauses.append(f'updated >= "{since.strftime("%Y-%m-%d %H:%M")}"')
        return " AND ".join(clauses) + " ORDER BY updated ASC, key ASC"

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        jql = self._jql(state)
        token: str | None = state.pos.get("token")
        start_at = int(state.pos.get("start_at", 0))
        more = not state.pos.get("finished")
        while more:
            if not budget.allow_page(token or ("start", start_at)):
                break
            params = {"jql": jql, "fields": ",".join(self.fields), "maxResults": self.page_size}
            if self.variant == "token":
                if token:
                    params["nextPageToken"] = token
            else:
                params["startAt"] = start_at
            page = self._get(f"{self.api}/search/jql" if self.variant == "token" else f"{self.api}/search", params)
            issues = page.get("issues") or []
            changes = [c for c in (self._to_change(i, state) for i in budget.clip(list(issues))) if c]
            if self.variant == "token":
                token = None if page.get("isLast") else page.get("nextPageToken")
                more = bool(token)
                pos = {"token": token} if more else {"finished": True}
            else:
                start_at += len(issues)
                total = page.get("total")
                more = bool(issues) and (start_at < int(total) if total is not None else len(issues) >= self.page_size)
                pos = {"start_at": start_at} if more else {"finished": True}
            yield ScanBatch(changes, state.cursor(**pos))
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True,
                        complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        issue_id = file.external_id.split(":", 1)[-1]
        if not issue_id.isdigit():
            raise HttpError(f"jira: {file.external_id!r} is not an issue id")
        issue = self._get(f"{self.api}/issue/{issue_id}", {"fields": ",".join(self.fields)})
        return io.BytesIO(self._render(issue)[2])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
