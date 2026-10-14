"""Drop users.chat_lease_until -- the one-stream-at-a-time chat lock has been
removed (see app/api/chat.py, core/ratelimit.py) so multiple chat messages
can stream concurrently for the same user.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("users", "chat_lease_until")


def downgrade() -> None:
    op.add_column("users", sa.Column("chat_lease_until", sa.DateTime(timezone=True)))
