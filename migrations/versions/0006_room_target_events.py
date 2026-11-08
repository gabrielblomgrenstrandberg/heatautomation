"""Record explicit room base-target changes without inventing past settings."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "room_target_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36), sa.ForeignKey("rooms.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("target_c", sa.Float, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
    )
    op.create_index("ix_room_target_events_room_time", "room_target_events", ["room_id", "recorded_at"])


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
