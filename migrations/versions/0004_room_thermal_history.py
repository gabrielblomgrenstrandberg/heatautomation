"""Store prediction-only room model inputs and validation results."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "room_thermal_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, unique=True),
        sa.Column("data_json", sa.Text, nullable=False),
    )
    op.create_table(
        "room_thermal_predictions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_seconds", sa.Integer, nullable=False),
        sa.Column("predicted_c", sa.Float, nullable=False),
        sa.Column("measured_c", sa.Float),
        sa.Column("measured_at", sa.DateTime(timezone=True)),
        sa.Column("measurement_id", sa.Integer,
                  sa.ForeignKey("room_measurements.id", ondelete="RESTRICT")),
    )
    op.create_index("ix_room_thermal_predictions_room_target", "room_thermal_predictions",
                    ["room_id", "target_at"])


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
