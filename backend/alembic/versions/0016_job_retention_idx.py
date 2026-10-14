"""index for pruning finished queue jobs

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS scale_jobs_finished_idx ON scale_jobs (status, updated_at) "
        "WHERE status IN ('done', 'dead')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS scale_jobs_finished_idx")
