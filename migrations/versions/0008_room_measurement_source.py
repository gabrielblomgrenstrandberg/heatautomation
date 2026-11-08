"""Record integration and transport for room observations."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("room_measurements", sa.Column("source_integration", sa.String(64)))
    op.add_column("room_measurements", sa.Column("connection_method", sa.String(32)))
    op.add_column("room_measurements", sa.Column("source_ref", sa.String(128)))


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
