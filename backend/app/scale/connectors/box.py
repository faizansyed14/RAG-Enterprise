"""Box (box.com) folders and files through the Box Platform REST API v2.

Endpoints used (Box API reference, developer.box.com/reference):
  * GET  api.box.com/2.0/folders/{folder_id}/items   usemarker=true, limit<=1000, marker, fields=...  (Get items in a folder)
  * GET  api.box.com/2.0/files/{file_id}/content      302 to a short-lived download URL; 202 + Retry-After while not ready
  * POST api.box.com/oauth2/token                     client-credentials grant (optional, `box_subject_type` / `box_subject_id`)
Header As-User (`as_user`) when an enterprise service account acts for a managed user.

Behaviour
  * Every scan walks the tree below `root_folder_id` (default "0" = All Files) depth-first, one `folders/{id}/items` page at a
    time, and follows `next_marker` inside each folder. Folders are never emitted, `web_link` items are skipped.
  * The cursor is the walk's work list, {"stack": [[folder_id, path, marker], ...]}: exactly the folders still to read and the
    marker to continue a half-read folder. It is JSON, small (bounded by the subfolders of the pages read along the current
    path) and resumes the walk without re-reading finished folders. A resumed scan is not authoritative. A saved marker
    that Box rejects (400) makes the walk read that folder again from its first page.
  * A scan that ran from the root to the end without skipping a folder sets complete_scope="": whatever the sync layer saw
    before and did not see now is deleted. Box's change feed (events) is NOT used, so every scan is a full enumeration.
  * A subfolder that answers 403 (no access) or 404 (deleted during the walk) is skipped and the scan is then not
    authoritative (its files stay as they were). The root folder failing, a 401, and any other error abort the scan:
    a failed listing is never read as an empty folder.
  * `external_id` = the Box file id (stable across rename and move). `path` = folder names from the root, slash separated.
  * version = sha1 (content only), else the current file_version id, else etag; plus the size. Renaming or moving a file
    keeps its version, uploading identical bytes again keeps it, editing it changes it.
  * Skipped: names matching `skip_names` (files and folders), files above `max_file_bytes` (default 200 MB), items that are
    not files or folders. Download streams to a temporary file with the same cap; a 202 (file not ready) is retried with
    backoff up to `max_retries` times. Authorization is not forwarded to the download host (httpx drops it on a cross-origin
    redirect).
  * 429 / 502 / 503 / 504 and transport errors are retried by `_http.Http` (Retry-After honoured); 401 / 403 raise
    `AuthError` (with a client-credentials grant a 401 first forces one token renewal).

Authentication (secrets by environment variable name only; read when used, never stored or logged): access_token_env (a
developer token or an OAuth access token), or client credentials: client_id_env + client_secret_env + subject_type
("enterprise" | "user") + subject_id (a non-secret id). JWT app authentication and OAuth refresh tokens (Box rotates them on
every use, which cannot be kept in an environment variable) are not implemented.

Permissions: folder -> principals through `AclRules` (acl_rules / default_acl) on `path`, fail closed. Box collaborations
(who a folder is shared with) are NOT read.

Config: root_folder_id, access_token_env | client_id_env + client_secret_env + subject_type + subject_id, as_user, page_size
(<=1000), batch_size (changes per batch, default 500), max_calls_per_batch (listing requests per batch, default 50),
skip_names, max_file_bytes, max_retries, api_base, token_url, acl_rules, default_acl.

Not exercised against live Box. Tested against a stateful in-memory fake of these endpoints behind an httpx.MockTransport,
following the documented request and response shapes (folder item entries, `next_marker`, error bodies, the download
redirect); nothing proves the real service behaves identically.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import re
import threading
import time
from datetime import datetime
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import DEFAULT_MAX_BYTES, AuthError, Http, HttpError, env_secret
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

API = "https://api.box.com/2.0"
TOKEN_URL = "https://api.box.com/oauth2/token"
DEFAULT_SKIP_NAMES = ["thumbs.db", "desktop.ini", "~$*", ".ds_store"]
FIELDS = "id,type,name,size,sha1,etag,modified_at,file_version"


class BoxAuth(httpx.Auth):
    def __init__(self, cfg: dict, *, token_url: str, transport: httpx.BaseTransport | None, sleep: Callable[[float], None],
                 clock: Callable[[], float]) -> None:
        self.cfg, self.token_url, self.clock = cfg, token_url, clock
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._http = Http(client=httpx.Client(transport=transport, timeout=httpx.Timeout(30.0, connect=15.0)),
                          sleep=sleep, max_retries=int(cfg.get("max_retries", 5)))

    @property
    def minted(self) -> bool:
        return not self.cfg.get("access_token_env") and bool(self.cfg.get("client_id_env"))

    def token(self, force: bool = False) -> str:
        if not self.minted:
            return env_secret(self.cfg.get("access_token_env"), "Box access token")
        with self._lock:
            if force or self._token is None or self._token[1] - 300 < self.clock():
                self._token = self._mint()
            return self._token[0]

    def _mint(self) -> tuple[str, float]:
        client_id = env_secret(self.cfg.get("client_id_env"), "Box client id")
        client_secret = env_secret(self.cfg.get("client_secret_env"), "Box client secret")
        subject_type = str(self.cfg.get("subject_type") or "enterprise")
        subject_id = str(self.cfg.get("subject_id") or "")
        if subject_type not in ("enterprise", "user") or not subject_id:
            raise ValueError("client credentials need subject_type ('enterprise' or 'user') and subject_id")
        try:
            body = self._http.request(
                "POST", self.token_url, ok=(200,),
                data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                      "box_subject_type": subject_type, "box_subject_id": subject_id},
            ).json()
        except HttpError as exc:
            if isinstance(exc, AuthError) or exc.status in (400, 401):
                code = re.search(r'"error"\s*:\s*"([^"]+)"', str(exc))
                raise AuthError(f"Box rejected the client credentials ({exc.status}{', ' + code.group(1) if code else ''})",
                                exc.status) from None
            raise
        return body["access_token"], self.clock() + int(body.get("expires_in", 3600))

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = "Bearer " + self.token()
        response = yield request
        if response.status_code == 401 and self.minted:
            request.headers["Authorization"] = "Bearer " + self.token(force=True)
            yield request


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class BoxConnector:
    kind = "box"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.cfg = config
        self.api = str(config.get("api_base", API)).rstrip("/")
        self.root_id = str(config.get("root_folder_id", "0"))
        self.page_size = max(1, min(int(config.get("page_size", 1000)), 1000))
        self.batch_size = max(1, int(config.get("batch_size", 500)))
        self.max_calls = max(1, int(config.get("max_calls_per_batch", 50)))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.max_retries = int(config.get("max_retries", 5))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self._sleep = sleep
        headers = {"As-User": str(config["as_user"])} if config.get("as_user") else {}
        auth = BoxAuth(config, token_url=str(config.get("token_url", TOKEN_URL)), transport=transport, sleep=sleep, clock=clock)
        self.http = Http(
            client=httpx.Client(auth=auth, headers=headers, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0),
                                follow_redirects=True),
            sleep=sleep, max_retries=self.max_retries,
        )
        self.stats: dict[str, int] = {}

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    def _to_file(self, entry: dict, folder_path: str) -> SourceFile | None:
        fid, name = str(entry.get("id") or ""), entry.get("name") or ""
        if not fid or not name:
            self._count("malformed")
            return None
        if self._skip_name(name):
            self._count("skipped_name")
            return None
        size = int(entry.get("size") or 0)
        if size > self.max_bytes:
            log.info("box file %s is %d bytes, over max_file_bytes; skipped", fid, size)
            self._count("oversize")
            return None
        path = f"{folder_path}/{name}" if folder_path else name
        marker = entry.get("sha1") or (entry.get("file_version") or {}).get("id") or entry.get("etag") or ""
        return SourceFile(
            external_id=fid, path=path, name=name, size=size, modified_at=_parse_dt(entry.get("modified_at")),
            version=f"{marker}:{size}", acl=self.rules.for_path(path), mime=mimetypes.guess_type(name)[0] or "",
        )

    def _items(self, folder_id: str, marker: str | None) -> dict:
        params = {"usemarker": "true", "limit": self.page_size, "fields": FIELDS}
        if marker:
            params["marker"] = marker
        return self.http.get_json(f"{self.api}/folders/{folder_id}/items", params=params, ok=(200,))  # type: ignore[return-value]

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        resumed = bool((cursor or {}).get("stack"))
        stack: list[list] = [list(t) for t in cursor["stack"]] if resumed else [[self.root_id, "", None]]
        skipped_folder = False
        changes: list[Change] = []
        calls = 0
        while stack:
            folder_id, path, marker = stack.pop()
            try:
                page = self._items(str(folder_id), marker)
            except HttpError as exc:
                if exc.status == 400 and marker:             # a saved marker Box no longer accepts: read the folder again
                    log.warning("box marker for folder %s was rejected; reading the folder from its start", folder_id)
                    self._count("marker_reset")
                    stack.append([folder_id, path, None])
                    continue
                if str(folder_id) != self.root_id and exc.status in (403, 404):
                    log.warning("box folder %s skipped (%s)", folder_id, exc.status)
                    self._count("folder_unreadable")
                    skipped_folder = True
                    continue
                raise
            calls += 1
            subfolders: list[list] = []
            for entry in page.get("entries") or []:
                kind = entry.get("type")
                if kind == "folder":
                    name = entry.get("name") or ""
                    if not entry.get("id") or not name:
                        self._count("malformed")
                    elif self._skip_name(name):
                        self._count("skipped_name")
                    else:
                        subfolders.append([str(entry["id"]), f"{path}/{name}" if path else name, None])
                elif kind == "file":
                    sf = self._to_file(entry, path)
                    if sf is not None:
                        changes.append(Change("upsert", sf.external_id, sf))
                else:
                    self._count("not_a_file")
            if page.get("next_marker"):
                stack.append([folder_id, path, page["next_marker"]])
            stack.extend(reversed(subfolders))             # the first subfolder is read next
            if not stack:
                yield ScanBatch(changes, {}, done=True, complete_scope="" if not resumed and not skipped_folder else None)
                return
            if len(changes) >= self.batch_size or calls >= self.max_calls:
                yield ScanBatch(changes, {"stack": [list(t) for t in stack]})
                changes, calls = [], 0
        # only reachable when the last folder in the work list was skipped: that scan cannot prove absence
        yield ScanBatch(changes, {}, done=True, complete_scope=None)

    def open(self, file: SourceFile) -> BinaryIO:
        url = f"{self.api}/files/{file.external_id}/content"
        for attempt in range(self.max_retries + 1):
            try:
                return self.http.download(url, max_bytes=self.max_bytes)
            except HttpError as exc:
                if exc.status != 202:
                    raise
                if attempt >= self.max_retries:
                    raise HttpError(f"Box still has not prepared file {file.external_id} for download "
                                    f"after {self.max_retries + 1} attempts (202)", 202) from None
                self._sleep(min(2 ** attempt, 30))
        raise HttpError(f"Box download of {file.external_id} gave up")        # pragma: no cover

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
