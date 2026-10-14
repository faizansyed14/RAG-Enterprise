"""
Auth: users live in the `users` table with argon2-hashed passwords. A login
issues a signed JWT (sub=user_id, role, tv=token_version). Every protected
request re-loads the user from the database, so disabling a user, changing
their password, or changing their role takes effect immediately -- the role in
the token is informational only and is never trusted for authorization.
"""

import time
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.db import User, async_session

_bearer = HTTPBearer(auto_error=False)
_ALGORITHM = "HS256"
_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so a wrong username and a
# wrong password cost the same time and can't be told apart by timing.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


@dataclass(frozen=True)
class CurrentUser:
    user_id: uuid.UUID
    username: str
    role: str


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def authenticate(session: AsyncSession, username: str, password: str) -> User | None:
    result = await session.execute(select(User).where(func.lower(User.username) == username.strip().lower()))
    user = result.scalar_one_or_none()
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    return user


def create_token(user_id: uuid.UUID | str, role: str, token_version: int, ttl_seconds: int | None = None) -> str:
    settings = get_settings()
    if ttl_seconds is None:
        ttl_seconds = settings.access_token_ttl_minutes * 60
    now = int(time.time())
    payload = {"sub": str(user_id), "role": role, "tv": token_version, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, settings.auth_secret, algorithm=_ALGORITHM)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    settings = get_settings()
    try:
        payload = jwt.decode(credentials.credentials, settings.auth_secret, algorithms=[_ALGORITHM])
        user_id = uuid.UUID(payload["sub"])
        token_version = int(payload["tv"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc

    # Own short-lived session so the connection is released before any streaming starts.
    async with async_session() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active or user.token_version != token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, sign in again")
    return CurrentUser(user_id=user.user_id, username=user.username, role=user.role)


# Any signed-in, active user.
require_user = get_current_user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user
