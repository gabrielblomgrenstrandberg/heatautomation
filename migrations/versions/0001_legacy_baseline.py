"""Baseline the existing single-zone schema without replacing historical tables."""

from alembic import op
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    inspect,
)


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _create_if_missing(name, *columns):
    inspector = inspect(op.get_bind())
    if name not in inspector.get_table_names():
        op.create_table(name, *columns)


def upgrade():
    expected_columns = {
        "thermal_observations": {"id", "timestamp"},
        "thermal_sensors": {"observation_id", "name", "value"},
        "thermal_sources": {"observation_id", "name", "is_on", "mode", "capacity_kw"},
        "thermal_evaluations": {"id", "timestamp", "metrics"},
        "mpc_plans": {
            "id", "created_at", "model_created_at", "initial_indoor_c",
            "settings_json", "changes_json",
        },
        "mpc_plan_intervals": {"plan_id", "start", "data_json"},
        "mpc_plan_evaluations": {"plan_id", "updated_at", "metrics_json"},
    }
    inspector = inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for name in existing.intersection(expected_columns):
        actual = {column["name"] for column in inspector.get_columns(name)}
        if not expected_columns[name].issubset(actual):
            raise RuntimeError(
                "Legacy table %s does not match the supported baseline; "
                "migration stopped without altering it" % name
            )

    _create_if_missing("thermal_observations",
        Column("id", Integer, primary_key=True),
        Column("timestamp", String(32), nullable=False, unique=True),
    )
    _create_if_missing("thermal_sensors",
        Column("observation_id", Integer, ForeignKey("thermal_observations.id"),
               primary_key=True),
        Column("name", String(64), primary_key=True),
        Column("value", Float),
    )
    _create_if_missing("thermal_sources",
        Column("observation_id", Integer, ForeignKey("thermal_observations.id"),
               primary_key=True),
        Column("name", String(64), primary_key=True),
        Column("is_on", Boolean),
        Column("mode", String(128)),
        Column("capacity_kw", Float),
    )
    _create_if_missing("thermal_evaluations",
        Column("id", Integer, primary_key=True),
        Column("timestamp", String(32), nullable=False),
        Column("metrics", Text, nullable=False),
    )
    _create_if_missing("mpc_plans",
        Column("id", Integer, primary_key=True),
        Column("created_at", String(32), nullable=False),
        Column("model_created_at", String(32), nullable=False),
        Column("initial_indoor_c", Float, nullable=False),
        Column("settings_json", Text, nullable=False),
        Column("changes_json", Text, nullable=False),
    )
    _create_if_missing("mpc_plan_intervals",
        Column("plan_id", Integer, ForeignKey("mpc_plans.id"), primary_key=True),
        Column("start", String(32), primary_key=True),
        Column("data_json", Text, nullable=False),
    )
    _create_if_missing("mpc_plan_evaluations",
        Column("plan_id", Integer, ForeignKey("mpc_plans.id"), primary_key=True),
        Column("updated_at", String(32), nullable=False),
        Column("metrics_json", Text, nullable=False),
    )


def downgrade():
    raise NotImplementedError("The legacy baseline is intentionally irreversible")
