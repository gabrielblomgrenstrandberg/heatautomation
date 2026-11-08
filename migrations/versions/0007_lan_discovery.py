"""Persist read-only LAN discovery results."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lan_devices",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("device_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("hostname", sa.String(256)),
        sa.Column("mac_address", sa.String(32), nullable=False),
        sa.Column("connection_method", sa.String(32), nullable=False),
        sa.Column("connection_state", sa.String(32), nullable=False),
        sa.Column("last_connection", sa.String(40)),
        sa.Column("last_measurement", sa.String(40)),
        sa.Column("last_discovery", sa.String(40)),
        sa.Column("last_error", sa.String(256)),
        sa.Column("capabilities_json", sa.Text, nullable=False),
        sa.Column("measurements_json", sa.Text, nullable=False),
    )
    op.create_table(
        "lan_scan_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.String(40), nullable=False),
        sa.Column("finished_at", sa.String(40)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("error", sa.String(256)),
        sa.Column("candidate_count", sa.Integer),
        sa.Column("verified_count", sa.Integer),
    )
    op.create_table(
        "lan_scan_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("requested_at", sa.String(40), nullable=False),
        sa.Column("handled_at", sa.String(40)),
    )
    op.create_index("ix_lan_scan_one_running", "lan_scan_runs", ["state"],
                    unique=True, sqlite_where=sa.text("state = 'RUNNING'"))
    op.create_table(
        "lan_probe_errors",
        sa.Column("device_id", sa.String(80), primary_key=True),
        sa.Column("checked_at", sa.String(40), nullable=False),
        sa.Column("error", sa.String(256), nullable=False),
    )


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
