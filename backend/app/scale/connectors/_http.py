"""Shared plumbing for connectors that talk HTTP: one place for retries, rate limits, auth errors and size caps.

Rules every connector built on this follows (so a source behaves the same way as the Graph connector does):

  * 429 / 502 / 503 / 504 and transport errors are retried with bounded exponential backoff; a `Retry-After`
    header (seconds or an HTTP date) is honoured, capped at `max_wait`.
  * 401 / 403 raise `AuthError` -- never retried, and the message carries the status and a short body excerpt
    but never a header, token or URL query string.
  * Secrets are read from the environment at call time (`env_secret`); a connection's config only names the
    variable, so nothing secret reaches the database, a cursor, a log line or an exception.
  * Downloads stream into a temporary file with a hard size cap, never into memory.
"""

from __future__ import annotations

import email.utils
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from typing import BinaryIO, Callable, Iterator

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 502, 503, 504}
DEFAULT_MAX_BYTES = 200 * 1024 * 1024


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(HttpError):
    """401 / 403: wrong or expired credentials, or the account may not read this. Not retried."""


class TooLarge(HttpError):
    pass


def env_secret(name: str | None, what: str = "credential") -> str:
    """The secret named by a config field such as `token_env`. Raises a clear error instead of sending an empty one."""
    if not name:
        raise HttpError(f"no environment variable name configured for the {what}")
    value = os.environ.get(name, "")
    if not value:
        raise HttpError(f"environment variable {name} (the {what}) is not set")
    return value


def parse_retry_after(value: str | None, default: float) -> float:
    if not value:
        return default
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return default


class Http:
    def __init__(
        self,
        base_url: str = "",
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout: float = 60.0,
        max_retries: int = 5,
        max_wait: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        follow_redirects: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries, self.max_wait, self._sleep = max_retries, max_wait, sleep
        self.client = client or httpx.Client(
            headers=headers, auth=auth, timeout=timeout, transport=transport, follow_redirects=follow_redirects,
        )
        if client is not None and headers:
            self.client.headers.update(headers)

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}" if self.base_url else path_or_url

    def request(self, method: str, path_or_url: str, *, ok: tuple[int, ...] = (200, 201, 202, 204), **kwargs) -> httpx.Response:
        url = self.url(path_or_url)
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last = exc
                if attempt >= self.max_retries:
                    raise HttpError(f"{method} {_safe(url)}: {type(exc).__name__}") from exc
                self._sleep(min(2 ** attempt, self.max_wait))
                continue
            if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = parse_retry_after(response.headers.get("Retry-After"), min(2 ** attempt, 30))
                log.warning("%s %s -> %s, retrying in %.1fs", method, _safe(url), response.status_code, min(wait, self.max_wait))
                self._sleep(min(wait, self.max_wait))
                continue
            if response.status_code in (401, 403):
                raise AuthError(f"{method} {_safe(url)} -> {response.status_code}: {_excerpt(response)}", response.status_code)
            if response.status_code not in ok:
                raise HttpError(f"{method} {_safe(url)} -> {response.status_code}: {_excerpt(response)}", response.status_code)
            return response
        raise HttpError(f"{method} {_safe(url)}: retries exhausted") from last

    def get(self, path_or_url: str, **kwargs) -> httpx.Response:
        return self.request("GET", path_or_url, **kwargs)

    def get_json(self, path_or_url: str, **kwargs) -> dict | list:
        return self.get(path_or_url, **kwargs).json()

    def post_json(self, path_or_url: str, json: dict | None = None, **kwargs) -> dict | list:
        return self.request("POST", path_or_url, json=json, **kwargs).json()

    def download(self, path_or_url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, method: str = "GET", **kwargs) -> BinaryIO:
        """Stream a body into a temporary file (deleted when closed). Raises TooLarge past `max_bytes`."""
        url = self.url(path_or_url)
        for attempt in range(self.max_retries + 1):
            tmp = tempfile.TemporaryFile()
            try:
                with self.client.stream(method, url, **kwargs) as response:
                    if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                        wait = parse_retry_after(response.headers.get("Retry-After"), min(2 ** attempt, 30))
                        tmp.close()
                        self._sleep(min(wait, self.max_wait))
                        continue
                    if response.status_code in (401, 403):
                        response.read()
                        raise AuthError(f"{method} {_safe(url)} -> {response.status_code}: {_excerpt(response)}", response.status_code)
                    if response.status_code != 200:
                        response.read()
                        raise HttpError(f"{method} {_safe(url)} -> {response.status_code}: {_excerpt(response)}", response.status_code)
                    total = 0
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise TooLarge(f"{_safe(url)}: larger than {max_bytes} bytes")
                        tmp.write(chunk)
                tmp.seek(0)
                return tmp
            except (HttpError, httpx.TransportError) as exc:
                tmp.close()
                if isinstance(exc, httpx.TransportError):
                    if attempt >= self.max_retries:
                        raise HttpError(f"{method} {_safe(url)}: {type(exc).__name__}") from exc
                    self._sleep(min(2 ** attempt, self.max_wait))
                    continue
                raise
        raise HttpError(f"{method} {_safe(url)}: retries exhausted")

    def close(self) -> None:
        self.client.close()


def follow_links(http: Http, first: str, next_of: Callable[[dict], str | None], items_of: Callable[[dict], list], **kwargs) -> Iterator[list]:
    """Pages of a "next link" API: yields each page's items until `next_of(page)` is falsy."""
    url: str | None = first
    while url:
        page = http.get_json(url, **kwargs)
        yield items_of(page)
        url = next_of(page)
        kwargs.pop("params", None)       # a next link already carries its query string


def _safe(url: str) -> str:
    return url.split("?", 1)[0]


def _excerpt(response: httpx.Response, limit: int = 200) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return ""
    return re.sub(r"\s+", " ", text)[:limit]


def render_document(title: str, meta: dict[str, object] | None, body: str, *, fmt: str = "md") -> bytes:
    """The bytes of a virtual file for a source whose objects are not files (a wiki page, an issue, a chat day)."""
    lines = [f"# {title}".strip(), ""] if fmt == "md" else [title.strip(), ""]
    for key, value in (meta or {}).items():
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {value}")
    if meta:
        lines.append("")
    lines.append(body.strip())
    return ("\n".join(lines).strip() + "\n").encode("utf-8")


_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def safe_name(name: str, fallback: str = "untitled", limit: int = 120) -> str:
    cleaned = _BAD.sub(" ", name or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:limit]
