"""Shared helpers for connectors whose sources hold OBJECTS (wiki pages, issues, chat messages, records)
instead of files. Each object is rendered to a Markdown "virtual file" (see `_http.render_document`).

What lives here, and why it is shared rather than copied into a dozen connectors:

  * `html_to_text`    HTML / Confluence storage format -> readable Markdown-ish text (stdlib `html.parser`).
  * `ScanState`       the one cursor protocol: resumable mid-scan, incremental after a finished scan, and a
                      periodic full resync (the only way to notice deletions in APIs without a delete feed).
  * `Budget`          `max_items` / `max_pages`: bounded work; a truncated scan is never authoritative.
  * `ThrottleAs429`   transport wrapper for APIs (GitHub, Google) that throttle with HTTP 403, which
                      `_http.Http` would otherwise treat as "bad credentials" and never retry.
  * small parsers     `parse_dt`, `iso_z`, `parse_link_next`, `short_hash`, `basic_auth`.

Cursor protocol (so a crashed scan resumes and a finished scan is incremental):

    mid-scan   {"mode": "full"|"inc", "since": <watermark or null>, "high": <max timestamp seen>,
                "last_full": <epoch or null>, "pos": {connector specific resume position}}
    finished   {"since": <watermark for the next scan>, "last_full": <epoch of the last full scan>}

A cursor with `mode` is a resumed scan; a cursor with only `since` starts an incremental scan unless the last
full scan is older than `full_resync_hours`. An empty cursor is a full scan. A full scan that ran from the
beginning to the end without hitting a limit is authoritative (`complete_scope` set by the connector); a
resumed, incremental or truncated one never is, so it can never delete anything it did not look at.
No secret is ever stored in a cursor.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

import httpx

from app.scale.connectors._http import HttpError

# ───────────────────────────── small parsers ─────────────────────────────

_FRACTION = re.compile(r"\.(\d+)")
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_dt(value) -> datetime | None:
    """ISO-8601 (Z / +0000 / +00:00 / fractional seconds of any length) or epoch seconds -> aware UTC datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    text = text.replace("Z", "+00:00").replace("z", "+00:00")
    text = _OFFSET_NO_COLON.sub(r"\1:\2", text)
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redacted(exc: HttpError, *secrets: str | None, hint: str = "") -> HttpError:
    """The same kind of error with every secret value scrubbed out of the message (belt and braces: a server
    that echoed a credential back must not get it into a log line or `connector_state.last_error`)."""
    message = str(exc)
    for secret in secrets:
        if secret and len(secret) >= 4:
            message = message.replace(secret, "***")
    return type(exc)(message + (f" ({hint})" if hint else ""), getattr(exc, "status", None))


def short_hash(*parts: object, size: int = 12) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part if isinstance(part, bytes) else str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:size]


def basic_auth(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")


_LINK = re.compile(r"<([^>]*)>([^<]*)")
_REL_NEXT = re.compile(r'rel\s*=\s*"(?:[^"]*\s)?next(?:\s[^"]*)?"|rel\s*=\s*next\b')


def parse_link_next(header: str | None) -> str | None:
    """The `rel="next"` target of an RFC 8288 `Link` header (GitHub, GitLab), or None."""
    for target, params in _LINK.findall(header or ""):
        if _REL_NEXT.search(params):
            return target
    return None


# ───────────────────────────── bounded work ─────────────────────────────


class Budget:
    """`max_items` / `max_pages` for one scan. Once either is hit the scan stops and `truncated` is set."""

    def __init__(self, max_items: int | None = None, max_pages: int | None = None) -> None:
        self.max_items = int(max_items) if max_items else None
        self.max_pages = int(max_pages) if max_pages else None
        self.items = 0
        self.pages = 0
        self.truncated = False
        self._seen: set = set()

    @classmethod
    def from_config(cls, config: dict) -> "Budget":
        return cls(config.get("max_items"), config.get("max_pages"))

    def allow_page(self, key: object = None) -> bool:
        """Call before requesting a page that is known to exist. False => stop; the scan is truncated.
        `key` (the page's cursor / token / URL) guards against a server that keeps returning the same next page."""
        if key is not None:
            if key in self._seen:
                raise HttpError("pagination did not advance: the server returned the same next page twice")
            self._seen.add(key)
        if (self.max_items is not None and self.items >= self.max_items) or (
            self.max_pages is not None and self.pages >= self.max_pages
        ):
            self.truncated = True
            return False
        self.pages += 1
        return True

    def stop_if_exhausted(self) -> bool:
        """Call before starting the next unit of work (a repository, a channel...). True, and the scan is marked
        truncated, when the allowance is used up while there is still work to do: nothing is requested for it."""
        if not self.truncated and (
            (self.max_items is not None and self.items >= self.max_items)
            or (self.max_pages is not None and self.pages >= self.max_pages)
        ):
            self.truncated = True
        return self.truncated

    def clip(self, rows: list) -> list:
        """Trim a page's rows to the remaining item allowance (truncating the scan if anything was cut)."""
        if self.max_items is None:
            self.items += len(rows)
            return rows
        room = max(self.max_items - self.items, 0)
        if len(rows) > room:
            self.truncated = True
            rows = rows[:room]
        self.items += len(rows)
        return rows


# ───────────────────────────── cursor protocol ─────────────────────────────


class ScanState:
    """Decides full vs incremental vs resumed, tracks the watermark, and builds every cursor."""

    def __init__(self, cursor: dict, *, full_resync_hours: float = 24.0, clock: Callable[[], float] = time.time) -> None:
        cursor = cursor or {}
        self.clock = clock
        self.started = clock()
        self.resumed = "mode" in cursor
        self.high: datetime | None = parse_dt(cursor.get("high"))
        self.last_full: float | None = cursor.get("last_full")
        self.pos: dict = dict(cursor.get("pos") or {})
        if self.resumed:
            self.mode = cursor["mode"]
            self.since: str | None = cursor.get("since")
        else:
            since = cursor.get("since")
            full_due = (
                not since
                or self.last_full is None
                or (self.started - float(self.last_full)) >= full_resync_hours * 3600
            )
            self.mode = "full" if full_due else "inc"
            self.since = None if full_due else since

    @property
    def incremental(self) -> bool:
        return self.mode == "inc"

    @property
    def authoritative(self) -> bool:
        """A full pass from the very beginning: whatever it did not see is gone (unless it was truncated)."""
        return self.mode == "full" and not self.resumed

    def observe(self, value) -> None:
        dt = value if isinstance(value, datetime) else parse_dt(value)
        if dt is not None and (self.high is None or dt > self.high):
            self.high = dt

    def cursor(self, **pos) -> dict:
        """The cursor stored with a mid-scan batch: enough to continue exactly here."""
        return {
            "mode": self.mode, "since": self.since, "last_full": self.last_full,
            "high": iso_z(self.high) if self.high else None, "pos": pos,
        }

    def final(self, *, truncated: bool = False) -> dict:
        """The cursor stored when the scan is done. A truncated scan does not advance the watermark."""
        if truncated:
            keep: dict = {"last_full": self.last_full}
            if self.since:
                keep["since"] = self.since
            return keep
        known = [dt for dt in (self.high, parse_dt(self.since)) if dt is not None]
        nxt = max(known) if known else datetime.fromtimestamp(self.started, tz=timezone.utc)
        return {"since": iso_z(nxt), "last_full": self.started if self.authoritative else self.last_full}

    def complete_scope(self, scope: str, *, truncated: bool = False) -> str | None:
        return scope if (self.authoritative and not truncated) else None


# ───────────────────────────── throttling signalled as 403 ─────────────────────────────


class ThrottleAs429(httpx.BaseTransport):
    """Rewrites "403 because of rate limiting" to a 429 with `Retry-After`, so `_http.Http` waits and retries
    instead of reporting bad credentials. `classify(response)` returns the seconds to wait, or None when the
    403 is a real permission error (which then passes through untouched)."""

    def __init__(self, inner: httpx.BaseTransport, classify: Callable[[httpx.Response], float | None]) -> None:
        self.inner, self.classify = inner, classify

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if response.status_code != 403:
            return response
        response.read()
        wait = self.classify(response)
        if wait is None:
            return response
        response.close()
        return httpx.Response(429, headers={"Retry-After": str(int(max(wait, 1)))}, content=b"", request=request)

    def close(self) -> None:
        self.inner.close()


# ───────────────────────────── HTML -> text ─────────────────────────────

_SKIP = {"script", "style", "head", "noscript", "template", "svg", "ac:parameter", "ac:task-id", "ac:placeholder"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
_BLOCK = {"div", "section", "article", "header", "footer", "main", "aside", "nav", "figure", "figcaption", "form",
          "fieldset", "details", "summary", "address", "dl", "dt", "dd", "center", "body", "html",
          "ac:rich-text-body", "ac:layout", "ac:layout-section", "ac:layout-cell"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CALLOUTS = {"info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip", "panel": "", "expand": ""}
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_REFS = {"ri:page", "ri:attachment", "ri:user", "ri:space", "ri:blog-post"}


class _Html2Text(HTMLParser):
    """Tag-stack-free converter: block tags emit line breaks, inline tags emit markers. Lists, tables, quotes,
    code and links keep their structure; scripts and styles are dropped. Confluence storage-format elements
    (`ac:*`, `ri:*`) are understood well enough to keep code, callouts, task lists, links and images."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.trail = 99               # trailing newlines already emitted (99 = nothing emitted yet)
        self.skip = 0
        self.pre = 0
        self.quote = 0
        self.lists: list[list] = []   # [kind, counter]
        self.links: list[list] = []   # [href_or_title, index into parts, kind]
        self.row: list[str] | None = None
        self.row_has_th = False
        self.row_count = 0
        self.cell_stack: list[tuple[list[str], int]] = []
        self.macros: list[str] = []
        self.capture: str | None = None
        self.captured = ""
        self.task_status = "incomplete"

    # -- output primitives --

    def _raw(self, text: str) -> None:
        if not text:
            return
        if self.trail > 0 and self.quote and not self.pre and text.strip("\n"):
            text = "> " * self.quote + text
        self.parts.append(text)
        stripped = text.rstrip("\n")
        if stripped:
            self.trail = len(text) - len(stripped)
        else:
            self.trail = (0 if self.trail >= 99 else self.trail) + len(text)

    def _break(self, n: int = 1) -> None:
        if not self.parts:
            return
        need = n - self.trail
        if need > 0:
            self.parts.append("\n" * need)
            self.trail = n

    def _text(self, data: str) -> None:
        if self.pre:
            self._raw(data)
            return
        data = re.sub(r"\s+", " ", data)
        if self.trail > 0 or (self.parts and self.parts[-1].endswith(" ")):
            data = data.lstrip()
        self._raw(data)

    # -- parser callbacks --

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _HEADINGS:
            self._break(2)
            self._raw("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._break(2)
        elif tag == "ac:task-body":
            self._break(1)
            self._raw("- [x] " if self.task_status == "complete" else "- [ ] ")
        elif tag in _BLOCK:
            self._break(1)
        elif tag == "br":
            if self.trail < 2:
                self._raw("\n")
        elif tag == "hr":
            self._break(2)
            self._raw("---")
            self._break(2)
        elif tag in ("ul", "ol"):
            self._break(1 if self.lists else 2)
            self.lists.append([tag, 0])
        elif tag == "li":
            self._break(1)
            depth = max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}. "
            else:
                marker = "- "
            self._raw("  " * depth + marker)
        elif tag == "blockquote":
            self._break(2)
            self.quote += 1
        elif tag in ("pre", "ac:plain-text-body"):
            self._break(2)
            self._raw("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self._raw("`")
        elif tag in ("strong", "b"):
            self._raw("**")
        elif tag in ("em", "i"):
            self._raw("*")
        elif tag == "a":
            self.links.append([a.get("href", ""), len(self.parts), "a"])
        elif tag == "ac:link":
            self.links.append(["", len(self.parts), "ac"])
        elif tag in _REFS:
            label = "@user" if tag == "ri:user" else (
                a.get("ri:content-title") or a.get("ri:filename") or a.get("ri:space-key") or "")
            if self.links and self.links[-1][2] == "ac" and label and not self.links[-1][0]:
                self.links[-1][0] = label
        elif tag == "img":
            if a.get("alt", "").strip():
                self._text(f"[image: {a['alt'].strip()}]")
        elif tag == "ac:image":
            self._text(f"[image: {a['ac:alt']}]" if a.get("ac:alt") else "[image]")
        elif tag == "ac:emoticon":
            self._text(a.get("ac:emoji-fallback") or f":{a.get('ac:name', '')}:")
        elif tag == "time" and a.get("datetime"):
            self._text(a["datetime"])
            self.capture, self.captured = "time", ""
        elif tag == "ac:structured-macro":
            name = a.get("ac:name", "")
            self.macros.append(name)
            if name in _CALLOUTS:
                self._break(2)
                if _CALLOUTS[name]:
                    self._raw(f"**{_CALLOUTS[name]}:** ")
        elif tag == "ac:task":
            self.task_status = "incomplete"
        elif tag == "ac:task-status":
            self.capture, self.captured = "task-status", ""
        elif tag == "table":
            self._break(2)
            self.row_count = 0
        elif tag == "tr":
            self.row, self.row_has_th = [], False
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
            self.row_has_th = self.row_has_th or tag == "th"
            self.cell_stack.append((self.parts, self.trail))
            self.parts, self.trail = [], 99

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(self.skip - 1, 0)
            return
        if self.skip:
            return
        if tag in _HEADINGS or tag == "p":
            self._break(2)
        elif tag in _BLOCK:
            self._break(1)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._break(1 if self.lists else 2)
        elif tag == "li":
            self._break(1)
        elif tag == "blockquote":
            self.quote = max(self.quote - 1, 0)
            self._break(2)
        elif tag in ("pre", "ac:plain-text-body"):
            self.pre = max(self.pre - 1, 0)
            self._break(1)
            self._raw("```")
            self._break(2)
        elif tag == "code" and not self.pre:
            self._raw("`")
        elif tag in ("strong", "b"):
            self._raw("**")
        elif tag in ("em", "i"):
            self._raw("*")
        elif tag in ("a", "ac:link"):
            if self.links:
                href, idx, kind = self.links.pop()
                inner = "".join(self.parts[idx:]).strip()
                if kind == "a":
                    if inner and href.startswith(("http://", "https://", "mailto:")) and inner != href:
                        self.parts.insert(idx, "[")
                        self._raw(f"]({href})")
                elif not inner and href:
                    self._text(href)
        elif tag == "ac:structured-macro":
            if self.macros and self.macros.pop() in _CALLOUTS:
                self._break(2)
        elif tag == "ac:task":
            self._break(1)
        elif tag == "ac:task-status":
            self.task_status = self.captured.strip()
            self.capture = None
        elif tag == "time":
            self.capture = None
        elif tag in ("td", "th"):
            if self.cell_stack:
                cell = re.sub(r"\s+", " ", "".join(self.parts)).strip().replace("|", "\\|")
                self.parts, self.trail = self.cell_stack.pop()
                if self.row is not None:
                    self.row.append(cell)
        elif tag == "tr":
            if self.row:
                self._break(1)
                self._raw("| " + " | ".join(self.row) + " |")
                self._break(1)
                if self.row_count == 0 and self.row_has_th:
                    self._raw("| " + " | ".join("---" for _ in self.row) + " |")
                    self._break(1)
                self.row_count += 1
            self.row = None
        elif tag == "table":
            self._break(2)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.capture == "task-status":
            self.captured += data
        elif self.capture is None:
            self._text(data)


def html_to_text(markup: str) -> str:
    """Readable Markdown-ish text from HTML or Confluence storage format. Never raises on malformed input."""
    if not markup:
        return ""
    markup = _CDATA.sub(lambda m: html.escape(m.group(1), quote=False), markup)
    parser = _Html2Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001  (a malformed fragment must not kill a scan: fall back to stripping tags)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(markup))).strip()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
