"""rate_limits (fixed-window counters) + users.chat_lease_until

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_index("rate_limits_window_idx", "rate_limits", ["window_start"])
    op.add_column("users", sa.Column("chat_lease_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("users", "chat_lease_until")
    op.drop_index("rate_limits_window_idx", table_name="rate_limits")
    op.drop_table("rate_limits")
