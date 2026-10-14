"""WebDAV (Nextcloud, ownCloud, Apache mod_dav, nginx dav, IIS / on-prem SharePoint WebDAV, Synology, ...) and the
shared crawl engine of the three "file protocol" connectors (webdav.py, ftp.py, sftp.py).

Protocol calls used (RFC 4918 "HTTP Extensions for Web Distributed Authoring and Versioning"):
  * PROPFIND <collection>/  with `Depth: 1` and an explicit <prop> body asking for resourcetype, getetag,
    getlastmodified, getcontentlength and getcontenttype; the 207 Multi-Status reply is read with a small
    ElementTree parser written for this module (no WebDAV client library).
  * GET <file> for the bytes (streamed into a temporary file with a size cap).
Never `Depth: infinity` (many servers refuse it and it cannot be resumed), never any write method.

Behaviour
  * Crawl style, resumable: the tree is walked one collection at a time in a total order (see `NetCrawlConnector`),
    the cursor is {"after": <last path emitted>}, and a scan that ran from the start to the end sets
    complete_scope="" so the sync layer deletes what was not seen. A resumed scan, a scan that had to skip a
    collection the server refused to list (HTTP 403), or one cut off by `max_depth` is NOT authoritative.
  * WebDAV has no pagination: batches follow `batch_size`, not server pages.
  * version = the `getetag` (weak/strong markers and quotes stripped) plus the size; when a server sends no etag,
    last-modified + size.
  * `external_id` = `path` = the path below `base_url`; `open()` and `refresh_acl()` need nothing else.
  * hrefs that point outside `base_url` (another host, or "../") are ignored, so a hostile or buggy server cannot
    make the connector read outside its configured tree. XML containing a DOCTYPE / entity declaration is
    rejected before parsing (no entity-expansion attacks).
  * Skips: collections, hidden dot-files (`skip_hidden`), names matching `skip_names`, files above
    `max_file_bytes` (default 200 MB).
  * Retries (429 / 502 / 503 / 504 and transport errors, honouring Retry-After) and the 401 / 403 -> `AuthError` rule
    come from `_http.Http`. A 403 while listing a sub-collection is not a bad credential: that collection is
    skipped and the scan is marked non-authoritative so nothing under it is deleted.

Config: base_url (required; e.g. https://cloud.example.com/remote.php/dav/files/alice/Documents), username +
password_env (HTTP Basic: Nextcloud / ownCloud app passwords go here) or token_env (Bearer), verify_tls, ca_bundle,
batch_size, max_depth, skip_hidden, skip_names, max_file_bytes, max_retries, acl_rules, default_acl.
Secrets are only ever read from the named environment variables, per request, and are not stored anywhere.

Permissions: folder -> principals through `AclRules` on the relative path. It is NOT the server's own sharing /
ACL model (WebDAV ACL, RFC 3744, is rarely implemented and not read here). No matching rule and no `default_acl`
means nobody can read the file (fail closed).

Not exercised against a live Nextcloud, ownCloud, IIS or SharePoint server. Tested against a REAL WebDAV server
(wsgidav served by cheroot on a local port, with real Basic authentication), plus a WSGI wrapper that injects
429 / 503 responses in front of it.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Callable, Iterator, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from app.scale.connectors._http import DEFAULT_MAX_BYTES, AuthError, Http, HttpError, env_secret
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

DEFAULT_SKIP_NAMES = ["thumbs.db", "desktop.ini", "~$*", ".ds_store"]
_DAV = "{DAV:}"
_UNSAFE_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


# ── shared crawl engine (also used by ftp.py and sftp.py) ───────────────────────────────────────────────────


@dataclass
class NetEntry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float | None = None      # POSIX seconds
    etag: str = ""
    mime: str = ""


class DirUnreadable(PermissionError):
    """Raised by a `NetFS.listdir` when the server refuses to list one (non-root) directory for this account."""


class NetFS(Protocol):
    def listdir(self, rel_dir: str) -> list[NetEntry]:
        """Entries of one directory ("" = the root). Raise `DirUnreadable` for "permission denied" on a sub-directory;
        any other failure must raise a non-OSError exception so the scan aborts instead of treating it as empty."""

    def open(self, rel_path: str, max_bytes: int) -> BinaryIO: ...


class NetCrawlConnector:
    """Walk a remote directory tree in one total order, resumably.

    Entries of a directory are sorted by `name` (+ "/" for directories). With that key, the order in which files are
    visited is exactly the order of their full relative paths as plain strings, so "everything after path P" is a
    well-defined resume point: a file is emitted iff its path > the cursor, and a directory is entered iff some
    path inside it can still be > the cursor. (Sorting by bare name does not have this property when a file name
    such as "report-2020.txt" sorts between a directory "report" and its content.)
    """

    kind = "netfs"

    def __init__(self, fs: NetFS, config: dict) -> None:
        self.fs = fs
        self.cfg = config
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.batch_size = max(1, int(config.get("batch_size", 500)))
        self.max_depth = int(config.get("max_depth", 64))
        self.max_bytes = int(config.get("max_file_bytes", DEFAULT_MAX_BYTES))
        self.skip_hidden = bool(config.get("skip_hidden", True))
        self.skip_names = [n.lower() for n in config.get("skip_names", DEFAULT_SKIP_NAMES)]
        self.stats: dict[str, int] = {}

    def _skipped(self, name: str) -> bool:
        lowered = name.lower()
        if self.skip_hidden and lowered.startswith("."):
            return True
        return any(lowered == s or (s.endswith("*") and lowered.startswith(s[:-1])) for s in self.skip_names)

    def _count(self, reason: str) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + 1

    def _walk(self, rel_dir: str, after: str | None, depth: int, state: dict) -> Iterator[tuple[str, NetEntry]]:
        try:
            entries = self.fs.listdir(rel_dir)
        except DirUnreadable:
            log.warning("%s: directory %r cannot be listed by this account; skipped, scan will not delete", self.kind, rel_dir)
            state["partial"] = True
            self._count("unreadable_dir")
            return
        for entry in sorted(entries, key=lambda e: e.name + ("/" if e.is_dir else "")):
            if self._skipped(entry.name):
                self._count("skipped_name")
                continue
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if entry.is_dir:
                prefix = rel + "/"
                if after is not None and not after.startswith(prefix) and prefix < after:
                    continue                       # everything inside sorts before the resume point
                if depth >= self.max_depth:
                    log.warning("%s: %r is deeper than max_depth=%d; not entered", self.kind, rel, self.max_depth)
                    state["partial"] = True
                    self._count("too_deep")
                    continue
                yield from self._walk(rel, after, depth + 1, state)
            elif after is None or rel > after:
                yield rel, entry

    def _to_file(self, rel: str, entry: NetEntry) -> SourceFile:
        modified = datetime.fromtimestamp(entry.mtime, tz=timezone.utc) if entry.mtime else None
        version = f"{entry.etag}:{entry.size}" if entry.etag else f"{int(round((entry.mtime or 0) * 1000))}:{entry.size}"
        return SourceFile(
            external_id=rel, path=rel, name=entry.name, size=entry.size, modified_at=modified, version=version,
            acl=self.rules.for_path(rel), mime=entry.mime or mimetypes.guess_type(entry.name)[0] or "",
        )

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        after = (cursor or {}).get("after") or None
        state: dict = {"partial": False}
        batch: list[Change] = []
        last = after
        for rel, entry in self._walk("", after, 0, state):
            if entry.size > self.max_bytes:
                log.info("%s: %s is %d bytes, over max_file_bytes; skipped", self.kind, rel, entry.size)
                self._count("oversize")
                continue
            batch.append(Change("upsert", rel, self._to_file(rel, entry)))
            last = rel
            if len(batch) >= self.batch_size:
                yield ScanBatch(batch, {"after": last})
                batch = []
        # Only a walk from the very first path to the last, that listed every directory, may declare the rest deleted.
        authoritative = after is None and not state["partial"]
        yield ScanBatch(batch, {}, done=True, complete_scope="" if authoritative else None)

    def open(self, file: SourceFile) -> BinaryIO:
        return self.fs.open(file.external_id, self.max_bytes)

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)


# ── WebDAV ──────────────────────────────────────────────────────────────────────────────────────────────────


def parse_xml(data: bytes | str) -> ET.Element:
    """ElementTree with the DOCTYPE / entity door closed (no entity-expansion or external-entity tricks)."""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    if _UNSAFE_XML.search(raw):
        raise HttpError("refusing an XML document that declares a DOCTYPE or entities")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HttpError(f"malformed XML in the server's reply: {exc}") from None


class _EnvAuth(httpx.Auth):
    """Basic or Bearer authentication whose secret is read from the environment on every request."""

    def __init__(self, username: str, password_env: str | None, token_env: str | None) -> None:
        self.username, self.password_env, self.token_env = username, password_env, token_env

    def auth_flow(self, request: httpx.Request):
        if self.token_env:
            request.headers["Authorization"] = "Bearer " + env_secret(self.token_env, "WebDAV bearer token")
        elif self.password_env:
            raw = f"{self.username}:{env_secret(self.password_env, 'WebDAV password')}".encode("utf-8")
            request.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        yield request


_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getetag/>'
    "<d:getlastmodified/><d:getcontentlength/><d:getcontenttype/></d:prop></d:propfind>"
)


def _parse_http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _clean_etag(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith(("W/", "w/")):
        value = value[2:]
    return value.strip('"')


class WebDavFS:
    def __init__(self, base_url: str, http: Http) -> None:
        parsed = urlparse(base_url)
        self.http = http
        self.root_url = base_url.rstrip("/")
        self.netloc = parsed.netloc.lower()
        self.base_path = unquote(parsed.path).rstrip("/")

    def _url(self, rel: str, trailing_slash: bool = False) -> str:
        url = self.root_url + ("/" + "/".join(quote(seg, safe="") for seg in rel.split("/")) if rel else "")
        return url + "/" if trailing_slash else url

    def listdir(self, rel_dir: str) -> list[NetEntry]:
        try:
            response = self.http.request(
                "PROPFIND", self._url(rel_dir, True), content=_PROPFIND_BODY.encode("utf-8"), ok=(207,),
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            )
        except AuthError as exc:
            if exc.status == 403 and rel_dir:
                raise DirUnreadable(rel_dir) from None
            raise
        except HttpError as exc:
            if exc.status == 404 and rel_dir:
                return []                      # the collection vanished since its parent was listed
            raise
        return self._parse(response.content, rel_dir)

    def _parse(self, body: bytes, rel_dir: str) -> list[NetEntry]:
        request_path = (self.base_path + "/" + rel_dir).rstrip("/")
        out: dict[str, NetEntry] = {}
        for node in parse_xml(body).iter(f"{_DAV}response"):
            href = (node.findtext(f"{_DAV}href") or "").strip()
            if not href:
                continue
            parsed = urlparse(href)
            if parsed.netloc and parsed.netloc.lower() != self.netloc:
                continue                       # another host: never follow it
            path = unquote(parsed.path)
            is_collection_href = path.endswith("/")
            path = path.rstrip("/")
            if path == request_path:
                continue                       # the collection itself
            if not path.startswith(request_path + "/"):
                continue                       # outside the directory we asked for
            name = path[len(request_path) + 1:]
            if "/" in name or name in ("", ".", ".."):
                continue                       # not a direct child (or a traversal attempt)
            props = self._ok_props(node)
            if props is None:
                continue
            resourcetype = props.find(f"{_DAV}resourcetype")
            is_dir = (resourcetype is not None and resourcetype.find(f"{_DAV}collection") is not None) or (
                resourcetype is None and is_collection_href)
            size = 0
            if not is_dir:
                try:
                    size = max(0, int((props.findtext(f"{_DAV}getcontentlength") or "0").strip() or 0))
                except ValueError:
                    size = 0
            out[name] = NetEntry(
                name=name, is_dir=is_dir, size=size, mtime=_parse_http_date(props.findtext(f"{_DAV}getlastmodified")),
                etag="" if is_dir else _clean_etag(props.findtext(f"{_DAV}getetag")),
                mime=(props.findtext(f"{_DAV}getcontenttype") or "").split(";")[0].strip(),
            )
        return list(out.values())

    @staticmethod
    def _ok_props(response: ET.Element) -> ET.Element | None:
        """Merge the <prop> blocks of the 2xx propstats (servers put unknown properties in a separate 404 propstat)."""
        merged = ET.Element(f"{_DAV}prop")
        for propstat in response.findall(f"{_DAV}propstat"):
            match = re.search(r"\s(\d{3})\b", propstat.findtext(f"{_DAV}status") or "")
            if match and not 200 <= int(match.group(1)) < 300:
                continue
            prop = propstat.find(f"{_DAV}prop")
            if prop is not None:
                merged.extend(list(prop))
        status = re.search(r"\s(\d{3})\b", response.findtext(f"{_DAV}status") or "")
        if status and not 200 <= int(status.group(1)) < 300:
            return None                        # the whole resource is reported as an error (e.g. 404)
        return merged

    def open(self, rel_path: str, max_bytes: int) -> BinaryIO:
        return self.http.download(self._url(rel_path), max_bytes=max_bytes)


class WebDavConnector(NetCrawlConnector):
    kind = "webdav"

    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        auth = _EnvAuth(config.get("username", ""), config.get("password_env"), config.get("token_env"))
        client = httpx.Client(
            auth=auth, transport=transport, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
            verify=config.get("ca_bundle") or bool(config.get("verify_tls", True)),
        )
        http = Http(client=client, sleep=sleep, max_retries=int(config.get("max_retries", 5)))
        super().__init__(WebDavFS(config["base_url"], http), config)
        self.http = http
