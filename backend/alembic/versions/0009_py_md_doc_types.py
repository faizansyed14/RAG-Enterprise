"""Widen documents_doc_type_check to allow "py" and "md" -- see
app/ingestion/python_ingest.py and markdown_ingest.py.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29 00:00:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TYPES = ("pdf", "docx", "csv", "xlsx", "eml", "txt", "json", "xer")
_NEW_TYPES = _OLD_TYPES + ("py", "md")


def upgrade() -> None:
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")
    op.create_check_constraint("documents_doc_type_check", "documents", f"doc_type IN {_NEW_TYPES!r}")


def downgrade() -> None:
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")
    op.create_check_constraint("documents_doc_type_check", "documents", f"doc_type IN {_OLD_TYPES!r}")
