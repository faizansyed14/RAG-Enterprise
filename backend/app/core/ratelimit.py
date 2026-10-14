"""
Rate limiting backed by Postgres, so limits hold across workers and restarts
without adding infrastructure.

Fixed windows: one row per (key, window_start) incremented with a single atomic
INSERT ... ON CONFLICT DO UPDATE, so concurrent requests can't slip past a limit.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text

from app.core.audit import audit
from app.core.config import get_settings
from app.core.netutil import client_ip
from app.core.security import CurrentUser, get_current_user
from app.models.db import async_session


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Hit:
    allowed: bool
    count: int
    retry_after_seconds: int


def _window(now: datetime, window_seconds: int) -> tuple[datetime, int]:
    start_ts = int(now.timestamp()) // window_seconds * window_seconds
    start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    return start, max(1, math.ceil((start + timedelta(seconds=window_seconds) - now).total_seconds()))


async def hit(key: str, limit: int, window_seconds: int, now: datetime | None = None) -> Hit:
    """Count one event for `key`; allowed while the window's count is <= limit."""
    now = now or utcnow()
    start, retry = _window(now, window_seconds)
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO rate_limits (key, window_start, count) VALUES (:k, :w, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1 RETURNING count"
            ),
            {"k": key, "w": start},
        )
        count = result.scalar_one()
        await session.commit()
    return Hit(allowed=count <= limit, count=count, retry_after_seconds=retry)


async def current_count(key: str, window_seconds: int, now: datetime | None = None) -> Hit:
    """Read (without counting) the current window's count for `key`."""
    now = now or utcnow()
    start, retry = _window(now, window_seconds)
    async with async_session() as session:
        result = await session.execute(
            text("SELECT count FROM rate_limits WHERE key = :k AND window_start = :w"), {"k": key, "w": start}
        )
        count = result.scalar_one_or_none() or 0
    return Hit(allowed=True, count=count, retry_after_seconds=retry)


async def reset(key: str) -> None:
    async with async_session() as session:
        await session.execute(text("DELETE FROM rate_limits WHERE key = :k"), {"k": key})
        await session.commit()


async def purge_old(older_than: timedelta = timedelta(days=1)) -> None:
    async with async_session() as session:
        await session.execute(text("DELETE FROM rate_limits WHERE window_start < :t"), {"t": utcnow() - older_than})
        await session.commit()


def throttled(retry_after_seconds: int, message: str = "Too many requests. Please slow down.") -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": "rate_limited", "message": message, "retry_after_seconds": retry_after_seconds},
        headers={"Retry-After": str(retry_after_seconds)},
    )


def limit_ip(scope: str, setting: str, window_seconds: int):
    """Dependency: at most `settings.<setting>` requests per window per client IP."""

    async def dependency(request: Request) -> None:
        limit = getattr(get_settings(), setting)
        result = await hit(f"{scope}:ip:{client_ip(request)}", limit, window_seconds)
        if not result.allowed:
            audit("rate_limited", scope=scope, ip=client_ip(request))
            raise throttled(result.retry_after_seconds)

    return dependency


def limit_user(scope: str, setting: str, window_seconds: int):
    """Dependency: at most `settings.<setting>` requests per window per signed-in user."""

    async def dependency(request: Request, user: CurrentUser = Depends(get_current_user)) -> None:
        limit = getattr(get_settings(), setting)
        result = await hit(f"{scope}:user:{user.user_id}", limit, window_seconds)
        if not result.allowed:
            audit("rate_limited", scope=scope, user=user.username, ip=client_ip(request))
            raise throttled(result.retry_after_seconds)

    return dependency
