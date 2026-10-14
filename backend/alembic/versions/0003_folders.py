"""folders + document_folders (many-to-many) -- flat folders, a document
can belong to zero, one, or many folders.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
    )

    op.create_table(
        "document_folders",
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                   sa.ForeignKey("documents.document_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True),
                   sa.ForeignKey("folders.folder_id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("document_folders_folder_idx", "document_folders", ["folder_id"])


def downgrade() -> None:
    op.drop_index("document_folders_folder_idx", table_name="document_folders")
    op.drop_table("document_folders")
    op.drop_table("folders")
