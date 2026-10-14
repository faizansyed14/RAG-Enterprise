"""Admin-only user management: create users, change username/password/role,
set the chat credit allowance, reset usage, disable or delete."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.bootstrap import MANAGED_BY_ENV_MESSAGE, is_env_admin
from app.core.config import get_settings
from app.core.ratelimit import limit_user
from app.core.quota import reset_usage, snapshot, sync_block_state
from app.core.security import CurrentUser, hash_password, require_admin
from app.models.db import User, get_session
from app.models.schemas import QuotaOut, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


def _to_out(user: User) -> UserOut:
    return UserOut(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        credit_limit=user.credit_limit,
        quota=QuotaOut(**snapshot(user).__dict__),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        managed_by_env=is_env_admin(user.username),
    )


async def _get_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


async def _username_taken(session: AsyncSession, username: str, exclude: uuid.UUID | None = None) -> bool:
    query = select(func.count()).select_from(User).where(func.lower(User.username) == username.lower())
    if exclude is not None:
        query = query.where(User.user_id != exclude)
    return bool(await session.scalar(query))


async def _other_active_admins(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.is_active.is_(True), User.user_id != user_id)
    )


def _validate_credit_limit(limit: int) -> None:
    cost = get_settings().chat_credit_cost
    if limit < cost:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Credit limit must be at least {cost} (one message)")


@router.get("", response_model=list[UserOut])
async def list_users(
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_to_out(u) for u in users]


_write_limit = Depends(limit_user("admin_write", "rl_admin_write_per_minute", 60))


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[_write_limit])
async def create_user(
    body: UserCreate,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if await _username_taken(session, body.username) or is_env_admin(body.username):
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken")
    if body.password.lower() == body.username.lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The password can't be the same as the username")
    limit = body.credit_limit if body.credit_limit is not None else get_settings().default_credit_limit
    _validate_credit_limit(limit)
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        credit_limit=limit,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    audit("user_created", by=_admin.username, user=user.username, role=user.role)
    return _to_out(user)


@router.patch("/{user_id}", response_model=UserOut, dependencies=[_write_limit])
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await _get_or_404(session, user_id)
    is_self = user.user_id == admin.user_id
    if is_env_admin(user.username):
        # Only the credit allowance means anything for this account; identity and
        # credentials come from .env and are re-applied on every start.
        touches_identity = (
            body.password is not None
            or (body.username is not None and body.username != user.username)
            or (body.role is not None and body.role != user.role)
            or (body.is_active is not None and body.is_active != user.is_active)
        )
        if touches_identity:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, MANAGED_BY_ENV_MESSAGE)
    if body.password is not None and body.password.lower() == (body.username or user.username).lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The password can't be the same as the username")

    if body.username is not None and body.username != user.username:
        if await _username_taken(session, body.username, exclude=user.user_id) or is_env_admin(body.username):
            raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken")
        user.username = body.username

    revoke_sessions = False
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        revoke_sessions = True

    loses_admin_access = (body.role == "user" and user.role == "admin") or (
        body.is_active is False and user.is_active and user.role == "admin"
    )
    if loses_admin_access:
        if is_self:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't demote or disable your own account")
        if await _other_active_admins(session, user.user_id) == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one active admin is required")
    if body.is_active is False and is_self:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't demote or disable your own account")

    if body.role is not None and body.role != user.role:
        user.role = body.role
        revoke_sessions = True
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        revoke_sessions = True
    if revoke_sessions:
        user.token_version += 1

    if body.credit_limit is not None:
        _validate_credit_limit(body.credit_limit)
        user.credit_limit = body.credit_limit
        sync_block_state(user)

    await session.commit()
    await session.refresh(user)
    audit("user_updated", by=admin.username, user=user.username, fields=",".join(sorted(body.model_dump(exclude_none=True))))
    return _to_out(user)


@router.post("/{user_id}/reset-usage", response_model=UserOut, dependencies=[_write_limit])
async def reset_user_usage(
    user_id: uuid.UUID,
    _admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await _get_or_404(session, user_id)
    reset_usage(user)
    await session.commit()
    await session.refresh(user)
    return _to_out(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_write_limit])
async def delete_user(
    user_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    user = await _get_or_404(session, user_id)
    if is_env_admin(user.username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, MANAGED_BY_ENV_MESSAGE)
    if user.user_id == admin.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't delete your own account")
    if user.role == "admin" and await _other_active_admins(session, user.user_id) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one active admin is required")
    await session.delete(user)
    await session.commit()
    audit("user_deleted", by=admin.username, user=user.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
