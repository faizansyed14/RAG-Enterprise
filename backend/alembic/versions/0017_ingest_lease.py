"""Shared ingestion state: a lease per document and its last progress event, so any API replica can serve progress
and no two replicas resume the same document.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS ingest_lease_until timestamptz")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress jsonb")
    op.execute(
        "CREATE INDEX IF NOT EXISTS documents_unfinished_idx ON documents (created_at) "
        "WHERE status IN ('queued', 'extracting', 'ocr', 'indexing')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS documents_unfinished_idx")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS progress")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS ingest_lease_until")
