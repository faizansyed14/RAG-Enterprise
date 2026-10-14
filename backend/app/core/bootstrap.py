"""The `.env` admin account.

The account named by ADMIN_USERNAME is owned by the `.env` file, not by the Users
page: on every boot it is created if missing, and its password is re-synced to
ADMIN_PASSWORD (so changing the password means editing `.env` and restarting).
The API refuses to rename, demote, disable, delete or set a password for it.
Other accounts, including other admins, are managed from the Users page as usual.
"""

import logging

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.models.db import User, async_session

log = logging.getLogger(__name__)

MANAGED_BY_ENV_MESSAGE = "The admin account is managed by the .env file (ADMIN_USERNAME / ADMIN_PASSWORD)."


def is_env_admin(username: str) -> bool:
    return username.strip().lower() == get_settings().admin_username.strip().lower()


async def ensure_bootstrap_admin() -> None:
    settings = get_settings()
    try:
        async with async_session() as session:
            user = (
                await session.execute(select(User).where(func.lower(User.username) == settings.admin_username.lower()))
            ).scalar_one_or_none()

            if user is None:
                session.add(
                    User(
                        username=settings.admin_username,
                        password_hash=hash_password(settings.admin_password),
                        role="admin",
                        credit_limit=settings.default_credit_limit,
                    )
                )
                await session.commit()
                log.info("Created the admin account %r from ADMIN_USERNAME/ADMIN_PASSWORD", settings.admin_username)
                return

            changed: list[str] = []
            if user.role != "admin":
                user.role = "admin"
                changed.append("role")
            if not user.is_active:
                user.is_active = True
                changed.append("active")
            if not verify_password(user.password_hash, settings.admin_password):
                user.password_hash = hash_password(settings.admin_password)
                changed.append("password")
            if changed:
                # Any change to the credentials signs out existing sessions.
                user.token_version += 1
                await session.commit()
                log.info("Synced the admin account %r with .env (%s)", user.username, ", ".join(changed))
    except Exception:
        log.exception("Could not sync the admin account -- has `alembic upgrade head` been run?")
