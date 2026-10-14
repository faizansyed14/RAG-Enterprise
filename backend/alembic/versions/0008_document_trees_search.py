"""document_trees full-text search -- a trigger-maintained tsvector over
name/description/top-level section titles, backing browse_documents'
sort="relevance" in local mode. See rag_core/postgres_store.py.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("document_trees", sa.Column("search_vector", postgresql.TSVECTOR, nullable=True))

    op.execute(
        r"""
        CREATE FUNCTION document_trees_search_vector_update() RETURNS trigger AS $$
        DECLARE
            titles text;
        BEGIN
            SELECT string_agg(elem->>'title', ' ')
            INTO titles
            FROM jsonb_array_elements(COALESCE(NEW.tree, '[]'::jsonb)) elem;

            -- Postgres's parser tokenizes a whole "name.pdf"-shaped string as one opaque
            -- "file" token instead of splitting on the extension (confirmed live: 'invoice'
            -- would not match 'Invoice-KPFUIKY8-0005.pdf' without this) -- strip the
            -- extension before indexing the name.
            NEW.search_vector :=
                setweight(to_tsvector('english',
                    coalesce(regexp_replace(NEW.meta->>'name', '\.[A-Za-z0-9]+$', ''), '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.meta->>'description', '')), 'B') ||
                setweight(to_tsvector('english', coalesce(titles, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        """
        CREATE TRIGGER document_trees_search_vector_trigger
        BEFORE INSERT OR UPDATE ON document_trees
        FOR EACH ROW EXECUTE FUNCTION document_trees_search_vector_update();
        """
    )

    op.create_index(
        "document_trees_search_vector_idx", "document_trees", ["search_vector"], postgresql_using="gin"
    )

    # Backfill existing rows -- fires the trigger above, no LLM/embedding cost.
    op.execute("UPDATE document_trees SET doc_id = doc_id")


def downgrade() -> None:
    op.drop_index("document_trees_search_vector_idx", table_name="document_trees")
    op.execute("DROP TRIGGER document_trees_search_vector_trigger ON document_trees")
    op.execute("DROP FUNCTION document_trees_search_vector_update()")
    op.drop_column("document_trees", "search_vector")
