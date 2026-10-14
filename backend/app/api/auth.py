from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import get_settings
from app.core.netutil import client_ip
from app.core.quota import snapshot, utcnow
from app.core.ratelimit import current_count, hit, limit_ip, limit_user, reset, throttled
from app.core.security import CurrentUser, authenticate, create_token, get_current_user
from app.models.db import User, get_session
from app.models.schemas import LoginRequest, LoginResponse, MeOut, QuotaOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(limit_ip("login", "rl_login_per_ip", 60))])
async def login(request: Request, body: LoginRequest, session: AsyncSession = Depends(get_session)) -> LoginResponse:
    settings = get_settings()
    ip = client_ip(request)
    username_key = f"login_fail:user:{body.username.strip().lower()[:64]}"
    ip_key = f"login_fail:ip:{ip}"
    window = settings.rl_login_lockout_seconds

    # Lockout: too many failures for this username or from this address -> refuse
    # without even checking the password, with the same message for both.
    for key, cap in ((username_key, settings.rl_login_max_failures), (ip_key, settings.rl_login_ip_max_failures)):
        recent = await current_count(key, window)
        if recent.count >= cap:
            audit("login_locked", ip=ip)
            raise throttled(recent.retry_after_seconds, "Too many failed sign-in attempts. Please try again later.")

    user = await authenticate(session, body.username, body.password)
    if user is None:
        await hit(username_key, settings.rl_login_max_failures, window)
        await hit(ip_key, settings.rl_login_ip_max_failures, window)
        audit("login_failed", ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    await reset(username_key)
    user.last_login_at = utcnow()
    await session.commit()
    audit("login_ok", user=user.username, role=user.role, ip=ip)
    return LoginResponse(token=create_token(user.user_id, user.role, user.token_version))


@router.get("/me", response_model=MeOut, dependencies=[Depends(limit_user("me", "rl_me_per_minute", 60))])
async def me(
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    quota = None
    if current.role != "admin":
        user = await session.get(User, current.user_id)
        quota = QuotaOut(**snapshot(user).__dict__)
    return MeOut(user_id=current.user_id, username=current.username, role=current.role, quota=quota)
