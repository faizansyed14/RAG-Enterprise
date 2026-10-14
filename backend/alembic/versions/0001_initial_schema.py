"""initial schema: documents, diagram_pages

Revision ID: 0001
Revises:
Create Date: 2026-09-22 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "documents",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), primary_key=True,
                   server_default=sa.text("gen_random_uuid()")),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False, unique=True),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("rag_doc_id", sa.Text, nullable=True),
        sa.Column("is_scanned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("status_detail", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
    )

    op.create_table(
        "diagram_pages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.document_id"),
                   nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("ocr_text", sa.Text, nullable=True),
        sa.Column("caption", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("figure_id", sa.Text, nullable=True),
        sa.Column("callouts", postgresql.JSONB, nullable=True),
        sa.Column("image_key", sa.Text, nullable=False),
        sa.Column("qdrant_point_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("embedding_model", sa.Text, nullable=True),
    )
    op.create_index("diagram_pages_document_idx", "diagram_pages", ["document_id"])


def downgrade() -> None:
    op.drop_index("diagram_pages_document_idx", table_name="diagram_pages")
    op.drop_table("diagram_pages")
    op.drop_table("documents")
