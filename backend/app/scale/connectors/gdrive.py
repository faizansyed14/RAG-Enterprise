"""Google Drive (My Drive, shared-with-me and shared drives) through the Drive API v3. Also hosts the Google
authentication and HTTP helpers that gcs.py imports (`GoogleAuth`, `GoogleClient`).

REST calls used (Google Drive API v3 reference, developers.google.com/drive/api/reference/rest/v3):
  * files.list            GET /files            q, pageSize, pageToken, corpora, driveId, supportsAllDrives,
                                                includeItemsFromAllDrives, fields
  * changes.getStartPageToken  GET /changes/startPageToken
  * changes.list          GET /changes          pageToken, includeRemoved, includeItemsFromAllDrives, supportsAllDrives,
                                                driveId; the `newStartPageToken` of the last page is the next cursor
  * files.get             GET /files/{id}       alt=media for blobs; fields=... to resolve folder names and file type
  * files.export          GET /files/{id}/export  Google Docs -> docx, Sheets -> xlsx, Slides -> pptx
                                                (export_mode "text": txt / csv / txt)
  * permissions.list      GET /files/{id}/permissions   only when use_drive_permissions is on
  * drives.get            GET /drives/{id}      the name of a shared drive, for the path prefix
  * root alias            GET /files/root?fields=id

Behaviour
  * Full scan: remember changes.getStartPageToken FIRST (so edits made while we list are not lost), list every folder
    (to build paths), then list every non-folder file page by page. The cursor is {"mode": "full", "page":
    <files.list pageToken>, "start": <startPageToken>}; a crashed scan resumes at the page (the cheap folder pass is
    repeated) and a pageToken Google no longer accepts restarts the scan from scratch. A full scan that ran from the
    beginning to the end sets complete_scope="" (the sync layer deletes whatever was not seen); a resumed one does not.
  * Incremental scan: the changes feed from the saved token. Removed or trashed files become Change("delete");
    losing access to a file arrives as `removed` too. A token Google rejects (400 / 404 / 410) falls back to a full
    scan. The final batch's cursor {"token": newStartPageToken, "last_full": <epoch>} is what makes the next scan
    incremental -- NOTE: sync.run_scan currently overwrites the cursor of the done batch with {}, in which case every
    scan is simply a full one (correct, just not incremental).
  * `full_resync_hours` (default 168, 0 = never): an incremental cursor older than this triggers a full scan, which
    also refreshes paths after folder renames and catches anything the feed missed.
  * `external_id` = Drive file id. `path` = folder names from the root, e.g. "Finance/2026/Q1.xlsx". Items shared with
    you whose parent you cannot see live under "Shared with me/"; shared-drive items under "<drive name>/".
    A folder rename or move is reflected in the paths of files below it at the next full scan (or when those files
    themselves change).
  * Google-native files are exported (the export API caps a document at 10 MB; a larger one fails with a clear
    error): Docs -> .docx, Sheets -> .xlsx, Slides -> .pptx (export_mode "text": .txt, .csv (first sheet only), .txt).
    Binary files are downloaded as they are; a name with no extension gets one from its MIME type so the pipeline can
    pick a reader. Skipped: folders, shortcuts, forms / maps / sites / scripts and other native types with no
    export, files the account may not download, files above `max_file_bytes` (default 200 MB), trashed files.
  * version = md5Checksum for blobs (content only, so a move or rename does not trigger re-parsing) and modifiedTime
    + export format for native documents.
  * Rate limits: Google reports them as HTTP 403 (rateLimitExceeded, userRateLimitExceeded, sharingRateLimitExceeded)
    or 429/500/503; `GoogleClient` turns the 403 flavours into 429 so `_http.Http` retries them with Retry-After /
    exponential backoff. Any other 403 on listing is `AuthError`; on a download it is an `HttpError` (the file cannot
    be downloaded; the account's credentials are fine).

Authentication (secrets only by environment variable name; read when used, never stored or logged)
  * access_token_env: an OAuth access token (short lived: refresh it outside, or use a service account).
  * service_account_file_env: environment variable holding the PATH of a service-account JSON key. The JWT-bearer
    flow (RFC 7523) is done here: an RS256 JWT signed with `cryptography` is exchanged at the key file's token_uri
    for an access token that is cached until shortly before it expires. `delegated_user` enables domain-wide
    delegation (the `sub` claim). google-auth is NOT required; to use Application Default Credentials / workload
    identity pass `token_provider=` (a callable returning an access token) to the constructor.
  * scopes (default drive.readonly).

Permissions
  * Default: folder -> principals through `AclRules` on `path` (acl_rules / default_acl), fail closed.
  * use_drive_permissions: true reads files.permissions per file and maps user -> "user:<email>" and group ->
    "group:<email>". `anyone` and `domain` grants are IGNORED unless you set anyone_principals (e.g. ["*"]) or
    domain_principals ({"example.com": ["group:all-staff"]}); a file whose permissions cannot be read gets an empty
    ACL (nobody). With this switch acl_rules / default_acl are not consulted. Google only shows the full permission
    list to accounts that can edit the file (organizer on shared drives): a reader-only account sees a partial list,
    which fails closed. Costs one extra request per file scanned. Group MEMBERSHIP is not expanded: users carry their
    group principals from SSO as with the other connectors.

Config: corpora ("user" | "drive" | "allDrives"; default "user"), drive_id (for "drive"), root_folder_id (index only
below this folder; paths are relative to it), export_mode, full_resync_hours, page_size (<=1000), skip_names,
max_file_bytes, max_retries, api_base, acl_rules, default_acl, use_drive_permissions, anyone_principals,
domain_principals, plus the authentication keys above.

Not exercised against live Google Drive. Tested against a stateful in-memory fake of the Drive v3 REST API behind an
httpx.MockTransport: its responses (fileList / changeList / startPageToken, error bodies with `reason`) follow the
documented shapes, but nothing proves the real service behaves the same. RS256 signing is verified with the public key.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import posixpath
import re
import threading
import time
from datetime import datetime
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.acl import normalize_acl
from app.scale.connectors._http import (
    DEFAULT_MAX_BYTES, AuthError, Http, HttpError, TooLarge, env_secret, safe_name,
)
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
_OFFICE = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
EXPORTS: dict[str, dict[str, tuple[str, str]]] = {
    "application/vnd.google-apps.document": {"office": (_OFFICE["docx"], ".docx"), "text": ("text/plain", ".txt")},
    "application/vnd.google-apps.spreadsheet": {"office": (_OFFICE["xlsx"], ".xlsx"), "text": ("text/csv", ".csv")},
    "application/vnd.google-apps.presentation": {"office": (_OFFICE["pptx"], ".pptx"), "text": ("text/plain", ".txt")},
}
_EXT_BY_MIME = {
    "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv", "text/markdown": ".md", "text/html": ".html",
    "application/json": ".json", "application/rtf": ".rtf", "message/rfc822": ".eml",
    "application/msword": ".doc", "application/vnd.ms-excel": ".xls", "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.oasis.opendocument.text": ".odt", "image/png": ".png", "image/jpeg": ".jpg",
    _OFFICE["docx"]: ".docx", _OFFICE["xlsx"]: ".xlsx", _OFFICE["pptx"]: ".pptx",
}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
FILE_FIELDS = ("id,name,mimeType,size,md5Checksum,modifiedTime,parents,driveId,trashed,"
               "capabilities(canDownload),shortcutDetails(targetId)")
_KNOWN_EXTS = set(_EXT_BY_MIME.values()) | {".htm", ".jpeg", ".tif", ".tiff", ".xer", ".py", ".xlsm"}
DEFAULT_SKIP_NAMES = ["thumbs.db", "desktop.ini", "~$*", ".ds_store"]


# ── shared Google plumbing (also used by gcs.py) ────────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt_rs256(claims: dict, private_key_pem: str, key_id: str | None = None) -> str:
    """An RS256 JWT (RFC 7519 / 7515) signed with `cryptography`."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover
        raise HttpError("service account authentication needs the 'cryptography' package") from exc
    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    header = {"alg": "RS256", "typ": "JWT", **({"kid": key_id} if key_id else {})}
    signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64url(
        json.dumps(claims, separators=(",", ":")).encode())
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return signing_input + "." + _b64url(signature)


class GoogleAuth(httpx.Auth):
    """Bearer authentication for Google APIs: a token from `token_provider`, from an environment variable, or minted
    from a service-account key with the JWT-bearer flow. Re-read / refreshed on demand; one retry on a 401."""

    def __init__(self, cfg: dict, *, scopes: list[str], token_provider: Callable[[], str] | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.scopes, self.provider, self.clock = cfg, scopes, token_provider, clock
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._token_http = Http(client=httpx.Client(transport=transport, timeout=httpx.Timeout(30.0, connect=15.0)),
                                sleep=sleep, max_retries=int(cfg.get("max_retries", 5)))

    def token(self, force: bool = False) -> str:
        if self.provider:
            return self.provider()
        if self.cfg.get("access_token_env"):
            return env_secret(self.cfg["access_token_env"], "Google access token")
        if self.cfg.get("service_account_file_env"):
            with self._lock:
                if force or self._token is None or self._token[1] - 300 < self.clock():
                    self._token = self._mint()
                return self._token[0]
        raise HttpError("configure access_token_env or service_account_file_env (or pass token_provider)")

    def _mint(self) -> tuple[str, float]:
        path = env_secret(self.cfg["service_account_file_env"], "service account key file path")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                key = json.load(fh)
            email, pem = key["client_email"], key["private_key"]
        except (OSError, ValueError, KeyError) as exc:
            raise AuthError(f"service account key file named by {self.cfg['service_account_file_env']} is unreadable "
                            f"or incomplete ({type(exc).__name__})") from None
        token_uri = key.get("token_uri") or "https://oauth2.googleapis.com/token"
        now = int(self.clock())
        claims = {"iss": email, "scope": " ".join(self.scopes), "aud": token_uri, "iat": now, "exp": now + 3600}
        if self.cfg.get("delegated_user"):
            claims["sub"] = self.cfg["delegated_user"]
        assertion = sign_jwt_rs256(claims, pem, key.get("private_key_id"))
        try:
            body = self._token_http.request(
                "POST", token_uri, ok=(200,),
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            ).json()
        except HttpError as exc:
            if isinstance(exc, AuthError) or exc.status in (400, 401, 403):
                code = re.search(r'"error"\s*:\s*"([^"]+)"', str(exc))
                raise AuthError(f"Google rejected the service account credentials ({exc.status}"
                                f"{', ' + code.group(1) if code else ''})", exc.status) from None
            raise
        return body["access_token"], float(now + int(body.get("expires_in", 3600)))

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = "Bearer " + self.token()
        response = yield request
        if response.status_code == 401 and self.cfg.get("service_account_file_env") and not self.provider:
            request.headers["Authorization"] = "Bearer " + self.token(force=True)
            yield request


class GoogleClient(httpx.Client):
    """httpx client that reports Google's errors the way `_http.Http` understands them.

      * 403 with reason rateLimitExceeded / userRateLimitExceeded / sharingRateLimitExceeded becomes 429 (Retry-After
        kept) and 500 `backendError` becomes 503, so they are retried with backoff.
      * every other 403 keeps its status but its body is shortened to {"reason": ..., "message": ...}: `Http` quotes only
        the first 200 characters of a body in its exception, and Google's real bodies are longer than that before the
        `reason` appears, which is the part callers need ("exportSizeLimitExceeded", "cannotDownloadFile", ...).
    Everything else is returned untouched, streaming included."""

    def send(self, request, **kwargs):  # type: ignore[override]
        response = super().send(request, **kwargs)
        if response.status_code in (403, 500):
            response.read()
            reason = google_error_reason(response)
            if response.status_code == 403 and reason in _RATE_REASONS:
                return _replace(response, 429)
            if response.status_code == 500 and reason in ("backendError", "internalError"):
                return _replace(response, 503)
            if response.status_code == 403:
                return _replace(response, 403, compact_error(response, reason))
        return response


def _replace(response: httpx.Response, status: int, content: bytes | None = None) -> httpx.Response:
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")}
    return httpx.Response(status, headers=headers, content=response.content if content is None else content, request=response.request)


def compact_error(response: httpx.Response, reason: str) -> bytes:
    try:
        message = str(response.json()["error"].get("message", ""))
    except Exception:  # noqa: BLE001
        message = response.text
    return json.dumps({"reason": reason, "message": re.sub(r"\s+", " ", message)[:120]}).encode("utf-8")


def google_error_reason(response: httpx.Response) -> str:
    try:
        errors = response.json()["error"].get("errors") or []
        return str(errors[0].get("reason", "")) if errors else str(response.json()["error"].get("status", ""))
    except Exception:  # noqa: BLE001
        return ""


# ── Drive ──────────────────────────────────────────────────────────────────────────────────────────────────


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _StaleCursor(Exception):
    pass


class GDriveConnector:
    kind = "gdrive"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, token_provider: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = config
        self.base = str(config.get("api_base", DRIVE_API)).rstrip("/")
        self.corpora = config.get("corpora", "user")
        if self.corpora not in ("user", "drive", "allDrives"):
            raise ValueError("corpora must be user, drive or allDrives")
        self.drive_id = config.get("drive_id")
        if self.corpora == "drive" and not self.drive_id:
            raise ValueError("corpora 'drive' needs drive_id")
        self.root_folder_id = config.get("root_folder_id")
        self.export_mode = config.get("export_mode", "office")
        if self.export_mode not in ("office", "text"):
            raise ValueError("export_mode must be office or text")
        self.page_size = max(1, min(int(config.get("page_size", 1000)), 1000))
        self.full_resync_s = float(config.get("full_resync_hours", 168)) * 3600
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.use_perms = bool(config.get("use_drive_permissions", False))
        self.anyone = normalize_acl(config.get("anyone_principals") or [])
        self.domains = {d.lower(): normalize_acl(p) for d, p in (config.get("domain_principals") or {}).items()}
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self._clock = clock
        auth = GoogleAuth(config, scopes=list(config.get("scopes") or [DRIVE_SCOPE]), token_provider=token_provider,
                          transport=transport, sleep=sleep, clock=clock)
        self.http = Http(
            client=GoogleClient(auth=auth, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True),
            sleep=sleep, max_retries=int(config.get("max_retries", 5)),
        )
        self._folders: dict[str, tuple[str, str | None]] = {}
        self._drive_names: dict[str, str] = {}
        self._root_id: str | None = None
        self.stats: dict[str, int] = {}

    # ── api helpers ──

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _scope_params(self) -> dict:
        params = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if self.corpora == "drive":
            params["driveId"] = self.drive_id
        return params

    def _list_params(self, q: str, fields: str, page: str | None) -> dict:
        params = {"q": q, "pageSize": str(self.page_size), "fields": fields, "corpora": self.corpora, **self._scope_params()}
        if page:
            params["pageToken"] = page
        return params

    def _get_json(self, path: str, params: dict) -> dict:
        return self.http.get_json(f"{self.base}/{path.lstrip('/')}", params=params)  # type: ignore[return-value]

    def _start_token(self) -> str:
        params = {"supportsAllDrives": "true"}
        if self.corpora == "drive":
            params["driveId"] = self.drive_id
        return str(self._get_json("changes/startPageToken", params)["startPageToken"])

    # ── paths ──

    def _root(self) -> str:
        if self._root_id is None:
            self._root_id = str(self._get_json("files/root", {"fields": "id"}).get("id", ""))
        return self._root_id

    def _folder(self, folder_id: str) -> tuple[str, str | None] | None:
        """(name, parent id) of a folder, from the cache or one files.get; None when it is not visible to us."""
        if folder_id in self._folders:
            return self._folders[folder_id]
        try:
            meta = self._get_json(f"files/{folder_id}", {"fields": "id,name,parents,driveId", "supportsAllDrives": "true"})
        except HttpError as exc:
            if exc.status in (403, 404):
                return None
            raise
        parents = meta.get("parents") or []
        self._folders[folder_id] = (meta.get("name", ""), parents[0] if parents else None)
        return self._folders[folder_id]

    def _drive_name(self, drive_id: str) -> str:
        if drive_id not in self._drive_names:
            try:
                self._drive_names[drive_id] = str(self._get_json(f"drives/{drive_id}", {"fields": "id,name"}).get("name") or "")
            except HttpError:
                self._drive_names[drive_id] = ""
            self._drive_names[drive_id] = self._drive_names[drive_id] or f"Shared drive {drive_id[:8]}"
        return self._drive_names[drive_id]

    def _dir_of(self, parent: str | None, drive_id: str | None) -> tuple[str, bool]:
        """(directory path, inside root_folder_id) for a file whose parent folder id is `parent`."""
        names: list[str] = []
        seen: set[str] = set()
        current, end = parent, "toodeep"
        for _ in range(100):
            if current is None:
                end = "top"                                  # no parent information: shared with us, parent invisible
                break
            if current == self.root_folder_id:
                end = "scope"
                break
            if drive_id and current == drive_id:
                end = "drive"
                break
            if self._root_id and current == self._root_id:
                end = "mydrive"
                break
            if current in seen:
                end = "loop"
                break
            seen.add(current)
            info = self._folder(current)
            if info is None:
                end = "invisible"                            # a folder we may not read: the chain ends here
                break
            names.append(safe_name(info[0], "folder"))
            current = info[1]
        names.reverse()
        if self.root_folder_id is not None:
            return "/".join(names), end == "scope"           # paths are relative to root_folder_id
        if end == "drive" and drive_id:
            names.insert(0, safe_name(self._drive_name(drive_id), "Shared drive"))
        elif end != "mydrive":
            names.insert(0, "Shared with me")
        return "/".join(names), True

    # ── conversion ──

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    def _filename(self, meta: dict, export_ext: str | None) -> str:
        """A safe file name that ends in an extension the pipeline can pick a reader for."""
        name = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", meta.get("name") or "")).strip(" .") or "untitled"
        wanted = export_ext or _EXT_BY_MIME.get(meta.get("mimeType", ""))
        if wanted is None or name.lower().endswith(wanted):
            return name[:200]
        if export_ext is None and posixpath.splitext(name)[1].lower() in _KNOWN_EXTS:
            return name[:200]                                # e.g. a .docx that Drive labels application/octet-stream
        return name[:150] + wanted

    def _to_file(self, meta: dict) -> SourceFile | None:
        mime = meta.get("mimeType", "")
        if meta.get("trashed") or mime == FOLDER or mime == SHORTCUT:
            return None
        native = mime.startswith("application/vnd.google-apps.")
        export = EXPORTS.get(mime, {}).get(self.export_mode) if native else None
        if native and export is None:
            self._count("unexportable_type")
            return None
        if (meta.get("capabilities") or {}).get("canDownload") is False:
            self._count("not_downloadable")
            return None
        if self._skip_name(meta.get("name") or ""):
            self._count("skipped_name")
            return None
        name = self._filename(meta, export[1] if export else None)
        try:
            size = int(meta.get("size") or 0)
        except ValueError:
            size = 0
        if size > self.max_bytes:
            log.info("gdrive file %s is %d bytes, over max_file_bytes; skipped", meta.get("id"), size)
            self._count("oversize")
            return None
        parents = meta.get("parents") or []
        directory, in_scope = self._dir_of(parents[0] if parents else None, meta.get("driveId"))
        if not in_scope:
            self._count("outside_root_folder")
            return None
        path = f"{directory}/{name}" if directory else name
        # content hash for blobs (a move or rename does not re-parse); modified time + export format for native documents
        version = f"{meta['md5Checksum']}:{size}" if meta.get("md5Checksum") else f"{meta.get('modifiedTime', '')}:{export[1] if export else ''}"
        file_id = meta["id"]
        return SourceFile(
            external_id=file_id, path=path, name=name, size=size, modified_at=_parse_dt(meta.get("modifiedTime")),
            version=version, acl=self._acl(file_id, path), mime=(export[0] if export else mime),
            extra={"gdrive_mime": mime, "web_link": meta.get("webViewLink", "")},
        )

    # ── permissions ──

    def _acl(self, file_id: str, path: str) -> list[str]:
        return self._drive_acl(file_id) if self.use_perms else self.rules.for_path(path)

    def _drive_acl(self, file_id: str) -> list[str]:
        principals: list[str] = []
        page = None
        try:
            while True:
                params = {"fields": "nextPageToken,permissions(type,emailAddress,domain,role,deleted)", "supportsAllDrives": "true",
                          "pageSize": "100"}
                if page:
                    params["pageToken"] = page
                body = self._get_json(f"files/{file_id}/permissions", params)
                for perm in body.get("permissions") or []:
                    principals.extend(self._principals(perm))
                page = body.get("nextPageToken")
                if not page:
                    return normalize_acl(principals)
        except HttpError as exc:
            log.warning("gdrive permissions for %s unreadable (%s); no access granted", file_id, exc.status)
            return []                                    # fail closed

    def _principals(self, perm: dict) -> list[str]:
        if perm.get("deleted"):
            return []
        kind, email = perm.get("type"), (perm.get("emailAddress") or "").strip().lower()
        if kind == "user" and email:
            return [f"user:{email}"]
        if kind == "group" and email:
            return [f"group:{email}"]
        if kind == "anyone":
            return list(self.anyone)
        if kind == "domain":
            return list(self.domains.get(str(perm.get("domain", "")).lower(), []))
        return []

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self._drive_acl(file.external_id) if self.use_perms else self.rules.for_path(file.path)

    # ── scanning ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        cursor = dict(cursor or {})
        self._folders = {}
        mode = cursor.get("mode")
        last_full = cursor.get("last_full")
        full_due = bool(self.full_resync_s) and (not last_full or self._clock() - float(last_full) > self.full_resync_s)
        try:
            if mode == "full":
                yield from self._scan_full(cursor)
            elif mode == "inc":
                yield from self._scan_incremental(cursor.get("page"), resumed=True, last_full=last_full)
            elif cursor.get("token") and not full_due:
                yield from self._scan_incremental(cursor["token"], resumed=False, last_full=last_full)
            else:
                yield from self._scan_full({})
        except _StaleCursor:
            log.warning("gdrive cursor no longer accepted by Google; starting a full scan")
            self._folders = {}
            yield from self._scan_full({})

    def _load_folders(self) -> None:
        page = None
        while True:
            body = self._get_json("files", self._list_params(
                f"mimeType = '{FOLDER}' and trashed = false", "nextPageToken,files(id,name,parents,driveId)", page))
            for f in body.get("files") or []:
                parents = f.get("parents") or []
                self._folders[f["id"]] = (f.get("name", ""), parents[0] if parents else None)
            page = body.get("nextPageToken")
            if not page:
                return

    def _scan_full(self, cursor: dict) -> Iterator[ScanBatch]:
        resumed = bool(cursor.get("mode") == "full")
        page = cursor.get("page") if resumed else None
        started = cursor.get("started") or self._clock()
        start_token = cursor.get("start") if resumed else None
        if start_token is None:
            start_token = self._start_token()
        self._root_id = None
        if self.corpora != "drive":
            self._root()
        self._load_folders()
        first = True
        while True:
            try:
                body = self._get_json("files", self._list_params(
                    f"mimeType != '{FOLDER}' and trashed = false", f"nextPageToken,files({FILE_FIELDS})", page))
            except HttpError as exc:
                if first and resumed and page and exc.status == 400:
                    raise _StaleCursor() from None
                raise
            first = False
            changes = []
            for meta in body.get("files") or []:
                sf = self._to_file(meta)
                if sf is not None:
                    changes.append(Change("upsert", sf.external_id, sf))
            page = body.get("nextPageToken")
            if page:
                yield ScanBatch(changes, {"mode": "full", "page": page, "start": start_token, "started": started,
                                          "last_full": cursor.get("last_full")})
            else:
                yield ScanBatch(changes, {"token": start_token, "last_full": started}, done=True,
                                complete_scope=None if resumed else "")
                return

    def _scan_incremental(self, page: str | None, *, resumed: bool, last_full) -> Iterator[ScanBatch]:
        if not page:
            raise _StaleCursor()
        if self.corpora != "drive":
            self._root()
        first = True
        while True:
            params = {"pageToken": page, "pageSize": str(self.page_size), "includeRemoved": "true",
                      "fields": f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,file({FILE_FIELDS}))",
                      **self._scope_params()}
            try:
                body = self._get_json("changes", params)
            except HttpError as exc:
                if first and exc.status in (400, 404, 410):
                    raise _StaleCursor() from None
                raise
            first = False
            changes: list[Change] = []
            latest: dict[str, Change] = {}
            for ch in body.get("changes") or []:
                if ch.get("changeType", "file") != "file":
                    continue
                file_id, meta = ch.get("fileId"), ch.get("file") or {}
                if not file_id:
                    continue
                if meta.get("mimeType") == FOLDER:
                    if not ch.get("removed") and not meta.get("trashed"):
                        parents = meta.get("parents") or []
                        self._folders[file_id] = (meta.get("name", ""), parents[0] if parents else None)
                    continue
                if ch.get("removed") or meta.get("trashed"):
                    latest[file_id] = Change("delete", file_id)
                    continue
                sf = self._to_file(meta) if meta else None
                # a file that stopped being indexable (now over the cap, no longer downloadable ...) must disappear
                latest[file_id] = Change("upsert", file_id, sf) if sf is not None else Change("delete", file_id)
            changes = list(latest.values())
            page = body.get("nextPageToken")
            if page:
                yield ScanBatch(changes, {"mode": "inc", "page": page, "last_full": last_full})
            else:
                yield ScanBatch(changes, {"token": body.get("newStartPageToken"), "last_full": last_full}, done=True,
                                complete_scope=None)
                return

    # ── bytes ──

    def open(self, file: SourceFile) -> BinaryIO:
        file_id = file.external_id
        mime = (file.extra or {}).get("gdrive_mime")
        meta: dict = {}
        if not mime:
            meta = self._get_json(f"files/{file_id}", {"fields": "id,mimeType,size,capabilities(canDownload)", "supportsAllDrives": "true"})
            mime = meta.get("mimeType", "")
        try:
            if mime.startswith("application/vnd.google-apps."):
                export = EXPORTS.get(mime, {}).get(self.export_mode)
                if export is None:
                    raise HttpError(f"Google file {file_id} of type {mime} cannot be exported")
                return self.http.download(f"{self.base}/files/{file_id}/export", max_bytes=self.max_bytes,
                                          params={"mimeType": export[0]})
            if meta.get("size") and int(meta["size"]) > self.max_bytes:
                raise TooLarge(f"Google file {file_id} is {meta['size']} bytes, larger than {self.max_bytes}")
            return self.http.download(f"{self.base}/files/{file_id}", max_bytes=self.max_bytes,
                                      params={"alt": "media", "supportsAllDrives": "true"})
        except AuthError as exc:
            if exc.status == 403 and re.search(r"exportSizeLimitExceeded|cannotDownload|downloadRestricted|DownloadRestricted|abusive|fileNotDownloadable", str(exc), re.I):
                raise HttpError(f"Google file {file_id} cannot be downloaded: {str(exc)[-160:]}", 403) from None
            raise
