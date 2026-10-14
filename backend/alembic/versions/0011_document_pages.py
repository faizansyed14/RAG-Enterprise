"""document_pages -- a per-page, full-text-indexed copy of document_trees.pages, backing the
agent's search_content tool (see rag_core/postgres_store.py::search_pages).

The pages themselves live as one jsonb array per document in document_trees.pages, which can't be
indexed per page. This table holds one row per page with a stored tsvector (page text weighted A,
document name B, document description C -- so a question that names a project still ranks that
project's pages above identical-looking pages of other projects) and a GIN index over it.
Maintained by PostgresDocStore.save_document; deleted automatically with its document (FK cascade).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-05 00:00:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # left(text, 200000): a single tsvector is capped at 1 MB, so one enormous page must not be able
    # to fail an insert (and with it an ingest). The full text is still stored untruncated.
    # The extension is stripped from the name for the same reason as in 0008 (Postgres tokenizes
    # "name.pdf" as one opaque token).
    op.execute(
        r"""
        CREATE TABLE document_pages (
            doc_id          text    NOT NULL REFERENCES document_trees (doc_id) ON DELETE CASCADE,
            page_number      integer NOT NULL,
            doc_name        text    NOT NULL DEFAULT '',
            doc_description text    NOT NULL DEFAULT '',
            text            text    NOT NULL DEFAULT '',
            text_tsv        tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english', left(coalesce(text, ''), 200000)), 'A') ||
                setweight(to_tsvector('english', coalesce(regexp_replace(doc_name, '\.[A-Za-z0-9]+$', ''), '')), 'B') ||
                setweight(to_tsvector('english', left(coalesce(doc_description, ''), 20000)), 'C')
            ) STORED,
            PRIMARY KEY (doc_id, page_number)
        )
        """
    )
    op.execute("CREATE INDEX document_pages_text_tsv_idx ON document_pages USING gin (text_tsv)")

    # Backfill every existing document from its stored pages -- plain SQL, no LLM/embedding cost.
    op.execute(
        r"""
        INSERT INTO document_pages (doc_id, page_number, doc_name, doc_description, text)
        SELECT t.doc_id,
               COALESCE((p.elem->>'page_number')::integer, p.ord::integer),
               COALESCE(t.meta->>'name', ''),
               COALESCE(t.meta->>'description', ''),
               COALESCE(p.elem->>'markdown', '')
        FROM document_trees t
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(t.pages, '[]'::jsonb)) WITH ORDINALITY AS p(elem, ord)
        ON CONFLICT (doc_id, page_number) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE document_pages")
