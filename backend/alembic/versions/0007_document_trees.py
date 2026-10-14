"""document_trees -- the tree engine's knowledge structure (doc.json/tree.json/pages.json),
moved off the .rag-data volume and into Postgres. See rag_core/postgres_store.py.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_trees",
        sa.Column("doc_id", sa.Text, primary_key=True),
        sa.Column("meta", postgresql.JSONB, nullable=False),
        sa.Column("tree", postgresql.JSONB, nullable=False),
        sa.Column("pages", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("document_trees")
