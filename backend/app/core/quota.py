"""
Per-user chat quota: a credit allowance that, once spent, blocks the user for
a fixed window and then refills -- the same "you've hit your limit, come back
at HH:MM" behavior ChatGPT and Claude use.

State lives in the users row (credit_limit, credits_used, blocked_until), so
it is shared across workers and survives restarts. Every mutation takes a
row lock (SELECT ... FOR UPDATE), which serializes concurrent requests from
the same user -- firing many chats at once cannot spend the allowance twice.
The server clock is the only clock; clients get retry_after_seconds.

Lifecycle of one message:
    reserve()  -> charge credits up front (429 if blocked / out of credits)
    refund()   -> give them back if the request failed before any answer
The charge that empties the allowance sets blocked_until immediately, so the
UI can show the countdown right after the last allowed message.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.db import UsageEvent, User


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Quota:
    limit: int
    used: int
    remaining: int
    cost: int
    messages_left: int
    blocked_until: datetime | None
    retry_after_seconds: int
    block_seconds: int
    server_time: datetime

    def to_dict(self) -> dict:
        data = asdict(self)
        data["blocked_until"] = self.blocked_until.isoformat() if self.blocked_until else None
        data["server_time"] = self.server_time.isoformat()
        return data


class QuotaExceeded(Exception):
    def __init__(self, quota: Quota):
        super().__init__("Usage limit reached")
        self.quota = quota


def snapshot(user: User, now: datetime | None = None) -> Quota:
    """Pure read of a user's effective quota (an expired block counts as refilled)."""
    now = now or utcnow()
    settings = get_settings()
    cost = settings.chat_credit_cost
    expired = user.blocked_until is not None and user.blocked_until <= now
    used = 0 if expired else user.credits_used
    blocked_until = None if expired else user.blocked_until
    remaining = max(user.credit_limit - used, 0)
    retry = math.ceil((blocked_until - now).total_seconds()) if blocked_until else 0
    return Quota(
        limit=user.credit_limit,
        used=used,
        remaining=remaining,
        cost=cost,
        messages_left=0 if blocked_until else remaining // cost,
        blocked_until=blocked_until,
        retry_after_seconds=max(retry, 0),
        block_seconds=settings.chat_block_seconds,
        server_time=now,
    )


def _refill_if_due(user: User, now: datetime) -> None:
    if user.blocked_until is not None and user.blocked_until <= now:
        user.credits_used = 0
        user.blocked_until = None


def sync_block_state(user: User, now: datetime | None = None) -> None:
    """Make blocked_until agree with the credits: unblock when there is room for
    another message, block (for the full window) when there isn't. Called after
    every charge/refund and whenever an admin changes the allowance."""
    now = now or utcnow()
    settings = get_settings()
    remaining = user.credit_limit - user.credits_used
    if remaining >= settings.chat_credit_cost:
        user.blocked_until = None
    elif user.blocked_until is None:
        user.blocked_until = now + timedelta(seconds=settings.chat_block_seconds)


def reset_usage(user: User) -> None:
    user.credits_used = 0
    user.blocked_until = None


async def _locked_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    result = await session.execute(select(User).where(User.user_id == user_id).with_for_update())
    return result.scalar_one()


async def reserve(session: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> Quota:
    """Charge one message. Raises QuotaExceeded (state committed) if the user is
    blocked or has less than one message of credits left."""
    now = now or utcnow()
    cost = get_settings().chat_credit_cost
    user = await _locked_user(session, user_id)
    _refill_if_due(user, now)

    blocked = user.blocked_until is not None
    if not blocked and user.credit_limit - user.credits_used < cost:
        sync_block_state(user, now)  # e.g. an admin lowered the allowance below what's spent
        blocked = True
    if blocked:
        await session.commit()
        raise QuotaExceeded(snapshot(user, now))

    user.credits_used += cost
    session.add(UsageEvent(user_id=user_id, cost=cost, outcome="charged"))
    sync_block_state(user, now)
    await session.commit()
    return snapshot(user, now)


async def refund(session: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> Quota:
    """Give back one message (request failed before producing an answer)."""
    now = now or utcnow()
    cost = get_settings().chat_credit_cost
    user = await _locked_user(session, user_id)
    user.credits_used = max(user.credits_used - cost, 0)
    session.add(UsageEvent(user_id=user_id, cost=cost, outcome="refunded"))
    sync_block_state(user, now)
    await session.commit()
    return snapshot(user, now)
