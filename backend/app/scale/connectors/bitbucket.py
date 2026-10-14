"""Bitbucket Cloud repositories: files and, optionally, issue-tracker issues with their comments, as virtual files.

Endpoints (Bitbucket Cloud REST API 2.0: "Repositories", "Refs", "Source (src)", "Commits / diffstat", "Issue tracker",
"Pagination", "Filtering and sorting", "Rate limiting"):

  GET /repositories/{workspace}/{repo_slug}                      mainbranch, has_issues, links.html.
  GET /repositories/{workspace}/{repo_slug}/refs/branches/{name} the commit the branch points to (`target.hash`).
  GET /repositories/{workspace}/{repo_slug}/src/{commit}/?max_depth=N&pagelen=100     the file listing at that commit;
        paginated through `next` (an absolute URL, followed only on the configured host). A directory at the depth
        limit is listed again, so the depth the server really honours does not matter.
  GET /repositories/{workspace}/{repo_slug}/src/{commit}/{path}  raw file bytes (open()); `?format=meta` the entry
        (type, size, commit) of one path, used to settle the paths an incremental scan found.
  GET /repositories/{workspace}/{repo_slug}/diffstat/{new}..{old}?pagelen=100    paths touched between two commits.
  GET /repositories/{workspace}/{repo_slug}/issues?sort=updated_on&q=updated_on>T&pagelen=100       `next` pagination.
  GET /repositories/{workspace}/{repo_slug}/issues/{id}, .../issues/{id}/comments?sort=created_on

Config: token_env (or password_env: the secret; never in the config), username (with it the secret is an app password
and goes out as HTTP Basic; without it the secret is a repository / project / workspace access token sent as Bearer;
`auth` overrides: "basic" or "bearer"), api_url (`https://api.bitbucket.org/2.0`), repos (["workspace/repo",
"workspace/repo@branch", {"repo": "workspace/repo", "ref": "branch"}]; a 40-hex ref is taken as a commit), include
(fnmatch globs on the path, default everything), exclude (default: vendored / generated / binary files), max_file_mb
(10), include_files (true), include_issues (false), issues_since (ISO date), max_depth (10), page_size (100), batch_size
(200), max_items, max_pages, full_resync_hours (24), overlap_minutes (5), acl_rules, default_acl.

Objects (external ids are the stable identity, `version` the change detector; the workspace and repository SLUGS are
part of the id, so renaming either one re-indexes the repository):
  file   external_id `bitbucket:<ws>/<repo>/src/<path>`   path `bitbucket/<ws>/<repo>/<path>`
         version = `<size>-<commit hash>`: Bitbucket's listing carries no blob / content hash, so a file is identified by
         its size and the commit its entry names (the listed commit; if Bitbucket names the last commit that touched the
         file, versions are stable, otherwise a new commit makes every file in a FULL listing look new and the pipeline
         has to download it again, which its content hash then recognises). open() fetches exactly that commit.
  issue  external_id `bitbucket:<ws>/<repo>/issue/<id>`   path `bitbucket/<ws>/<repo>/_issues/<id>-<title>.md`
         version = `<updated_on>-<hash of the rendered text>`
Scans: the file listing of a full scan is complete, so it is authoritative for `bitbucket:<ws>/<repo>/src/` when it ran
from the start to the end without hitting `max_items` / `max_pages`. After a finished scan the cursor remembers each
repository's commit; the next scan asks `diffstat` which paths were touched between that commit and the branch head and
looks only at those (`?format=meta`: gone => delete, a file => upsert), so an unchanged repository costs two requests.
Only the SET of touched paths is used, never the status or the side of the diff, so an inverted `new..old` reading by
the server cannot turn into wrong deletions. If `diffstat` fails (the old commit is gone after a force push) the full
listing runs instead. Issues use `q=updated_on>T` after a finished scan; every `full_resync_hours` the issue listing is
complete and authoritative for `bitbucket:<ws>/<repo>/issue/`. (Until the sync layer persists the cursor of a finished
scan, every scan is a full one.)

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `bitbucket/acme/payments`
readable by `group:payments`; no match and no `default_acl` => nobody). It is not Bitbucket's repository / project
permissions; whoever may read a private repository there is not looked up. Mirror it in the rules.

What this does NOT do: Bitbucket Server / Data Center (a different API), pull requests, wikis, snippets, downloads,
pipelines, git history, symlinks and submodules, attachments on issues. The `q=updated_on>T` filter writes the date
without quotes as the "Filtering and sorting" page does; if a server wants it quoted the incremental issue scan fails
loudly (HTTP 400) rather than silently returning everything.

Not exercised against a live Bitbucket; tested against fixtures shaped like the vendor documentation.
"""

from __future__ import annotations

import fnmatch
import io
import mimetypes
import re
import time
from typing import BinaryIO, Callable, Iterator
from urllib.parse import quote, urlparse

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, basic_auth, html_to_text, iso_z, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

DEFAULT_EXCLUDE = ["node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", ".git/*", "*/.git/*", "dist/*",
                   "*/dist/*", "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "*.png", "*.jpg",
                   "*.jpeg", "*.gif", "*.ico", "*.woff", "*.woff2", "*.ttf", "*.zip", "*.gz", "*.jar", "*.class", "*.pyc"]
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_HASH = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^(\d+)-([0-9a-f]{40})$")
_EXTERNAL_ID = re.compile(r"bitbucket:([^/]+)/([^/]+)/(src|issue)/(.+)", re.S)
_SKIP_ATTRIBUTES = {"link", "subrepository"}            # symlinks and submodules


def _valid_name(name: str) -> bool:
    return bool(_NAME.match(name)) and name not in (".", "..")


def _content(obj: dict | None) -> str:
    """The text of a Bitbucket `content` / comment object: the raw markup (Markdown), else the rendered HTML as text."""
    obj = obj or {}
    raw = (obj.get("raw") or "").strip()
    return raw or html_to_text(obj.get("html") or "").strip()


def _path_url(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


class BitbucketConnector:
    kind = "bitbucket"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.api = config.get("api_url", "https://api.bitbucket.org/2.0").rstrip("/")
        self.repos: list[tuple[str, str, str | None]] = []
        for entry in config.get("repos") or []:
            parsed = self._parse_repo(entry)
            if all(parsed[:2] != other[:2] for other in self.repos):        # a repository listed twice is scanned once
                self.repos.append(parsed)
        if not self.repos:
            raise ValueError("bitbucket: config needs `repos` (e.g. [\"workspace/repo\"])")
        self.auth = config.get("auth") or ("basic" if config.get("username") else "bearer")
        if self.auth not in ("basic", "bearer"):
            raise ValueError("bitbucket: `auth` must be \"basic\" or \"bearer\"")
        if self.auth == "basic" and not config.get("username"):
            raise ValueError("bitbucket: basic auth needs `username` (the account the app password belongs to)")
        self.secret_env = config.get("token_env") or config.get("password_env")
        self.include = list(config.get("include") or ["*"])
        self.exclude = list(config.get("exclude", DEFAULT_EXCLUDE))
        self.max_bytes = int(float(config.get("max_file_mb", 10)) * 1024 * 1024)
        self.files = bool(config.get("include_files", True))
        self.issues = bool(config.get("include_issues", False))
        if not (self.files or self.issues):
            raise ValueError("bitbucket: nothing to index (include_files / include_issues)")
        self.phases = [p for p, on in (("files", self.files), ("issues", self.issues)) if on]
        self.since_floor = parse_dt(config.get("issues_since"))
        self.max_depth = max(1, int(config.get("max_depth", 10)))
        self.page_size = int(config.get("page_size", 100))
        self.batch_size = int(config.get("batch_size", 200))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_minutes", 5)) * 60
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(self.api, transport=transport, client=client, sleep=sleep)
        self._repos: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _parse_repo(entry) -> tuple[str, str, str | None]:
        ref = None
        if isinstance(entry, dict):
            entry, ref = entry["repo"], entry.get("ref")
        elif "@" in entry:
            entry, ref = entry.split("@", 1)
        workspace, _, slug = entry.partition("/")
        if not (_valid_name(workspace) and _valid_name(slug)):
            raise ValueError(f"bitbucket: unusable repository {entry!r} (expected workspace/repo)")
        return workspace, slug, ref

    # ── http ──

    def _headers(self) -> tuple[dict[str, str], str]:
        secret = env_secret(self.secret_env, "Bitbucket token / app password")
        value = basic_auth(self.cfg["username"], secret) if self.auth == "basic" else f"Bearer {secret}"
        return {"Authorization": value, "Accept": "application/json"}, secret

    def _guard(self, url: str) -> str:
        if url.startswith(("http://", "https://")) and urlparse(url).netloc != urlparse(self.api).netloc:
            raise HttpError(f"bitbucket: refusing to follow a link to another host ({urlparse(url).netloc})")
        return url

    def _get(self, url: str, params: dict | None = None, *, ok: tuple[int, ...] = (200,)) -> httpx.Response:
        headers, secret = self._headers()
        try:
            return self.http.get(self._guard(url), params=params, headers=headers, ok=ok)
        except AuthError as exc:
            raise redacted(exc, secret, self._encoded(headers), hint=f"check the credentials in environment variable {self.secret_env}") from None
        except HttpError as exc:
            raise redacted(exc, secret, self._encoded(headers)) from None

    @staticmethod
    def _encoded(headers: dict[str, str]) -> str:
        return headers["Authorization"].split(" ", 1)[1]

    def _json(self, url: str, params: dict | None = None):
        return self._get(url, params).json()

    def _pages(self, url: str, params: dict | None, budget: Budget | None = None) -> Iterator[list]:
        """The `values` of every page of a `next`-paginated list; stops quietly when the budget says so."""
        guard = budget or Budget()
        while url:
            if not guard.allow_page(url):
                return
            body = self._get(url, params).json()
            yield body.get("values") or []
            url, params = body.get("next"), None

    def _repo_url(self, owner: str, name: str) -> str:
        return f"/repositories/{owner}/{name}"

    def _repo(self, owner: str, name: str) -> dict:
        """The repository record, fetched once per scan."""
        if (owner, name) not in self._repos:
            self._repos[(owner, name)] = self._json(self._repo_url(owner, name))
        return self._repos[(owner, name)]

    # ── files ──

    def _wanted(self, entry: dict) -> bool:
        if entry.get("type") != "commit_file" or _SKIP_ATTRIBUTES & set(entry.get("attributes") or []):
            return False
        path = entry["path"]
        if int(entry.get("size") or 0) > self.max_bytes:
            return False
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.include):
            return False
        return not any(fnmatch.fnmatch(path, pattern) for pattern in self.exclude)

    def _head(self, owner: str, name: str, ref: str | None) -> str | None:
        """The commit to read: a 40-hex ref as it is, a branch (default: the main branch) resolved to its tip. None
        for an empty repository."""
        if ref and _HASH.match(ref):
            return ref
        if not ref:
            ref = (self._repo(owner, name).get("mainbranch") or {}).get("name")
            if not ref:
                return None
        target = self._json(f"{self._repo_url(owner, name)}/refs/branches/{quote(ref, safe='')}").get("target") or {}
        if not _HASH.match(target.get("hash") or ""):
            raise HttpError(f"bitbucket: branch {ref!r} of {owner}/{name} came back without a commit hash")
        return target["hash"]

    def _listing(self, owner: str, name: str, head: str, budget: Budget) -> tuple[list[dict], bool]:
        """Every file entry of the tree at `head`. (entries, complete): incomplete when `max_pages` ran out.
        A directory that came back without any of its children is listed on its own, whatever `max_depth` the server
        actually honoured (git has no empty directories, so such a directory was cut off by the depth limit)."""
        base = f"{self._repo_url(owner, name)}/src/{head}"
        queue, seen = [""], set()
        files: dict[str, dict] = {}
        while queue:
            directory = queue.pop()
            if directory in seen:
                continue
            seen.add(directory)
            url = f"{base}/{_path_url(directory)}/" if directory else f"{base}/"
            directories: set[str] = set()
            has_children: set[str] = set()
            for values in self._pages(url, {"max_depth": self.max_depth, "pagelen": self.page_size}, budget):
                for entry in values:
                    path = entry.get("path") or ""
                    parts = path.split("/")
                    has_children.update("/".join(parts[:i]) for i in range(1, len(parts)))
                    if entry.get("type") == "commit_directory":
                        directories.add(path)
                    elif entry.get("type") == "commit_file":
                        files[path] = entry
            if budget.truncated:
                return list(files.values()), False
            queue.extend(sorted(directories - has_children))
        return list(files.values()), True

    def _file_change(self, owner: str, name: str, head: str, entry: dict) -> Change:
        commit = (entry.get("commit") or {}).get("hash")
        commit = commit if _HASH.match(commit or "") else head
        ext = f"bitbucket:{owner}/{name}/src/{entry['path']}"
        path = f"bitbucket/{owner}/{name}/{entry['path']}"
        sf = SourceFile(
            external_id=ext, path=path, name=entry["path"].rsplit("/", 1)[-1], size=int(entry.get("size") or 0), modified_at=None,
            version=f"{int(entry.get('size') or 0)}-{commit}", acl=self.rules.for_path(path),
            mime=mimetypes.guess_type(entry["path"])[0] or "", extra={"commit": commit},
        )
        return Change("upsert", ext, sf)

    def _touched(self, owner: str, name: str, old: str, new: str, budget: Budget) -> list[str] | None:
        """Paths touched between two commits (both sides of every record), or None when diffstat cannot tell."""
        paths: set[str] = set()
        try:
            for values in self._pages(f"{self._repo_url(owner, name)}/diffstat/{new}..{old}", {"pagelen": self.page_size}, budget):
                for record in values:
                    for side in ("old", "new"):
                        if (record.get(side) or {}).get("path"):
                            paths.add(record[side]["path"])
        except AuthError:
            raise
        except HttpError:
            return None
        return None if budget.truncated else sorted(paths)

    def _settle(self, owner: str, name: str, head: str, path: str) -> Change:
        """What a path touched by a commit is now, at `head`."""
        ext = f"bitbucket:{owner}/{name}/src/{path}"
        try:
            entry = self._get(f"{self._repo_url(owner, name)}/src/{head}/{_path_url(path)}", {"format": "meta"}).json()
        except HttpError as exc:
            if exc.status == 404:
                return Change("delete", ext)
            raise
        entry.setdefault("path", path)
        return self._file_change(owner, name, head, entry) if self._wanted(entry) else Change("delete", ext)

    def _cursor(self, state: ScanState, prev: dict, heads: dict, **pos) -> dict:
        return {**state.cursor(**pos), "prev": dict(prev), "heads": dict(heads)}

    def _next_cursor(self, state, index, phase, prev, heads) -> dict:
        k = self.phases.index(phase)
        if k + 1 < len(self.phases):
            return self._cursor(state, prev, heads, repo=index, phase=self.phases[k + 1])
        return self._cursor(state, prev, heads, repo=index + 1, phase=self.phases[0])

    def _files_phase(self, index: int, state: ScanState, budget: Budget, prev: dict, heads: dict, resume: dict) -> Iterator[ScanBatch]:
        owner, name, ref = self.repos[index]
        key = f"{owner}/{name}"
        scope = f"bitbucket:{owner}/{name}/src/"
        head = resume.get("head") or self._head(owner, name, ref)
        if head is None:                                           # an empty repository
            heads[key] = ""
            yield ScanBatch([], self._next_cursor(state, index, "files", prev, heads), complete_scope=scope)
            return
        old = prev.get(key)
        diff = bool(state.incremental and old and resume.get("full") is not True)
        if diff and old == head:                                   # nothing was pushed
            heads[key] = head
            yield ScanBatch([], self._next_cursor(state, index, "files", prev, heads))
            return
        touched = self._touched(owner, name, old, head, budget) if diff else None
        if budget.truncated:
            return
        diff = touched is not None
        complete = True
        if diff:
            rows = [{"path": path} for path in touched]
            make = lambda row: self._settle(owner, name, head, row["path"])        # noqa: E731
        else:                                                      # first scan, full resync, or diffstat could not tell
            entries, complete = self._listing(owner, name, head, budget)
            rows = sorted((e for e in entries if self._wanted(e)), key=lambda e: e["path"])
            make = lambda row: self._file_change(owner, name, head, row)           # noqa: E731
        after = resume.get("after")
        if after:
            rows = [row for row in rows if row["path"] > after]
        chunks = [rows[i:i + self.batch_size] for i in range(0, len(rows), self.batch_size)] or [[]]
        for number, chunk in enumerate(chunks):
            chunk = budget.clip(chunk)
            last = number == len(chunks) - 1
            finished = last and complete and not budget.truncated
            if finished:
                heads[key] = head
            cursor = self._next_cursor(state, index, "files", prev, heads) if last else self._cursor(
                state, prev, heads, repo=index, phase="files", head=head, full=not diff,
                after=chunk[-1]["path"] if chunk else after)
            yield ScanBatch([make(row) for row in chunk], cursor, complete_scope=scope if (finished and not diff and not after) else None)
            if budget.truncated:
                return

    # ── issues ──

    def _render_issue(self, owner: str, name: str, issue: dict) -> tuple[str, bytes]:
        number = int(issue["id"])
        title = issue.get("title") or f"#{number}"
        person = lambda key: (issue.get(key) or {}).get("display_name")        # noqa: E731
        meta = {
            "repository": f"{owner}/{name}", "type": "issue", "state": issue.get("state"), "kind": issue.get("kind"),
            "priority": issue.get("priority"), "reporter": person("reporter"), "assignee": person("assignee"),
            "component": (issue.get("component") or {}).get("name"), "milestone": (issue.get("milestone") or {}).get("name"),
            "version": (issue.get("version") or {}).get("name"), "created": issue.get("created_on"),
            "updated": issue.get("updated_on"), "url": ((issue.get("links") or {}).get("html") or {}).get("href"),
        }
        comments = []
        for values in self._pages(f"{self._repo_url(owner, name)}/issues/{number}/comments", {"sort": "created_on", "pagelen": 100}):
            for comment in values:
                body = "" if comment.get("deleted") else _content(comment.get("content"))
                if body:
                    comments.append(f"### {(comment.get('user') or {}).get('display_name', 'unknown')} ({comment.get('created_on', '')})\n\n{body}")
        sections = [_content(issue.get("content"))]
        if comments:
            sections.append("## Comments\n\n" + "\n\n".join(comments))
        text = render_document(f"{owner}/{name}#{number}: {title}", meta, "\n\n".join(s for s in sections if s))
        return f"bitbucket/{owner}/{name}/_issues/{safe_name(f'{number}-{title}', str(number), 100)}.md", text

    def _issue_change(self, owner: str, name: str, issue: dict) -> Change:
        path, text = self._render_issue(owner, name, issue)
        ext = f"bitbucket:{owner}/{name}/issue/{issue['id']}"
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(issue.get("updated_on")),
            version=f"{issue.get('updated_on', '')}-{short_hash(text)}", acl=self.rules.for_path(path), mime="text/markdown",
            extra={"id": issue["id"]},
        )
        return Change("upsert", ext, sf)

    def _issues_phase(self, index: int, state: ScanState, budget: Budget, prev: dict, heads: dict, nxt: str | None) -> Iterator[ScanBatch]:
        owner, name, _ = self.repos[index]
        scope = f"bitbucket:{owner}/{name}/issue/"
        if not nxt and self._repo(owner, name).get("has_issues") is False:
            yield ScanBatch([], self._next_cursor(state, index, "issues", prev, heads), complete_scope=scope)
            return
        params = {"sort": "updated_on", "pagelen": self.page_size}
        since = None
        if state.incremental and state.since:
            since = (parse_dt(state.since) or parse_dt(0)).timestamp() - self.overlap
        elif self.since_floor:
            since = self.since_floor.timestamp()
        if since is not None:
            params["q"] = f"updated_on>{iso_z(parse_dt(since)).replace('Z', '+00:00')}"
        url, first = nxt or f"{self._repo_url(owner, name)}/issues", nxt is None
        while True:
            if not budget.allow_page(url):
                return
            body = self._get(url, params if first else None).json()
            changes = []
            for issue in budget.clip(body.get("values") or []):
                changes.append(self._issue_change(owner, name, issue))
                state.observe(issue.get("updated_on"))
            url, first = body.get("next"), False
            if url:
                yield ScanBatch(changes, self._cursor(state, prev, heads, repo=index, phase="issues", next=url))
                continue
            complete = scope if (state.mode == "full" and nxt is None and not budget.truncated) else None
            yield ScanBatch(changes, self._next_cursor(state, index, "issues", prev, heads), complete_scope=complete)
            return

    # ── scan ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        self._repos = {}
        previous = (cursor or {}).get("prev") if state.resumed else (cursor or {}).get("heads")
        prev: dict = dict(previous or {})
        heads: dict = dict((cursor or {}).get("heads") or {}) if state.resumed else {}
        start, phase0 = int(state.pos.get("repo", 0)), state.pos.get("phase")
        if phase0 not in self.phases:                 # the configuration changed since the cursor was written
            state.pos, phase0 = {}, self.phases[0]
        for index in range(start, len(self.repos)):
            if budget.stop_if_exhausted():
                break
            for phase in self.phases:
                if index == start and self.phases.index(phase) < self.phases.index(phase0):
                    continue
                resume = state.pos if (index == start and phase == phase0) else {}
                if phase == "files":
                    yield from self._files_phase(index, state, budget, prev, heads, resume)
                else:
                    yield from self._issues_phase(index, state, budget, prev, heads, resume.get("next"))
        final = state.final(truncated=budget.truncated)
        yield ScanBatch([], {**final, "heads": {**prev, **heads}}, done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = _EXTERNAL_ID.fullmatch(file.external_id)
        if not match or not (_valid_name(match.group(1)) and _valid_name(match.group(2))):
            raise HttpError(f"bitbucket: {file.external_id!r} is not a Bitbucket object id")
        owner, name, kind, rest = match.groups()
        if kind == "issue":
            if not rest.isdigit():
                raise HttpError(f"bitbucket: {file.external_id!r} is not an issue number")
            return io.BytesIO(self._render_issue(owner, name, self._json(f"{self._repo_url(owner, name)}/issues/{rest}"))[1])
        version = _VERSION.match(file.version or "")
        if not version or any(part in ("", ".", "..") for part in rest.split("/")):
            raise HttpError(f"bitbucket: {file.external_id!r} is not a file at a commit (`<size>-<commit>` version, plain path)")
        headers, secret = self._headers()
        try:
            return self.http.download(self._guard(f"{self._repo_url(owner, name)}/src/{version.group(2)}/{_path_url(rest)}"),
                                      max_bytes=self.max_bytes, headers=headers)
        except AuthError as exc:
            raise redacted(exc, secret, self._encoded(headers), hint=f"check the credentials in environment variable {self.secret_env}") from None
        except HttpError as exc:
            raise redacted(exc, secret, self._encoded(headers)) from None

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)

