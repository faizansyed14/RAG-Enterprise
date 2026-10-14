"""Client IP resolution. X-Forwarded-For is attacker-controlled unless it was
appended by a proxy we run, so it is only consulted when TRUSTED_PROXY_HOPS > 0,
and then only the entry our own proxy added (counting from the right)."""

from fastapi import Request

from app.core.config import get_settings


def client_ip(request: Request) -> str:
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"
