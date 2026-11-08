"""Add room-specific maximum preheating to runtime settings."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("room_settings", sa.Column(
        "max_preheat_c", sa.Float, sa.CheckConstraint(
            "max_preheat_c >= 0 AND max_preheat_c <= 2",
            name="ck_room_settings_preheat"), nullable=False, server_default="2"))


def downgrade():
    raise NotImplementedError("Multi-room migrations are intentionally additive")
