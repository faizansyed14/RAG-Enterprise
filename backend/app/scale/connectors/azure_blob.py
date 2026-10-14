"""Azure Blob Storage (one container, optionally one prefix) through the Blob service REST API.

Endpoints used (Azure Storage REST API reference, learn.microsoft.com/rest/api/storageservices):
  * List Blobs  GET {endpoint}/{container}?restype=container&comp=list&prefix=&maxresults=&marker=   (flat listing, XML,
                <NextMarker> continuation)
  * Get Blob    GET {endpoint}/{container}/{blob}
Nothing else is called: no Get Blob Properties, no metadata, tags, snapshots, versions or writes. The credential only needs
read and list permission on the container (SAS `sp=rl`, role "Storage Blob Data Reader", or a read-only key holder).
Every request carries the `x-ms-version` header (`api_version`, default 2021-12-02).

Behaviour
  * Crawl style, one `ScanBatch` per List Blobs page. The cursor is {"marker": <NextMarker of the last page>}; a resumed scan
    continues there and is not authoritative (complete_scope=None). A scan that ran from the first blob to the last sets
    complete_scope="" and the sync layer deletes whatever it did not see. A marker the service rejects (400) restarts the
    listing from its beginning; a marker repeated by the service ends the scan with an error instead of looping.
  * `external_id` = the blob name. `path` = the name relative to `prefix` (so `acl_rules` prefixes are relative to it too).
    `open()` and `refresh_acl()` need only those two.
  * version = Content-MD5 when the service holds one (content only), else the Etag, plus the size. Azure has no rename:
    a "moved" blob is a new name.
  * Skipped without failing the scan: names ending in "/" and zero-length blobs (including the folder markers of hierarchical
    namespace accounts), names matching `skip_names`, blobs in the Archive tier (`skip_archived`, default true: they cannot
    be read until rehydrated), blobs above `max_file_bytes` (default 200 MB). Names the service percent-encodes
    (`<Name Encoded="true">`) are decoded.
  * 429 / 502 / 503 / 504 and transport errors are retried with bounded backoff (Retry-After honoured) by `_http.Http`.
    A 500 is not retried. 401 / 403 raise `AuthError` whose message is the status and Azure's error code only (never the
    signature string, the SAS or the key).
  * Downloads stream into a temporary file with a hard size cap.

Authentication (secrets by environment variable name only, read when a request is made, never stored or logged):
  * sas_token_env       the SAS query string (`sv=...&sig=...`, with or without a leading "?"), merged into every request URL
  * account_key_env     the storage account key (base64) with `account_name`: Shared Key signing of every request
                        (HMAC-SHA256 over the canonicalized request, "Authorize with Shared Key")
  * access_token_env    a Microsoft Entra ID bearer token for https://storage.azure.com (not minted or refreshed here)
  * anonymous: true     a public container
Exactly one is required; nothing falls back to anonymous silently.

Permissions: folder / name-prefix mapping through `AclRules` (acl_rules / default_acl), fail closed; Azure RBAC and container
access policies are NOT read.

Config: container (required), account_name or endpoint_url (default https://{account_name}.blob.core.windows.net; a path-style
emulator endpoint such as http://127.0.0.1:10000/devstoreaccount1 works), prefix, one credential option above, api_version,
page_size (<=5000), max_file_bytes, skip_names, skip_archived, max_retries, acl_rules, default_acl.

Not exercised against live Azure. Tested against Azurite, Microsoft's Azure Storage emulator (a real HTTP server that verifies
Shared Key signatures and SAS tokens), seeded and SAS-signed by the official azure-storage-blob SDK; the Entra bearer mode,
throttling, the encoded-name case and error bodies use MockTransport fixtures that follow the documented shapes. The
tests of the real server are skipped when Azurite (npm install azurite; AZURITE_BIN) is not installed.
"""

from __future__ import annotations

import base64
import email.utils
import fnmatch
import hashlib
import hmac
import logging
import mimetypes
import posixpath
import re
import time
from datetime import datetime
from typing import BinaryIO, Callable, Iterator
from urllib.parse import parse_qsl, quote, unquote

import httpx

from app.scale.connectors._http import DEFAULT_MAX_BYTES, AuthError, Http, HttpError, env_secret
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile
from app.scale.connectors.webdav import parse_xml

log = logging.getLogger(__name__)

API_VERSION = "2021-12-02"
DEFAULT_SKIP_NAMES = [".keep", ".gitkeep", ".ds_store", "thumbs.db", "desktop.ini", "~$*"]


def shared_key_signature(method: str, raw_path: str, query: list[tuple[str, str]], headers: dict[str, str], account: str,
                         key_b64: str) -> str:
    """Authorization value for Shared Key (the "Authorize with Shared Key" document, versions 2015-02-21 and later).

    `headers` are the request headers as they will be sent (names case-insensitive); `x-ms-date` stands in for Date, so the
    Date line of the string to sign is empty.
    """
    h = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    length = h.get("content-length", "")
    if length == "0":
        length = ""
    standard = [method.upper(), h.get("content-encoding", ""), h.get("content-language", ""), length, h.get("content-md5", ""),
                h.get("content-type", ""), "", h.get("if-modified-since", ""), h.get("if-match", ""), h.get("if-none-match", ""),
                h.get("if-unmodified-since", ""), h.get("range", "")]
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(h.items()) if k.startswith("x-ms-"))
    grouped: dict[str, list[str]] = {}
    for name, value in query:
        grouped.setdefault(name.lower(), []).append(value)
    resource = f"/{account}{raw_path}" + "".join(f"\n{k}:{','.join(sorted(v))}" for k, v in sorted(grouped.items()))
    string_to_sign = "\n".join(standard) + "\n" + canonical_headers + resource
    digest = hmac.new(base64.b64decode(key_b64), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return f"SharedKey {account}:{base64.b64encode(digest).decode()}"


class AzureAuth(httpx.Auth):
    def __init__(self, cfg: dict, clock: Callable[[], float]) -> None:
        self.cfg, self.clock = cfg, clock

    def auth_flow(self, request: httpx.Request):
        cfg = self.cfg
        if cfg.get("sas_token_env"):
            sas = env_secret(cfg["sas_token_env"], "Azure SAS token").lstrip("?")
            request.url = request.url.copy_merge_params(parse_qsl(sas, keep_blank_values=True))
        elif cfg.get("account_key_env"):
            key = env_secret(cfg["account_key_env"], "Azure storage account key")
            request.headers["x-ms-date"] = email.utils.formatdate(self.clock(), usegmt=True)
            raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
            try:
                request.headers["Authorization"] = shared_key_signature(
                    request.method, raw_path, list(request.url.params.multi_items()), dict(request.headers),
                    str(cfg["account_name"]), key,
                )
            except ValueError:                      # binascii.Error: the key is not base64; say which setting, never the key
                raise HttpError(f"the value of {cfg['account_key_env']} (the Azure storage account key) is not valid base64") from None
        elif cfg.get("access_token_env"):
            request.headers["Authorization"] = "Bearer " + env_secret(cfg["access_token_env"], "Azure access token")
        yield request


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _error_code(exc: HttpError) -> str:
    match = re.search(r"<Code>\s*([A-Za-z0-9]+)\s*</Code>", str(exc))
    return match.group(1) if match else ""


class AzureBlobConnector:
    kind = "azure_blob"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.cfg = config
        self.container = str(config["container"])
        account = config.get("account_name")
        endpoint = config.get("endpoint_url") or (f"https://{account}.blob.core.windows.net" if account else "")
        if not endpoint:
            raise ValueError("configure account_name or endpoint_url")
        self.endpoint = str(endpoint).rstrip("/")
        modes = [k for k in ("sas_token_env", "account_key_env", "access_token_env") if config.get(k)]
        if len(modes) > 1:
            raise ValueError(f"configure only one credential, not {', '.join(modes)}")
        if not modes and not config.get("anonymous"):
            raise ValueError("configure sas_token_env, account_key_env (with account_name) or access_token_env "
                             "(or anonymous: true for a public container)")
        if config.get("account_key_env") and not account:
            raise ValueError("account_key_env needs account_name (it is part of what is signed)")
        prefix = str(config.get("prefix") or "").lstrip("/")
        self.prefix = prefix if (not prefix or prefix.endswith("/")) else prefix + "/"
        self.page_size = max(1, min(int(config.get("page_size", 5000)), 5000))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.skip_archived = bool(config.get("skip_archived", True))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.http = Http(
            client=httpx.Client(auth=AzureAuth(config, clock), headers={"x-ms-version": str(config.get("api_version", API_VERSION))},
                                transport=transport, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False),
            sleep=sleep, max_retries=int(config.get("max_retries", 5)),
        )
        self.stats: dict[str, int] = {}

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    def _request(self, url: str, **kwargs):
        try:
            return self.http.get(url, ok=(200,), **kwargs)
        except AuthError as exc:
            code = _error_code(exc)
            raise AuthError(f"Azure Blob rejected the credentials ({exc.status}{', ' + code if code else ''})", exc.status) from None

    # ── scanning ──

    def _list(self, marker: str) -> tuple[list[dict], str]:
        params = {"restype": "container", "comp": "list", "maxresults": self.page_size}
        if self.prefix:
            params["prefix"] = self.prefix
        if marker:
            params["marker"] = marker
        root = parse_xml(self._request(f"{self.endpoint}/{quote(self.container)}", params=params).content)
        blobs = []
        for blob in root.iter("Blob"):
            name_el = blob.find("Name")
            if name_el is None or not (name_el.text or ""):
                continue
            name = name_el.text
            if name_el.get("Encoded") == "true":
                name = unquote(name)
            props_el = blob.find("Properties")
            props = {child.tag: (child.text or "") for child in (props_el if props_el is not None else [])}
            blobs.append({"name": name, **props})
        return blobs, (root.findtext("NextMarker") or "").strip()

    def _to_file(self, blob: dict) -> SourceFile | None:
        name = blob["name"]
        if name.endswith("/"):
            self._count("folder_marker")
            return None
        rel = name[len(self.prefix):] if name.startswith(self.prefix) else name
        base = posixpath.basename(rel)
        if not base or self._skip_name(base):
            self._count("skipped_name")
            return None
        try:
            size = int(blob.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size == 0:
            self._count("empty")
            return None
        if size > self.max_bytes:
            log.info("azure blob %s is %d bytes, over max_file_bytes; skipped", name, size)
            self._count("oversize")
            return None
        if self.skip_archived and blob.get("AccessTier") == "Archive":
            self._count("archived")
            return None
        marker = blob.get("Content-MD5") or (blob.get("Etag") or "").strip('"')
        return SourceFile(
            external_id=name, path=rel, name=base, size=size, modified_at=_parse_dt(blob.get("Last-Modified")),
            version=f"{marker}:{size}", acl=self.rules.for_path(rel), mime=mimetypes.guess_type(base)[0] or "",
        )

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        marker = (cursor or {}).get("marker") or ""
        resumed = bool(marker)
        seen_markers = {marker}
        while True:
            try:
                blobs, next_marker = self._list(marker)
            except HttpError as exc:
                if exc.status == 400 and marker:
                    log.warning("azure blob marker was rejected; listing again from the start")
                    self._count("marker_reset")
                    marker, seen_markers = "", {""}
                    continue
                raise
            changes = []
            for blob in blobs:
                sf = self._to_file(blob)
                if sf is not None:
                    changes.append(Change("upsert", sf.external_id, sf))
            if not next_marker:
                yield ScanBatch(changes, {}, done=True, complete_scope=None if resumed else "")
                return
            if next_marker in seen_markers:
                raise HttpError("Azure Blob returned the same continuation marker twice; stopping instead of looping")
            seen_markers.add(next_marker)
            marker = next_marker
            yield ScanBatch(changes, {"marker": marker})

    # ── bytes / permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        url = f"{self.endpoint}/{quote(self.container)}/{quote(file.external_id, safe='/')}"
        try:
            return self.http.download(url, max_bytes=self.max_bytes)
        except AuthError as exc:
            code = _error_code(exc)
            raise AuthError(f"Azure Blob rejected the credentials ({exc.status}{', ' + code if code else ''})", exc.status) from None

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
