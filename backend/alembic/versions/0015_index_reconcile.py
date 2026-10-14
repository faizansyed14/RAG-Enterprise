"""Self-healing index: what each document should have in the chunk index.

documents.chunks_expected   how many extracted chunks the last indexing wrote (hand-written chunks are counted from
                            chunk_edits, so this number never needs updating when someone adds or deletes one)
documents.index_signature   the collection name and chunking settings those chunks were built with; a document whose
                            signature differs from the current one is rebuilt (embedding model or chunking changed)
documents.reconciled_at     when the reconcile job last compared this document with the index
scale_state                 small key/value store for the reconcile job (when it last swept for orphan chunks, last result)

The reconcile job compares Postgres (the source of truth) with Qdrant (a derived index) and repairs the index.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunks_expected integer")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_signature text")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS reconciled_at timestamptz")
    op.execute(
        "CREATE INDEX IF NOT EXISTS documents_reconcile_idx ON documents (reconciled_at NULLS FIRST) "
        "WHERE chunks_indexed_at IS NOT NULL"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS scale_state ("
        " key text PRIMARY KEY, value text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scale_state")
    op.execute("DROP INDEX IF EXISTS documents_reconcile_idx")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS reconciled_at")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS index_signature")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS chunks_expected")
