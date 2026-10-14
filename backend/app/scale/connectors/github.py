"""GitHub repositories (github.com and GitHub Enterprise Server): files, and optionally issues / pull requests with
their discussions, as virtual files.

Endpoints (GitHub REST API, version 2022-11-28: "Repositories", "Git trees", "Git blobs", "Issues",
"Issue comments", "Pull request reviews / review comments", "Using pagination", "Rate limits"):

  GET /repos/{o}/{r}                                   default branch (when the repo entry names no ref).
  GET /repos/{o}/{r}/git/trees/{ref}?recursive=1       the whole file tree in one response. If `truncated` is true
        (more than ~100k entries) the tree is walked one directory at a time with GET .../git/trees/{sha}.
  GET /repos/{o}/{r}/git/blobs/{sha}   (Accept: application/vnd.github.raw+json)    file bytes, by blob sha: open().
  GET /repos/{o}/{r}/issues?state=all&sort=updated&direction=asc&since=T&per_page=100
        `Link: <...>; rel="next"` pagination, followed until absent (only on the configured host). Pull requests also
        come back from this endpoint (they carry a `pull_request` key).
  GET /repos/{o}/{r}/issues/{n}/comments, /pulls/{n}/reviews, /pulls/{n}/comments      discussions, same pagination.

Config: token_env (a fine-grained or classic token with read access; secrets are never in the config), api_url
(`https://api.github.com`; Enterprise Server: `https://ghe.example.com/api/v3`), repos (["owner/repo", "owner/repo@ref",
{"repo": "owner/repo", "ref": "main"}]), include (fnmatch globs on the path, default everything), exclude (default:
vendored / generated directories), max_file_mb (10), include_files (true), include_issues (false), include_pulls (false),
include_pr_reviews (true), issues_state ("all"), issues_since (ISO date), page_size (100), batch_size (200), max_items,
max_pages, full_resync_hours (24), overlap_minutes (5), acl_rules, default_acl.

Objects (external ids are the stable identity, `version` the change detector):
  file   external_id `github:<o>/<r>/blob/<path>`   path `github/<o>/<r>/<path>`    version = the git blob sha
  issue  external_id `github:<o>/<r>/issue/<n>`     path `github/<o>/<r>/_issues/<n>-<title>.md` (`_pulls` for PRs)
         version = `<updated_at>-<hash of the rendered text>`
The file listing is complete on every scan (one tree request per repo; unchanged blobs cost nothing downstream), so each
repo's file listing is authoritative for `github:<o>/<r>/blob/` whenever it ran from the start to the end without
hitting `max_items` / `max_pages`. Issues use `since` (the newest `updated_at` seen, minus `overlap_minutes`) after a
finished scan; every `full_resync_hours` the issue listing is complete and authoritative for `github:<o>/<r>/issue/`.
(Until the sync layer persists the cursor of a finished scan, every scan is a full one.)

Rate limits: 429 and secondary-limit responses honour Retry-After; GitHub's 403 "rate limit exceeded" (which
`_http.Http` would treat as bad credentials) is rewritten to 429 with a wait computed from `x-ratelimit-reset` by the
transport wrapper `ThrottleAs429`. This wrapper is installed only when the connector builds its own HTTP client.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `github/acme/payments`
readable by `group:payments`; no match and no `default_acl` => nobody). It is not GitHub's repository permission model;
whoever may read a private repo on GitHub is not looked up. Mirror it in the rules.

What this does NOT do: git history, branches other than the one named, forks, wikis, releases, gists, discussions,
Actions logs, submodule contents, LFS pointers' targets, symlinks. Branch names containing `/` are sent percent-encoded
and have not been verified against a live server.

Not exercised against a live GitHub; tested against fixtures shaped like the vendor documentation.
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
from app.scale.connectors._objects import (
    Budget, ScanState, ThrottleAs429, parse_dt, parse_link_next, redacted, short_hash,
)
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

DEFAULT_EXCLUDE = ["node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", ".git/*", "*/.git/*", "dist/*",
                   "*/dist/*", "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "*.png", "*.jpg",
                   "*.jpeg", "*.gif", "*.ico", "*.woff", "*.woff2", "*.ttf", "*.zip", "*.gz", "*.jar", "*.class", "*.pyc"]
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _valid_name(name: str) -> bool:
    return bool(_NAME.match(name)) and name not in (".", "..")


def github_throttle_wait(response: httpx.Response) -> float | None:
    """Seconds to wait if this 403 is rate limiting (primary or secondary), None if it is a real permission error."""
    headers = response.headers
    if headers.get("retry-after"):
        try:
            return float(headers["retry-after"])
        except ValueError:
            return 60.0
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(float(headers.get("x-ratelimit-reset", "0")) - time.time(), 1.0)
        except ValueError:
            return 60.0
    return 60.0 if "rate limit" in response.text.lower() else None


class GitHubConnector:
    kind = "github"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.api = config.get("api_url", "https://api.github.com").rstrip("/")
        self.repos = []
        for entry in config.get("repos") or []:
            parsed = self._parse_repo(entry)
            if all(parsed[:2] != other[:2] for other in self.repos):        # a repository listed twice is scanned once
                self.repos.append(parsed)
        if not self.repos:
            raise ValueError("github: config needs `repos` (e.g. [\"owner/repo\"])")
        self.include = list(config.get("include") or ["*"])
        self.exclude = list(config.get("exclude", DEFAULT_EXCLUDE))
        self.max_bytes = int(float(config.get("max_file_mb", 10)) * 1024 * 1024)
        self.files = bool(config.get("include_files", True))
        self.issues = bool(config.get("include_issues", False))
        self.pulls = bool(config.get("include_pulls", False))
        self.reviews = bool(config.get("include_pr_reviews", True))
        self.issues_state = config.get("issues_state", "all")
        self.issues_since = parse_dt(config.get("issues_since"))
        self.page_size = int(config.get("page_size", 100))
        self.batch_size = int(config.get("batch_size", 200))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_minutes", 5)) * 60
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        if client is None:
            transport = ThrottleAs429(transport or httpx.HTTPTransport(), github_throttle_wait)
        self.http = Http(self.api, transport=transport, client=client, sleep=sleep)

    @staticmethod
    def _parse_repo(entry) -> tuple[str, str, str | None]:
        ref = None
        if isinstance(entry, dict):
            entry, ref = entry["repo"], entry.get("ref")
        elif "@" in entry:
            entry, ref = entry.split("@", 1)
        owner, _, name = entry.partition("/")
        if not (_valid_name(owner) and _valid_name(name)):
            raise ValueError(f"github: unusable repository {entry!r} (expected owner/repo)")
        return owner, name, ref

    # ── http ──

    def _headers(self, accept: str = "application/vnd.github+json") -> tuple[dict[str, str], str]:
        token = env_secret(self.cfg.get("token_env"), "GitHub token")
        return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}, token

    def _guard(self, url: str) -> str:
        if url.startswith(("http://", "https://")) and urlparse(url).netloc != urlparse(self.api).netloc:
            raise HttpError(f"github: refusing to follow a link to another host ({urlparse(url).netloc})")
        return url

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        headers, token = self._headers()
        try:
            return self.http.get(self._guard(url), params=params, headers=headers)
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    def _json(self, url: str, params: dict | None = None):
        return self._get(url, params).json()

    def _all(self, url: str, params: dict | None = None) -> list[dict]:
        """Every item of a Link-paginated list endpoint."""
        items: list[dict] = []
        budget = Budget()
        while url:
            budget.allow_page(url)
            response = self._get(url, params)
            items.extend(response.json())
            url, params = parse_link_next(response.headers.get("link")), None
        return items

    # ── files ──

    def _wanted(self, entry: dict) -> bool:
        if entry.get("type") != "blob" or entry.get("mode") == "120000":
            return False
        path = entry["path"]
        if int(entry.get("size") or 0) > self.max_bytes:
            return False
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.include):
            return False
        return not any(fnmatch.fnmatch(path, pattern) for pattern in self.exclude)

    def _tree(self, owner: str, name: str, ref: str) -> list[dict]:
        try:
            data = self._json(f"/repos/{owner}/{name}/git/trees/{quote(ref, safe='')}", {"recursive": "1"})
        except HttpError as exc:
            if exc.status == 409:                                  # "Git Repository is empty."
                return []
            raise
        if not data.get("truncated"):
            return data.get("tree") or []
        entries: list[dict] = []                                   # too large for one response: walk it directory by directory
        stack = [("", data["sha"])]
        seen = set()
        while stack:
            prefix, sha = stack.pop()
            if sha in seen:
                continue
            seen.add(sha)
            for entry in self._json(f"/repos/{owner}/{name}/git/trees/{sha}").get("tree") or []:
                if entry.get("type") == "tree":
                    stack.append((f"{prefix}{entry['path']}/", entry["sha"]))
                else:
                    entries.append({**entry, "path": prefix + entry["path"]})
        return entries

    def _file_change(self, owner: str, name: str, entry: dict) -> Change:
        path = f"github/{owner}/{name}/{entry['path']}"
        ext = f"github:{owner}/{name}/blob/{entry['path']}"
        sf = SourceFile(
            external_id=ext, path=path, name=entry["path"].rsplit("/", 1)[-1], size=int(entry.get("size") or 0),
            modified_at=None, version=entry["sha"], acl=self.rules.for_path(path),
            mime=mimetypes.guess_type(entry["path"])[0] or "", extra={"sha": entry["sha"]},
        )
        return Change("upsert", ext, sf)

    def _files_phase(self, index: int, state: ScanState, budget: Budget, after: str | None) -> Iterator[ScanBatch]:
        owner, name, ref = self.repos[index]
        if not budget.allow_page(("tree", owner, name)):
            return
        ref = ref or self._json(f"/repos/{owner}/{name}").get("default_branch") or "HEAD"
        entries = sorted((e for e in self._tree(owner, name, ref) if self._wanted(e)), key=lambda e: e["path"])
        if after:
            entries = [e for e in entries if e["path"] > after]
        scope = f"github:{owner}/{name}/blob/"
        after_phase = (lambda: state.cursor(repo=index, phase="issues")) if (self.issues or self.pulls) \
            else (lambda: state.cursor(repo=index + 1, phase="files"))
        chunks = [entries[i:i + self.batch_size] for i in range(0, len(entries), self.batch_size)] or [[]]
        for number, chunk in enumerate(chunks):
            chunk = budget.clip(chunk)
            last = number == len(chunks) - 1
            cursor = after_phase() if last else state.cursor(repo=index, phase="files", after=chunk[-1]["path"] if chunk else after)
            complete = scope if (last and not after and not budget.truncated) else None
            yield ScanBatch([self._file_change(owner, name, e) for e in chunk], cursor, complete_scope=complete)
            if budget.truncated:
                return

    # ── issues and pull requests ──

    @staticmethod
    def _strip(body: str | None) -> str:
        return _HTML_COMMENT.sub("", body or "").strip()

    def _discussion(self, owner: str, name: str, number: int, is_pr: bool) -> list[str]:
        base = f"/repos/{owner}/{name}"
        blocks: list[str] = []
        comments = [f"### {(c.get('user') or {}).get('login', 'ghost')} ({c.get('created_at', '')})\n\n{self._strip(c.get('body'))}"
                    for c in self._all(f"{base}/issues/{number}/comments", {"per_page": 100}) if self._strip(c.get("body"))]
        if comments:
            blocks.append("## Comments\n\n" + "\n\n".join(comments))
        if is_pr and self.reviews:
            reviews = []
            for r in self._all(f"{base}/pulls/{number}/reviews", {"per_page": 100}):
                if r.get("state") == "PENDING":
                    continue
                login = (r.get("user") or {}).get("login", "ghost")
                reviews.append(f"### {login}: {r.get('state', '')} ({r.get('submitted_at', '')})\n\n{self._strip(r.get('body'))}".rstrip())
            if reviews:
                blocks.append("## Reviews\n\n" + "\n\n".join(reviews))
            inline = [f"### {(c.get('user') or {}).get('login', 'ghost')} on {c.get('path', '')} ({c.get('created_at', '')})\n\n{self._strip(c.get('body'))}"
                      for c in self._all(f"{base}/pulls/{number}/comments", {"per_page": 100}) if self._strip(c.get("body"))]
            if inline:
                blocks.append("## Review comments\n\n" + "\n\n".join(inline))
        return blocks

    def _render_issue(self, owner: str, name: str, item: dict) -> tuple[str, bytes]:
        is_pr = bool(item.get("pull_request"))
        number = int(item["number"])
        title = item.get("title") or f"#{number}"
        names = lambda key, field: ", ".join(x.get(field, "") for x in item.get(key) or []) or None    # noqa: E731
        meta = {
            "repository": f"{owner}/{name}", "type": "pull request" if is_pr else "issue", "state": item.get("state"),
            "author": (item.get("user") or {}).get("login"), "labels": names("labels", "name"),
            "assignees": names("assignees", "login"), "milestone": (item.get("milestone") or {}).get("title"),
            "created": item.get("created_at"), "updated": item.get("updated_at"), "closed": item.get("closed_at"),
            "url": item.get("html_url"),
        }
        sections = [self._strip(item.get("body"))] + self._discussion(owner, name, number, is_pr)
        text = render_document(f"{owner}/{name}#{number}: {title}", meta, "\n\n".join(s for s in sections if s))
        folder = "_pulls" if is_pr else "_issues"
        return f"github/{owner}/{name}/{folder}/{safe_name(f'{number}-{title}', str(number), 100)}.md", text

    def _wants_item(self, item: dict) -> bool:
        return self.pulls if item.get("pull_request") else self.issues

    def _issue_change(self, owner: str, name: str, item: dict, state: ScanState) -> Change:
        is_pr = bool(item.get("pull_request"))
        path, text = self._render_issue(owner, name, item)
        state.observe(item.get("updated_at"))
        ext = f"github:{owner}/{name}/issue/{item['number']}"
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(text), modified_at=parse_dt(item.get("updated_at")),
            version=f"{item.get('updated_at', '')}-{short_hash(text)}", acl=self.rules.for_path(path), mime="text/markdown",
            extra={"number": item["number"], "pull": is_pr},
        )
        return Change("upsert", ext, sf)

    def _issues_phase(self, index: int, state: ScanState, budget: Budget, nxt: str | None) -> Iterator[ScanBatch]:
        owner, name, _ = self.repos[index]
        params = {"state": self.issues_state, "sort": "updated", "direction": "asc", "per_page": self.page_size}
        since = None
        if state.incremental and state.since:
            since = (parse_dt(state.since) or parse_dt(0)).timestamp() - self.overlap
        elif self.issues_since:
            since = self.issues_since.timestamp()
        if since is not None:
            params["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
        url, first = nxt or f"/repos/{owner}/{name}/issues", nxt is None
        scope = f"github:{owner}/{name}/issue/"
        while True:
            if not budget.allow_page(url):
                return
            response = self._get(url, params if first else None)
            rows = budget.clip([item for item in response.json() if self._wants_item(item)])
            changes = [self._issue_change(owner, name, item, state) for item in rows]
            url, first = parse_link_next(response.headers.get("link")), False
            if url:
                yield ScanBatch(changes, state.cursor(repo=index, phase="issues", next=url))
                continue
            complete = scope if (state.mode == "full" and nxt is None and not budget.truncated) else None
            yield ScanBatch(changes, state.cursor(repo=index + 1, phase="files"), complete_scope=complete)
            return

    # ── scan ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        start, phase0 = int(state.pos.get("repo", 0)), state.pos.get("phase", "files")
        for index in range(start, len(self.repos)):
            for phase in ("files", "issues"):
                here = index == start and phase == phase0
                if index == start and phase == "files" and phase0 == "issues":
                    continue
                if phase == "files" and not self.files:
                    continue
                if phase == "issues" and not (self.issues or self.pulls):
                    continue
                if phase == "files":
                    yield from self._files_phase(index, state, budget, state.pos.get("after") if here else None)
                else:
                    yield from self._issues_phase(index, state, budget, state.pos.get("next") if here else None)
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = re.fullmatch(r"github:([^/]+)/([^/]+)/(blob|issue)/(.+)", file.external_id, re.S)
        if not match or not (_valid_name(match.group(1)) and _valid_name(match.group(2))):
            raise HttpError(f"github: {file.external_id!r} is not a GitHub object id")
        owner, name, kind, rest = match.groups()
        if kind == "issue":
            if not rest.isdigit():
                raise HttpError(f"github: {file.external_id!r} is not an issue number")
            item = self._json(f"/repos/{owner}/{name}/issues/{rest}")
            return io.BytesIO(self._render_issue(owner, name, item)[1])
        if not _SHA.match(file.version or ""):
            raise HttpError(f"github: {file.external_id!r} has no blob sha as its version")
        headers, token = self._headers("application/vnd.github.raw+json")
        try:
            return self.http.download(self._guard(f"/repos/{owner}/{name}/git/blobs/{file.version}"),
                                      max_bytes=self.max_bytes, headers=headers)
        except HttpError as exc:
            raise redacted(exc, token) from None

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
