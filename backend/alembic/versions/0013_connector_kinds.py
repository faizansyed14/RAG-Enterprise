"""Allow every registered connector kind in source_connections.

0012 limited `kind` to local / smb / graph with a CHECK. The registry (app/scale/connectors/__init__.py) now holds
more than two dozen kinds, so the constraint becomes a shape check; the application validates the kind against the
registry before it stores a connection.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE source_connections DROP CONSTRAINT IF EXISTS source_connections_kind_check")
    op.execute("ALTER TABLE source_connections ADD CONSTRAINT source_connections_kind_check CHECK (kind ~ '^[a-z][a-z0-9_]{1,40}$')")


def downgrade() -> None:
    op.execute("DELETE FROM source_connections WHERE kind NOT IN ('local', 'smb', 'graph')")
    op.execute("ALTER TABLE source_connections DROP CONSTRAINT IF EXISTS source_connections_kind_check")
    op.execute("ALTER TABLE source_connections ADD CONSTRAINT source_connections_kind_check CHECK (kind IN ('local', 'smb', 'graph'))")
