"""
One pure-ASGI middleware (so streaming responses are untouched) that adds, to
every HTTP request:

  * a request id (X-Request-ID, also in the security log)
  * security headers on every response
  * a request-body size cap (JSON: 1 MB; the upload route: MAX_UPLOAD_MB + 1 MB),
    enforced on the bytes actually received, not just on Content-Length
  * a coarse per-IP request backstop (in-process sliding window). The precise
    limits (login, chat, upload...) live in core/ratelimit.py; this only stops
    an address from hammering everything else.
"""

import json
import time
import uuid
from collections import defaultdict, deque

from starlette.requests import Request

from app.core.audit import audit, request_id_var
from app.core.config import get_settings
from app.core.netutil import client_ip

_JSON_BODY_LIMIT = 1_000_000
_EXEMPT_PATHS = ("/api/health",)


class _BodyTooLarge(Exception):
    pass


def _json_response_parts(status: int, payload: dict, extra_headers: list[tuple[bytes, bytes]] | None = None):
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += extra_headers or []
    return (
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    )


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _security_headers(self, path: str, is_prod: bool) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if is_prod:
            headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        return headers

    def _over_global_limit(self, ip: str) -> int:
        """Seconds until the caller may retry, or 0 if under the limit."""
        limit = get_settings().rl_global_per_minute
        now = time.monotonic()
        window = self._hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(60 - (now - window[0])) + 1)
        window.append(now)
        if len(self._hits) > 20000:  # bound memory under address churn
            for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                del self._hits[key]
        return 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path: str = scope["path"]
        method: str = scope["method"]
        request_id = uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        base_headers = [(b"x-request-id", request_id.encode())] + self._security_headers(path, settings.is_prod)
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                existing = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in base_headers if n not in existing]
                if path.startswith("/api/") and b"cache-control" not in existing:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        try:
            if method != "OPTIONS" and not path.startswith(_EXEMPT_PATHS):
                ip = client_ip(Request(scope))
                retry = self._over_global_limit(ip)
                if retry:
                    audit("rate_limited", scope="global", ip=ip)
                    start, body = _json_response_parts(
                        429,
                        {"detail": {"code": "rate_limited", "message": "Too many requests.", "retry_after_seconds": retry}},
                        [(b"retry-after", str(retry).encode())],
                    )
                    await send_wrapper(start)
                    await send_wrapper(body)
                    return

            is_upload = method == "POST" and path == "/api/documents"
            if method == "POST" and path == "/api/scale/uploads":
                # Admin file upload into a source: many files in one request, so its cap is its own setting.
                from app.scale.settings import get_scale_settings

                limit = (get_scale_settings().upload_request_mb + 1) * 1024 * 1024
            else:
                limit = (settings.max_upload_mb + 1) * 1024 * 1024 if is_upload else _JSON_BODY_LIMIT
            headers = dict(scope["headers"])
            declared = headers.get(b"content-length", b"")
            if declared.isdigit() and int(declared) > limit:
                start, body = _json_response_parts(413, {"detail": "Request body too large"})
                await send_wrapper(start)
                await send_wrapper(body)
                return

            received = 0

            async def limited_receive():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _BodyTooLarge
                return message

            try:
                await self.app(scope, limited_receive, send_wrapper)
            except _BodyTooLarge:
                if not started:
                    start, body = _json_response_parts(413, {"detail": "Request body too large"})
                    await send_wrapper(start)
                    await send_wrapper(body)
        finally:
            request_id_var.reset(token)
