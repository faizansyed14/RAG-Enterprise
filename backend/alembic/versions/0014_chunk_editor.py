"""Chunk editor.

chunk_edits      what people changed in the chunk editor: corrected text, extra keywords / questions, a chunk switched
                 off, a chunk added by hand. An edit follows its chunk by a hash of the chunk's ORIGINAL text, not by its
                 position, so it survives a re-index of a file that changed elsewhere.
chunk_edit_log   who changed what, when, with the before and after.

Every table that holds document content cascades on document delete, so removing a document removes its edits and
its history together. Access control is not stored here: it is read from document_access at query time.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chunk_edits (
            edit_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
            kind        text NOT NULL CHECK (kind IN ('edit', 'manual')),
            chunk       integer NOT NULL,
            occurrence  integer NOT NULL DEFAULT 0,
            orig_hash   text,
            orig_text   text,
            page        integer,
            text        text,
            keywords    text[] NOT NULL DEFAULT '{}',
            questions   text[] NOT NULL DEFAULT '{}',
            available   boolean NOT NULL DEFAULT true,
            orphaned    boolean NOT NULL DEFAULT false,
            created_by  text,
            updated_by  text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            CHECK ((kind = 'edit' AND orig_hash IS NOT NULL) OR (kind = 'manual' AND text IS NOT NULL))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX chunk_edits_target ON chunk_edits (document_id, orig_hash, occurrence) WHERE kind = 'edit'")
    op.execute("CREATE UNIQUE INDEX chunk_edits_manual ON chunk_edits (document_id, chunk) WHERE kind = 'manual'")
    op.execute("CREATE INDEX chunk_edits_doc ON chunk_edits (document_id)")
    op.execute(
        """
        CREATE TABLE chunk_edit_log (
            log_id      bigserial PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
            chunk       integer,
            action      text NOT NULL,
            actor       text,
            before      jsonb,
            after       jsonb,
            at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX chunk_edit_log_doc ON chunk_edit_log (document_id, at DESC)")


def downgrade() -> None:
    for table in ("chunk_edit_log", "chunk_edits"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
