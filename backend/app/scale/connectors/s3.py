"""S3-compatible object stores through boto3 (pip install boto3): AWS S3, Cloudflare R2, MinIO, Wasabi,
Backblaze B2 (S3 API), DigitalOcean Spaces, Oracle OCI (S3 compatibility API), Ceph RGW.

Protocol calls used (Amazon S3 API Reference):
  * ListObjectsV2   GET /{bucket}?list-type=2&prefix=&max-keys=&continuation-token=&start-after=
  * GetObject       GET /{bucket}/{key}
Nothing else is called: no HeadObject, no ACL or tagging reads, no writes of any kind. The credentials
only need s3:ListBucket and s3:GetObject on the bucket / prefix.

Behaviour
  * Crawl style: the bucket has no change feed we can rely on across vendors, so each scan lists the prefix
    in key order (S3 returns UTF-8 binary order), one `ScanBatch` per ListObjectsV2 page. Unchanged objects
    cost nothing downstream because `version` ("<etag>:<size>") is compared by the sync layer.
  * Resumable: the cursor is {"after": <last key seen>} and the resumed listing starts with `StartAfter`,
    so a crashed multi-million-object scan continues where it stopped (continuation tokens are opaque and
    may expire, a key never does). A resumed scan is not authoritative (complete_scope=None); a scan that ran
    from the start to the end sets complete_scope="" so the sync layer deletes whatever was not seen.
  * `external_id` is the full object key. `path` is the key relative to the configured `prefix` (like the
    local / SMB connectors, whose paths are relative to the connection root), so `acl_rules` prefixes are
    relative to `prefix`. `open()` and `refresh_acl()` need nothing but those two fields (the sync layer
    does not persist `SourceFile.extra`).
  * Skipped, never failing the scan: "folder" marker objects (key ending in "/" or "_$folder$"), empty
    objects, names matching `skip_names` (default includes .keep / .gitkeep / .DS_Store / Thumbs.db),
    objects in the GLACIER / DEEP_ARCHIVE storage classes (not readable without a restore; `skip_archived`),
    and objects larger than `max_file_bytes` (default 200 MB).
  * Retries: throttling (HTTP 429/503, SlowDown / Throttling codes), 5xx and transport errors are retried
    with bounded exponential backoff, honouring Retry-After. botocore's own retry layer is switched off (max_attempts=0 means
    zero retries after the first attempt) so there is exactly one place, with one set of limits, that decides.
  * 401/403 and expired / invalid credential codes raise `AuthError` with the HTTP status and S3 error code
    only: not the request, the access key, or S3's free-text message.
  * Downloads stream into a temporary file with a hard size cap.

Config: bucket (required), prefix, endpoint_url (omit for AWS), region, addressing_style ("path" by default
when endpoint_url is set, otherwise "auto"), access_key_env / secret_key_env / session_token_env (names of
environment variables; when both key variables are omitted boto3's default chain is used: instance role,
profile, AWS_* variables), ca_bundle, page_size (<=1000), max_file_bytes, skip_names, skip_archived,
max_retries, acl_rules, default_acl.

Permissions: folder / key-prefix mapping through `AclRules` (see base.py), NOT the object's own S3 ACL,
bucket policy or IAM. Keys with no matching rule and no `default_acl` are readable by nobody (fail closed).

Not exercised against live AWS S3, R2, B2, Wasabi, Spaces or OCI. Tested against a real S3 protocol
server (moto's ThreadedMotoServer) with real boto3, plus botocore `before-send` hooks that return genuine
503 / 403 HTTP responses to exercise the retry and auth paths.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import posixpath
import tempfile
import time
from typing import BinaryIO, Callable, Iterator

from app.scale.connectors._http import (
    DEFAULT_MAX_BYTES, RETRY_STATUS, AuthError, HttpError, TooLarge, env_secret, parse_retry_after,
)
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

DEFAULT_SKIP_NAMES = [".keep", ".gitkeep", ".ds_store", "thumbs.db", "desktop.ini", "~$*"]
ARCHIVED_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}
_THROTTLE_CODES = {"SlowDown", "Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException",
                   "RequestThrottled", "ProvisionedThroughputExceededException", "RequestTimeout", "InternalError",
                   "ServiceUnavailable"}
_AUTH_CODES = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken",
               "TokenRefreshRequired", "InvalidSecurity", "AllAccessDisabled", "AccountProblem", "AuthorizationHeaderMalformed"}


class S3Connector:
    kind = "s3"

    def __init__(self, config: dict, *, client=None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = config
        self.bucket: str = config["bucket"]
        prefix = (config.get("prefix") or "").lstrip("/")
        self.prefix = prefix if (not prefix or prefix.endswith("/")) else prefix + "/"
        self.page_size = max(1, min(int(config.get("page_size", 1000)), 1000))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.skip_archived = bool(config.get("skip_archived", True))
        self.max_retries = int(config.get("max_retries", 5))
        self.max_wait = float(config.get("max_wait", 120.0))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self._sleep = sleep
        self._client = client
        self.stats: dict[str, int] = {}

    # ── client / retries ──

    def _s3(self):
        """The boto3 client, created on first use so secrets are read from the environment at call time."""
        if self._client is None:
            import boto3
            from botocore.config import Config

            cfg = self.cfg
            creds: dict[str, str] = {}
            if cfg.get("access_key_env") or cfg.get("secret_key_env"):
                creds["aws_access_key_id"] = env_secret(cfg.get("access_key_env"), "S3 access key id")
                creds["aws_secret_access_key"] = env_secret(cfg.get("secret_key_env"), "S3 secret access key")
                if cfg.get("session_token_env"):
                    creds["aws_session_token"] = env_secret(cfg["session_token_env"], "S3 session token")
            style = cfg.get("addressing_style") or ("path" if cfg.get("endpoint_url") else "auto")
            options = dict(
                signature_version="s3v4", s3={"addressing_style": style}, connect_timeout=15, read_timeout=60,
                retries={"max_attempts": 0, "mode": "standard"},   # no botocore retries: _retrying is the one place that decides
            )
            try:   # third-party stores do not all return the checksums newer botocore versions ask for
                config = Config(request_checksum_calculation="when_required", response_checksum_validation="when_required", **options)
            except TypeError:   # older botocore
                config = Config(**options)
            self._client = boto3.client(
                "s3", endpoint_url=cfg.get("endpoint_url") or None, region_name=cfg.get("region") or None,
                config=config, verify=cfg.get("ca_bundle") or True, **creds,
            )
        return self._client

    def _retrying(self, op: str, fn: Callable[[], object]):
        from botocore import exceptions as bex

        transient = (bex.ConnectionError, bex.HTTPClientError, bex.IncompleteReadError)
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except bex.ClientError as exc:
                status, code, headers = _error_parts(exc)
                if status in (401, 403) or code in _AUTH_CODES:
                    raise AuthError(f"S3 {op} on bucket {self.bucket!r} -> {status} {code}", status) from None
                if (status in RETRY_STATUS or status == 500 or code in _THROTTLE_CODES) and attempt < self.max_retries:
                    wait = parse_retry_after(headers.get("retry-after"), min(2 ** attempt, 30))
                    log.warning("S3 %s on %r -> %s %s, retrying in %.1fs", op, self.bucket, status, code, min(wait, self.max_wait))
                    self._sleep(min(wait, self.max_wait))
                    continue
                raise HttpError(f"S3 {op} on bucket {self.bucket!r} -> {status} {code}", status) from None
            except bex.SSLError as exc:
                raise HttpError(f"S3 {op} on bucket {self.bucket!r}: TLS error ({type(exc).__name__})") from None
            except (bex.NoCredentialsError, bex.PartialCredentialsError):
                raise AuthError(f"S3 {op} on bucket {self.bucket!r}: no usable credentials") from None
            except transient as exc:
                if attempt >= self.max_retries:
                    raise HttpError(f"S3 {op} on bucket {self.bucket!r}: {type(exc).__name__}") from None
                self._sleep(min(2 ** attempt, self.max_wait))
        raise HttpError(f"S3 {op} on bucket {self.bucket!r}: retries exhausted")

    # ── scanning ──

    def _skip_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.skip_names)

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _to_file(self, obj: dict) -> SourceFile | None:
        key = obj["Key"]
        if key.endswith("/") or key.endswith("_$folder$"):
            self._count("folder_marker")
            return None
        rel = key[len(self.prefix):] if key.startswith(self.prefix) else key
        name = posixpath.basename(rel)
        if not name or self._skip_name(name):
            self._count("skipped_name")
            return None
        size = int(obj.get("Size") or 0)
        if size == 0:
            self._count("empty")
            return None
        if size > self.max_bytes:
            log.info("S3 %s/%s is %d bytes, over max_file_bytes; skipped", self.bucket, key, size)
            self._count("oversize")
            return None
        if self.skip_archived and obj.get("StorageClass") in ARCHIVED_CLASSES:
            self._count("archived")
            return None
        etag = str(obj.get("ETag") or "").strip('"')
        return SourceFile(
            external_id=key, path=rel, name=name, size=size, modified_at=obj.get("LastModified"),
            version=f"{etag}:{size}", acl=self.rules.for_path(rel), mime=mimetypes.guess_type(name)[0] or "",
            extra={"bucket": self.bucket, "key": key},
        )

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        after = (cursor or {}).get("after") or ""
        params: dict = {"Bucket": self.bucket, "Prefix": self.prefix, "MaxKeys": self.page_size}
        if after:
            params["StartAfter"] = after
        last = after
        while True:
            page = self._retrying("ListObjectsV2", lambda p=dict(params): self._s3().list_objects_v2(**p))
            changes: list[Change] = []
            for obj in page.get("Contents") or []:
                last = obj["Key"]
                sf = self._to_file(obj)
                if sf is not None:
                    changes.append(Change("upsert", sf.external_id, sf))
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                # Only a listing that ran from the first key to the last may declare the rest deleted.
                yield ScanBatch(changes, {}, done=True, complete_scope=None if after else "")
                return
            yield ScanBatch(changes, {"after": last})
            params.pop("StartAfter", None)
            params["ContinuationToken"] = token

    # ── bytes / permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        key = file.external_id

        def fetch() -> BinaryIO:
            response = self._s3().get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            tmp = tempfile.TemporaryFile()
            try:
                length = response.get("ContentLength")
                if length is not None and int(length) > self.max_bytes:
                    raise TooLarge(f"S3 object {key!r} is {length} bytes, larger than {self.max_bytes}")
                total = 0
                for chunk in body.iter_chunks(1 << 20):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise TooLarge(f"S3 object {key!r} is larger than {self.max_bytes} bytes")
                    tmp.write(chunk)
                tmp.seek(0)
                return tmp
            except BaseException:
                tmp.close()
                raise
            finally:
                body.close()

        return self._retrying("GetObject", fetch)  # type: ignore[return-value]

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)


def _error_parts(exc) -> tuple[int, str, dict]:
    response = getattr(exc, "response", None) or {}
    meta = response.get("ResponseMetadata") or {}
    headers = {str(k).lower(): v for k, v in (meta.get("HTTPHeaders") or {}).items()}
    return int(meta.get("HTTPStatusCode") or 0), str((response.get("Error") or {}).get("Code") or ""), headers
