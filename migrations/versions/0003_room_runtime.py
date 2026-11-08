"""Add room settings, measurements, runtime states and optimiser history."""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "room_settings",
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("comfort_profile", sa.String(64), nullable=False,
                  server_default="default"),
        sa.Column("min_c", sa.Float),
        sa.Column("max_c", sa.Float),
        sa.Column("target_c", sa.Float),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "room_overrides",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_c", sa.Float, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.CheckConstraint("expires_at > starts_at", name="ck_room_overrides_expiry"),
    )
    op.create_table(
        "room_schedules",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("weekday", sa.Integer, nullable=False),
        sa.Column("start_min", sa.Integer, nullable=False),
        sa.Column("end_min", sa.Integer, nullable=False),
        sa.Column("target_c", sa.Float, nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_room_schedules_weekday"),
        sa.CheckConstraint(
            "start_min BETWEEN 0 AND 1439 AND end_min BETWEEN 1 AND 1440",
            name="ck_room_schedules_time",
        ),
    )
    op.create_table(
        "room_measurements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.String(32), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_device_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT")),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "quality IN ('good', 'suspect', 'stale', 'bad')",
            name="ck_room_measurements_quality",
        ),
        sa.CheckConstraint(
            "classification IN ('measured', 'estimated', 'predicted')",
            name="ck_room_measurements_classification",
        ),
    )
    op.create_index(
        "ix_room_measurements_room_time",
        "room_measurements",
        ["room_id", "quantity", "observed_at"],
    )
    op.create_table(
        "device_runtime_state",
        sa.Column("device_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("reported_power", sa.String(16)),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.Column("commanded_power", sa.String(16)),
        sa.Column("commanded_at", sa.DateTime(timezone=True)),
        sa.Column("connectivity", sa.String(16), nullable=False,
                  server_default="unknown"),
    )
    op.create_table(
        "command_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("command", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text),
        sa.CheckConstraint(
            "outcome IN ('simulated', 'sent', 'rejected', 'failed', 'timeout')",
            name="ck_command_log_outcome",
        ),
    )
    op.create_table(
        "optimizer_plans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("details_json", sa.Text, nullable=False),
    )
    op.create_table(
        "optimizer_plan_intervals",
        sa.Column("plan_id", sa.Integer,
                  sa.ForeignKey("optimizer_plans.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_json", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("plan_id", "room_id", "starts_at"),
    )


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
