"""SQLite storage for thermal observations and model evaluations."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, create_engine, inspect, select)


metadata = MetaData()
observations = Table(
    "thermal_observations", metadata,
    Column("id", Integer, primary_key=True),
    Column("timestamp", String(32), nullable=False, unique=True),
)
sensors = Table(
    "thermal_sensors", metadata,
    Column("observation_id", Integer, ForeignKey("thermal_observations.id"), primary_key=True),
    Column("name", String(64), primary_key=True),
    Column("value", Float),
)
sources = Table(
    "thermal_sources", metadata,
    Column("observation_id", Integer, ForeignKey("thermal_observations.id"), primary_key=True),
    Column("name", String(64), primary_key=True),
    Column("is_on", Boolean),
    Column("mode", String(128)),
    Column("capacity_kw", Float),
)
evaluations = Table(
    "thermal_evaluations", metadata,
    Column("id", Integer, primary_key=True),
    Column("timestamp", String(32), nullable=False),
    Column("metrics", Text, nullable=False),
)
plans = Table(
    "mpc_plans", metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", String(32), nullable=False),
    Column("model_created_at", String(32), nullable=False),
    Column("initial_indoor_c", Float, nullable=False),
    Column("settings_json", Text, nullable=False),
    Column("changes_json", Text, nullable=False),
)
plan_intervals = Table(
    "mpc_plan_intervals", metadata,
    Column("plan_id", Integer, ForeignKey("mpc_plans.id"), primary_key=True),
    Column("start", String(32), primary_key=True),
    Column("data_json", Text, nullable=False),
)
plan_evaluations = Table(
    "mpc_plan_evaluations", metadata,
    Column("plan_id", Integer, ForeignKey("mpc_plans.id"), primary_key=True),
    Column("updated_at", String(32), nullable=False),
    Column("metrics_json", Text, nullable=False),
)


class ThermalDatabase:
    def __init__(self, path):
        self.engine = create_engine("sqlite:///" + str(path))
        existing_tables = set(inspect(self.engine).get_table_names())
        missing_tables = set(metadata.tables) - existing_tables
        if missing_tables:
            self.engine.dispose()
            raise RuntimeError(
                "Thermal database is missing legacy tables %s. Run "
                "'python -m heatautomation.house.migrate --database <path>' "
                "before starting the application." % ", ".join(sorted(missing_tables))
            )

    def save(self, timestamp, sensor_values, source_values):
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Observation timestamp must include a timezone")
        timestamp = timestamp.astimezone(timezone.utc).isoformat()
        with self.engine.begin() as connection:
            result = connection.execute(observations.insert().values(timestamp=timestamp))
            observation_id = result.inserted_primary_key[0]
            if sensor_values:
                connection.execute(sensors.insert(), [
                    {"observation_id": observation_id, "name": name, "value": value}
                    for name, value in sensor_values.items()
                ])
            if source_values:
                connection.execute(sources.insert(), [
                    dict(values, observation_id=observation_id, name=name)
                    for name, values in source_values.items()
                ])

    def history(self):
        with self.engine.connect() as connection:
            records = {
                row.id: {"timestamp": row.timestamp}
                for row in connection.execute(select(observations).order_by(observations.c.timestamp))
            }
            for row in connection.execute(select(sensors)):
                records[row.observation_id][row.name] = row.value
            for row in connection.execute(select(sources)):
                for field in ("is_on", "mode", "capacity_kw"):
                    records[row.observation_id][row.name + "_" + field] = getattr(row, field)
        return list(records.values())

    def save_evaluation(self, metrics):
        with self.engine.begin() as connection:
            connection.execute(evaluations.insert().values(
                timestamp=datetime.now(timezone.utc).isoformat(),
                metrics=json.dumps(metrics, allow_nan=False),
            ))

    def save_plan(self, created_at, model_created_at, initial_indoor_c, settings, steps):
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("Planning timestamp must include a timezone")
        if not steps:
            raise ValueError("Cannot save an empty plan")
        with self.engine.begin() as connection:
            previous_id = connection.execute(
                select(plans.c.id).order_by(plans.c.id.desc()).limit(1)
            ).scalar()
            previous = {}
            if previous_id is not None:
                previous = {row.start: json.loads(row.data_json) for row in connection.execute(
                    select(plan_intervals).where(plan_intervals.c.plan_id == previous_id)
                )}
            changes = [
                {"start": step["start"], "previous_mode": previous[step["start"]]["mode"],
                 "mode": step["mode"]}
                for step in steps if step["start"] in previous
                and step["mode"] != previous[step["start"]]["mode"]
            ]
            result = connection.execute(plans.insert().values(
                created_at=created_at.astimezone(timezone.utc).isoformat(),
                model_created_at=model_created_at, initial_indoor_c=initial_indoor_c,
                settings_json=json.dumps(settings, allow_nan=False),
                changes_json=json.dumps(changes, allow_nan=False),
            ))
            plan_id = result.inserted_primary_key[0]
            connection.execute(plan_intervals.insert(), [
                {"plan_id": plan_id, "start": step["start"],
                 "data_json": json.dumps(step, allow_nan=False)}
                for step in steps
            ])
        return plan_id, changes

    def evaluate_plans(self, evaluate):
        history = self.history()
        if not history:
            return
        latest = datetime.fromisoformat(history[-1]["timestamp"])
        cutoff = (latest - timedelta(hours=37)).isoformat()
        with self.engine.begin() as connection:
            for plan in connection.execute(select(plans).where(
                    plans.c.created_at >= cutoff)).fetchall():
                from .profile import compatible
                plan_settings = json.loads(plan.settings_json)
                plan_profile = plan_settings.get("thermal_profile", {
                    "indoor_source": "sensibo", "tado_room_id": "",
                    "tado_zone": "",
                    "heatpump_absent": False, "pelletstove_absent": False,
                    "other_heating_absent": False,
                })
                steps = [json.loads(row.data_json) for row in connection.execute(
                    select(plan_intervals).where(plan_intervals.c.plan_id == plan.id)
                    .order_by(plan_intervals.c.start)
                )]
                score = evaluate(steps, [row for row in history if compatible(row, plan_profile)])
                if not score:
                    continue
                values = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "metrics_json": json.dumps(score, allow_nan=False),
                }
                existing = connection.execute(
                    select(plan_evaluations.c.plan_id).where(
                        plan_evaluations.c.plan_id == plan.id)
                ).scalar()
                if existing is None:
                    connection.execute(plan_evaluations.insert().values(plan_id=plan.id, **values))
                else:
                    connection.execute(plan_evaluations.update().where(
                        plan_evaluations.c.plan_id == plan.id).values(**values))
