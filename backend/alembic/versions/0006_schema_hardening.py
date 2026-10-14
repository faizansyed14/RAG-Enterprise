"""schema hardening: timezone-aware timestamps, cascading diagram_pages
delete, a sort index for the documents list, and CHECK constraints on
the enum-like text columns (doc_type, status, role, outcome).

The container's Postgres runs with TimeZone=Etc/UTC (confirmed via
`SHOW timezone` before writing this), so every existing naive
`documents.created_at` / `folders.created_at` value already IS a UTC
instant -- converting with `AT TIME ZONE 'UTC'` reinterprets the same
stored value as timezone-aware without shifting it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25 00:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOC_TYPES = ("pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer")
_DOC_STATUSES = ("queued", "extracting", "ocr", "indexing", "indexed", "failed")


def upgrade() -> None:
    # 1. Timezone-aware timestamps, matching users/usage_events/rate_limits.
    op.execute("ALTER TABLE documents ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC'")
    op.execute("ALTER TABLE folders ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC'")

    # 2. diagram_pages should cascade like every other document-owned row
    # (document_folders already does). The app already deletes these rows
    # itself before deleting a document, so this changes nothing about
    # today's behavior -- it just stops relying on that being the only path.
    op.drop_constraint("diagram_pages_document_id_fkey", "diagram_pages", type_="foreignkey")
    op.create_foreign_key(
        "diagram_pages_document_id_fkey", "diagram_pages", "documents",
        ["document_id"], ["document_id"], ondelete="CASCADE",
    )

    # 3. The documents list is always sorted newest-first.
    op.create_index("documents_created_at_idx", "documents", [sa.text("created_at DESC")])

    # 4. CHECK constraints on the enum-like text columns. Every existing
    # value was confirmed in-range before writing this migration.
    op.create_check_constraint("documents_doc_type_check", "documents", f"doc_type IN {_DOC_TYPES!r}")
    op.create_check_constraint("documents_status_check", "documents", f"status IN {_DOC_STATUSES!r}")
    op.create_check_constraint("users_role_check", "users", "role IN ('admin', 'user')")
    op.create_check_constraint("usage_events_outcome_check", "usage_events", "outcome IN ('charged', 'refunded')")


def downgrade() -> None:
    op.drop_constraint("usage_events_outcome_check", "usage_events", type_="check")
    op.drop_constraint("users_role_check", "users", type_="check")
    op.drop_constraint("documents_status_check", "documents", type_="check")
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")

    op.drop_index("documents_created_at_idx", table_name="documents")

    op.drop_constraint("diagram_pages_document_id_fkey", "diagram_pages", type_="foreignkey")
    op.create_foreign_key(
        "diagram_pages_document_id_fkey", "diagram_pages", "documents", ["document_id"], ["document_id"]
    )

    op.execute("ALTER TABLE folders ALTER COLUMN created_at TYPE timestamp USING created_at AT TIME ZONE 'UTC'")
    op.execute("ALTER TABLE documents ALTER COLUMN created_at TYPE timestamp USING created_at AT TIME ZONE 'UTC'")
