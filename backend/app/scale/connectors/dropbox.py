"""Dropbox (personal and Business/team spaces) through the Dropbox API v2 HTTP endpoints.

Endpoints used (Dropbox HTTP API documentation, dropbox.com/developers/documentation/http/documentation):
  * POST api.dropboxapi.com/2/files/list_folder            recursive=true, limit<=2000, include_non_downloadable_files=false
  * POST api.dropboxapi.com/2/files/list_folder/continue   pages of a listing AND, with a saved cursor, the changes since
  * POST api.dropboxapi.com/2/files/list_revisions         only to tell whether a deleted path used to be a file or a folder
  * POST content.dropboxapi.com/2/files/download           argument in the Dropbox-API-Arg header
  * POST api.dropboxapi.com/oauth2/token                   refresh-token grant (optional)
Headers: Dropbox-API-Path-Root (`path_root_namespace_id`, for a Business team space) and Dropbox-API-Select-User
(`select_user`, a team admin acting for a member) when configured.

Behaviour
  * The returned `cursor` is the whole state. A full listing returns one cursor per page; after the last page the cursor
    is kept and the next scan calls list_folder/continue with it: only changes come back (files that were added or
    edited, and `deleted` entries). A cursor Dropbox answers with `reset` (409), or
    rejects as malformed (400), starts a new full scan.
  * Cursors: {"mode": "full" | "inc", "cursor": <resume point>} while a scan is running; {"cursor": <feed cursor>,
    "last_full": <epoch>} when it finished. NOTE: sync.run_scan currently overwrites the cursor of the done batch with
    {}, in which case each scan is simply a full one (correct, just not incremental).
  * `external_id` = the lower-cased full path (`path_lower`). Dropbox's `deleted` entries carry only a path, never an id,
    so a path is the only key under which a deletion can be applied; the price is that a rename or move is a delete
    plus an add. `path` = display path relative to `root_path`. open() and refresh_acl() need only these two.
  * Deletions: a `deleted` entry becomes Change("delete", path_lower). Dropbox reports a deleted FOLDER as one entry (its
    children are not listed), so every deleted path is classified with list_revisions (a file answers; a folder answers
    409 path/not_file) and a folder additionally emits a batch whose complete_scope is "<path_lower>/": the sync layer
    then deletes everything under that prefix that was not seen again in the scan. At most `deleted_lookup_limit`
    (default 200) lookups are made per scan; beyond that the next scan is a full one (`force_full`).
  * A full scan that ran from the start to the end sets complete_scope=""; a resumed one does not.
    `full_resync_hours` (default 168, 0 = never) forces a full scan periodically: it refreshes paths after folder moves
    and is the safety net for anything the feed did not report.
  * version = content_hash when present, else rev, plus the size. Skipped: folders, non-downloadable items (Paper docs,
    web links), names matching `skip_names`, files above `max_file_bytes` (default 200 MB).
  * Rate limits: 429 / 503 with Retry-After (and transport errors) are retried by `_http.Http`; 401 / 403 raise
    `AuthError`. With a refresh token a 401 first forces one token refresh.

Authentication (secrets by environment variable name only; read when used, never stored or logged): access_token_env,
or refresh_token_env + app_key_env + app_secret_env (the access token is minted, cached until shortly before it
expires, and refreshed on a 401).

Permissions: folder -> principals through `AclRules` (acl_rules / default_acl) on `path`, fail closed. Dropbox's own
sharing (shared-folder members) is NOT read.

Config: root_path (default "" = the whole namespace), access_token_env | refresh_token_env + app_key_env + app_secret_env,
path_root_namespace_id, select_user, page_size (<=2000), full_resync_hours, deleted_lookup_limit, skip_names,
max_file_bytes, max_retries, api_base, content_base, acl_rules, default_acl.

Not exercised against live Dropbox. Tested against a stateful in-memory fake of these endpoints behind an
httpx.MockTransport, following the documented request and response shapes (list_folder entries with ".tag", the
`reset` error, 429 bodies, Dropbox-API-Arg); nothing proves the real service behaves identically.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import mimetypes
import posixpath
import re
import threading
import time
from datetime import datetime
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import DEFAULT_MAX_BYTES, AuthError, Http, HttpError, env_secret
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
DEFAULT_SKIP_NAMES = ["thumbs.db", "desktop.ini", "~$*", ".ds_store", ".dropbox", ".dropbox.attr"]


class _Reset(Exception):
    """The cursor is no longer valid (Dropbox error tag `reset`): start a full scan."""


class DropboxAuth(httpx.Auth):
    def __init__(self, cfg: dict, *, api: str, transport: httpx.BaseTransport | None, sleep: Callable[[float], None],
                 clock: Callable[[], float]) -> None:
        self.cfg, self.api, self.clock = cfg, api, clock
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._http = Http(client=httpx.Client(transport=transport, timeout=httpx.Timeout(30.0, connect=15.0)),
                          sleep=sleep, max_retries=int(cfg.get("max_retries", 5)))

    @property
    def refreshable(self) -> bool:
        return bool(self.cfg.get("refresh_token_env"))

    def token(self, force: bool = False) -> str:
        if not self.refreshable:
            return env_secret(self.cfg.get("access_token_env"), "Dropbox access token")
        with self._lock:
            if force or self._token is None or self._token[1] - 300 < self.clock():
                self._token = self._refresh()
            return self._token[0]

    def _refresh(self) -> tuple[str, float]:
        refresh = env_secret(self.cfg["refresh_token_env"], "Dropbox refresh token")
        key = env_secret(self.cfg.get("app_key_env"), "Dropbox app key")
        secret = env_secret(self.cfg.get("app_secret_env"), "Dropbox app secret")
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
        try:
            body = self._http.request(
                "POST", f"{self.api}/oauth2/token", ok=(200,), headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            ).json()
        except HttpError as exc:
            if isinstance(exc, AuthError) or exc.status in (400, 401):
                code = re.search(r'"error"\s*:\s*"([^"]+)"', str(exc))
                raise AuthError(f"Dropbox rejected the refresh token ({exc.status}{', ' + code.group(1) if code else ''})",
                                exc.status) from None
            raise
        return body["access_token"], self.clock() + int(body.get("expires_in", 14400))

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = "Bearer " + self.token()
        response = yield request
        if response.status_code == 401 and self.refreshable:
            request.headers["Authorization"] = "Bearer " + self.token(force=True)
            yield request


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_tag(exc: HttpError) -> str:
    return re.sub(r"\s+", " ", str(exc))


class DropboxConnector:
    kind = "dropbox"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.cfg = config
        self.api = str(config.get("api_base", API)).rstrip("/")
        self.content = str(config.get("content_base", CONTENT)).rstrip("/")
        self.root_path = "/" + str(config.get("root_path", "")).strip("/") if str(config.get("root_path", "")).strip("/") else ""
        self.page_size = max(1, min(int(config.get("page_size", 2000)), 2000))
        self.full_resync_s = float(config.get("full_resync_hours", 168)) * 3600
        self.lookup_limit = int(config.get("deleted_lookup_limit", 200))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self._clock = clock
        headers = {}
        if config.get("path_root_namespace_id"):
            headers["Dropbox-API-Path-Root"] = json.dumps({".tag": "root", "root": str(config["path_root_namespace_id"])})
        if config.get("select_user"):
            headers["Dropbox-API-Select-User"] = str(config["select_user"])
        auth = DropboxAuth(config, api=self.api, transport=transport, sleep=sleep, clock=clock)
        self.http = Http(
            client=httpx.Client(auth=auth, headers=headers, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0),
                                follow_redirects=True),
            sleep=sleep, max_retries=int(config.get("max_retries", 5)),
        )
        self.stats: dict[str, int] = {}

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _rpc(self, route: str, body: dict) -> dict:
        try:
            return self.http.post_json(f"{self.api}/2/{route}", json=body, ok=(200,))  # type: ignore[return-value]
        except HttpError as exc:
            text = str(exc)
            if exc.status == 409 and ('"reset"' in text or "reset/" in text):
                raise _Reset() from None
            if exc.status == 400 and route.endswith("/continue") and "cursor" in text.lower():
                raise _Reset() from None        # a malformed / foreign cursor is as useless as a reset one
            raise

    # ── conversion ──

    def _relative(self, entry: dict) -> str:
        """The display path below `root_path` (whole path when the entry is not under it, which Dropbox does not send)."""
        parts = (entry.get("path_display") or entry.get("path_lower") or "").strip("/").split("/")
        root = [p.lower() for p in self.root_path.strip("/").split("/") if p]
        if root and [p.lower() for p in parts[:len(root)]] == root:
            parts = parts[len(root):]
        return "/".join(p for p in parts if p)

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    def _to_file(self, entry: dict) -> SourceFile | None:
        if entry.get(".tag") != "file" or entry.get("is_downloadable") is False:
            if entry.get(".tag") == "file":
                self._count("not_downloadable")
            return None
        external_id = (entry.get("path_lower") or entry.get("path_display") or "").lower()
        if not external_id:
            self._count("malformed")
            return None
        name = entry.get("name") or posixpath.basename(entry.get("path_display") or external_id)
        if not name or self._skip_name(name):
            self._count("skipped_name")
            return None
        size = int(entry.get("size") or 0)
        if size > self.max_bytes:
            log.info("dropbox file %s is %d bytes, over max_file_bytes; skipped", entry.get("path_lower"), size)
            self._count("oversize")
            return None
        path = self._relative(entry)
        marker = entry.get("content_hash") or entry.get("rev") or entry.get("server_modified") or ""
        return SourceFile(
            external_id=external_id, path=path, name=name, size=size,
            modified_at=_parse_dt(entry.get("server_modified") or entry.get("client_modified")),
            version=f"{marker}:{size}", acl=self.rules.for_path(path), mime=mimetypes.guess_type(name)[0] or "",
            extra={"id": entry.get("id", "")},
        )

    # ── scanning ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        cursor = dict(cursor or {})
        last_full = cursor.get("last_full")
        full_due = bool(self.full_resync_s) and (not last_full or self._clock() - float(last_full) > self.full_resync_s)
        try:
            if cursor.get("mode") == "full" and cursor.get("cursor"):
                yield from self._scan_full(cursor)
            elif cursor.get("mode") == "inc" and cursor.get("cursor"):
                yield from self._scan_incremental(cursor["cursor"], last_full)
            elif cursor.get("cursor") and not full_due and not cursor.get("force_full"):
                yield from self._scan_incremental(cursor["cursor"], last_full)
            else:
                yield from self._scan_full({})
        except _Reset:
            log.warning("dropbox cursor was reset by the server; starting a full scan")
            yield from self._scan_full({})

    def _scan_full(self, cursor: dict) -> Iterator[ScanBatch]:
        resumed = bool(cursor.get("cursor"))
        started = cursor.get("started") or self._clock()
        if resumed:
            body = self._rpc("files/list_folder/continue", {"cursor": cursor["cursor"]})
        else:
            body = self._rpc("files/list_folder", {
                "path": self.root_path, "recursive": True, "include_deleted": False, "include_non_downloadable_files": False,
                "include_mounted_folders": True, "limit": self.page_size,
            })
        while True:
            changes = []
            for entry in body.get("entries") or []:
                sf = self._to_file(entry)
                if sf is not None:
                    changes.append(Change("upsert", sf.external_id, sf))
            if body.get("has_more"):
                yield ScanBatch(changes, {"mode": "full", "cursor": body["cursor"], "started": started})
                body = self._rpc("files/list_folder/continue", {"cursor": body["cursor"]})
            else:
                yield ScanBatch(changes, {"cursor": body["cursor"], "last_full": started}, done=True,
                                complete_scope=None if resumed else "")
                return

    def _is_folder(self, path: str) -> bool | None:
        """True / False when list_revisions can tell, None when it cannot."""
        try:
            self._rpc("files/list_revisions", {"path": path, "mode": "path", "limit": 1})
            return False
        except HttpError as exc:
            if exc.status == 409 and "not_file" in str(exc):
                return True
            if exc.status == 409:
                return None
            raise

    def _scan_incremental(self, cursor: str, last_full) -> Iterator[ScanBatch]:
        lookups = 0
        force_full = False
        while True:
            body = self._rpc("files/list_folder/continue", {"cursor": cursor})
            latest: dict[str, Change] = {}
            folder_scopes: list[str] = []
            for entry in body.get("entries") or []:
                tag, lower = entry.get(".tag"), (entry.get("path_lower") or "")
                if not lower:
                    continue
                if tag == "deleted":
                    latest[lower] = Change("delete", lower)
                    if lookups < self.lookup_limit:
                        lookups += 1
                        if self._is_folder(lower) is not False:
                            folder_scopes.append(lower + "/")
                    else:
                        force_full = True
                elif tag == "file":
                    sf = self._to_file(entry)
                    # a file that is no longer indexable (now too big, not downloadable) must disappear
                    latest[lower] = Change("upsert", lower, sf) if sf is not None else Change("delete", lower)
            # Folder deletions first, carrying the cursor we are resuming from: a crash here repeats this page.
            for scope in folder_scopes:
                yield ScanBatch([], {"mode": "inc", "cursor": cursor, "last_full": last_full}, complete_scope=scope)
            changes = list(latest.values())
            if body.get("has_more"):
                cursor = body["cursor"]
                yield ScanBatch(changes, {"mode": "inc", "cursor": cursor, "last_full": last_full})
            else:
                final = {"cursor": body["cursor"], "last_full": last_full}
                if force_full:
                    final["force_full"] = True
                yield ScanBatch(changes, final, done=True, complete_scope=None)
                return

    # ── bytes / permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        try:
            return self.http.download(
                f"{self.content}/2/files/download", method="POST", max_bytes=self.max_bytes,
                headers={"Dropbox-API-Arg": json.dumps({"path": file.external_id})},
            )
        except HttpError as exc:
            if exc.status == 409:
                raise HttpError(f"Dropbox cannot download {file.external_id!r}: {_error_tag(exc)[-160:]}", 409) from None
            raise

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
