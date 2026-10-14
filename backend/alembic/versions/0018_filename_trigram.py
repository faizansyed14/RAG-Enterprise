"""trigram index for document-name search (so ILIKE '%text%' does not scan the table)

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pg_trgm ships with PostgreSQL's contrib package. If the database user may not create extensions the index is skipped;
    # search still works, only slower. Ask a database admin to run CREATE EXTENSION pg_trgm and re-run this migration's index.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE INDEX IF NOT EXISTS documents_filename_trgm ON documents USING gin (filename gin_trgm_ops);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_trgm index skipped: %', SQLERRM;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS documents_filename_trgm")
