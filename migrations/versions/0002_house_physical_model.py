"""Add physical floors, rooms, devices, heat paths and thermal priors."""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "houses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("config_hash", sa.String(64)),
        sa.Column("config_loaded_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "floors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("house_id", sa.String(36),
                  sa.ForeignKey("houses.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("level", sa.Integer),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "rooms",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("floor_id", sa.String(36),
                  sa.ForeignKey("floors.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT")),
        sa.Column("adapter", sa.String(64)),
        sa.Column("external_id", sa.String(256)),
        sa.Column("rated_kw", sa.Float),
        sa.Column("rated_w_at_dt50", sa.Float),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("controlled_by", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT")),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "device_capabilities",
        sa.Column("device_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("device_id", "capability"),
    )
    op.create_table(
        "heat_paths",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("generator_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("path_type", sa.String(16), nullable=False),
        sa.Column("target_room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT")),
        sa.Column("distribution", sa.String(64)),
        sa.Column("provenance", sa.String(32)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("path_type IN ('circuit', 'direct')",
                           name="ck_heat_paths_type"),
        sa.CheckConstraint(
            "(path_type = 'direct' AND target_room_id IS NOT NULL) OR "
            "(path_type = 'circuit' AND target_room_id IS NULL)",
            name="ck_heat_paths_target",
        ),
    )
    op.create_table(
        "heat_path_emitters",
        sa.Column("heat_path_id", sa.String(128),
                  sa.ForeignKey("heat_paths.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("emitter_id", sa.String(36),
                  sa.ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False),
        sa.PrimaryKeyConstraint("heat_path_id", "emitter_id"),
    )
    op.create_table(
        "thermal_links",
        sa.Column("room_a_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("room_b_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("conductance_w_per_k", sa.Float, nullable=False),
        sa.Column("prior_conductance_w_per_k", sa.Float, nullable=False),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("room_a_id", "room_b_id"),
        sa.CheckConstraint("room_a_id < room_b_id", name="ck_thermal_links_order"),
        sa.CheckConstraint("conductance_w_per_k > 0", name="ck_thermal_links_value"),
    )
    op.create_table(
        "thermal_parameters",
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("prior_value", sa.Float, nullable=False),
        sa.Column("sample_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("covariance", sa.Float),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("room_id", "name"),
    )
    op.create_table(
        "thermal_parameter_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("room_id", sa.String(36),
                  sa.ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Float),
        sa.Column("new_value", sa.Float),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
