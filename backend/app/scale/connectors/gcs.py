"""Google Cloud Storage (one bucket, optionally one prefix) through the Cloud Storage JSON API v1.

Endpoints used (Cloud Storage JSON API reference, cloud.google.com/storage/docs/json_api/v1: "Objects: list", "Objects: get", plus
"Download objects" for `alt=media`):
  * objects.list  GET {api_base}/storage/v1/b/{bucket}/o?prefix=&maxResults=&pageToken=&fields=&userProject=
                  flat listing in UTF-8 name order, no `delimiter`; the answer is {"kind": "storage#objects", "items": [...],
                  "nextPageToken": "..."}; `items` is ABSENT (not empty) when nothing matches; the last page has no nextPageToken.
  * objects.get   GET {api_base}/storage/v1/b/{bucket}/o/{object}?alt=media&generation=N&userProject=
                  the object name is percent-encoded as ONE path segment (a "/" in the name is %2F).
Nothing else is called: no metadata reads, ACL / IAM reads, notifications, Pub/Sub or writes. The identity needs `storage.objects.list`
(listing) and `storage.objects.get` (reading) on the bucket: the predefined role Storage Object Viewer covers both. Scope requested for
service accounts: https://www.googleapis.com/auth/devstorage.read_only.

Behaviour
  * Crawl style, one `ScanBatch` per list page. GCS has no change feed this connector can pull, so every scan lists the prefix and
    unchanged objects cost nothing downstream because `version` is compared by the sync layer. The cursor is {"page": <nextPageToken
    of the last page>}; a resumed scan continues there and is NOT authoritative (complete_scope=None). A scan that ran from the first
    object to the last sets complete_scope="" and the sync layer deletes whatever it did not see (so a deleted object is a missing one).
    A page token the service rejects (HTTP 400) restarts the listing from the beginning, once per scan, and that restarted listing
    is authoritative again; a token repeated by the service ends the scan with an error instead of looping. An empty listing is
    authoritative too: a misspelled `prefix` that matches nothing deletes the indexed objects of this connection at the next scan.
  * `external_id` = the object name. `path` = the name relative to `prefix`, so `acl_rules` prefixes are relative to it too.
    `open()` and `refresh_acl()` need nothing else (the sync layer does not keep SourceFile.extra).
  * version = the object's `generation` (a new number for every overwrite of the object's data; metadata-only changes bump
    `metageneration` and are ignored). open() reads that very generation (`generation=N`), so the bytes always match the version
    that was recorded; if the object was overwritten or deleted after the scan and the bucket keeps no old generations the read is a
    404 that says so, and the next scan carries the new generation.
  * Skipped without failing the scan, and counted in `self.stats`: "folder" placeholder objects (names ending in "/"), empty
    objects, names matching `skip_names`, objects over `max_file_bytes` (default 200 MB), objects encrypted with a customer-supplied key
    (`customerEncryption`: unreadable without the key), objects whose storage class is in `skip_storage_classes` (default none: Nearline,
    Coldline and Archive are readable at once but cost retrieval fees), and objects without a `generation`.
  * `size` is the stored size. For an object stored with Content-Encoding: gzip, GCS serves the decompressed bytes to clients that do
    not ask for gzip, and httpx asks for it and decodes it, so open() returns the plain bytes and its size cap counts the decoded ones.
  * 429, 502, 503, 504 and transport errors are retried with bounded backoff (Retry-After honoured) by `_http.Http`; GoogleClient
    (gdrive.py) also turns a 403 with reason rateLimitExceeded into a 429 and a 500 backendError into a 503. A plain 500 and a 408 are
    NOT retried. 401 / 403 raise `AuthError` carrying the status, Google's reason and a hint, never the token.
  * Downloads stream into a temporary file with a hard size cap.

Authentication (secrets by environment variable name only, read when a request is made, never stored or logged). The mechanism is
gdrive.py's `GoogleAuth` / `GoogleClient`, imported here, not copied:
  * access_token_env            an OAuth2 access token (short lived: refresh it outside, or use a service account)
  * service_account_file_env    the name of the variable holding the PATH of a service-account JSON key: an RS256 JWT is exchanged at
                                the key's token_uri for an access token, cached until shortly before it expires (needs `cryptography`)
  * token_provider=             constructor argument, a callable returning an access token (Application Default Credentials, workload
                                identity, `gcloud auth print-access-token`)
  * anonymous: true             a bucket readable by allUsers
Exactly one is required; nothing falls back to anonymous silently.

Permissions: folder / name-prefix mapping through `AclRules` (acl_rules / default_acl), fail closed. Bucket IAM and object ACLs are NOT
read: whoever the rules name can search the content, whoever could read it in GCS or not.

Config: bucket (required), prefix, one credential option above, scopes, user_project (billing project of a Requester Pays bucket, sent as
`userProject`), api_base (default https://storage.googleapis.com; an emulator such as fake-gcs-server), page_size (<=1000),
max_file_bytes, skip_names, skip_storage_classes, max_retries, acl_rules, default_acl.

What this does NOT do: noncurrent generations (`versions=true`), soft-deleted objects, composite or Object Contexts metadata, `delimiter`
folder browsing, notifications, several buckets per connection (add one connection per bucket).

Not exercised against a live Google Cloud Storage; tested against fixtures shaped like the vendor documentation (a stateful fake of
the JSON API behind an httpx.MockTransport). Unsure of: whether `fields=` is honoured on objects.list exactly as written here (it is a
standard Google system parameter), `userProject` on both calls, whether a rejected pageToken is always an HTTP 400, whether the object's
`updated` is the best "modified" time (it is the metadata modification time), and the behaviour of `generation=` reads on a bucket
without Object Versioning for a replaced generation (assumed 404).
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import os
import posixpath
import re
import time
from datetime import datetime
from typing import BinaryIO, Callable, Iterator
from urllib.parse import quote

import httpx

from app.scale.connectors._http import DEFAULT_MAX_BYTES, AuthError, Http, HttpError, TooLarge
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile
from app.scale.connectors.gdrive import GoogleAuth, GoogleClient

log = logging.getLogger(__name__)

API_BASE = "https://storage.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
DEFAULT_SKIP_NAMES = [".keep", ".gitkeep", ".ds_store", "thumbs.db", "desktop.ini", "~$*"]
LIST_FIELDS = "nextPageToken,items(name,generation,size,updated,contentType,storageClass,customerEncryption)"
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GcsConnector:
    kind = "gcs"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 token_provider: Callable[[], str] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = config
        self.bucket = str(config.get("bucket") or "")
        if not _BUCKET.match(self.bucket):
            raise ValueError("configure `bucket` (a Cloud Storage bucket name: lowercase letters, digits, '-', '_' and '.')")
        modes = [k for k in ("access_token_env", "service_account_file_env") if config.get(k)]
        anonymous = bool(config.get("anonymous"))
        if token_provider is not None:
            modes.append("token_provider")
        if len(modes) > 1 or (anonymous and modes):
            raise ValueError(f"configure only one credential, not {', '.join(modes + (['anonymous'] if anonymous else []))}")
        if not modes and not anonymous:
            raise ValueError("configure access_token_env or service_account_file_env (or pass token_provider, or set anonymous: true "
                             "for a public bucket)")
        prefix = str(config.get("prefix") or "").lstrip("/")
        self.prefix = prefix if (not prefix or prefix.endswith("/")) else prefix + "/"
        self.user_project = str(config.get("user_project") or "")
        self.page_size = max(1, min(int(config.get("page_size", 1000)), 1000))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.skip_classes = {str(c).upper() for c in config.get("skip_storage_classes") or []}
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.objects_url = f"{str(config.get('api_base', API_BASE)).rstrip('/')}/storage/v1/b/{self.bucket}/o"
        self.auth = None if anonymous else GoogleAuth(config, scopes=list(config.get("scopes") or [SCOPE]), token_provider=token_provider,
                                                      transport=transport, sleep=sleep, clock=clock)
        self.http = Http(
            client=GoogleClient(auth=self.auth, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False),
            sleep=sleep, max_retries=int(config.get("max_retries", 5)),
        )
        self.stats: dict[str, int] = {}

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    # ── errors (nothing secret in a message) ──

    def _scrub(self, text: str) -> str:
        secrets = [os.environ.get(str(self.cfg.get("access_token_env") or ""), "")]
        if self.auth is not None and self.auth._token:
            secrets.append(self.auth._token[0])
        for secret in secrets:
            if secret and len(secret) >= 8:
                text = text.replace(secret, "***")
        return text

    def _auth_error(self, exc: AuthError) -> AuthError:
        hint = ""
        if exc.status == 401:
            source = self.cfg.get("access_token_env") or self.cfg.get("service_account_file_env")
            hint = "the access token was rejected or has expired" + (f": check the environment variable {source}" if source else "")
        elif exc.status == 403:
            hint = (f"the identity needs storage.objects.list and storage.objects.get on bucket {self.bucket}, for example the role "
                    f"Storage Object Viewer" + ("; a Requester Pays bucket also needs user_project" if not self.user_project else ""))
        return AuthError(self._scrub(f"{exc}" + (f" ({hint})" if hint else "")), exc.status)

    def _request(self, url: str, **kwargs) -> httpx.Response:
        try:
            return self.http.get(url, ok=(200,), **kwargs)
        except AuthError as exc:
            raise self._auth_error(exc) from None
        except HttpError as exc:
            raise HttpError(self._scrub(str(exc)), exc.status) from None

    # ── scanning ──

    def _list(self, page: str) -> tuple[list[dict], str]:
        params = {"maxResults": str(self.page_size), "fields": LIST_FIELDS}
        if self.prefix:
            params["prefix"] = self.prefix
        if page:
            params["pageToken"] = page
        if self.user_project:
            params["userProject"] = self.user_project
        body = self._request(self.objects_url, params=params).json()
        if not isinstance(body, dict):
            raise HttpError(f"GET {self.objects_url}: the answer is not a JSON object")
        return body.get("items") or [], str(body.get("nextPageToken") or "")

    def _to_file(self, item: dict) -> SourceFile | None:
        name = str(item.get("name") or "")
        if not name or name.endswith("/"):
            self._count("folder_marker")
            return None
        rel = name[len(self.prefix):] if name.startswith(self.prefix) else name
        base = posixpath.basename(rel)
        if not base or self._skip_name(base):
            self._count("skipped_name")
            return None
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size == 0:
            self._count("empty")
            return None
        if size > self.max_bytes:
            log.info("gcs object %s is %d bytes, over max_file_bytes; skipped", name, size)
            self._count("oversize")
            return None
        if item.get("customerEncryption"):
            self._count("customer_encrypted")
            return None
        if str(item.get("storageClass") or "").upper() in self.skip_classes:
            self._count("storage_class")
            return None
        generation = str(item.get("generation") or "")
        if not generation:
            self._count("no_generation")
            return None
        content_type = str(item.get("contentType") or "").split(";")[0].strip().lower()
        mime = mimetypes.guess_type(base)[0] or "" if content_type in _GENERIC_TYPES else content_type
        return SourceFile(
            external_id=name, path=rel, name=base, size=size, modified_at=_parse_dt(item.get("updated")), version=generation,
            acl=self.rules.for_path(rel), mime=mime, extra={"bucket": self.bucket, "generation": generation},
        )

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        page = (cursor or {}).get("page") or ""
        resumed = bool(page)
        seen = {page}
        restarted = False
        while True:
            try:
                items, next_page = self._list(page)
            except HttpError as exc:
                if exc.status == 400 and page and not restarted:
                    log.warning("gcs page token was rejected; listing again from the start")
                    self._count("page_token_reset")
                    page, seen, resumed, restarted = "", {""}, False, True
                    continue
                raise
            changes = []
            for item in items:
                sf = self._to_file(item)
                if sf is not None:
                    changes.append(Change("upsert", sf.external_id, sf))
            if not next_page:
                yield ScanBatch(changes, {}, done=True, complete_scope=None if resumed else "")
                return
            if next_page in seen:
                raise HttpError("Cloud Storage returned the same nextPageToken twice; stopping instead of looping")
            seen.add(next_page)
            page = next_page
            yield ScanBatch(changes, {"page": page})

    # ── bytes / permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        url = f"{self.objects_url}/{quote(file.external_id, safe='')}"
        params = {"alt": "media"}
        generation = file.version if str(file.version).isdigit() else ""
        if generation:
            params["generation"] = generation
        if self.user_project:
            params["userProject"] = self.user_project
        try:
            return self.http.download(url, max_bytes=self.max_bytes, params=params)
        except AuthError as exc:
            raise self._auth_error(exc) from None
        except TooLarge:
            raise
        except HttpError as exc:
            if exc.status == 404:
                raise HttpError(f"gs://{self.bucket}/{file.external_id}" + (f" generation {generation}" if generation else "") +
                                ": not found (deleted or replaced since it was listed; the next scan carries the current generation)",
                                404) from None
            raise HttpError(self._scrub(str(exc)), exc.status) from None

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
