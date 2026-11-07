"""SQLite storage for thermal observations and model evaluations."""

import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, Float, ForeignKey, Integer, MetaData,
                        String, Table, Text, create_engine, select)


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


class ThermalDatabase:
    def __init__(self, path):
        self.engine = create_engine("sqlite:///" + str(path))
        metadata.create_all(self.engine)

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
