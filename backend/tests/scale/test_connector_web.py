"""Website crawler tests.

They run against a REAL HTTP server: the standard library's `http.server` on a local port in a thread, serving a small
site (robots.txt with Disallow and Crawl-delay, linked pages, a sitemap, a sitemap index, a gzipped sitemap, RSS and Atom
feeds, ETag and Last-Modified with real 304 replies, redirects, a PDF, a DOCX served as octet-stream, Basic / Bearer
authentication, 503 + Retry-After). Every request the server sees is logged, so "was never requested" is a real assertion.

Only time is faked: the connector's sleep and monotonic clock are injected so the politeness delay and Retry-After can be
asserted without waiting.
"""

import gzip
import itertools
import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.web import WebConnector, normalize_url

PASSWORD = "web-pw-MUST-NOT-LEAK-42"
TOKEN = "web-token-MUST-NOT-LEAK-77"
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 40 + b"\n%%EOF\n"
DOCX = b"PK\x03\x04" + bytes(range(256)) * 10
LM1, LM2 = "Wed, 01 Jan 2025 00:00:00 GMT", "Thu, 02 Jan 2025 00:00:00 GMT"


@dataclass
class Page:
    body: bytes = b""
    ctype: str = "text/html; charset=utf-8"
    etag: str | None = None
    last_modified: str | None = None
    status: int = 200
    location: str | None = None
    headers: dict = field(default_factory=dict)
    nolength: bool = False


class Site:
    def __init__(self):
        self.pages: dict[str, Page] = {}
        self.log: list[dict] = []
        self.fail: dict[str, list] = {}
        self.auth = None
        self.base = self.other = ""
        self.port = 0

    def add(self, path, body=b"", **kw):
        self.pages[path] = Page(body if isinstance(body, bytes) else body.encode("utf-8"), **kw)

    def html(self, path, title="Page", links=(), head="", text="", **kw):
        anchors = "".join(f'<a href="{href}">link</a> ' for href in links)
        self.add(path, f"<html><head><title>{title}</title>{head}</head><body><h1>{title}</h1><p>{text}</p>{anchors}</body></html>", **kw)

    def hits(self, path):
        return [e for e in self.log if e["path"].split("?")[0] == path]

    def paths(self):
        return [e["path"] for e in self.log]

    def authorized(self, headers):
        kind, *secret = self.auth
        got = headers.get("Authorization", "")
        if kind == "bearer":
            return got == f"Bearer {secret[0]}"
        import base64
        return got == "Basic " + base64.b64encode(f"{secret[0]}:{secret[1]}".encode()).decode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.server.site.log.append({"method": "HEAD", "path": self.path, "host": self.headers.get("Host"), "headers": {}, "status": 405})
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def reply(self, entry, status, headers, body, nolength=False):
        entry["status"] = status
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if nolength:
                self.send_header("Connection", "close")
                self.close_connection = True
            elif status != 304:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if status != 304 and body:
                for start in range(0, len(body), 8192):
                    self.wfile.write(body[start:start + 8192])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        site = self.server.site
        entry = {"method": "GET", "path": self.path, "host": self.headers.get("Host"),
                 "headers": {k.lower(): v for k, v in self.headers.items()}, "status": None}
        site.log.append(entry)
        base = self.path.split("?")[0]
        if site.auth and base != "/robots.txt" and not site.authorized(self.headers):
            return self.reply(entry, 401, {"WWW-Authenticate": 'Basic realm="site"'}, b"unauthorized")
        if site.fail.get(base):
            status, headers = site.fail[base].pop(0)
            return self.reply(entry, status, headers, b"busy")
        page = site.pages.get(self.path) or site.pages.get(base)
        if page is None:
            return self.reply(entry, 404, {"Content-Type": "text/plain"}, b"not found")
        if page.location:
            return self.reply(entry, page.status, {"Location": page.location}, b"")
        headers = {"Content-Type": page.ctype, **page.headers}
        if page.etag:
            headers["ETag"] = page.etag
        if page.last_modified:
            headers["Last-Modified"] = page.last_modified
        inm, ims = self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")
        if (page.etag and inm == page.etag) or (not page.etag and page.last_modified and ims == page.last_modified):
            return self.reply(entry, 304, {k: v for k, v in headers.items() if k in ("ETag", "Last-Modified")}, b"")
        self.reply(entry, page.status, headers, page.body, page.nolength)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self):
        return self.t


def build_default(site):
    h = site.html
    h("/", "Home", ["/about", "/docs/", "/docs/guide.html", "/private/secret.html", "/files/report.pdf", "/files/pic.png",
                    f"{site.other}/other-host", "mailto:someone@example.com", "/old", "#top", "/about?utm_source=news",
                    "/files/notes.docx", "/files/data.txt", "/blob", "javascript:void(0)"], etag='"home-v1"')
    h("/about", "About", ["/team"], etag='"about-v1"')
    h("/team", "Team", ["/deep/1"], etag='"team-v1"')
    h("/deep/1", "Deep 1", ["/deep/2"], etag='"d1"')
    h("/deep/2", "Deep 2", ["/deep/3"], etag='"d2"')
    h("/deep/3", "Deep 3", [], etag='"d3"')
    h("/docs/", "Docs", ["/docs/guide.html"], etag='"docs-v1"')
    h("/docs/guide.html", "Guide", [], text="the guide", last_modified=LM1)
    h("/private/secret.html", "Secret", [])
    h("/orphan.html", "Orphan", [], etag='"orphan"')
    h("/posts/1.html", "Post 1", [], etag='"p1"')
    h("/posts/2.html", "Post 2", [], etag='"p2"')
    h("/posts/3.html", "Post 3", [], etag='"p3"')
    site.add("/files/report.pdf", PDF, ctype="application/pdf", etag='"pdf-v1"')
    site.add("/files/notes.docx", DOCX, ctype="application/octet-stream", etag='"docx-v1"')
    site.add("/files/data.txt", "plain text body\n", ctype="text/plain; charset=utf-8")
    site.add("/files/pic.png", b"\x89PNG", ctype="image/png")
    site.add("/blob", b"PK", ctype="application/zip")
    site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n\nUser-agent: BadBot\nDisallow: /\n", ctype="text/plain")
    site.add("/old", location="/about", status=301)
    site.add("/away", location=f"{site.other}/about", status=302)
    site.add("/loop", location="/loop", status=302)
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    urlset = lambda *locs: f'<?xml version="1.0"?><urlset {ns}>' + "".join(f"<url><loc>{u}</loc></url>" for u in locs) + "</urlset>"   # noqa: E731
    site.add("/sitemap.xml", urlset(site.base + "/orphan.html", site.base + "/docs/guide.html", site.base + "/private/secret.html",
                                    site.other + "/elsewhere.html", "/about?utm_campaign=x"), ctype="application/xml")
    site.add("/sm-a.xml", urlset(site.base + "/orphan.html"), ctype="application/xml")
    site.add("/sm-b.xml.gz", gzip.compress(urlset(site.base + "/posts/1.html").encode()), ctype="application/gzip")
    site.add("/sitemap-index.xml", f'<?xml version="1.0"?><sitemapindex {ns}><sitemap><loc>{site.base}/sm-a.xml</loc></sitemap>'
                                   f"<sitemap><loc>{site.base}/sm-b.xml.gz</loc></sitemap><sitemap><loc>{site.other}/sm-c.xml</loc></sitemap></sitemapindex>",
             ctype="application/xml")
    site.add("/feed.xml", '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
                          f"<item><title>1</title><link>{site.base}/posts/1.html</link></item>"
                          f"<item><title>2</title><link>/posts/2.html</link></item></channel></rss>", ctype="application/rss+xml")
    site.add("/atom.xml", '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
                          f'<entry><title>3</title><link rel="self" href="{site.base}/atom-self"/><link rel="alternate" href="{site.base}/posts/3.html"/></entry></feed>',
             ctype="application/atom+xml")


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    s = Site()
    server = QuietServer(("127.0.0.1", 0), Handler)
    server.site = s
    s.port = server.server_address[1]
    s.base, s.other = f"http://127.0.0.1:{s.port}", f"http://localhost:{s.port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    build_default(s)
    yield s
    server.shutdown()
    server.server_close()
    thread.join(5)


def connector(site, clock=None, **cfg):
    clock = clock or Clock()
    config = {"seed_urls": [site.base + "/"], "delay_seconds": 0, "default_acl": ["*"], "max_retries": 2, **cfg}
    return WebConnector(config, sleep=clock.sleep, monotonic=clock.now), clock


def scan_all(c, cursor=None):
    batches = list(itertools.islice(c.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [x for b in batches for x in b.changes], batches


def urls(site, changes):
    return [x.external_id[len("web:") + len(site.base):] for x in changes]


def read(c, file):
    with c.open(file) as fh:
        return fh.read()


DEFAULT_PAGES = ["/", "/about", "/docs/", "/docs/guide.html", "/files/report.pdf", "/files/notes.docx", "/files/data.txt", "/team", "/deep/1"]


# ── configuration and URL hygiene ──


def test_configuration_is_validated():
    for cfg, match in [({}, "seed_urls"), ({"seed_urls": ["ftp://x/"]}, "http"), ({"seed_urls": ["http://u:p@h/"]}, "http"),
                       ({"seed_urls": ["http://a/"], "allowed_hosts": ["b"]}, "not in allowed_hosts")]:
        with pytest.raises(ValueError, match=match):
            WebConnector(cfg)
    WebConnector({"sitemap_urls": "http://a/sitemap.xml"})
    WebConnector({"feed_urls": ["http://a/feed"]})


@pytest.mark.parametrize("raw,base,expected", [
    ("HTTP://Example.COM:80/a/./b/../c?x=1#frag", None, "http://example.com/a/c?x=1"),
    ("https://example.com:443", None, "https://example.com/"),
    ("https://example.com:8443/p", None, "https://example.com:8443/p"),
    ("../up?utm_source=a&keep=1&gclid=z&FBCLID=3", "http://h/a/b/c", "http://h/a/up?keep=1"),
    ("/a b/é", "http://h/", "http://h/a%20b/%C3%A9"),
    ("//other.com/x", "https://h/y", "https://other.com/x"),
    ("/../../x", "http://h/", "http://h/x"),
    ("/a/", "http://h/", "http://h/a/"),
    ("mailto:a@b.c", None, None), ("javascript:void(0)", "http://h/", None), ("tel:123", "http://h/", None),
    ("ftp://h/x", None, None), ("http://user:pw@h/x", None, None), ("", "http://h/", "http://h/"), ("http://[::1]:99/", None, "http://[::1]:99/"),
])
def test_urls_are_normalised(raw, base, expected):
    assert normalize_url(raw, base, ["utm_source", "gclid", "fbclid"]) == expected


# ── crawling ──


def test_crawl_follows_same_host_links_and_makes_one_virtual_file_per_page(site):
    c, _ = connector(site)
    changes, batches = scan_all(c)
    assert urls(site, changes) == DEFAULT_PAGES and batches[-1].done and batches[-1].complete_scope == "web:"
    by = {urls(site, [x])[0]: x.file for x in changes}
    host = f"127.0.0.1-{site.port}"
    assert by["/"].path == f"web/{host}/index.html" and by["/about"].path == f"web/{host}/about.html"
    assert by["/docs/"].path == f"web/{host}/docs/index.html" and by["/docs/guide.html"].path == f"web/{host}/docs/guide.html"
    assert by["/files/report.pdf"].path == f"web/{host}/files/report.pdf" and by["/files/notes.docx"].path == f"web/{host}/files/notes.docx"
    assert by["/files/data.txt"].path == f"web/{host}/files/data.txt"
    assert (by["/"].mime, by["/files/report.pdf"].mime, by["/files/notes.docx"].mime) == ("text/html", "application/pdf", "application/octet-stream")
    assert by["/"].external_id == f"web:{site.base}/" and by["/"].name == "index.html" and by["/"].acl == ["*"]
    asked = site.paths()
    assert "/files/pic.png" not in asked and "/private/secret.html" not in asked          # binary extension; robots.txt
    assert not [e for e in site.log if e["host"] != f"127.0.0.1:{site.port}"]            # the other host was never contacted
    assert [e for e in site.log if e["method"] != "GET"] == []                           # no HEAD, no POST
    assert asked.count("/about") == 1 and "/old" in asked                                # /old redirects to a page this scan already has
    assert "/about?utm_source=news" not in asked and "/deep/2" not in asked               # tracking parameter stripped; depth limit


def test_html_is_kept_as_served_and_other_files_pass_through_with_their_extension(site):
    c, _ = connector(site)
    by = {urls(site, [x])[0]: x.file for x in scan_all(c)[0]}
    assert read(c, by["/files/report.pdf"]) == PDF and read(c, by["/files/notes.docx"]) == DOCX
    assert read(c, by["/files/data.txt"]) == b"plain text body\n"
    assert read(c, by["/docs/guide.html"]) == site.pages["/docs/guide.html"].body
    assert read(c, by["/"]).startswith(b"<html><head><title>Home</title>")
    assert by["/files/report.pdf"].size == len(PDF) and by["/docs/guide.html"].size == len(site.pages["/docs/guide.html"].body)
    assert c.stats["skipped_type"] == 1                                                  # /blob is application/zip: requested, then dropped


def test_robots_txt_is_fetched_once_with_our_user_agent_and_disallowed_paths_are_never_requested(site):
    c, _ = connector(site, user_agent="RagEngineBot/2.1 (+https://example.com/bot)")
    scan_all(c)
    assert len(site.hits("/robots.txt")) == 1 and "/private/secret.html" not in site.paths()
    assert {e["headers"]["user-agent"] for e in site.log} == {"RagEngineBot/2.1 (+https://example.com/bot)"}
    assert c.stats["robots_blocked"] == 1
    site.log.clear()
    off, _ = connector(site, respect_robots=False)
    changes, _ = scan_all(off)
    assert "/private/secret.html" in urls(site, changes) and site.hits("/robots.txt") == []


def test_rules_for_our_own_product_token_apply_and_a_site_that_forbids_us_yields_nothing_authoritatively(site):
    c, _ = connector(site, user_agent="BadBot/1.0 (+https://x)")
    changes, batches = scan_all(c)
    assert changes == [] and site.paths() == ["/robots.txt"] and batches[-1].complete_scope == "web:"


def test_missing_robots_txt_allows_everything_and_an_unreachable_one_forbids_everything(site):
    del site.pages["/robots.txt"]
    c, _ = connector(site)
    assert "/private/secret.html" in urls(site, scan_all(c)[0])
    site.pages["/robots.txt"] = Page(b"Disallow: /", ctype="text/plain")
    site.fail["/robots.txt"] = [(503, {})] * 5
    c2, _ = connector(site)
    site.log.clear()
    with pytest.raises(HttpError, match="nothing could be fetched"):
        scan_all(c2)
    assert site.paths() == ["/robots.txt"] * 3                                            # retried (max_retries=2), then no page was touched


def test_robots_txt_answering_403_is_not_a_licence_to_delete_everything(site):
    site.fail["/robots.txt"] = [(403, {})]
    c, _ = connector(site)
    with pytest.raises(HttpError, match="nothing could be fetched"):
        scan_all(c)
    assert site.paths() == ["/robots.txt"]


def test_crawl_delay_and_delay_seconds_space_the_requests_of_a_host(site):
    site.add("/robots.txt", "User-agent: *\nCrawl-delay: 2\nDisallow: /private/\n", ctype="text/plain")
    c, clock = connector(site, delay_seconds=0.5)
    changes, _ = scan_all(c)
    assert len(changes) == len(DEFAULT_PAGES)
    assert clock.sleeps[0] == 0.5 and set(clock.sleeps[1:]) == {2.0} and len(clock.sleeps) == len(site.log) - 1
    capped, clock2 = connector(site, delay_seconds=0, max_crawl_delay=1)
    scan_all(capped)
    assert set(clock2.sleeps[1:]) == {1.0}
    quiet, clock3 = connector(site, respect_robots=False, delay_seconds=0)
    scan_all(quiet)
    assert clock3.sleeps == []


def test_depth_limit(site):
    for depth, expected in ((0, ["/"]), (1, ["/", "/about", "/docs/", "/docs/guide.html", "/files/report.pdf", "/files/notes.docx", "/files/data.txt"]),
                            (2, DEFAULT_PAGES[:7] + ["/team"]), (9, DEFAULT_PAGES + ["/deep/2", "/deep/3"])):
        c, _ = connector(site, max_depth=depth)
        assert urls(site, scan_all(c)[0]) == expected, depth


def test_max_pages_truncates_and_a_truncated_scan_never_deletes(site):
    c, _ = connector(site, max_pages=4)
    changes, batches = scan_all(c)
    assert urls(site, changes) == DEFAULT_PAGES[:4] and batches[-1].complete_scope is None
    exact, eb = scan_all(connector(site, max_pages=len(DEFAULT_PAGES))[0])
    assert len(exact) == len(DEFAULT_PAGES) and eb[-1].complete_scope == "web:"           # exactly the cap is not "more than"


# ── sitemaps and feeds ──


def test_sitemap_pages_are_found_without_any_link_to_them(site):
    c, _ = connector(site, seed_urls=[], sitemap_urls=[site.base + "/sitemap.xml"], max_depth=0)
    changes, batches = scan_all(c)
    assert urls(site, changes) == ["/orphan.html", "/docs/guide.html", "/about"]          # robots drops /private; the other host is ignored
    assert batches[-1].complete_scope == "web:" and "/elsewhere.html" not in site.paths() and "/private/secret.html" not in site.paths()
    assert read(c, changes[0].file) == site.pages["/orphan.html"].body


def test_sitemap_index_is_followed_including_gzip_and_other_hosts_are_not(site):
    c, _ = connector(site, seed_urls=[], sitemap_urls=[site.base + "/sitemap-index.xml"], max_depth=0)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/orphan.html", "/posts/1.html"] and "/sm-c.xml" not in site.paths()


def test_rss_and_atom_items_become_pages(site):
    c, _ = connector(site, seed_urls=[], feed_urls=[site.base + "/feed.xml", site.base + "/atom.xml"], max_depth=0)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/posts/1.html", "/posts/2.html", "/posts/3.html"] and "/atom-self" not in site.paths()


def test_a_sitemap_that_cannot_be_read_is_counted_and_blocks_deletion_but_the_crawl_goes_on(site):
    c, _ = connector(site, sitemap_urls=[site.base + "/missing-sitemap.xml"])
    changes, batches = scan_all(c)
    assert urls(site, changes) == DEFAULT_PAGES and batches[-1].complete_scope is None and c.stats["listing_errors"] == 1
    site.add("/evil.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset><url><loc>/orphan.html</loc></url></urlset>')
    site.add("/notxml.xml", "<html>nope</html>")
    for bad in ("/evil.xml", "/notxml.xml"):
        c2, _ = connector(site, seed_urls=[], sitemap_urls=[site.base + "/sitemap.xml", site.base + bad], max_depth=0)
        changes, batches = scan_all(c2)
        assert "/orphan.html" in urls(site, changes) and batches[-1].complete_scope is None and c2.stats["listing_errors"] == 1


def test_an_oversized_sitemap_is_refused(site):
    site.add("/big.xml", b'<?xml version="1.0"?><urlset>' + b"<url><loc>/x</loc></url>" * 5000 + b"</urlset>")
    site.add("/bomb.xml.gz", gzip.compress(b"<urlset>" + b" " * 2_000_000 + b"</urlset>"), ctype="application/gzip")
    for name in ("/big.xml", "/bomb.xml.gz"):
        c, _ = connector(site, seed_urls=[], sitemap_urls=[site.base + name, site.base + "/sitemap.xml"], max_xml_bytes=50_000, max_depth=0)
        changes, batches = scan_all(c)
        assert c.stats["listing_errors"] == 1 and "/orphan.html" in urls(site, changes) and batches[-1].complete_scope is None


# ── versions, conditional requests, deletions ──


def test_version_is_the_etag_when_there_is_one_and_a_body_hash_when_there_is_not(site):
    c, _ = connector(site)
    first = {urls(site, [x])[0]: x.file.version for x in scan_all(c)[0]}
    assert first["/"] == "etag:home-v1" and first["/files/report.pdf"] == "etag:pdf-v1" and first["/docs/guide.html"].startswith("sha256:")
    assert first == {urls(site, [x])[0]: x.file.version for x in scan_all(c)[0]}
    site.pages["/"].etag = '"home-v2"'                                                   # etag changes
    site.pages["/docs/guide.html"].body = b"<html><body>the guide, edited</body></html>"      # no etag: the body hash changes
    site.pages["/files/data.txt"].body = b"plain text body\n"                              # same bytes: same hash
    second = {urls(site, [x])[0]: x.file.version for x in scan_all(c)[0]}
    assert {k for k in second if second[k] != first[k]} == {"/", "/docs/guide.html"}
    assert second["/"] == "etag:home-v2" and second["/docs/guide.html"] != first["/docs/guide.html"]


def test_weak_etags_are_normalised_for_the_version_but_replayed_as_sent(site):
    site.pages["/about"].etag = 'W/"abc"'
    c, _ = connector(site, max_depth=1)
    changes, batches = scan_all(c)
    assert next(x.file.version for x in changes if x.external_id.endswith("/about")) == "etag:abc"
    scan_all(c, batches[-1].cursor)
    assert site.hits("/about")[-1]["headers"]["if-none-match"] == 'W/"abc"' and site.hits("/about")[-1]["status"] == 304


def test_the_next_scan_asks_with_if_none_match_and_if_modified_since_and_a_304_keeps_the_page(site):
    c, _ = connector(site)
    first, batches = scan_all(c)
    cursor = json.loads(json.dumps(batches[-1].cursor))
    assert set(cursor) == {"known"} and len(cursor["known"]) == len(DEFAULT_PAGES)
    site.log.clear()
    second, sb = scan_all(c, cursor)
    sent = {e["path"]: e["headers"] for e in site.log if e["path"] != "/robots.txt"}
    assert sent["/"]["if-none-match"] == '"home-v1"' and sent["/docs/guide.html"]["if-modified-since"] == LM1
    assert "if-none-match" not in sent["/files/data.txt"] and "if-modified-since" not in sent["/files/data.txt"]    # it had no validator
    statuses = {e["path"]: e["status"] for e in site.log}
    assert statuses["/"] == 304 and statuses["/docs/guide.html"] == 304 and statuses["/files/report.pdf"] == 304 and statuses["/files/data.txt"] == 200
    assert sorted(urls(site, second)) == sorted(urls(site, first)) and sb[-1].complete_scope == "web:"
    assert {x.external_id: (x.file.version, x.file.path, x.file.size, x.file.mime) for x in second} == \
           {x.external_id: (x.file.version, x.file.path, x.file.size, x.file.mime) for x in first}
    assert c.stats["not_modified"] >= 8
    assert next(x.file for x in second if x.external_id.endswith("/docs/guide.html")).modified_at is not None


def test_a_changed_page_comes_back_as_200_with_a_new_version_when_the_validators_do_not_match(site):
    c, _ = connector(site)
    first, batches = scan_all(c)
    site.pages["/about"].etag = '"about-v2"'
    site.pages["/about"].body = b"<html><body>new about <a href='/new-child'>c</a></body></html>"
    site.html("/new-child", "Child", [], etag='"child"')
    site.pages["/docs/guide.html"].last_modified = LM2
    site.pages["/docs/guide.html"].body = b"<html><body>guide v2</body></html>"
    site.log.clear()
    second, sb = scan_all(c, batches[-1].cursor)
    status = {e["path"]: e["status"] for e in site.log}
    assert status["/about"] == 200 and status["/docs/guide.html"] == 200 and status["/"] == 304
    old, new = ({x.external_id: x.file.version for x in s} for s in (first, second))
    assert new[f"web:{site.base}/about"] == "etag:about-v2" and new[f"web:{site.base}/docs/guide.html"] != old[f"web:{site.base}/docs/guide.html"]
    assert f"web:{site.base}/new-child" in new and new[f"web:{site.base}/"] == old[f"web:{site.base}/"]       # links of a changed page are followed


def test_pages_that_vanish_are_missing_from_an_authoritative_scan_and_reported_as_deletes_when_known(site):
    c, _ = connector(site)
    first, batches = scan_all(c)
    del site.pages["/docs/guide.html"]
    plain, pb = scan_all(c)                                                              # no cursor: the sync layer deletes what is unseen
    assert f"{site.base}/docs/guide.html" not in [x.external_id[4:] for x in plain] and pb[-1].complete_scope == "web:"
    again, ab = scan_all(c, batches[-1].cursor)                                          # with the cursor: an explicit delete
    deleted = [x for x in again if x.kind == "delete"]
    assert [x.external_id for x in deleted] == [f"web:{site.base}/docs/guide.html"] and deleted[0].file is None
    assert not any(u.endswith("/docs/guide.html") for u in ab[-1].cursor["known"])
    site.pages["/files/report.pdf"] = Page(b"gone", status=410)
    gone, _ = scan_all(c)
    assert not any(x.external_id.endswith("report.pdf") for x in gone)


def test_a_failing_server_never_causes_deletions(site):
    c, _ = connector(site)
    scan_all(c)
    site.fail["/about"] = [(500, {})] * 9
    changes, batches = scan_all(c)
    assert "/about" not in urls(site, changes) and batches[-1].complete_scope is None and c.stats["errors"] == 1
    site.fail["/about"] = []
    site.pages["/team"] = Page(b"x", status=403)
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope is None and c.stats["forbidden"] == 1


def test_resume_from_every_batch_boundary_is_exact_and_never_authoritative(site):
    c, _ = connector(site, batch_size=2)
    everything, batches = scan_all(c)
    assert urls(site, everything) == DEFAULT_PAGES and len(batches) >= 4
    for k in range(len(batches) - 1):
        cursor = json.loads(json.dumps(batches[k].cursor))
        head = [u for b in batches[: k + 1] for u in urls(site, b.changes)]
        fresh, _ = connector(site, batch_size=2)
        resumed, rb = scan_all(fresh, cursor)
        assert head + urls(site, resumed) == DEFAULT_PAGES, f"resume after batch {k}"
        assert rb[-1].complete_scope is None
    assert PASSWORD not in json.dumps([b.cursor for b in batches])


# ── redirects ──


def test_same_host_redirects_are_followed_and_never_produce_a_duplicate(site):
    site.html("/start", "Start", ["/old", "/about"], etag='"s"')
    c, _ = connector(site, seed_urls=[site.base + "/start", site.base + "/old"], max_depth=1)
    changes, _ = scan_all(c)
    assert urls(site, changes).count("/about") == 1 and "/old" not in urls(site, changes)       # identity is the final URL
    assert c.stats["redirect_duplicates"] >= 1


def test_a_redirect_to_another_host_is_refused_and_that_host_is_never_contacted(site):
    c, _ = connector(site, seed_urls=[site.base + "/away", site.base + "/about"], max_depth=0)
    changes, batches = scan_all(c)
    assert urls(site, changes) == ["/about"] and c.stats["offsite_redirects"] == 1 and batches[-1].complete_scope == "web:"
    assert [e for e in site.log if e["host"].startswith("localhost")] == []
    allowed, _ = connector(site, seed_urls=[site.base + "/away"], max_depth=0, allowed_hosts=[f"127.0.0.1:{site.port}", f"localhost:{site.port}"])
    changes, _ = scan_all(allowed)
    assert [x.external_id for x in changes] == [f"web:{site.other}/about"] and changes[0].file.path == f"web/localhost-{site.port}/about.html"
    assert len(site.hits("/robots.txt")) == 3                                              # 1 (first scan) + 2 hosts (second scan)


def test_redirect_loops_long_chains_and_redirects_into_robots_disallowed_paths_are_contained(site):
    site.add("/into-private", location="/private/secret.html", status=302)
    for i in range(1, 9):
        site.add(f"/c{i}", location=f"/c{i + 1}", status=302)
    c, _ = connector(site, seed_urls=[site.base + p for p in ("/loop", "/into-private", "/c1", "/about")], max_redirects=5, max_depth=1)
    changes, batches = scan_all(c)
    assert urls(site, changes) == ["/about", "/team"]
    assert c.stats["redirect_duplicates"] == 1 and c.stats["robots_blocked"] == 1 and c.stats["errors"] == 1     # loop; into /private; chain of 8 > 5
    asked = site.paths()
    assert "/private/secret.html" not in asked and asked.count("/c6") == 1 and "/c7" not in asked      # 5 redirects followed, the 6th refused
    assert batches[-1].complete_scope is None and len(site.hits("/loop")) == 1


# ── robots directives in the page ──


def test_meta_robots_and_x_robots_tag(site):
    site.html("/noindex", "Hidden", ["/posts/1.html"], head='<meta name="robots" content="noindex, follow">')
    site.html("/nofollow", "Leaf", ["/posts/2.html"], head='<meta name="RagEngineBot" content="nofollow">')
    site.html("/none", "None", ["/posts/3.html"], head='<meta name="robots" content="none">')
    site.html("/header", "Header", ["/orphan.html"], headers={"X-Robots-Tag": "noindex"})
    site.html("/other-agent", "Other", [], headers={"X-Robots-Tag": "googlebot: noindex"})
    site.add("/linked", '<a href="/posts/2.html" rel="nofollow">x</a><a href="/posts/3.html" rel="noopener NoFollow">y</a><a href="/orphan.html">z</a>')
    seeds = [site.base + p for p in ("/noindex", "/nofollow", "/none", "/header", "/other-agent", "/linked")]
    c, _ = connector(site, seed_urls=seeds)
    got = urls(site, scan_all(c)[0])
    # indexed: the page that only forbids following, a rule meant for another robot, a page with nofollow links; followed: noindex pages' links
    assert got == ["/nofollow", "/other-agent", "/linked", "/posts/1.html", "/orphan.html"] and c.stats["noindex"] == 3
    assert "/posts/2.html" not in site.paths() and "/posts/3.html" not in site.paths()
    ignoring, _ = connector(site, respect_robots=False, seed_urls=seeds)
    assert {"/noindex", "/none", "/header", "/posts/2.html", "/posts/3.html"} <= set(urls(site, scan_all(ignoring)[0]))


# ── size caps ──


def test_size_caps_by_content_length_and_by_streamed_size(site):
    site.add("/big.txt", b"b" * 5000, ctype="text/plain")
    site.add("/stream.txt", b"s" * 5000, ctype="text/plain", nolength=True)                 # no Content-Length: counted while streaming
    site.add("/small.txt", b"s" * 100, ctype="text/plain")
    site.add("/big-etag.pdf", b"%PDF" + b"x" * 5000, ctype="application/pdf", etag='"be"')   # an ETag file is never read: only the header can tell
    site.html("/idx", "Idx", ["/big.txt", "/stream.txt", "/small.txt", "/big-etag.pdf"])
    c, _ = connector(site, seed_urls=[site.base + "/idx"], max_file_bytes=1000)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/idx", "/small.txt"] and c.stats["oversize"] == 3
    big = SourceFile(f"web:{site.base}/big.txt", "p", "big.txt", 5000, None, "v", [])
    with pytest.raises(TooLarge):
        c.open(big)
    streamed = SourceFile(f"web:{site.base}/stream.txt", "p", "stream.txt", 5000, None, "v", [])
    with pytest.raises(TooLarge):
        c.open(streamed)
    assert read(connector(site, max_file_bytes=10_000)[0], big) == b"b" * 5000


# ── permissions ──


def test_acl_rules_follow_the_virtual_folders_and_everything_else_is_unreadable(site):
    host = f"web/127.0.0.1-{site.port}"
    rules = [{"prefix": f"{host}/docs", "principals": ["group:Docs"]}, {"prefix": f"{host}/files", "principals": ["group:Staff", "user:Ann"]},
             {"prefix": f"{host}/docs/guide.html", "principals": ["*"]}]
    c, _ = connector(site, default_acl=None, acl_rules=rules)
    by = {urls(site, [x])[0]: x.file for x in scan_all(c)[0]}
    assert by["/docs/"].acl == ["group:docs"] and by["/docs/guide.html"].acl == ["*"] and by["/files/report.pdf"].acl == ["group:staff", "user:ann"]
    assert by["/"].acl == [] and by["/about"].acl == [] and by["/team"].acl == []
    assert c.refresh_acl(by["/docs/"]) == ["group:docs"] and c.refresh_acl(by["/about"]) == []
    nothing, _ = connector(site, default_acl=None)
    assert all(x.file.acl == [] for x in scan_all(nothing)[0])


# ── authentication, secrets ──


def test_basic_authentication_reads_the_password_at_request_time_and_never_leaks_it(site, monkeypatch):
    site.auth = ("basic", "alice", PASSWORD)
    monkeypatch.setenv("T_WEB_PW", PASSWORD)
    c, _ = connector(site, username="alice", password_env="T_WEB_PW")
    changes, batches = scan_all(c)
    assert urls(site, changes) == DEFAULT_PAGES
    assert PASSWORD not in json.dumps([b.cursor for b in batches]) + repr(c.stats) + repr([x.file for x in changes])
    assert read(c, changes[1].file) == site.pages["/about"].body
    monkeypatch.setenv("T_WEB_PW", "WRONG-" + PASSWORD)                                    # rotated / wrong: picked up without a new connector
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert "401" in blob and PASSWORD not in blob and "WRONG-" not in blob
    monkeypatch.delenv("T_WEB_PW")
    with pytest.raises(HttpError, match="T_WEB_PW"):
        scan_all(c)


def test_bearer_token_and_no_credentials(site, monkeypatch):
    site.auth = ("bearer", TOKEN)
    monkeypatch.setenv("T_WEB_TOKEN", TOKEN)
    c, _ = connector(site, token_env="T_WEB_TOKEN")
    assert urls(site, scan_all(c)[0]) == DEFAULT_PAGES
    anonymous, _ = connector(site)                                                          # without credentials every page is refused
    with pytest.raises(HttpError, match="nothing could be fetched"):
        scan_all(anonymous)
    assert anonymous.stats["forbidden"] >= 1


def test_credentials_are_only_ever_sent_to_allowed_hosts(site, monkeypatch):
    site.auth = ("basic", "alice", PASSWORD)
    monkeypatch.setenv("T_WEB_PW", PASSWORD)
    c, _ = connector(site, username="alice", password_env="T_WEB_PW")
    scan_all(c)
    assert {e["host"] for e in site.log} == {f"127.0.0.1:{site.port}"}


# ── open() ──


def test_open_streams_the_exact_bytes_and_refuses_what_it_should(site):
    c, _ = connector(site)
    f = SourceFile(f"web:{site.base}/files/report.pdf", "p", "report.pdf", 1, None, "v", [])
    assert read(c, f) == PDF
    for bad in (f"web:{site.other}/about", "web:http://169.254.169.254/latest/meta-data", "web:ftp://x/y", "web:not a url", "/files/report.pdf"):
        with pytest.raises(HttpError):
            c.open(SourceFile(bad, "p", "n", 1, None, "v", []))
    assert [e for e in site.log if e["host"].startswith("localhost")] == []
    with pytest.raises(HttpError) as err:
        c.open(SourceFile(f"web:{site.base}/no-such-page", "p", "n", 1, None, "v", []))
    assert err.value.status == 404
    via_redirect = SourceFile(f"web:{site.base}/old", "p", "n", 1, None, "v", [])
    assert read(c, via_redirect) == site.pages["/about"].body
    with pytest.raises(HttpError, match="not allowed"):
        c.open(SourceFile(f"web:{site.base}/away", "p", "n", 1, None, "v", []))


# ── rate limits and failures ──


def test_503_with_retry_after_and_429_are_waited_for_then_retried(site):
    site.fail["/about"] = [(503, {"Retry-After": "7"}), (429, {"Retry-After": "3"})]
    c, clock = connector(site, max_retries=3)
    changes, batches = scan_all(c)
    assert "/about" in urls(site, changes) and clock.sleeps == [7.0, 3.0] and batches[-1].complete_scope == "web:"
    site.fail["/about"] = [(503, {"Retry-After": "9999"})]
    c2, clock2 = connector(site, max_retries=3, max_wait=20)
    scan_all(c2)
    assert clock2.sleeps == [20.0]                                                         # capped at max_wait
    site.fail["/about"] = [(503, {})] * 9
    c3, clock3 = connector(site, max_retries=2)
    changes, batches = scan_all(c3)
    assert "/about" not in urls(site, changes) and clock3.sleeps == [1.0, 2.0] and batches[-1].complete_scope is None and c3.stats["errors"] == 1


def test_nothing_reachable_at_all_is_an_error_not_an_empty_success():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    clock = Clock()
    c = WebConnector({"seed_urls": [f"http://127.0.0.1:{port}/"], "delay_seconds": 0, "max_retries": 1}, sleep=clock.sleep, monotonic=clock.now)
    with pytest.raises(HttpError, match="nothing could be fetched"):
        scan_all(c)
    assert clock.sleeps == [1]                                                             # robots.txt retried once; with it unreachable no page is tried


# ── link hygiene ──


def test_allowed_hosts_decide_where_links_may_lead(site):
    site.html("/hub", "Hub", [f"{site.other}/about", "/orphan.html"], etag='"h"')
    c, _ = connector(site, seed_urls=[site.base + "/hub"], max_depth=1)
    assert urls(site, scan_all(c)[0]) == ["/hub", "/orphan.html"] and c.stats["offsite_links"] == 1
    both, _ = connector(site, seed_urls=[site.base + "/hub"], max_depth=1, allowed_hosts=[f"127.0.0.1:{site.port}", f"localhost:{site.port}"])
    got = [x.external_id for x in scan_all(both)[0]]
    assert f"web:{site.other}/about" in got and f"web:{site.base}/orphan.html" in got


def test_include_and_exclude_patterns_and_tracking_parameters(site):
    c, _ = connector(site, exclude_patterns=[r"/files/", r"/deep/"], include_patterns=[r"/(about|docs|team|$)"])
    assert urls(site, scan_all(c)[0]) == ["/", "/about", "/docs/", "/docs/guide.html", "/team"]
    site.html("/q", "Q", ["/orphan.html?utm_source=a&utm_medium=b", "/orphan.html?utm_source=c", "/p?page=1", "/p?page=2"])
    site.html("/p", "P", [])
    qc, _ = connector(site, seed_urls=[site.base + "/q"], max_depth=1)
    changes, _ = scan_all(qc)
    assert urls(site, changes) == ["/q", "/orphan.html", "/p?page=1", "/p?page=2"] and "/orphan.html?utm_source=a&utm_medium=b" not in site.paths()
    paths = [x.file.path for x in changes[2:]]
    assert len(set(paths)) == 2 and all(p.startswith(f"web/127.0.0.1-{site.port}/p~") and p.endswith(".html") for p in paths)


def test_pages_in_other_charsets_still_yield_their_links_and_the_bytes_are_kept_as_served(site):
    body = '<html><head><meta charset="iso-8859-1"></head><body><a href="/café">c</a></body></html>'.encode("iso-8859-1")
    site.add("/latin", body, ctype="text/html; charset=iso-8859-1")
    site.html("/caf%C3%A9", "Cafe", [], etag='"c"')
    c, _ = connector(site, seed_urls=[site.base + "/latin"], max_depth=1)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/latin", "/caf%C3%A9"] and read(c, changes[0].file) == body
    assert changes[1].file.path.endswith("/café.html")


def test_a_non_html_file_with_an_etag_is_not_downloaded_during_the_scan(site):
    site.add("/files/etag.pdf", b"%PDF" + b"e" * 5000, ctype="application/pdf", etag='"e1"', nolength=True)
    site.add("/files/plain.pdf", b"%PDF" + b"p" * 5000, ctype="application/pdf", nolength=True)
    site.html("/idx", "Idx", ["/files/etag.pdf", "/files/plain.pdf"])
    c, _ = connector(site, seed_urls=[site.base + "/idx"], max_depth=1, max_file_bytes=1000)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/idx", "/files/etag.pdf"]            # the ETag is the version: no need to read the body (so no size check);
    assert changes[1].file.version == "etag:e1" and c.stats["oversize"] == 1    # without an ETag the body has to be hashed, and is too big
    with pytest.raises(TooLarge):                                         # ... but open() still enforces the cap
        c.open(changes[1].file)


def test_gzip_encoded_pages_are_decoded_for_links_and_open_returns_the_decoded_bytes(site):
    page = b'<html><body><a href="/orphan.html">o</a></body></html>'
    site.add("/zipped", gzip.compress(page), headers={"Content-Encoding": "gzip"})
    c, _ = connector(site, seed_urls=[site.base + "/zipped"], max_depth=1)
    changes, _ = scan_all(c)
    assert urls(site, changes) == ["/zipped", "/orphan.html"] and changes[0].file.size == len(page) and read(c, changes[0].file) == page


def test_a_damaged_cursor_means_a_fresh_scan_and_validators_of_unvisited_pages_survive_a_truncated_scan(site):
    c, _ = connector(site)
    for junk in ({"queue": "junk"}, {"queue": [["x"]], "seen": []}, {"queue": [], "seen": [], "known": "nope"}, {"known": {"u": [1]}}):
        changes, batches = scan_all(c, junk)
        assert len(changes) == len(DEFAULT_PAGES) and batches[-1].complete_scope == "web:"
    _, full = scan_all(c)
    known = full[-1].cursor["known"]
    short, sb = scan_all(connector(site, max_pages=2)[0], full[-1].cursor)
    assert len(short) == 2 and sb[-1].complete_scope is None and set(sb[-1].cursor["known"]) == set(known)    # nothing forgotten
