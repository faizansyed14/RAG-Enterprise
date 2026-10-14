"""documents.preview_storage_key -- rendered PDF for DOCX preview

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22 12:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("preview_storage_key", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "preview_storage_key")
