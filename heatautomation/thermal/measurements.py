"""Timestamped, read-only room measurements and quality handling."""

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import insert, select

from heatautomation.house import models


class Quality(str, Enum):
    GOOD = "good"
    SUSPECT = "suspect"
    STALE = "stale"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class Classification(str, Enum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    PREDICTED = "predicted"


@dataclass(frozen=True)
class Measurement:
    id: Optional[int]
    device_id: Optional[str]
    room_id: str
    quantity: str
    value: Optional[float]
    unit: str
    observed_at: Optional[datetime]
    received_at: datetime
    source_integration: str
    connection_method: str
    quality: Quality
    classification: Classification
    source_ref: Optional[str] = None

    def at(self, now, freshness_seconds):
        if now.utcoffset() is None or self.received_at.utcoffset() is None:
            raise ValueError("Measurement times must include timezones")
        if not math.isfinite(freshness_seconds) or freshness_seconds <= 0:
            raise ValueError("Freshness must be positive and finite")
        if self.received_at > now:
            return replace(self, quality=Quality.INVALID)
        if self.value is None or self.observed_at is None:
            return replace(self, quality=Quality.UNKNOWN)
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            return replace(self, quality=Quality.INVALID)
        if self.observed_at.utcoffset() is None:
            return replace(self, quality=Quality.INVALID)
        age = (now - self.observed_at).total_seconds()
        if age < 0:
            return replace(self, quality=Quality.INVALID)
        if self.quality in (Quality.INVALID, Quality.UNKNOWN, Quality.STALE):
            return self
        if age > freshness_seconds:
            return replace(self, quality=Quality.STALE)
        return self


def _time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def tado_measurements(status, *, now, freshness_seconds=600):
    """Translate Tado zone values without attributing them to a physical sensor."""
    room = str(status["room_id"])
    home, zone = status["home_id"], status["zone_id"]
    if type(home) is not int or type(zone) is not int or home <= 0 or zone <= 0:
        raise ValueError("Tado home and zone IDs must be positive integers")
    source_ref = "tado:{}:{}".format(home, zone)
    reading = status["temperature_reading"]
    received = _time(reading.get("received_at"))
    if received is None:
        raise ValueError("Tado receipt timestamp is unavailable")
    quality = {"bad": Quality.INVALID, "stale": Quality.STALE,
               "suspect": Quality.SUSPECT, "good": Quality.GOOD}.get(
                   reading.get("quality"), Quality.UNKNOWN)
    temperature = Measurement(
        None, None, room, "temperature", reading.get("value"), "C",
        _time(reading.get("timestamp")), received, "tado", "CLOUD",
        quality, Classification.MEASURED, source_ref,
    )
    demand = Measurement(
        None, None, room, "heating_demand", status.get("heating_demand_percent"), "%",
        _time(status.get("heating_demand_observed_at")), received, "tado", "CLOUD",
        Quality.STALE if status.get("connection") == "OFFLINE" else Quality.SUSPECT,
        Classification.MEASURED, source_ref,
    )
    target = Measurement(
        None, None, room, "target_temperature", status.get("requested_temperature_c"), "C",
        None, received, "tado", "CLOUD", Quality.UNKNOWN, Classification.MEASURED, source_ref,
    )
    return tuple(value.at(now, freshness_seconds) for value in (temperature, target, demand))


class MeasurementStore:
    """Store usable observations in the existing room measurement table."""

    def __init__(self, engine):
        self.engine = engine

    def save(self, measurement):
        if measurement.quality not in (Quality.GOOD, Quality.SUSPECT):
            raise ValueError("Only usable observations can be saved")
        checked = measurement.at(measurement.received_at, 3153600000)
        if checked.quality not in (Quality.GOOD, Quality.SUSPECT):
            raise ValueError("Measurement needs a finite value and valid observation time")
        with self.engine.begin() as conn:
            existing = conn.execute(select(models.room_measurements).where(
                (models.room_measurements.c.room_id == measurement.room_id)
                & (models.room_measurements.c.quantity == measurement.quantity)
                & (models.room_measurements.c.source_integration == measurement.source_integration)
                & (models.room_measurements.c.source_ref == measurement.source_ref)
                & (models.room_measurements.c.source_device_id == measurement.device_id)
                & (models.room_measurements.c.observed_at == measurement.observed_at.astimezone(timezone.utc))
            )).first()
            if existing is not None:
                if existing.value != measurement.value or existing.unit != measurement.unit:
                    raise ValueError("Conflicting observation at the same timestamp")
                return replace(measurement, id=existing.id)
            result = conn.execute(insert(models.room_measurements).values(
                room_id=measurement.room_id, quantity=measurement.quantity, value=measurement.value,
                unit=measurement.unit, observed_at=measurement.observed_at.astimezone(timezone.utc),
                received_at=measurement.received_at.astimezone(timezone.utc),
                source_device_id=measurement.device_id, quality=measurement.quality.value,
                classification=measurement.classification.value,
                source_integration=measurement.source_integration,
                connection_method=measurement.connection_method,
                source_ref=measurement.source_ref,
            ))
        return replace(measurement, id=result.inserted_primary_key[0])

    def latest(self, room_id, quantity, *, now, freshness_seconds=600,
               source_integration=None, source_ref=None):
        if now.utcoffset() is None:
            raise ValueError("Measurement query time must include a timezone")
        now = now.astimezone(timezone.utc)
        query = select(models.room_measurements).where(
            (models.room_measurements.c.room_id == room_id)
            & (models.room_measurements.c.quantity == quantity)
            & (models.room_measurements.c.observed_at <= now)
            & (models.room_measurements.c.received_at <= now)
            & models.room_measurements.c.quality.in_(("good", "suspect")))
        if source_integration is not None:
            query = query.where(models.room_measurements.c.source_integration == source_integration)
        if source_ref is not None:
            query = query.where(models.room_measurements.c.source_ref == source_ref)
        with self.engine.connect() as conn:
            row = conn.execute(query.order_by(models.room_measurements.c.observed_at.desc(),
                       models.room_measurements.c.id.desc()).limit(1)).first()
        if row is None:
            return None
        def utc(value):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return Measurement(
            row.id, row.source_device_id, row.room_id, row.quantity, row.value, row.unit,
            utc(row.observed_at), utc(row.received_at), row.source_integration or "unknown",
            row.connection_method or "UNKNOWN", Quality(row.quality),
            Classification(row.classification), row.source_ref,
        ).at(now, freshness_seconds)


def collect_tado_room(service, store, room_id, *, now=None, freshness_seconds=600):
    """Fetch one mapped zone and persist only current, valid observations."""
    status = service.get_room_status(room_id)
    if str(status.get("room_id")) != str(room_id):
        raise ValueError("Tado returned a different room")
    if now is None:
        now = datetime.now(timezone.utc)
    values = tado_measurements(status, now=now, freshness_seconds=freshness_seconds)
    return tuple(store.save(value) if value.quality in (Quality.GOOD, Quality.SUSPECT)
                 else value for value in values)
