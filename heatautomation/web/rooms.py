"""Room monitoring and runtime settings; never dispatch heating commands."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from heatautomation.house import models
from .status import StatusUnavailable


def _engine(path, readonly=True):
    path = Path(path).resolve()
    return create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=2
        ),
    )


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat() if value is not None else None


def _load(database_path, callback, readonly=True):
    engine = _engine(database_path, readonly)
    try:
        with engine.begin() as conn:
            return callback(conn)
    except (SQLAlchemyError, sqlite3.Error) as error:
        raise StatusUnavailable("Room database is unavailable or needs migration.") from error
    finally:
        engine.dispose()


def read_house(database_path):
    def query(conn):
        floors = [dict(row._mapping) for row in conn.execute(
            select(models.floors).where(models.floors.c.removed_at.is_(None))
            .order_by(models.floors.c.level.desc(), models.floors.c.name))]
        rooms = conn.execute(select(models.rooms).where(models.rooms.c.removed_at.is_(None))).all()
        settings = {r.room_id: r for r in conn.execute(select(models.room_settings))}
        scheduled_rooms = set(conn.execute(select(models.room_schedules.c.room_id)).scalars())
        devices = conn.execute(select(models.devices).where(models.devices.c.removed_at.is_(None))).all()
        runtime = {r.device_id: r for r in conn.execute(select(models.device_runtime_state))}
        links = conn.execute(select(models.thermal_links).where(models.thermal_links.c.removed_at.is_(None))).all()
        paths = conn.execute(select(models.heat_paths).where(models.heat_paths.c.removed_at.is_(None))).all()
        emitters = conn.execute(select(models.heat_path_emitters)).all()
        now = datetime.now(timezone.utc)
        overrides = conn.execute(select(models.room_overrides).where(
            (models.room_overrides.c.starts_at <= now)
            & (models.room_overrides.c.expires_at > now)).order_by(
                models.room_overrides.c.starts_at.desc())).all()
        by_id = {d.id: d for d in devices}
        result = []
        for room in rooms:
            setting = settings.get(room.id)
            measurement = conn.execute(select(models.room_measurements).where(
                (models.room_measurements.c.room_id == room.id)
                & (models.room_measurements.c.quantity == "temperature")
                & (models.room_measurements.c.unit == "C")
                & (models.room_measurements.c.classification == "measured")
            ).order_by(models.room_measurements.c.observed_at.desc()).limit(1)).first()
            fresh = bool(measurement and measurement.quality == "good"
                         and 0 <= (now - _utc(measurement.observed_at)).total_seconds() <= 900)
            override = next((o for o in overrides if o.room_id == room.id
                             and _utc(o.starts_at) <= now < _utc(o.expires_at)), None)
            target = (override.target_c if override else
                      setting.target_c if setting and room.id not in scheduled_rooms else None)
            predicted = conn.execute(select(models.room_thermal_predictions).where(
                (models.room_thermal_predictions.c.room_id == room.id)
                & (models.room_thermal_predictions.c.created_at <= now)
                & (models.room_thermal_predictions.c.created_at >= now - timedelta(minutes=15))
                & (models.room_thermal_predictions.c.target_at > now)
            ).order_by(models.room_thermal_predictions.c.created_at.desc(),
                       models.room_thermal_predictions.c.target_at.asc()).limit(1)).first()
            room_devices = [d for d in devices if d.room_id == room.id]
            assignments = []
            for path in paths:
                assigned = path.target_room_id == room.id or any(
                    item.heat_path_id == path.id and item.emitter_id in by_id
                    and by_id[item.emitter_id].room_id == room.id for item in emitters)
                if assigned and path.generator_id in by_id:
                    generator = by_id[path.generator_id]
                    assignments.append({"id": generator.id, "name": generator.name,
                                        "type": path.path_type})
            state = "unknown"
            if fresh and target is not None:
                state = "cold" if measurement.value < target - 0.5 else (
                    "warm" if measurement.value > target + 0.5 else "comfortable")
            result.append({
                "id": room.id, "floor_id": room.floor_id, "name": room.name,
                "temperature_c": measurement.value if fresh else None,
                "temperature_observed_at": _iso(measurement.observed_at) if measurement else None,
                "temperature_quality": measurement.quality if measurement else "unknown",
                "temperature_stale": bool(measurement and not fresh),
                "target_c": target, "base_target_c": setting.target_c if setting else None,
                "min_c": setting.min_c if setting else None,
                "max_c": setting.max_c if setting else None,
                "max_preheat_c": setting.max_preheat_c if setting else None,
                "override_expires_at": _iso(override.expires_at) if override else None,
                "comfort": state, "predicted_c": predicted.predicted_c if predicted else None,
                "prediction_at": _iso(predicted.target_at) if predicted else None,
                "heating_demand_kw": None, "active_sources": None, "heating_activity": None,
                "devices": [{"id": d.id, "name": d.name, "adapter": d.adapter,
                             "external_id": d.external_id,
                             "connectivity": runtime[d.id].connectivity
                             if d.id in runtime and runtime[d.id].reported_at is not None
                             and 0 <= (now - _utc(runtime[d.id].reported_at)).total_seconds() <= 900
                             else "unknown"}
                            for d in room_devices],
                "sources": assignments,
            })
        return {
            "floors": [{"id": f["id"], "name": f["name"], "level": f["level"]} for f in floors],
            "rooms": result,
            "thermal_links": [{"room_a_id": l.room_a_id, "room_b_id": l.room_b_id,
                               "conductance_w_per_k": l.conductance_w_per_k} for l in links],
            "message": "Measurements and plans are observations or estimates, not verified heat delivery.",
        }
    return _load(database_path, query)


def read_history(database_path, room_id):
    def query(conn):
        exists = conn.execute(select(models.rooms.c.id).where(
            (models.rooms.c.id == room_id) & models.rooms.c.removed_at.is_(None))).first()
        if not exists:
            raise ValueError("Room not found")
        rows = conn.execute(select(models.room_measurements).where(
            (models.room_measurements.c.room_id == room_id)
            & (models.room_measurements.c.quantity == "temperature")
            & (models.room_measurements.c.unit == "C")
        ).order_by(models.room_measurements.c.observed_at.desc()).limit(500)).all()
        forecasts = conn.execute(select(models.room_thermal_predictions).where(
            models.room_thermal_predictions.c.room_id == room_id).order_by(
            models.room_thermal_predictions.c.created_at.desc()).limit(500)).all()
        snapshots = conn.execute(select(models.room_thermal_snapshots).order_by(
            models.room_thermal_snapshots.c.observed_at.desc()).limit(500)).all()
        targets = conn.execute(select(models.room_target_events).where(
            models.room_target_events.c.room_id == room_id).order_by(
                models.room_target_events.c.recorded_at.desc()).limit(500)).all()
        heat_inputs = []
        for snapshot in reversed(snapshots):
            data = json.loads(snapshot.data_json)
            for source_id, reading in data.get("heat_inputs", {}).get(room_id, {}).items():
                heat_inputs.append({"at": _iso(snapshot.observed_at), "source_id": source_id,
                                    "watts": reading["watts"],
                                    "classification": reading["classification"]})
        return {
            "temperatures": [{"at": _iso(r.observed_at), "value": r.value,
                              "quality": r.quality, "classification": r.classification}
                             for r in reversed(rows)],
            "predictions": [{"at": _iso(r.target_at), "predicted_c": r.predicted_c,
                             "actual_c": r.measured_c} for r in reversed(forecasts)],
            "target_history": [{"at": _iso(row.recorded_at), "value": row.target_c,
                                "source": row.source} for row in reversed(targets)],
            "heating_activity_history": None,
            "source_contribution_history": heat_inputs, "measured_expenditure": None,
        }
    return _load(database_path, query)


def read_plan(database_path):
    def query(conn):
        plan = conn.execute(select(models.optimizer_plans).where(
            models.optimizer_plans.c.mode == "simulation").order_by(
            models.optimizer_plans.c.created_at.desc()).limit(1)).first()
        if plan is None:
            return None
        rows = conn.execute(select(models.optimizer_plan_intervals).where(
            models.optimizer_plan_intervals.c.plan_id == plan.id).order_by(
            models.optimizer_plan_intervals.c.starts_at)).all()
        return {"created_at": _iso(plan.created_at), "mode": plan.mode,
                "intervals": [{"room_id": r.room_id, "start": _iso(r.starts_at),
                               "data": json.loads(r.data_json)} for r in rows]}
    return _load(database_path, query)


def read_schedules(database_path, room_id):
    def query(conn):
        exists = conn.execute(select(models.rooms.c.id).where(
            (models.rooms.c.id == room_id) & models.rooms.c.removed_at.is_(None))).first()
        if not exists:
            raise ValueError("Room not found")
        rows = conn.execute(select(models.room_schedules).where(
            models.room_schedules.c.room_id == room_id).order_by(
                models.room_schedules.c.weekday, models.room_schedules.c.start_min)).all()
        return [{"weekday": r.weekday, "start_min": r.start_min,
                 "end_min": r.end_min, "target_c": r.target_c} for r in rows]
    return _load(database_path, query)


def set_schedules(database_path, room_id, entries):
    ordered = sorted(entries, key=lambda entry: (entry["weekday"], entry["start_min"]))
    previous = None
    for entry in ordered:
        if (entry["start_min"] >= entry["end_min"] or entry["start_min"] % 15
                or entry["end_min"] % 15):
            raise ValueError("Schedule boundaries must be ordered 15-minute intervals")
        if (previous and previous["weekday"] == entry["weekday"]
                and previous["end_min"] > entry["start_min"]):
            raise ValueError("Room schedule intervals cannot overlap")
        previous = entry

    def write(conn):
        setting = conn.execute(select(models.room_settings).join(
            models.rooms, models.rooms.c.id == models.room_settings.c.room_id).where(
                (models.room_settings.c.room_id == room_id)
                & models.rooms.c.removed_at.is_(None))).first()
        if setting is None:
            raise ValueError("Room not found")
        if any(setting.min_c is None or setting.max_c is None
               or not setting.min_c <= entry["target_c"] <= setting.max_c for entry in ordered):
            raise ValueError("Schedule targets must be within room limits")
        conn.execute(delete(models.room_schedules).where(models.room_schedules.c.room_id == room_id))
        if ordered:
            conn.execute(insert(models.room_schedules), [
                dict(entry, room_id=room_id) for entry in ordered])
    return _load(database_path, write, readonly=False)


def set_room_settings(database_path, room_id, target_c, minimum_c, maximum_c, max_preheat_c=None):
    if not -30 <= minimum_c <= target_c <= maximum_c <= 60:
        raise ValueError("Temperature target must be within valid room limits")
    def write(conn):
        exists = conn.execute(select(models.rooms.c.id).where(
            (models.rooms.c.id == room_id) & models.rooms.c.removed_at.is_(None))).first()
        if not exists:
            raise ValueError("Room not found")
        conflicting_schedule = conn.execute(select(models.room_schedules.c.id).where(
            (models.room_schedules.c.room_id == room_id)
            & ((models.room_schedules.c.target_c < minimum_c)
               | (models.room_schedules.c.target_c > maximum_c))).limit(1)).first()
        conflicting_override = conn.execute(select(models.room_overrides.c.id).where(
            (models.room_overrides.c.room_id == room_id)
            & (models.room_overrides.c.expires_at > datetime.now(timezone.utc))
            & ((models.room_overrides.c.target_c < minimum_c)
               | (models.room_overrides.c.target_c > maximum_c))).limit(1)).first()
        if conflicting_schedule or conflicting_override:
            raise ValueError("Room limits conflict with a schedule or unexpired override")
        values = {"target_c": target_c, "min_c": minimum_c, "max_c": maximum_c,
                  "updated_at": datetime.now(timezone.utc)}
        if max_preheat_c is not None:
            values["max_preheat_c"] = max_preheat_c
        updated = conn.execute(update(models.room_settings).where(
            models.room_settings.c.room_id == room_id).values(**values))
        if not updated.rowcount:
            raise ValueError("Room not found")
        conn.execute(insert(models.room_target_events).values(
            room_id=room_id, target_c=target_c, recorded_at=values["updated_at"], source="web"))
    return _load(database_path, write, readonly=False)


def set_override(database_path, room_id, target_c, minutes):
    now = datetime.now(timezone.utc)
    seconds = int(now.timestamp())
    starts = datetime.fromtimestamp((seconds // 900 + 1) * 900, timezone.utc)
    expires = starts + timedelta(minutes=((minutes + 14) // 15) * 15)
    def write(conn):
        setting = conn.execute(select(models.room_settings).join(
            models.rooms, models.rooms.c.id == models.room_settings.c.room_id).where(
                (models.room_settings.c.room_id == room_id)
                & models.rooms.c.removed_at.is_(None))).first()
        if setting is None:
            raise ValueError("Room not found")
        if setting.min_c is None or setting.max_c is None or not setting.min_c <= target_c <= setting.max_c:
            raise ValueError("Set valid room limits before requesting an override")
        pending = conn.execute(select(models.room_overrides.c.id).where(
            (models.room_overrides.c.room_id == room_id)
            & (models.room_overrides.c.starts_at >= starts)
            & (models.room_overrides.c.starts_at < expires)
            & (models.room_overrides.c.expires_at > starts)).limit(1)).first()
        if pending:
            raise ValueError("A future override already overlaps this interval")
        conn.execute(update(models.room_overrides).where(
            (models.room_overrides.c.room_id == room_id)
            & (models.room_overrides.c.starts_at < starts)
            & (models.room_overrides.c.expires_at > starts)
        ).values(expires_at=starts))
        conn.execute(insert(models.room_overrides).values(
            room_id=room_id, target_c=target_c, starts_at=starts,
            expires_at=expires, source="web"))
    return _load(database_path, write, readonly=False)
