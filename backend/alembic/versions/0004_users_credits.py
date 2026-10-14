"""users (roles, hashed passwords, chat credit allowance) + usage_events

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("credit_limit", sa.Integer, nullable=False),
        sa.Column("credits_used", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("token_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("users_username_lower_idx", "users", [sa.text("lower(username)")], unique=True)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                   sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("cost", sa.Integer, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("usage_events_user_idx", "usage_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("usage_events_user_idx", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("users_username_lower_idx", table_name="users")
    op.drop_table("users")
