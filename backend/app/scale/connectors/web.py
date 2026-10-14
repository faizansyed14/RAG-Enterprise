"""A website as a source: crawl pages from seed URLs, XML sitemaps and RSS / Atom feeds; every page is one file.

What it requests (plain HTTP GET with the standard `User-Agent`; nothing else, never a POST / HEAD / cookie login):
  * GET <scheme>://<host>/robots.txt          once per host and scan, parsed with the standard library's `urllib.robotparser`
  * GET <sitemap_urls>                        <urlset> and <sitemapindex> (nested up to 3 levels, 50 files, `.xml.gz` too)
  * GET <feed_urls>                           RSS 2.0 / RSS 1.0 (RDF) / Atom: the links of the items are queued as pages
  * GET <page>                                seeds, sitemap / feed entries, links found in HTML (<a>, <area>, <iframe>, <frame>)
        with `If-None-Match` / `If-Modified-Since` when a previous scan saw the page, same-host redirects followed by hand
  * open(): GET <page> again, streamed into a temporary file (size capped)

Behaviour
  * Breadth-first from the seeds, depth 0 = seeds, sitemap entries and feed items; HTML links go one level deeper, up to
    `max_depth`. At most `max_pages` pages are accepted per scan. Fragments, `utm_*` / `gclid` / `fbclid` parameters and dot
    segments are removed from URLs; `include_patterns` / `exclude_patterns` (regular expressions on the full URL) filter links.
  * Same host only. A host is a link target only if it is in `allowed_hosts` (default: the hosts of the seeds, sitemaps and
    feeds, written as in the URL: `example.com`, `intranet:8080`). A redirect is followed only to an allowed host and only
    after robots.txt allows the target; a redirect elsewhere is refused (counted, never requested). Credentials are never put
    in a URL (such links are ignored) and are only sent to allowed hosts.
  * robots.txt (`respect_robots`, default true): rules for our product token (the first word of `user_agent`) or `*`, with
    `Crawl-delay` honoured on top of `delay_seconds` (capped at `max_crawl_delay`). A missing robots.txt (404, other 4xx)
    allows everything; 401 / 403 and an unreachable one (5xx, timeouts) forbid everything, and the second also marks the
    scan non-authoritative. Also honoured: `<meta name="robots">` and `X-Robots-Tag` (`noindex`: the page is not indexed but
    its links are followed; `nofollow` / `none`: the links are not followed) and `rel="nofollow"` on links. Whether
    `*` / `$` / longest-match rules work is up to the `urllib.robotparser` of the Python that runs this.
  * What is indexed: HTML (`.html`), PDF, DOCX / XLSX / PPTX (and the old DOC / XLS / PPT), TXT, MD, CSV -- by Content-Type, or
    by the URL's extension when the server says `application/octet-stream`. Everything else (images, scripts, archives,
    media) is skipped, and links with an obviously binary extension are not even requested. Non-HTML files are passed
    through with their extension; HTML is kept as it was served, as `.html`.
  * Politeness: one request per `delay_seconds` per host (default 1 s), also across redirects and robots.txt fetches.
    429 / 502 / 503 / 504 are retried with `Retry-After` honoured (capped at `max_wait`), up to `max_retries`.
  * `external_id` = `web:<normalised final URL>`; `path` = `<name>/<host>[-<port>]/<url path>` (`/` -> `index.html`, `/about`
    -> `about.html`, a query string -> `~<hash>` before the extension) so folder prefixes of `acl_rules` follow the site's
    own structure. `version` = the ETag when the server sent one, else a hash of the body ("etag:..." / "sha256:...").
    Pages whose body changes on every request (a timestamp, a CSRF token) and that send no stable ETag will be re-indexed on
    every scan; nothing here can tell.
  * Cursors. Mid-scan: {queue, seen (hashes of URLs), known, fresh, counters} -- enough to continue exactly there; about
    100-250 bytes per page, so keep `max_pages` reasonable. After a finished scan: {"known": {url: validators}}, which the next
    scan uses for conditional requests (a 304 keeps the page, and a URL that answers 404 / 410 is reported as a delete). Until
    the sync layer persists the cursor of a finished scan, every scan is a full one (it saves {} at the end), so conditional
    requests only help when that cursor is given back. No secret is ever put in a cursor; do not put one in a URL either.
  * Deletions: a scan that started from the beginning, emptied its queue, hit no `max_pages` / queue limit and met no
    transient failure (5xx, timeout, a 401 / 403 page, an unreachable robots.txt, an unreadable sitemap) sets
    `complete_scope="web:"`, so the sync layer deletes every page it did not see. Anything else never deletes. A scan in which
    nothing at all could be fetched raises instead of reporting success.

Config: seed_urls and / or sitemap_urls and / or feed_urls (at least one), allowed_hosts, max_pages (500), max_depth (3),
respect_robots (true), user_agent ("RagEngineBot/1.0"; add a contact URL), delay_seconds (1.0), max_crawl_delay (30),
include_patterns, exclude_patterns, strip_params, skip_extensions, name ("web"), batch_size (50), timeout (30),
max_retries (3), max_wait (60), max_redirects (5), max_file_bytes (50 MB), max_xml_bytes (20 MB), username + password_env
(HTTP Basic) or token_env (Bearer) -- secrets are only read from those environment variables, per request --, verify_tls,
ca_bundle, acl_rules, default_acl.

Permissions: folder -> principals through `AclRules` on the virtual path, nothing from the website itself (a public
site and an intranet look the same to us); no matching rule and no `default_acl` => nobody can read it (fail closed).

Not done: JavaScript rendering (a client-side-rendered page is indexed as its empty shell), `<meta http-equiv=refresh>`,
canonical links (two URLs with the same content are two documents unless one redirects), cookie logins / forms, `Sitemap:`
lines of robots.txt (list the sitemaps in `sitemap_urls`), inline text of feed items (an item is the page its link points to),
HTML character-set sniffing from <meta> (the Content-Type header or UTF-8 is used for link extraction only; the file itself
is stored byte for byte), HTTP/2.

Tested against a REAL local `http.server` serving a small site (robots.txt with Disallow and Crawl-delay, ETag and
Last-Modified with real 304 replies, redirects, a sitemap, a sitemap index, a gzipped sitemap, RSS and Atom feeds, PDF / DOCX,
Basic and Bearer auth, 503 + Retry-After). The politeness clock is injected. Not exercised against any real website, CDN,
WAF or a site behind a login page.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, BinaryIO, Callable, Iterator
from urllib import robotparser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.scale.connectors._http import RETRY_STATUS, AuthError, Http, HttpError, TooLarge, env_secret, parse_retry_after, safe_name
from app.scale.connectors._objects import short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile
from app.scale.connectors.webdav import parse_xml

log = logging.getLogger(__name__)

SCOPE = "web:"
_TYPES = {
    "text/html": ".html", "application/xhtml+xml": ".html", "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc", "application/vnd.ms-excel": ".xls", "application/vnd.ms-powerpoint": ".ppt",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
}
_URL_EXTENSIONS = {".html", ".htm", ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".txt", ".md", ".csv"}
_UNTYPED = {"", "application/octet-stream", "binary/octet-stream"}
_SKIP_EXTENSIONS = (
    ".png .jpg .jpeg .gif .webp .svg .ico .bmp .tif .tiff .avif .mp3 .mp4 .m4a .m4v .avi .mov .wmv .webm .ogg .wav .flac "
    ".zip .gz .tgz .tar .rar .7z .bz2 .xz .css .js .mjs .map .woff .woff2 .ttf .otf .eot .exe .dmg .iso .apk .msi .bin"
).split()
_TRACKING = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"]
_HTML_EXTENSIONS = {".html", ".htm"}
_PASS = {200, 304, 301, 302, 303, 307, 308, 404, 410}
_REDIRECTS = {301, 302, 303, 307, 308}
_ENC = "!$&'()*+,;=:@~-._"


class _OffSite(Exception):
    """A redirect target outside `allowed_hosts`: refused, never requested."""


class _Blocked(Exception):
    """robots.txt forbids this URL."""


class _Duplicate(Exception):
    """A redirect to a URL this scan already has."""


# ── URLs ──


def _clean_path(path: str) -> str:
    out: list[str] = []
    for seg in unquote(path or "/").split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(seg)
    if not out or out[0] != "":
        out.insert(0, "")
    joined = "/".join(out)
    return quote(joined if joined else "/", safe="/" + _ENC) or "/"


def normalize_url(raw: str, base: str | None = None, strip_params: list[str] | tuple[str, ...] = ()) -> str | None:
    """Absolute http(s) URL without fragment, credentials, default port, dot segments or tracking parameters; None if unusable."""
    try:
        absolute = urljoin(base, raw.strip()) if base else raw.strip()
        parts = urlsplit(absolute)
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower().rstrip(".")
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port and port != (80 if scheme == "http" else 443):
        netloc += f":{port}"
    drop = {p.lower() for p in strip_params}
    query = "&".join(q for q in parts.query.split("&") if q and q.split("=", 1)[0].lower() not in drop)
    return urlunsplit((scheme, netloc, _clean_path(parts.path), query, ""))


def _hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _safe(url: str) -> str:
    return url.split("?", 1)[0]


def _clean_etag(value: str | None) -> str:
    value = (value or "").strip()
    return (value[2:] if value[:2] in ("W/", "w/") else value).strip('"')


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _gunzip(data: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(31)
    out = inflater.decompress(data, limit + 1)
    if len(out) > limit or inflater.unconsumed_tail:
        raise TooLarge(f"a compressed sitemap or feed expands to more than {limit} bytes")
    return out


# ── HTML ──


class _PageParser(HTMLParser):
    """Links, <base>, and robots directives of one HTML page, fed incrementally."""

    def __init__(self, token: str, max_links: int = 10_000) -> None:
        super().__init__(convert_charrefs=True)
        self.token, self.max_links = token.lower(), max_links
        self.links: list[str] = []
        self.base: str | None = None
        self.directives: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag in ("a", "area"):
            if a.get("href") and "nofollow" not in a.get("rel", "").lower().split() and len(self.links) < self.max_links:
                self.links.append(a["href"])
        elif tag in ("iframe", "frame"):
            if a.get("src") and len(self.links) < self.max_links:
                self.links.append(a["src"])
        elif tag == "meta" and a.get("name", "").strip().lower() in ("robots", self.token):
            self.directives |= _directives(a.get("content", ""))


def _directives(text: str) -> set[str]:
    found = {t.strip().lower() for t in re.split(r"[,\s]+", text) if t.strip()}
    if "none" in found:
        found |= {"noindex", "nofollow"}
    return found & {"noindex", "nofollow"}


def _header_directives(values: list[str], token: str) -> set[str]:
    found: set[str] = set()
    for value in values:
        head, sep, rest = value.partition(":")
        if sep and re.fullmatch(r"[A-Za-z0-9_-]+", head.strip()) and head.strip().lower() not in ("unavailable_after", "max-snippet"):
            if head.strip().lower() in (token.lower(), "*"):
                found |= _directives(rest)
        else:
            found |= _directives(value)
    return found


# ── what the scan learns about one page ──


@dataclass
class _Page:
    ext: str
    mime: str
    size: int
    version: str
    etag: str = ""                     # as sent (quotes, W/): replayed in If-None-Match
    last_modified: str = ""
    links: list[str] = field(default_factory=list)
    base: str | None = None
    directives: set[str] = field(default_factory=set)

    def entry(self, depth: int) -> list:
        return [self.etag, self.last_modified, self.version, self.size, self.mime, depth, self.ext]

    @classmethod
    def from_entry(cls, entry: list) -> "_Page":
        return cls(ext=entry[6], mime=entry[4], size=entry[3], version=entry[2], etag=entry[0], last_modified=entry[1])


@dataclass
class _Skip:
    reason: str


@dataclass
class _Response:
    status: int
    url: str
    headers: httpx.Headers
    value: Any


class _Robots:
    def __init__(self, parser: robotparser.RobotFileParser | None = None, *, allow_all: bool = False) -> None:
        self.parser, self.allow_all = parser, allow_all

    def can_fetch(self, token: str, url: str) -> bool:
        if self.parser is not None:
            return self.parser.can_fetch(token, url)
        return self.allow_all

    def crawl_delay(self, token: str) -> float:
        return float(self.parser.crawl_delay(token) or 0) if self.parser is not None else 0.0


class _Scan:
    """Everything one scan knows; `cursor()` is its JSON form."""

    def __init__(self, cursor: dict) -> None:
        usable = isinstance(cursor.get("queue"), list) and isinstance(cursor.get("seen"), list) and cursor.get("discovered") is True
        self.queue: deque[tuple[str, int]] = deque()
        self.seen: set[str] = set()
        self.fetched = self.errors = self.forbidden = self.emitted = 0
        self.truncated = self.discovered = False
        self.resumed = False
        if usable:
            try:
                self.queue = deque((str(u), int(d)) for u, d in cursor["queue"])
                self.seen = {str(s) for s in cursor["seen"]}
                self.fetched, self.errors, self.forbidden = (int(cursor.get(k, 0)) for k in ("fetched", "errors", "forbidden"))
                self.truncated, self.discovered = bool(cursor.get("truncated")), bool(cursor.get("discovered"))
                self.resumed = True
            except (TypeError, ValueError):
                self.queue, self.seen, self.fetched, self.errors, self.forbidden = deque(), set(), 0, 0, 0
                self.truncated = self.discovered = False
        self.known = self._entries(cursor.get("known"))
        self.fresh = self._entries(cursor.get("fresh")) if self.resumed else {}
        self.robots: dict[str, _Robots] = {}
        self.last_error = ""

    @staticmethod
    def _entries(raw: Any) -> dict[str, list]:
        out: dict[str, list] = {}
        for url, entry in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(entry, list) and len(entry) == 7 and isinstance(entry[3], int):
                out[str(url)] = entry
        return out

    def cursor(self) -> dict:
        return {
            "queue": [[u, d] for u, d in self.queue], "seen": sorted(self.seen), "fetched": self.fetched,
            "errors": self.errors, "forbidden": self.forbidden, "truncated": self.truncated, "discovered": self.discovered,
            "known": self.known, "fresh": self.fresh,
        }


class _EnvAuth(httpx.Auth):
    """Basic or Bearer authentication whose secret is read from the environment on every request."""

    def __init__(self, username: str, password_env: str | None, token_env: str | None) -> None:
        self.username, self.password_env, self.token_env = username, password_env, token_env

    def auth_flow(self, request: httpx.Request):
        if self.token_env:
            request.headers["Authorization"] = "Bearer " + env_secret(self.token_env, "website bearer token")
        elif self.password_env:
            raw = f"{self.username}:{env_secret(self.password_env, 'website password')}".encode("utf-8")
            request.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        yield request


class WebConnector:
    kind = "web"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = config
        self.seeds = self._urls(config.get("seed_urls"), "seed_urls")
        self.sitemaps = self._urls(config.get("sitemap_urls"), "sitemap_urls")
        self.feeds = self._urls(config.get("feed_urls"), "feed_urls")
        if not (self.seeds or self.sitemaps or self.feeds):
            raise ValueError("web: config needs `seed_urls`, `sitemap_urls` or `feed_urls`")
        self.strip_params = list(config.get("strip_params", _TRACKING))
        given = config.get("allowed_hosts")
        derived = {urlsplit(u).netloc for u in self.seeds + self.sitemaps + self.feeds}
        self.hosts = {self._host(h) for h in given} if given else derived
        if given and not derived <= self.hosts:
            raise ValueError(f"web: the host of a seed / sitemap / feed ({sorted(derived - self.hosts)}) is not in allowed_hosts")
        self.respect_robots = bool(config.get("respect_robots", True))
        self.user_agent = str(config.get("user_agent") or "RagEngineBot/1.0")
        self.token = re.split(r"[/\s]", self.user_agent.strip())[0] or "RagEngineBot"
        self.delay = max(0.0, float(config.get("delay_seconds", 1.0)))
        self.max_crawl_delay = max(0.0, float(config.get("max_crawl_delay", 30)))
        self.max_pages = int(config.get("max_pages", 500))
        self.max_depth = int(config.get("max_depth", 3))
        self.max_queue = max(1000, 2 * self.max_pages)
        self.batch_size = max(1, int(config.get("batch_size", 50)))
        self.max_redirects = int(config.get("max_redirects", 5))
        self.max_retries = int(config.get("max_retries", 3))
        self.max_wait = float(config.get("max_wait", 60.0))
        self.max_bytes = int(config.get("max_file_bytes", 50 * 1024 * 1024))
        self.max_xml = int(config.get("max_xml_bytes", 20 * 1024 * 1024))
        self.name = safe_name(str(config.get("name") or "web"), "web", 60)
        self.skip_ext = {e.lower() if e.startswith(".") else "." + e.lower() for e in config.get("skip_extensions", _SKIP_EXTENSIONS)}
        self.include = [re.compile(p) for p in config.get("include_patterns", [])]
        self.exclude = [re.compile(p) for p in config.get("exclude_patterns", [])]
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.has_credentials = bool(config.get("password_env") or config.get("token_env"))
        if client is None:
            client = httpx.Client(
                auth=_EnvAuth(config.get("username", ""), config.get("password_env"), config.get("token_env")),
                transport=transport, timeout=httpx.Timeout(float(config.get("timeout", 30)), connect=15.0), follow_redirects=False,
                verify=config.get("ca_bundle") or bool(config.get("verify_tls", True)), headers={"User-Agent": self.user_agent},
            )
        self.http = Http(client=client, sleep=sleep, max_retries=self.max_retries, max_wait=self.max_wait, follow_redirects=False)
        self._sleep, self._mono = sleep, monotonic
        self._lock = threading.Lock()
        self._ready: dict[str, float] = {}
        self._crawl_delay: dict[str, float] = {}
        self.stats: dict[str, int] = {}

    @staticmethod
    def _urls(raw: Any, field_name: str) -> list[str]:
        values = [raw] if isinstance(raw, str) else list(raw or [])
        out = []
        for value in values:
            url = normalize_url(str(value))
            if url is None:
                raise ValueError(f"web: `{field_name}` contains something that is not an http(s) URL: {str(value)[:80]!r}")
            out.append(url)
        return out

    @staticmethod
    def _host(value: str) -> str:
        value = str(value).strip().lower()
        return urlsplit(value).netloc if "://" in value else value.rstrip("/")

    def _count(self, reason: str, n: int = 1) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + n

    def _allowed(self, url: str) -> bool:
        return urlsplit(url).netloc in self.hosts

    # ── http ──

    def _delay_for(self, netloc: str) -> float:
        return max(self.delay, self._crawl_delay.get(netloc, 0.0))

    def _pause(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:                                   # one slot per host, handed out in order, also across threads
            now = self._mono()
            slot = max(self._ready.get(host, 0.0), now)
            self._ready[host] = slot + self._delay_for(host)
        if slot > now:
            self._sleep(slot - now)

    def _send(self, url: str, headers: dict, consume: Callable[[httpx.Response, str], Any] | None) -> tuple[int, httpx.Headers, Any]:
        last = ""
        for attempt in range(self.max_retries + 1):
            self._pause(url)
            wait: float | None = None
            try:
                with self.http.client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5", **headers}) as response:
                    status = response.status_code
                    if status in RETRY_STATUS and attempt < self.max_retries:
                        wait = parse_retry_after(response.headers.get("Retry-After"), min(2 ** attempt, 30))
                        last = f"HTTP {status}"
                    elif status in (401, 403):
                        raise AuthError(f"GET {_safe(url)} -> {status}", status)
                    elif status in _PASS:
                        return status, response.headers, (consume(response, url) if (status == 200 and consume) else None)
                    else:
                        raise HttpError(f"GET {_safe(url)} -> {status}", status)
            except httpx.InvalidURL:
                raise HttpError(f"GET {_safe(url)}: not a valid URL") from None
            except httpx.RequestError as exc:
                last = type(exc).__name__
                if attempt >= self.max_retries:
                    raise HttpError(f"GET {_safe(url)}: {last}") from None
                wait = min(2 ** attempt, self.max_wait)
            if wait is not None:
                self._sleep(min(wait, self.max_wait))
        raise HttpError(f"GET {_safe(url)}: retries exhausted ({last})")

    def _fetch(
        self, url: str, *, consume: Callable[[httpx.Response, str], Any] | None = None,
        validators: Callable[[str], dict] | None = None, follow: Callable[[str], None] | None = None,
    ) -> _Response:
        """GET `url`, following same-host redirects by hand. Returns on 200 / 304 / 404 / 410; anything else raises."""
        current = url
        for _ in range(self.max_redirects + 1):
            status, headers, value = self._send(current, validators(current) if validators else {}, consume)
            if status not in _REDIRECTS:
                return _Response(status, current, headers, value)
            target = normalize_url(headers.get("location", ""), current, self.strip_params)
            if target is None:
                raise HttpError(f"GET {_safe(current)} -> {status} without a usable Location")
            if not self._allowed(target):
                raise _OffSite(target)
            if follow:
                follow(target)
            current = target
        raise HttpError(f"GET {_safe(url)}: more than {self.max_redirects} redirects")

    # ── robots.txt ──

    def _robots(self, st: _Scan, url: str) -> _Robots:
        parts = urlsplit(url)
        if parts.netloc in st.robots:
            return st.robots[parts.netloc]
        robots = _Robots(allow_all=False)
        try:
            resp = self._fetch(f"{parts.scheme}://{parts.netloc}/robots.txt", consume=self._read(512 * 1024, strict=False))
            if resp.status == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(resp.value.decode("utf-8", "replace").splitlines())
                robots = _Robots(parser)
                self._crawl_delay[parts.netloc] = min(robots.crawl_delay(self.token), self.max_crawl_delay)
            else:
                robots = _Robots(allow_all=True)                       # 404 / 410: no robots.txt
        except AuthError:
            st.forbidden += 1                              # a WAF or a login in front of the site: not a reason to delete anything
            st.last_error = f"robots.txt of {parts.netloc} answers 401/403"
            log.warning("web: robots.txt of %s answers 401/403; treating the whole site as disallowed", parts.netloc)
        except (_OffSite, _Blocked, _Duplicate):
            pass
        except HttpError as exc:
            if exc.status and 400 <= exc.status < 500 and exc.status != 429:
                robots = _Robots(allow_all=True)
            else:
                st.errors += 1
                st.last_error = str(exc)
                log.warning("web: robots.txt of %s is unreachable (%s); treating the whole site as disallowed", parts.netloc, exc)
        st.robots[parts.netloc] = robots
        return robots

    def _may(self, st: _Scan, url: str) -> bool:
        return (not self.respect_robots) or self._robots(st, url).can_fetch(self.token, url)

    # ── bodies ──

    def _read(self, limit: int, *, strict: bool) -> Callable[[httpx.Response, str], bytes]:
        def consume(response: httpx.Response, url: str) -> bytes:
            out, total = bytearray(), 0
            for chunk in response.iter_bytes(65536):
                total += len(chunk)
                if total > limit:
                    if strict:
                        raise TooLarge(f"{_safe(url)} is larger than {limit} bytes")
                    break
                out += chunk
            return bytes(out)
        return consume

    def _extension(self, ctype: str, url: str) -> str | None:
        if ctype in _TYPES:
            return _TYPES[ctype]
        ext = os.path.splitext(unquote(urlsplit(url).path))[1].lower()
        return ext if ctype in _UNTYPED and ext in _URL_EXTENSIONS else None

    def _inspect(self) -> Callable[[httpx.Response, str], _Page | _Skip]:
        def consume(response: httpx.Response, url: str) -> _Page | _Skip:
            headers = response.headers
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            ext = self._extension(ctype, url)
            if ext is None:
                return _Skip("skipped_type")
            length = int(headers["content-length"]) if headers.get("content-length", "").isdigit() else None
            if length is not None and length > self.max_bytes:
                return _Skip("oversize")
            etag, modified = headers.get("etag", ""), headers.get("last-modified", "")
            is_html = ext in _HTML_EXTENSIONS
            directives = _header_directives(headers.get_list("x-robots-tag"), self.token) if self.respect_robots else set()
            if not is_html and etag:                         # the validator is the version: the body need not be read
                return _Page(ext, ctype or "application/octet-stream", length or 0, "etag:" + _clean_etag(etag), etag, modified, directives=directives)
            digest, size = hashlib.sha256(), 0
            parser = _PageParser(self.token) if is_html else None
            decoder = None
            if parser:
                match = re.search(r"charset=([\w.:-]+)", headers.get("content-type", ""), re.I)
                try:
                    decoder = codecs.getincrementaldecoder(match.group(1) if match else "utf-8")("replace")
                except LookupError:
                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
            for chunk in response.iter_bytes(65536):
                size += len(chunk)
                if size > self.max_bytes:
                    return _Skip("oversize")
                digest.update(chunk)
                if parser and decoder:
                    parser.feed(decoder.decode(chunk))
            if parser and decoder:
                parser.feed(decoder.decode(b"", final=True))
                parser.close()
                directives |= parser.directives if self.respect_robots else set()
            version = "etag:" + _clean_etag(etag) if etag else "sha256:" + digest.hexdigest()[:24]
            return _Page(ext, ctype or "application/octet-stream", size, version, etag, modified, parser.links if parser else [],
                         parser.base if parser else None, directives)
        return consume

    # ── frontier ──

    def _enqueue(self, st: _Scan, raw: str, depth: int, *, base: str | None = None, force: bool = False) -> bool:
        url = normalize_url(raw, base, self.strip_params)
        if url is None:
            return False
        if not self._allowed(url):
            self._count("offsite_links")
            return False
        if not force:
            if os.path.splitext(urlsplit(url).path)[1].lower() in self.skip_ext:
                self._count("skipped_extension")
                return False
            if any(p.search(url) for p in self.exclude) or (self.include and not any(p.search(url) for p in self.include)):
                self._count("filtered")
                return False
        key = _hash(url)
        if key in st.seen:
            return False
        if len(st.queue) >= self.max_queue:
            st.truncated = True
            return False
        st.seen.add(key)
        st.queue.append((url, depth))
        return True

    def _listing(self, st: _Scan, url: str, level: int, visited: set[str]) -> list[str]:
        """Page URLs of a sitemap, sitemap index or feed."""
        resp = self._fetch(url, consume=self._read(self.max_xml, strict=True))
        if resp.status != 200:
            raise HttpError(f"GET {_safe(url)} -> {resp.status}", resp.status)
        data: bytes = resp.value
        if data[:2] == b"\x1f\x8b":
            data = _gunzip(data, self.max_xml)
        root = parse_xml(data)
        kind = _local(root.tag)

        def children(parent, name):
            return [c for c in parent if _local(c.tag) == name]

        def text(parent, name):
            return [(c.text or "").strip() for c in children(parent, name) if (c.text or "").strip()]

        urls: list[str] = []
        if kind == "sitemapindex":
            for sitemap in children(root, "sitemap"):
                for loc in text(sitemap, "loc"):
                    sub = normalize_url(loc, resp.url)
                    if sub is None or sub in visited or level >= 3 or len(visited) >= 50 or not self._allowed(sub):
                        continue
                    visited.add(sub)
                    try:
                        urls += self._listing(st, sub, level + 1, visited)
                    except (HttpError, _OffSite, _Blocked, _Duplicate) as exc:
                        st.errors += 1
                        st.last_error = str(exc)
                        self._count("listing_errors")
        elif kind == "urlset":
            for entry in children(root, "url"):
                urls += [urljoin(resp.url, loc) for loc in text(entry, "loc")]
        elif kind in ("rss", "rdf"):
            for item in root.iter():
                if _local(item.tag) == "item":
                    links = text(item, "link") or [g for g in text(item, "guid") if g.startswith(("http://", "https://"))]
                    urls += [urljoin(resp.url, link) for link in links[:1]]
        elif kind == "feed":
            for entry in children(root, "entry"):
                for link in children(entry, "link"):
                    if (link.get("rel") or "alternate") == "alternate" and link.get("href"):
                        urls.append(urljoin(resp.url, link.get("href")))
                        break
        else:
            raise HttpError(f"{_safe(url)} is neither a sitemap nor a feed (root element <{kind}>)")
        return urls

    def _discover(self, st: _Scan) -> None:
        for url in self.seeds:
            self._enqueue(st, url, 0, force=True)
        for listing in self.sitemaps + self.feeds:
            try:
                for url in self._listing(st, listing, 0, {listing}):
                    self._enqueue(st, url, 0)
            except (HttpError, _OffSite, _Blocked, _Duplicate) as exc:
                if isinstance(exc, AuthError) and exc.status == 401 and self.has_credentials:
                    raise
                st.errors += 1
                st.last_error = str(exc)
                self._count("listing_errors")
                log.warning("web: cannot read %s: %s", _safe(listing), exc)
        for url, entry in sorted(st.known.items()):
            self._enqueue(st, url, int(entry[5]))

    # ── one page ──

    def _path(self, url: str, ext: str) -> str:
        parts = urlsplit(url)
        netloc = parts.netloc.replace(":", "-")
        raw = unquote(parts.path)
        segments = [s for s in raw.split("/") if s]
        if raw.endswith("/") or not segments:
            directories, leaf = segments, "index"
        else:
            directories, leaf = segments[:-1], segments[-1]
        stem, url_ext = os.path.splitext(leaf)
        if url_ext.lower() in (_HTML_EXTENSIONS if ext == ".html" else {ext}):
            ext = url_ext.lower()
        else:
            stem = leaf
        if parts.query:
            stem += "~" + short_hash(parts.query, size=8)
        parts_out = [safe_name(netloc, "host", 80), *(safe_name(d, "dir", 80) for d in directories), safe_name(stem, "index", 100) + ext]
        return f"{self.name}/" + "/".join(parts_out)

    def _file(self, url: str, page: _Page) -> SourceFile:
        path = self._path(url, page.ext)
        try:
            modified = parsedate_to_datetime(page.last_modified).astimezone(timezone.utc) if page.last_modified else None
        except (TypeError, ValueError):
            modified = None
        return SourceFile(
            external_id=SCOPE + url, path=path, name=path.rsplit("/", 1)[-1], size=page.size, modified_at=modified,
            version=page.version, acl=self.rules.for_path(path), mime=page.mime, extra={"url": url},
        )

    def _visit(self, st: _Scan, url: str, depth: int) -> Change | None:
        if not self._may(st, url):
            self._count("robots_blocked")
            return None

        def validators(candidate: str) -> dict:
            entry = st.known.get(candidate)
            headers = {}
            if entry and entry[0]:
                headers["If-None-Match"] = entry[0]
            if entry and entry[1]:
                headers["If-Modified-Since"] = entry[1]
            return headers

        def follow(target: str) -> None:
            if not self._may(st, target):
                raise _Blocked(target)
            key = _hash(target)
            if key in st.seen:
                raise _Duplicate(target)
            st.seen.add(key)

        try:
            resp = self._fetch(url, consume=self._inspect(), validators=validators, follow=follow)
        except _Blocked:
            self._count("robots_blocked")
            return None
        except _Duplicate:
            self._count("redirect_duplicates")
            return None
        except _OffSite:
            self._count("offsite_redirects")
            log.info("web: %s redirects to a host that is not allowed; not followed", _safe(url))
            return None
        except AuthError as exc:
            if exc.status == 401 and self.has_credentials:
                raise                                          # the configured credentials were refused: stop, do not hammer
            st.forbidden += 1
            self._count("forbidden")
            return None
        except HttpError as exc:
            st.errors += 1
            st.last_error = str(exc)
            self._count("errors")
            log.warning("web: %s", exc)
            return None
        final = resp.url
        if resp.status in (404, 410):
            self._count("gone")
            known = st.known.pop(final, None) or st.known.pop(url, None)
            return Change("delete", SCOPE + final) if known is not None else None
        if resp.status == 304:
            entry = st.known.get(final)
            if entry is None:
                st.errors += 1
                st.last_error = f"GET {_safe(final)} -> 304 without a validator we sent"
                return None
            page = _Page.from_entry(entry)
            self._count("not_modified")
        else:
            page = resp.value
            if isinstance(page, _Skip):
                self._count(page.reason)
                st.known.pop(final, None)
                return None
            if "nofollow" not in page.directives and depth < self.max_depth:
                base = normalize_url(page.base, final) if page.base else None
                for link in page.links:
                    self._enqueue(st, link, depth + 1, base=base or final)
            if "noindex" in page.directives:
                self._count("noindex")
                st.known.pop(final, None)
                return None
        st.known.pop(final, None)
        st.fresh[final] = page.entry(depth)
        st.fetched += 1
        self._count("pages")
        return Change("upsert", SCOPE + final, self._file(final, page))

    # ── scan ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        st = _Scan(cursor if isinstance(cursor, dict) else {})
        self._ready, self._crawl_delay = {}, {}
        if not st.discovered:
            self._discover(st)
            st.discovered = True
        batch: list[Change] = []
        while st.queue:
            if self.max_pages and st.fetched >= self.max_pages:
                st.truncated = True
                break
            url, depth = st.queue.popleft()
            change = self._visit(st, url, depth)
            if change is not None:
                batch.append(change)
                st.emitted += 1
            if len(batch) >= self.batch_size:
                yield ScanBatch(batch, st.cursor())
                batch = []
        if (st.errors or st.forbidden) and st.fetched == 0 and st.emitted == 0:
            raise HttpError(
                f"web: nothing could be fetched ({st.errors} failures, {st.forbidden} refusals; last: {st.last_error[:200] or 'none'})"
            )
        authoritative = not st.resumed and not st.truncated and st.errors == 0 and st.forbidden == 0
        known = st.fresh if authoritative else {**st.known, **st.fresh}
        yield ScanBatch(batch, {"known": known}, done=True, complete_scope=SCOPE if authoritative else None)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        if not file.external_id.startswith(SCOPE):
            raise HttpError(f"web: {file.external_id!r} is not a page of a website connection")
        url = normalize_url(file.external_id[len(SCOPE):])
        if url is None or not self._allowed(url):
            raise HttpError("web: refusing to fetch a URL whose host is not allowed")

        def to_file(response: httpx.Response, _url: str) -> BinaryIO:
            tmp, total = tempfile.TemporaryFile(), 0
            try:
                for chunk in response.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise TooLarge(f"{_safe(url)} is larger than {self.max_bytes} bytes")
                    tmp.write(chunk)
                tmp.seek(0)
                return tmp
            except BaseException:
                tmp.close()
                raise

        try:
            resp = self._fetch(url, consume=to_file)
        except (_OffSite, _Blocked, _Duplicate):
            raise HttpError("web: the page redirects to a host that is not allowed") from None
        if resp.status in (404, 410):
            raise HttpError(f"web: {_safe(url)} is gone ({resp.status})", resp.status)
        return resp.value

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
