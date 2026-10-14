"""Confluence wiki pages (Cloud and Data Center / Server) as virtual files, one Markdown file per page.

Endpoints (Confluence Cloud REST API v1 "Content" and "Content search" groups; Data Center: the "content"
resource of the Confluence REST API; Atlassian's "Advanced searching using CQL" for the query language):

  GET {api}/content?type=page&spaceKey=KEY&expand=body.storage,version,space,ancestors&limit=N
        paged with `start` / `limit`; the next page is the server-supplied `_links.next` (relative to
        `_links.base`), followed until it is absent. Used for the full enumeration, one space at a time.
  GET {api}/content/search?cql=...&expand=...&limit=N
        incremental scans: `type in (...) AND space in (...) AND lastmodified >= "yyyy-MM-dd HH:mm"`.
  GET {api}/content?type=page&status=trashed&spaceKey=KEY
        incremental scans: every trashed page becomes a delete (idempotent for pages we never indexed).
  GET {api}/content/{id}?expand=...      used by open() to re-render exactly one page.

  {api} is `/wiki/rest/api` for Cloud (`deployment: "cloud"`, the default) and `/rest/api` for Data Center
  (`deployment: "datacenter"`; put any context path such as `/confluence` into `base_url`).

Config: base_url, deployment, auth ("basic" | "bearer"), email | email_env (basic user), token_env (API token,
password or personal access token), space_keys ([] = every space the account can read), content_types
(["page"], optionally "blogpost"), body_format ("storage" | "view" | "export_view"), page_size (50), max_items,
max_pages, full_resync_hours (24), overlap_minutes (1440), detect_trashed (true), skip_restricted_pages (true),
acl_rules, default_acl.

Objects: external_id `confluence:<content id>`, path `confluence/<SPACEKEY>/<ancestor titles>/<Title>.md`,
version `<version.number>-<hash of the rendered text>` (so moving or renaming a page also re-indexes it).

Incremental scans and deletions: a finished scan leaves `{"since", "last_full"}` in the cursor. The next scan
asks only for pages modified since then (minus `overlap_minutes`: CQL date literals are interpreted in the
searching user's time zone, so a generous overlap is the safe choice; unchanged pages cost nothing downstream
because their version is identical). Confluence has no feed of hard-deleted pages, so every
`full_resync_hours` the scan is a full enumeration, and only that one declares unseen pages deleted.
(Until the sync layer persists the cursor of a finished scan, every scan is simply a full one.)

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. everything
under `confluence/HR` is readable by `group:hr`; no match and no `default_acl` => nobody). This is not
Confluence's native per-page permission model. As a safety net, when the API returns direct read
restrictions for a page (`restrictions.read.restrictions.user|group`) that page is left out of the index
(`skip_restricted_pages`); RESTRICTIONS INHERITED FROM AN ANCESTOR ARE NOT DETECTED, so do not point this
connector at a space that relies on page restrictions without separate review.

What this does NOT do: attachments, comments, page history, whiteboards/databases, Cloud's v2 API
(`/wiki/api/v2/pages`, cursor based). Macros that fetch other content (include, children, jira) render as
nothing.

Not exercised against a live Confluence; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): the exact JSON of the `restrictions` expansion and whether every Data Center
release honours `status=trashed` on the list endpoint.
"""

from __future__ import annotations

import io
import re
import time
from datetime import timedelta
from typing import BinaryIO, Callable, Iterator
from urllib.parse import urlparse

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import (
    Budget, ScanState, basic_auth, html_to_text, parse_dt, redacted, short_hash,
)
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

_SPACE_KEY = re.compile(r"^[~A-Za-z0-9_.-]+$")
_TYPES = {"page", "blogpost"}


class ConfluenceConnector:
    kind = "confluence"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        if not config.get("base_url"):
            raise ValueError("confluence: config needs base_url (e.g. https://acme.atlassian.net)")
        self.cfg = config
        self.site = config["base_url"].rstrip("/")
        self.cloud = config.get("deployment", "cloud") == "cloud"
        self.context = "/wiki" if self.cloud else ""
        self.api = f"{self.context}/rest/api"
        self.auth = config.get("auth", "basic" if self.cloud else "bearer")
        self.space_keys: list[str] = list(config.get("space_keys") or [])
        for key in self.space_keys:
            if not _SPACE_KEY.match(key):
                raise ValueError(f"confluence: unusable space key {key!r}")
        self.types: list[str] = list(config.get("content_types") or ["page"])
        if not set(self.types) <= _TYPES:
            raise ValueError(f"confluence: content_types must be a subset of {sorted(_TYPES)}")
        self.body_format = config.get("body_format", "storage")
        self.page_size = int(config.get("page_size", 50))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = timedelta(minutes=float(config.get("overlap_minutes", 1440)))
        self.detect_trashed = bool(config.get("detect_trashed", True))
        self.skip_restricted = bool(config.get("skip_restricted_pages", True))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.http = Http(self.site, transport=transport, client=client, sleep=sleep)

    # ── http ──

    def _headers(self) -> tuple[dict[str, str], str]:
        token = env_secret(self.cfg.get("token_env"), "Confluence API token / personal access token")
        if self.auth == "bearer":
            return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, token
        user = self.cfg.get("email") or env_secret(self.cfg.get("email_env"), "Confluence account email")
        return {"Authorization": basic_auth(user, token), "Accept": "application/json"}, token

    def _get(self, url: str, params: dict | None = None) -> dict:
        headers, token = self._headers()
        try:
            return self.http.get_json(url, params={k: v for k, v in (params or {}).items() if v is not None} or None,
                                      headers=headers)
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the credential in environment variable {self.cfg.get('token_env')}") from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    def _next(self, page: dict) -> str | None:
        """The server's `_links.next` (relative to `_links.base`), re-rooted on the configured host: the
        credentials are never sent anywhere else, whatever the response says."""
        links = page.get("_links") or {}
        nxt = links.get("next")
        if not nxt:
            return None
        site = urlparse(self.site)
        origin = f"{site.scheme}://{site.netloc}"
        if nxt.startswith(("http://", "https://")):
            parsed = urlparse(nxt)
            return origin + parsed.path + (f"?{parsed.query}" if parsed.query else "")
        root = urlparse(links["base"]).path if links.get("base") else site.path + self.context
        return origin + root.rstrip("/") + nxt

    # ── rendering ──

    @property
    def _expand(self) -> str:
        fields = [f"body.{self.body_format}", "version", "space", "ancestors"]
        if self.skip_restricted:
            fields += ["restrictions.read.restrictions.user", "restrictions.read.restrictions.group"]
        return ",".join(fields)

    @staticmethod
    def _external_id(content_id: str) -> str:
        return f"confluence:{content_id}"

    def _restricted(self, item: dict) -> bool:
        read = ((item.get("restrictions") or {}).get("read") or {}).get("restrictions") or {}
        return any((read.get(kind) or {}).get("results") for kind in ("user", "group"))

    def _render(self, item: dict) -> tuple[str, str, bytes]:
        """(path, title, bytes) of one content object."""
        title = item.get("title") or f"Untitled {item.get('id', '')}"
        space = item.get("space") or {}
        key = space.get("key") or "_"
        ancestors = [a.get("title") or "" for a in item.get("ancestors") or []]
        version = item.get("version") or {}
        body = ((item.get("body") or {}).get(self.body_format) or {}).get("value", "")
        webui = (item.get("_links") or {}).get("webui")
        meta = {
            "space": f"{space.get('name') or key} ({key})",
            "breadcrumb": " > ".join(a for a in ancestors if a) or None,
            "last updated": version.get("when"),
            "updated by": (version.get("by") or {}).get("displayName"),
            "url": f"{self.site}{self.context}{webui}" if webui else None,
        }
        text = render_document(title, meta, html_to_text(body))
        segments = ["confluence", safe_name(key, "_", 40)] + [safe_name(a, "untitled", 80) for a in ancestors if a]
        path = "/".join(segments + [safe_name(title, "untitled", 100) + ".md"])
        return path, title, text

    def _to_change(self, item: dict, phase: str, state: ScanState) -> Change | None:
        content_id = str(item.get("id", ""))
        if not content_id or item.get("type") not in self.types:
            return None
        ext = self._external_id(content_id)
        if phase == "trashed" or item.get("status") == "trashed":
            return Change("delete", ext)
        if item.get("status") not in (None, "current"):
            return None
        if self.skip_restricted and self._restricted(item):
            return Change("delete", ext)            # restricted pages must not be (or stay) searchable
        path, title, text = self._render(item)
        version = item.get("version") or {}
        state.observe(version.get("when"))
        sf = SourceFile(
            external_id=ext, path=path, name=safe_name(title, "untitled", 100) + ".md", size=len(text),
            modified_at=parse_dt(version.get("when")),
            version=f"{version.get('number', 0)}-{short_hash(text)}", acl=self.rules.for_path(path),
            mime="text/markdown", extra={"space": (item.get("space") or {}).get("key", "")},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    @staticmethod
    def _quoted(values: list[str]) -> str:
        return ",".join(f'"{v}"' for v in values)

    def _phases(self, state: ScanState) -> list[tuple[str, str, dict]]:
        spaces: list[str | None] = list(self.space_keys) or [None]
        if state.incremental:
            since = (parse_dt(state.since) or parse_dt(0)) - self.overlap
            cql = [f"type in ({self._quoted(self.types)})"]
            if self.space_keys:
                cql.append(f"space in ({self._quoted(self.space_keys)})")
            cql.append(f'lastmodified >= "{since.strftime("%Y-%m-%d %H:%M")}"')
            phases = [("search", f"{self.api}/content/search",
                       {"cql": " AND ".join(cql) + " order by lastmodified asc", "expand": self._expand, "limit": self.page_size})]
            if self.detect_trashed:
                phases += [("trashed", f"{self.api}/content",
                            {"type": t, "status": "trashed", "spaceKey": sp, "expand": "version,space", "limit": self.page_size})
                           for sp in spaces for t in self.types]
            return phases
        return [("list", f"{self.api}/content",
                 {"type": t, "spaceKey": sp, "expand": self._expand, "limit": self.page_size})
                for sp in spaces for t in self.types]

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        phases = self._phases(state)
        start = int(state.pos.get("phase", 0))
        stopped = False
        for i in range(start, len(phases)):
            phase, path, params = phases[i]
            url = state.pos.get("next") if i == start else None
            first = url is None
            while True:
                if not budget.allow_page(url or (i, "first")):
                    stopped = True
                    break
                page = self._get(url or path, params if first else None)
                changes = [c for c in (self._to_change(item, phase, state) for item in budget.clip(page.get("results") or [])) if c]
                nxt = self._next(page)
                yield ScanBatch(changes, state.cursor(phase=i, next=nxt) if nxt else state.cursor(phase=i + 1))
                if not nxt:
                    break
                url, first = nxt, False
            if stopped:
                break
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True,
                        complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        content_id = file.external_id.split(":", 1)[-1]
        if not content_id.isdigit():
            raise HttpError(f"confluence: {file.external_id!r} is not a content id")
        item = self._get(f"{self.api}/content/{content_id}", {"expand": self._expand})
        return io.BytesIO(self._render(item)[2])

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
