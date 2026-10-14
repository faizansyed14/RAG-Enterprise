"""Scale core: source registry, per-document access control, durable job queue, indexed name lookup.

Everything here is additive. Existing documents are backfilled with the '*' principal (visible to
every signed-in user), which is exactly how the system behaves today, so applying this migration
changes no behaviour until a connector starts writing restricted items.

Tables
  source_connections  one row per configured source (local folder, SMB share, SharePoint/OneDrive drive)
  connector_state     delta tokens / crawl cursors, one row per connection
  source_items        every file seen at a source: version, ACL, link to documents.document_id
  document_access     who may read a document (union of its live source_items; '*' = everyone)
  user_principals     extra principals (AD/Entra groups, UPN, SIDs) a login carries at query time
  scale_jobs          durable work queue (FOR UPDATE SKIP LOCKED), replaces in-process asyncio tasks

Also
  documents.tier      'tree' (full section tree) or 'lite' (pages + chunk index only)
  documents.doc_type  widened with 'other' so connector files with any extension can be stored
  documents.chunks_indexed_at  which documents are in the chunk index (UI uploads + the existing library are indexed by a job)
  document_trees_name_idx  expression index so a document can be found by name without scanning
                           every row (rag_core/local_api.py and agent_tools.py do that scan today)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09 00:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TYPES = ("pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer", "py", "md")
_NEW_TYPES = _OLD_TYPES + ("other",)


def upgrade() -> None:
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")
    op.create_check_constraint("documents_doc_type_check", "documents", f"doc_type IN {_NEW_TYPES!r}")
    op.execute("ALTER TABLE documents ADD COLUMN tier text NOT NULL DEFAULT 'tree'")
    op.execute("ALTER TABLE documents ADD CONSTRAINT documents_tier_check CHECK (tier IN ('tree', 'lite'))")

    op.execute(
        """
        CREATE TABLE source_connections (
            connection_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind          text NOT NULL CHECK (kind IN ('local', 'smb', 'graph')),
            name          text NOT NULL,
            config        jsonb NOT NULL DEFAULT '{}'::jsonb,
            enabled       boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE connector_state (
            connection_id     uuid PRIMARY KEY REFERENCES source_connections (connection_id) ON DELETE CASCADE,
            cursor            jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_scan_started  timestamptz,
            last_scan_finished timestamptz,
            last_error        text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE source_items (
            item_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            connection_id   uuid NOT NULL REFERENCES source_connections (connection_id) ON DELETE CASCADE,
            external_id     text NOT NULL,
            path            text NOT NULL,
            name            text NOT NULL,
            size_bytes      bigint,
            modified_at     timestamptz,
            version         text NOT NULL DEFAULT '',
            acl             text[] NOT NULL DEFAULT '{}',
            acl_synced_at   timestamptz,
            deleted         boolean NOT NULL DEFAULT false,
            document_id     uuid REFERENCES documents (document_id) ON DELETE SET NULL,
            indexed_version text,
            state           text NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'indexed', 'skipped', 'failed')),
            error           text,
            last_seen_scan  text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (connection_id, external_id)
        )
        """
    )
    op.execute("CREATE INDEX source_items_open_idx ON source_items (state) WHERE state IN ('pending', 'failed')")
    op.execute("CREATE INDEX source_items_document_idx ON source_items (document_id)")

    op.execute(
        """
        CREATE TABLE document_access (
            document_id uuid NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
            principal   text NOT NULL,
            PRIMARY KEY (document_id, principal)
        )
        """
    )
    op.execute("CREATE INDEX document_access_principal_idx ON document_access (principal)")
    # Existing documents stay visible to every signed-in user: today's behaviour, made explicit.
    op.execute("INSERT INTO document_access (document_id, principal) SELECT document_id, '*' FROM documents")

    op.execute(
        """
        CREATE TABLE user_principals (
            user_id   uuid NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
            principal text NOT NULL,
            source    text NOT NULL DEFAULT 'manual',
            synced_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, principal)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE scale_jobs (
            job_id       bigserial PRIMARY KEY,
            kind         text NOT NULL,
            payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
            dedupe_key   text,
            status       text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'done', 'failed', 'dead')),
            attempts     integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5,
            run_after    timestamptz NOT NULL DEFAULT now(),
            lease_until  timestamptz,
            locked_by    text,
            last_error   text,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX scale_jobs_ready_idx ON scale_jobs (run_after, job_id) WHERE status = 'queued'")
    op.execute("CREATE INDEX scale_jobs_lease_idx ON scale_jobs (lease_until) WHERE status = 'running'")
    # One live job per dedupe key: re-scanning a source cannot pile up duplicates of the same work.
    op.execute(
        "CREATE UNIQUE INDEX scale_jobs_dedupe_idx ON scale_jobs (dedupe_key) "
        "WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')"
    )

    # Which documents have been chunk-indexed. Documents uploaded through the UI (and everything that existed
    # before the scale layer) are indexed by a background job; this column is how that job finds the rest.
    op.execute("ALTER TABLE documents ADD COLUMN chunks_indexed_at timestamptz")
    op.execute("CREATE INDEX documents_chunks_pending_idx ON documents (created_at) WHERE chunks_indexed_at IS NULL")

    # text_pattern_ops serves both `name = 'x'` and `name LIKE 'prefix%'` whatever the database collation is.
    op.execute("CREATE INDEX document_trees_name_idx ON document_trees ((meta ->> 'name') text_pattern_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS documents_chunks_pending_idx")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS chunks_indexed_at")
    op.execute("DROP INDEX IF EXISTS document_trees_name_idx")
    op.execute("DROP TABLE IF EXISTS scale_jobs")
    op.execute("DROP TABLE IF EXISTS user_principals")
    op.execute("DROP TABLE IF EXISTS document_access")
    op.execute("DROP TABLE IF EXISTS source_items")
    op.execute("DROP TABLE IF EXISTS connector_state")
    op.execute("DROP TABLE IF EXISTS source_connections")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_tier_check")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS tier")
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")
    op.create_check_constraint("documents_doc_type_check", "documents", f"doc_type IN {_OLD_TYPES!r}")
