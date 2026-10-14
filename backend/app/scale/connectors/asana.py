"""Asana tasks (with their comments) as virtual files: one Markdown file per task per project.

Endpoints (Asana REST API 1.0, groups "Projects", "Tasks", "Stories"; "Pagination" in the API reference):

  GET /projects/{project_gid}?opt_fields=name                        the project name (a path component).
  GET /projects/{project_gid}/tasks?limit=N&opt_fields=...           a full scan: every task of the project.
  GET /tasks?project={gid}&modified_since=T&limit=N&opt_fields=...   an incremental scan (and `tasks_since`): only tasks
        modified since T. `modified_since` is a parameter of GET /tasks, not of /projects/{gid}/tasks; the API reference
        says adding a story (a comment included) counts as modifying a task, so a new comment is picked up.
  GET /tasks/{task_gid}/stories?limit=N&opt_fields=...               comments (`resource_subtype: comment_added`) and, if
        asked for, system stories ("Ada marked this task complete").
  GET /tasks/{task_gid}?opt_fields=...                               open(): re-render one task.

Pagination: every list answers `{"data": [...], "next_page": {"offset": "...", "path": "...", "uri": "..."}}` and
`"next_page": null` on the last page. Only the `offset` token is used (sent back as `offset=` with the same query); the
`uri` / `path` the server offers are NEVER followed, so the bearer token cannot be sent to another host. Asana only accepts
an offset it issued, for a limited time: a saved offset that is refused (HTTP 400) restarts that project's listing.

Auth: `Authorization: Bearer <token>`, a personal access token (Asana: My settings > Apps > Developer apps > Personal
access tokens, or https://app.asana.com/0/my-apps) or an OAuth access token, in the environment variable named by
`token_env`. The token sees what its user sees: use a service-account user that is a member of the projects to index.
Rate limits: 429 with `Retry-After` is honoured by `_http.Http`. A comment fetch costs one request per task; set
`include_comments: false` for speed.

Config: token_env, projects (list of project gids, required), base_url (`https://app.asana.com/api/1.0`),
include_completed (true; false sends `completed_since=now`), include_comments (true), include_system_stories (false),
tasks_since (ISO date: only tasks modified since), page_size (100, Asana's maximum), max_items, max_pages,
full_resync_hours (24), overlap_minutes (5), acl_rules, default_acl.

Objects: external_id `asana:<project gid>/<task gid>` (a task that sits in two projects is two files, each under the rules of
its own project folder), path `asana/<project name>/<task gid>-<name>.md`, version = hash of path and rendered text
(modification times are left out of the text, so touching a task without changing it does not re-index it).

Incremental: after a finished scan the cursor holds the START time of that scan (a task's `modified_at` is not used as the
watermark: it need not move when a comment is added). The next scan asks `modified_since` = that time minus
`overlap_minutes`. The API has no feed of deletions: only the periodic full scan (`full_resync_hours`) is authoritative and
declares, per project (`asana:<gid>/`), that unseen tasks are gone. A project the token can no longer read (403 / 404) is
treated as gone in a full scan (its tasks are removed: fail closed). (Until the sync layer persists the cursor of a
finished scan, every scan is a full one.)

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `asana/Payroll` readable by
`group:finance`; no match and no `default_acl` => nobody). It is NOT Asana's project / task privacy model: a project is
indexed for whoever the rules name, whatever its members in Asana can see. A folder is the project NAME, so two projects
with the same name share rules; rename one or keep one out of the index.

What this does NOT do: subtasks (only tasks listed directly in the project), attachments, task dependencies, goals,
portfolios, forms, custom field definitions beyond their display value, edit history of comments, workspaces' project
discovery (list the project gids), webhooks / the Events API.

Not exercised against a live Asana; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): the exact wording of error bodies, whether an expired offset is refused with 400, and
whether `completed_since=now` behaves identically on both list endpoints.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, html_to_text, iso_z, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

_GID = re.compile(r"^\d{1,30}$")
_EXTERNAL_ID = re.compile(r"asana:(\d{1,30})/(\d{1,30})")
TASK_FIELDS = ",".join([
    "name", "notes", "completed", "completed_at", "created_at", "modified_at", "due_on", "due_at", "start_on", "assignee.name",
    "created_by.name", "tags.name", "memberships.project.gid", "memberships.section.name", "parent.name", "permalink_url",
    "resource_subtype", "custom_fields.name", "custom_fields.display_value",
])
STORY_FIELDS = "created_at,created_by.name,resource_subtype,type,text,html_text"
GONE = (403, 404)


class AsanaConnector:
    kind = "asana"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        raw = config.get("projects") or []
        self.projects = list(dict.fromkeys(str(p) for p in raw)) if isinstance(raw, (list, tuple)) else []
        if not self.projects or not all(_GID.match(p) for p in self.projects):
            raise ValueError("asana: config needs `projects`, a list of numeric project gids (the number in the project's URL)")
        self.completed = bool(config.get("include_completed", True))
        self.comments = bool(config.get("include_comments", True))
        self.system = bool(config.get("include_system_stories", False))
        self.floor = parse_dt(config.get("tasks_since"))
        self.page_size = max(1, min(int(config.get("page_size", 100)), 100))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_minutes", 5)) * 60
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(config.get("base_url", "https://app.asana.com/api/1.0"), transport=transport, client=client, sleep=sleep)

    # ── http ──

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        token = env_secret(self.cfg.get("token_env"), "Asana access token")
        try:
            return self.http.get(path, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}" if exc.status == 401 else "") from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    def _pages(self, path: str, params: dict, offset: str | None = None) -> Iterator[tuple[list[dict], str | None]]:
        """(rows, offset of the next page) for each page; the offset is the only thing taken from `next_page`."""
        guard = Budget()
        while True:
            guard.allow_page(offset)
            body = self._get(path, {**params, **({"offset": offset} if offset else {})}).json()
            offset = (body.get("next_page") or {}).get("offset") or None
            yield body.get("data") or [], offset
            if not offset:
                return

    # ── rendering ──

    def _stories(self, task_gid: str) -> list[dict]:
        if not (self.comments or self.system):
            return []
        stories: list[dict] = []
        for rows, _ in self._pages(f"/tasks/{task_gid}/stories", {"limit": self.page_size, "opt_fields": STORY_FIELDS}):
            stories.extend(rows)
        return stories

    def _story_text(self, story: dict) -> str:
        return (story.get("text") or "").strip() or html_to_text(story.get("html_text") or "").strip()

    def _render(self, task: dict, project: str, folder: str, stories: list[dict]) -> tuple[str, bytes]:
        gid = str(task["gid"])
        name = task.get("name") or f"Task {gid}"
        section = next(((m.get("section") or {}).get("name") for m in task.get("memberships") or []
                        if str((m.get("project") or {}).get("gid")) == project and (m.get("section") or {}).get("name")), None)
        fields = [f"{f.get('name')}: {f.get('display_value')}" for f in task.get("custom_fields") or [] if f.get("name") and f.get("display_value") not in (None, "")]
        meta = {
            "type": "task", "project": folder, "section": section,
            "status": "completed" + (f" ({task['completed_at']})" if task.get("completed_at") else "") if task.get("completed") else "open",
            "assignee": (task.get("assignee") or {}).get("name"), "created by": (task.get("created_by") or {}).get("name"),
            "parent task": (task.get("parent") or {}).get("name"), "start": task.get("start_on"), "due": task.get("due_at") or task.get("due_on"),
            "tags": ", ".join(t.get("name") for t in task.get("tags") or [] if t.get("name")) or None,
            "created": task.get("created_at"), "url": task.get("permalink_url"),
        }
        comments, activity = [], []
        for story in sorted(stories, key=lambda s: s.get("created_at") or ""):
            body = self._story_text(story)
            if not body:
                continue
            who, when = (story.get("created_by") or {}).get("name") or "unknown", story.get("created_at") or ""
            if story.get("resource_subtype") == "comment_added" or story.get("type") == "comment":
                if self.comments:
                    comments.append(f"### {who} ({when})\n\n{body}")
            elif self.system:
                activity.append(f"- {when}: {body}")
        parts = [(task.get("notes") or "").strip()]
        if fields:
            parts.append("## Fields\n\n" + "\n".join(f"- {line}" for line in fields))
        if comments:
            parts.append("## Comments\n\n" + "\n\n".join(comments))
        if activity:
            parts.append("## Activity\n\n" + "\n".join(activity))
        text = render_document(name, meta, "\n\n".join(p for p in parts if p))
        return f"asana/{folder}/{safe_name(f'{gid}-{name}', gid, 100)}.md", text

    def _task_change(self, task: dict, project: str, folder: str, state: ScanState) -> Change:
        ext = f"asana:{project}/{task['gid']}"
        state.observe(task.get("modified_at"))
        try:
            stories = self._stories(str(task["gid"]))
        except HttpError as exc:
            if exc.status != 404:
                raise
            return Change("delete", ext)                                        # the task vanished while the scan was running
        path, text = self._render(task, project, folder, stories)
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(task.get("modified_at")),
            version=short_hash(path, text), acl=self.rules.for_path(path), mime="text/markdown", extra={"project": project, "task": task["gid"]},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    def _listing(self, state: ScanState, project: str) -> tuple[str, dict]:
        params: dict = {"limit": self.page_size, "opt_fields": TASK_FIELDS}
        if not self.completed:
            params["completed_since"] = "now"
        since = None
        if state.incremental and state.since:
            since = (parse_dt(state.since) or parse_dt(0)) - timedelta(seconds=self.overlap)
        if self.floor and (since is None or self.floor > since):
            since = self.floor
        if since is None:
            return f"/projects/{project}/tasks", params
        return "/tasks", {**params, "project": project, "modified_since": iso_z(since)}

    def _project(self, state: ScanState, budget: Budget, index: int, offset: str | None) -> Iterator[ScanBatch]:
        project, scope = self.projects[index], f"asana:{self.projects[index]}/"
        resumed = offset is not None
        full = state.mode == "full"

        def gone(exc: HttpError) -> ScanBatch:
            log.warning("asana: project %s cannot be read (HTTP %s); it is treated as empty", project, exc.status)
            return ScanBatch([], state.cursor(project=index + 1), complete_scope=scope if full and not resumed and not budget.truncated else None)

        if not budget.allow_page(("project", project)):
            return
        try:
            name = (self._get(f"/projects/{project}", {"opt_fields": "name"}).json().get("data") or {}).get("name")
        except HttpError as exc:
            if exc.status not in GONE:
                raise
            yield gone(exc)
            return
        folder = safe_name(str(name or ""), project, 80)
        path, params = self._listing(state, project)
        while True:
            if not budget.allow_page(("tasks", project, offset)):
                return
            try:
                body = self._get(path, {**params, **({"offset": offset} if offset else {})}).json()
            except HttpError as exc:
                if offset and exc.status == 400:                                # an offset Asana no longer accepts: start the project over
                    resumed, offset = False, None
                    continue
                if exc.status not in GONE:
                    raise
                yield gone(exc)
                return
            rows = [t for t in body.get("data") or [] if t.get("resource_subtype") != "section"]
            changes = [self._task_change(task, project, folder, state) for task in budget.clip(rows)]
            offset = (body.get("next_page") or {}).get("offset") or None
            if offset:
                yield ScanBatch(changes, state.cursor(project=index, next=offset))
                continue
            complete = scope if (full and not resumed and not budget.truncated) else None
            yield ScanBatch(changes, state.cursor(project=index + 1), complete_scope=complete)
            return

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        if not state.resumed:                                                   # the watermark is when this scan began, not a task time
            state.observe(datetime.fromtimestamp(state.started, tz=timezone.utc))
        first, offset = int(state.pos.get("project", 0)), state.pos.get("next")
        for index in range(first, len(self.projects)):
            if budget.stop_if_exhausted():
                break
            yield from self._project(state, budget, index, offset if index == first else None)
            if budget.truncated:
                break
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = _EXTERNAL_ID.fullmatch(file.external_id)
        if not match:
            raise HttpError(f"asana: {file.external_id!r} is not an Asana task id")
        project, gid = match.groups()
        task = self._get(f"/tasks/{gid}", {"opt_fields": TASK_FIELDS}).json().get("data") or {}
        if not task.get("gid"):
            raise HttpError(f"asana: {file.external_id!r} came back without a task", 404)
        segments = file.path.split("/")
        folder = segments[1] if len(segments) >= 3 and segments[0] == "asana" else project
        return io.BytesIO(self._render(task, project, folder, self._stories(gid))[1])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
