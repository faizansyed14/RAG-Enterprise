"""GitLab projects (gitlab.com and self-managed): repository files, wiki pages, issues and merge requests with their
discussions, as virtual files.

Endpoints (GitLab REST API v4: "Projects", "Repository tree", "Repository blobs", "Issues", "Merge requests", "Notes",
"Wikis", "Pagination", "Rate limits"). `:id` is the numeric project id (the configured path is only used to look it up):

  GET /projects/:id_or_url_encoded_path                    project: id, path_with_namespace, default_branch, web_url,
        empty_repo, issues_enabled, merge_requests_enabled, wiki_enabled. One request per project and scan.
  GET /projects/:id/repository/tree?recursive=true&ref=&per_page=100&pagination=keyset[&page_token=]
        the whole tree. Keyset pagination (the `page_token` of the response's `Link: <...>; rel="next"`); a server
        that ignores `pagination=keyset` (before GitLab 15) answers with offset pagination, followed through
        `X-Next-Page`. The tree carries no sizes: `size` is 0 and the size cap is enforced when the file is opened.
  GET /projects/:id/repository/blobs/:sha/raw              file bytes, by blob sha: open().
  GET /projects/:id/wikis?with_content=1                   every wiki page with its content (not paginated by GitLab;
        `X-Next-Page` is followed if a server paginates anyway). GET /projects/:id/wikis/:slug for open().
  GET /projects/:id/issues | /merge_requests ?scope=all&state=all&order_by=updated_at&sort=asc&per_page=100
        &page=N[&updated_after=T]       offset pagination: `X-Next-Page` is empty on the last page.
  GET /projects/:id/issues|merge_requests/:iid/notes?sort=asc&order_by=created_at   discussions, same pagination.

Config: token_env (a personal / project / group access token with `read_api` and `read_repository`; secrets are never in
the config), auth ("private_token", the default: `PRIVATE-TOKEN` header; or "bearer": `Authorization: Bearer`, for OAuth
tokens), api_url (`https://gitlab.com/api/v4`; self-managed: `https://gitlab.example.com/api/v4`), projects (required;
["group/project", "group/sub/project@ref", 123, {"project": "group/project", "ref": "main"}]), include (fnmatch globs on
the path, default everything), exclude (default: vendored / generated / binary files), max_file_mb (10), include_files
(true), include_wiki (false), include_issues (false), include_merge_requests (false), issues_state ("all", "opened",
"closed"), issues_since (ISO date), include_confidential (false), include_internal_notes (false), tree_pagination
("keyset" or "offset"), page_size (100), batch_size (200), max_items, max_pages, full_resync_hours (24),
overlap_minutes (5), acl_rules, default_acl.

Objects (external ids are the stable identity, `version` the change detector; `<id>` is the numeric project id, which
survives renames and transfers, so the path of an existing object may change while its id does not):
  file   external_id `gitlab:<id>/blob/<path>`   path `gitlab/<namespace>/<project>/<path>`   version = the blob sha
  wiki   external_id `gitlab:<id>/wiki/<slug>`   path `gitlab/<namespace>/<project>/_wiki/<slug>.md`
         version = hash of title, format and content (the API has no timestamp)
  issue  external_id `gitlab:<id>/issue/<iid>`   path `.../_issues/<iid>-<title>.md`
  MR     external_id `gitlab:<id>/mr/<iid>`      path `.../_merge_requests/<iid>-<title>.md`
         version = `<updated_at>-<hash of the rendered text>`
Files and wiki pages are listed completely on every scan, so each project's listing is authoritative for
`gitlab:<id>/blob/` and `gitlab:<id>/wiki/` whenever it ran from its start to its end without hitting `max_items` /
`max_pages`. Issues and merge requests use `updated_after` (the newest `updated_at` seen, minus `overlap_minutes`)
after a finished scan; every `full_resync_hours` the listing is complete and authoritative for `gitlab:<id>/issue/` and
`gitlab:<id>/mr/` (GitLab has no feed of deleted issues, so a full listing is the only way to notice them). A project
whose features are switched off (`issues_enabled`, `wiki_enabled`, ... false, or an empty repository) is an
authoritative empty listing: what was indexed from it before is removed. (Until the sync layer persists the cursor of a
finished scan, every scan is a full one.)

Fail closed: confidential issues and internal notes are NOT indexed unless `include_confidential` /
`include_internal_notes` say so (they are readable only by project members, while the ACL rules below work per path
prefix); an issue that becomes confidential is deleted from the index. System notes ("changed the description") are
never indexed.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `gitlab/acme/payments`
readable by `group:payments`; no match and no `default_acl` => nobody). It is not GitLab's project / group membership;
whoever may read a private project on GitLab is not looked up. Mirror it in the rules.

What this does NOT do: groups (list the projects), git history or branches other than the one named, forks, releases,
snippets, CI logs, submodule contents, LFS targets, symlinks, attachments and uploads, issue / MR change history, MR
diffs. Offset pagination over issues sorted by `updated_at` can skip an item that is edited while the scan runs (GitLab
offers no stable cursor there); a resumed scan starts one page early, and the next full resync repairs a skip. A
rate-limited request (429) is retried after `Retry-After`.

Not exercised against a live GitLab; tested against fixtures shaped like the vendor documentation.
"""

from __future__ import annotations

import fnmatch
import io
import mimetypes
import re
import time
from typing import BinaryIO, Callable, Iterator
from urllib.parse import parse_qs, quote, urlparse

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, parse_dt, parse_link_next, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

DEFAULT_EXCLUDE = ["node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", ".git/*", "*/.git/*", "dist/*",
                   "*/dist/*", "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "*.png", "*.jpg",
                   "*.jpeg", "*.gif", "*.ico", "*.woff", "*.woff2", "*.ttf", "*.zip", "*.gz", "*.jar", "*.class", "*.pyc"]
PHASES = ("files", "wiki", "issues", "merge_requests")
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_EXTERNAL_ID = re.compile(r"gitlab:(\d+)/(blob|wiki|issue|mr)/(.+)", re.S)
_KIND_TAG = {"issues": "issue", "merge_requests": "mr"}


def _valid_name(name: str) -> bool:
    return bool(_NAME.match(name)) and name not in (".", "..")


def _next_page(response: httpx.Response) -> int | None:
    """`X-Next-Page`: the number of the next page, an empty string on the last one."""
    value = (response.headers.get("x-next-page") or "").strip()
    return int(value) if value.isdigit() else None


def _page_token(response: httpx.Response) -> str | None:
    """Keyset pagination: the `page_token` inside the `Link: <...>; rel="next"` URL (only the token is used, so the
    host GitLab believes it lives on can never redirect the credentials)."""
    link = parse_link_next(response.headers.get("link"))
    if not link:
        return None
    token = (parse_qs(urlparse(link).query).get("page_token") or [""])[0]
    if not token:
        raise HttpError("gitlab: the next link of a keyset page carries no page_token")
    return token


class GitLabConnector:
    kind = "gitlab"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.api = config.get("api_url", "https://gitlab.com/api/v4").rstrip("/")
        self.projects: list[tuple[str, str | None]] = []
        for entry in config.get("projects") or []:
            parsed = self._parse_project(entry)
            if all(parsed[0] != other[0] for other in self.projects):        # a project listed twice is scanned once
                self.projects.append(parsed)
        if not self.projects:
            raise ValueError("gitlab: config needs `projects` (e.g. [\"group/project\"])")
        self.auth = config.get("auth", "private_token")
        if self.auth not in ("private_token", "bearer"):
            raise ValueError("gitlab: `auth` must be \"private_token\" or \"bearer\"")
        self.tree_pagination = config.get("tree_pagination", "keyset")
        if self.tree_pagination not in ("keyset", "offset"):
            raise ValueError("gitlab: `tree_pagination` must be \"keyset\" or \"offset\"")
        self.include = list(config.get("include") or ["*"])
        self.exclude = list(config.get("exclude", DEFAULT_EXCLUDE))
        self.max_bytes = int(float(config.get("max_file_mb", 10)) * 1024 * 1024)
        wanted = {"files": config.get("include_files", True), "wiki": config.get("include_wiki", False),
                  "issues": config.get("include_issues", False), "merge_requests": config.get("include_merge_requests", False)}
        self.phases = [phase for phase in PHASES if wanted[phase]]
        if not self.phases:
            raise ValueError("gitlab: nothing to index (include_files / include_wiki / include_issues / include_merge_requests)")
        self.confidential = bool(config.get("include_confidential", False))
        self.internal_notes = bool(config.get("include_internal_notes", False))
        self.state_filter = config.get("issues_state", "all")
        self.since_floor = parse_dt(config.get("issues_since"))
        self.page_size = int(config.get("page_size", 100))
        self.batch_size = int(config.get("batch_size", 200))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_minutes", 5)) * 60
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(self.api, transport=transport, client=client, sleep=sleep)

    @staticmethod
    def _parse_project(entry) -> tuple[str, str | None]:
        ref = None
        if isinstance(entry, dict):
            entry, ref = entry["project"], entry.get("ref")
        elif isinstance(entry, str) and "@" in entry:
            entry, ref = entry.split("@", 1)
        text = str(entry).strip()
        if not text.isdigit():
            parts = text.split("/")
            if len(parts) < 2 or not all(_valid_name(part) for part in parts):
                raise ValueError(f"gitlab: unusable project {entry!r} (expected group/project or a numeric id)")
        return text, ref

    # ── http ──

    def _headers(self) -> tuple[dict[str, str], str]:
        token = env_secret(self.cfg.get("token_env"), "GitLab token")
        key = {"PRIVATE-TOKEN": token} if self.auth == "private_token" else {"Authorization": f"Bearer {token}"}
        return {**key, "Accept": "application/json"}, token

    def _get(self, path: str, params: dict | None = None, *, hint: str = "") -> httpx.Response:
        headers, token = self._headers()
        try:
            return self.http.get(path, params=params, headers=headers)
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, token, hint=hint if exc.status == 404 else "") from None

    def _json(self, path: str, params: dict | None = None, *, hint: str = ""):
        return self._get(path, params, hint=hint).json()

    def _all(self, path: str, params: dict | None = None) -> list:
        """Every item of an offset-paginated list endpoint (`X-Next-Page`)."""
        items: list = []
        guard = Budget()
        page: int | None = 1
        while page:
            guard.allow_page((path, page))
            response = self._get(path, {**(params or {}), "page": page})
            items.extend(response.json())
            page = _next_page(response)
        return items

    def _project(self, key: str) -> dict:
        info = self._json(f"/projects/{quote(key, safe='')}", hint="not found, or this token cannot see it")
        if not isinstance(info.get("id"), int) or not info.get("path_with_namespace"):
            raise HttpError(f"gitlab: project {key!r} came back without an id / path_with_namespace")
        return info

    # ── what is indexed ──

    def _wanted(self, entry: dict) -> bool:
        if entry.get("type") != "blob" or entry.get("mode") == "120000":
            return False
        path = entry["path"]
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.include):
            return False
        return not any(fnmatch.fnmatch(path, pattern) for pattern in self.exclude)

    @staticmethod
    def _strip(body: str | None) -> str:
        return _HTML_COMMENT.sub("", body or "").strip()

    @staticmethod
    def _prefix(info: dict) -> str:
        return f"gitlab/{info['path_with_namespace']}"

    # ── files ──

    def _tree(self, info: dict, ref: str, budget: Budget) -> tuple[list[dict], bool]:
        """(entries, complete). Incomplete when `max_pages` ran out part-way through the tree."""
        path = f"/projects/{info['id']}/repository/tree"
        params: dict = {"recursive": "true", "ref": ref, "per_page": self.page_size}
        if self.tree_pagination == "keyset":
            params["pagination"] = "keyset"
        entries: list[dict] = []
        while True:
            if not budget.allow_page(("tree", info["id"], params.get("page_token"), params.get("page"))):
                return entries, False
            response = self._get(path, params, hint="the ref does not exist in this project")
            entries.extend(response.json())
            token, page = _page_token(response), _next_page(response)
            if token:
                params["page_token"] = token
            elif page:
                params["page"] = page
            else:
                return entries, True

    def _file_change(self, info: dict, entry: dict) -> Change:
        ext = f"gitlab:{info['id']}/blob/{entry['path']}"
        path = f"{self._prefix(info)}/{entry['path']}"
        sf = SourceFile(
            external_id=ext, path=path, name=entry["path"].rsplit("/", 1)[-1], size=0, modified_at=None,
            version=entry["id"], acl=self.rules.for_path(path), mime=mimetypes.guess_type(entry["path"])[0] or "",
            extra={"sha": entry["id"]},
        )
        return Change("upsert", ext, sf)

    def _files_phase(self, index: int, info: dict, ref: str | None, state: ScanState, budget: Budget, after: str | None) -> Iterator[ScanBatch]:
        scope = f"gitlab:{info['id']}/blob/"
        ref = ref or info.get("default_branch")
        if info.get("empty_repo") or not ref:
            yield ScanBatch([], self._next_cursor(state, index, "files"), complete_scope=scope)
            return
        entries, complete = self._tree(info, ref, budget)
        entries = sorted((e for e in entries if self._wanted(e)), key=lambda e: e["path"])
        yield from self._chunks(index, "files", scope, entries, "path", lambda e: self._file_change(info, e), after, complete, state, budget)

    def _chunks(self, index, phase, scope, rows, key, make, after, complete, state, budget) -> Iterator[ScanBatch]:
        """Batches of a complete listing sorted by `key`; a resume continues after the last key of the last batch."""
        if after:
            rows = [row for row in rows if row[key] > after]
        chunks = [rows[i:i + self.batch_size] for i in range(0, len(rows), self.batch_size)] or [[]]
        for number, chunk in enumerate(chunks):
            chunk = budget.clip(chunk)
            last = number == len(chunks) - 1
            cursor = self._next_cursor(state, index, phase) if last else state.cursor(
                project=index, phase=phase, after=chunk[-1][key] if chunk else after)
            declared = scope if (last and not after and complete and not budget.truncated) else None
            yield ScanBatch([make(row) for row in chunk], cursor, complete_scope=declared)
            if budget.truncated:
                return

    def _next_cursor(self, state: ScanState, index: int, phase: str) -> dict:
        k = self.phases.index(phase)
        if k + 1 < len(self.phases):
            return state.cursor(project=index, phase=self.phases[k + 1])
        return state.cursor(project=index + 1, phase=self.phases[0])

    # ── wiki ──

    def _wiki_change(self, info: dict, page: dict) -> Change:
        slug = page["slug"]
        path, text = self._render_wiki(info, page)
        ext = f"gitlab:{info['id']}/wiki/{slug}"
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=None,
            version=short_hash(page.get("title"), page.get("format"), page.get("content")), acl=self.rules.for_path(path),
            mime="text/markdown", extra={"slug": slug},
        )
        return Change("upsert", ext, sf)

    def _render_wiki(self, info: dict, page: dict) -> tuple[str, bytes]:
        slug = page["slug"]
        meta = {"project": info["path_with_namespace"], "type": "wiki page", "format": page.get("format"),
                "url": f"{info.get('web_url', '').rstrip('/')}/-/wikis/{slug}" if info.get("web_url") else None}
        text = render_document(page.get("title") or slug, meta, page.get("content") or "")
        parts = [safe_name(part, "page", 100) for part in slug.split("/") if part]
        return f"{self._prefix(info)}/_wiki/{'/'.join(parts) or 'page'}.md", text

    def _wiki_phase(self, index: int, info: dict, state: ScanState, budget: Budget, after: str | None) -> Iterator[ScanBatch]:
        scope = f"gitlab:{info['id']}/wiki/"
        if info.get("wiki_enabled") is False:
            yield ScanBatch([], self._next_cursor(state, index, "wiki"), complete_scope=scope)
            return
        if not budget.allow_page(("wiki", info["id"])):
            return
        try:
            pages = self._all(f"/projects/{info['id']}/wikis", {"with_content": 1})
            complete = True
        except HttpError as exc:
            if exc.status != 404:
                raise
            pages, complete = [], False               # "wiki not found": not enough to delete anything on
        pages = sorted((p for p in pages if p.get("slug")), key=lambda p: p["slug"])
        yield from self._chunks(index, "wiki", scope, pages, "slug", lambda p: self._wiki_change(info, p), after, complete, state, budget)

    # ── issues and merge requests ──

    def _notes(self, pid: int, kind: str, item: dict) -> str:
        if item.get("user_notes_count") == 0:
            return ""
        rendered = []
        for note in self._all(f"/projects/{pid}/{kind}/{item['iid']}/notes", {"sort": "asc", "order_by": "created_at", "per_page": 100}):
            if note.get("system") or ((note.get("internal") or note.get("confidential")) and not self.internal_notes):
                continue
            body = self._strip(note.get("body"))
            if body:
                rendered.append(f"### {(note.get('author') or {}).get('username', 'ghost')} ({note.get('created_at', '')})\n\n{body}")
        return "## Comments\n\n" + "\n\n".join(rendered) if rendered else ""

    def _render_item(self, info: dict, kind: str, item: dict) -> tuple[str, bytes]:
        is_mr = kind == "merge_requests"
        iid = int(item["iid"])
        title = item.get("title") or f"#{iid}"
        names = lambda key, field: ", ".join(x.get(field, "") for x in item.get(key) or []) or None    # noqa: E731
        meta = {
            "project": info["path_with_namespace"], "type": "merge request" if is_mr else "issue", "state": item.get("state"),
            "author": (item.get("author") or {}).get("username"), "labels": ", ".join(item.get("labels") or []) or None,
            "assignees": names("assignees", "username"), "milestone": (item.get("milestone") or {}).get("title"),
            "created": item.get("created_at"), "updated": item.get("updated_at"), "closed": item.get("closed_at"),
            "url": item.get("web_url"),
        }
        if is_mr:
            meta.update({"source branch": item.get("source_branch"), "target branch": item.get("target_branch"),
                         "merged": item.get("merged_at"), "draft": "yes" if item.get("draft") else None,
                         "reviewers": names("reviewers", "username")})
        sections = [self._strip(item.get("description")), self._notes(info["id"], kind, item)]
        text = render_document(f"{info['path_with_namespace']}{'!' if is_mr else '#'}{iid}: {title}", meta,
                               "\n\n".join(s for s in sections if s))
        folder = "_merge_requests" if is_mr else "_issues"
        return f"{self._prefix(info)}/{folder}/{safe_name(f'{iid}-{title}', str(iid), 100)}.md", text

    def _visible(self, kind: str, item: dict) -> bool:
        return kind == "merge_requests" or self.confidential or not item.get("confidential")

    def _item_change(self, info: dict, kind: str, item: dict) -> Change:
        path, text = self._render_item(info, kind, item)
        ext = f"gitlab:{info['id']}/{_KIND_TAG[kind]}/{item['iid']}"
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(item.get("updated_at")),
            version=f"{item.get('updated_at', '')}-{short_hash(text)}", acl=self.rules.for_path(path), mime="text/markdown",
            extra={"iid": item["iid"]},
        )
        return Change("upsert", ext, sf)

    def _items_phase(self, index: int, info: dict, kind: str, state: ScanState, budget: Budget, resume_page: int | None) -> Iterator[ScanBatch]:
        scope = f"gitlab:{info['id']}/{_KIND_TAG[kind]}/"
        flag = "issues_enabled" if kind == "issues" else "merge_requests_enabled"
        if info.get(flag) is False:
            yield ScanBatch([], self._next_cursor(state, index, kind), complete_scope=scope)
            return
        params = {"scope": "all", "state": self.state_filter, "order_by": "updated_at", "sort": "asc", "per_page": self.page_size}
        since = None
        if state.incremental and state.since:
            since = (parse_dt(state.since) or parse_dt(0)).timestamp() - self.overlap
        elif self.since_floor:
            since = self.since_floor.timestamp()
        if since is not None:
            params["updated_after"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        page = max(1, resume_page - 1) if resume_page else 1          # a resumed listing starts one page early: edits shift offsets
        while True:
            if not budget.allow_page((kind, info["id"], page)):
                return
            response = self._get(f"/projects/{info['id']}/{kind}", {**params, "page": page})
            rows = response.json()
            hidden = [item for item in rows if not self._visible(kind, item)]
            changes = [Change("delete", f"gitlab:{info['id']}/{_KIND_TAG[kind]}/{item['iid']}") for item in hidden]
            for item in hidden:
                state.observe(item.get("updated_at"))
            for item in budget.clip([item for item in rows if self._visible(kind, item)]):
                changes.append(self._item_change(info, kind, item))
                state.observe(item.get("updated_at"))
            nxt = _next_page(response)
            if nxt:
                yield ScanBatch(changes, state.cursor(project=index, phase=kind, page=nxt))
                page = nxt
                continue
            complete = scope if (state.mode == "full" and not resume_page and not budget.truncated) else None
            yield ScanBatch(changes, self._next_cursor(state, index, kind), complete_scope=complete)
            return

    # ── scan ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        start, phase0 = int(state.pos.get("project", 0)), state.pos.get("phase")
        if phase0 not in self.phases:                 # the configuration changed since the cursor was written
            state.pos, phase0 = {}, self.phases[0]
        for index in range(start, len(self.projects)):
            if budget.stop_if_exhausted():
                break
            key, ref = self.projects[index]
            info = self._project(key)
            for phase in self.phases:
                if index == start and self.phases.index(phase) < self.phases.index(phase0):
                    continue
                resumed = index == start and phase == phase0
                if phase == "files":
                    yield from self._files_phase(index, info, ref, state, budget, state.pos.get("after") if resumed else None)
                elif phase == "wiki":
                    yield from self._wiki_phase(index, info, state, budget, state.pos.get("after") if resumed else None)
                else:
                    yield from self._items_phase(index, info, phase, state, budget, state.pos.get("page") if resumed else None)
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = _EXTERNAL_ID.fullmatch(file.external_id)
        if not match:
            raise HttpError(f"gitlab: {file.external_id!r} is not a GitLab object id")
        pid, kind, rest = int(match.group(1)), match.group(2), match.group(3)
        if kind == "blob":
            if not _SHA.match(file.version or ""):
                raise HttpError(f"gitlab: {file.external_id!r} has no blob sha as its version")
            headers, token = self._headers()
            try:
                return self.http.download(f"/projects/{pid}/repository/blobs/{file.version}/raw",
                                          max_bytes=self.max_bytes, headers=headers)
            except AuthError as exc:
                raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}") from None
            except HttpError as exc:
                raise redacted(exc, token) from None
        if kind != "wiki" and not rest.isdigit():
            raise HttpError(f"gitlab: {file.external_id!r} is not an issue / merge request number")
        info = self._json(f"/projects/{pid}")
        if kind == "wiki":
            return io.BytesIO(self._render_wiki(info, self._json(f"/projects/{pid}/wikis/{quote(rest, safe='')}"))[1])
        resource = "issues" if kind == "issue" else "merge_requests"
        item = self._json(f"/projects/{pid}/{resource}/{rest}")
        if not self._visible(resource, item):
            raise HttpError(f"gitlab: {file.external_id!r} is confidential and include_confidential is off", 403)
        return io.BytesIO(self._render_item(info, resource, item)[1])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
