"""Prediction-only room forecasts and conservative physics-based learning."""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import lsq_linear
from sqlalchemy import insert, select, update

from heatautomation.house import models
from .model import room_heat_demand_w, simulate_step


HORIZONS = (900, 1800, 3600, 7200, 10800, 21600)


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number" % name)
    return float(value)


class RoomThermalService:
    """Uses SQL configuration, explicit observed inputs and no device commands."""

    def __init__(self, engine, *, now=None):
        self.engine = engine
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _configuration(self):
        with self.engine.connect() as conn:
            rooms = [r.id for r in conn.execute(select(models.rooms.c.id).where(
                models.rooms.c.removed_at.is_(None)))]
            rows = conn.execute(select(models.thermal_parameters)).all()
            links = conn.execute(select(models.thermal_links).where(
                models.thermal_links.c.removed_at.is_(None))).all()
            paths = conn.execute(select(models.heat_paths).where(
                models.heat_paths.c.removed_at.is_(None))).all()
            emitters = conn.execute(select(models.heat_path_emitters)).all()
            devices = {r.id: r for r in conn.execute(select(models.devices).where(
                models.devices.c.removed_at.is_(None)))}
            settings = {r.room_id: r.target_c for r in conn.execute(
                select(models.room_settings.c.room_id, models.room_settings.c.target_c))}
        params = {room: {"conductances_w_per_k": {}} for room in rooms}
        for row in rows:
            if row.room_id in params and row.name in ("capacity_j_per_k", "ua_w_per_k"):
                params[row.room_id][row.name] = row.value
        for link in links:
            if link.room_a_id in params and link.room_b_id in params:
                params[link.room_a_id]["conductances_w_per_k"][link.room_b_id] = link.conductance_w_per_k
                params[link.room_b_id]["conductances_w_per_k"][link.room_a_id] = link.conductance_w_per_k
        sources = {room: set() for room in rooms}
        for path in paths:
            if path.generator_id not in devices:
                continue
            if path.path_type == "direct" and path.target_room_id in sources:
                sources[path.target_room_id].add(path.generator_id)
            elif path.path_type == "circuit":
                for item in emitters:
                    device = devices.get(item.emitter_id)
                    if item.heat_path_id == path.id and device and device.room_id in sources:
                        sources[device.room_id].add(path.generator_id)
        return params, sources, settings

    def save_snapshot(self, observed_at, temperatures, outdoor_c, heat_inputs):
        """Store synchronized observations. Heat values are delivered watts, not commands.

        temperatures maps room ID to {value, source_device_id, quality}; heat_inputs
        maps room ID to generator ID to {watts, classification}. All input values
        must refer to the same observation time. Missing values are unknown.
        """
        observed_at = _utc(observed_at)
        params, sources, _ = self._configuration()
        with self.engine.connect() as conn:
            sensor_rooms = {row.id: row.room_id for row in conn.execute(
                select(models.devices.c.id, models.devices.c.room_id).where(
                    models.devices.c.removed_at.is_(None)
                    & models.devices.c.kind.in_(("thermostat", "sensor", "actuator_sensor"))))}
        outdoor = None if outdoor_c is None else _number(outdoor_c, "outdoor_c")
        clean_temp, clean_heat = {}, {}
        for room, item in temperatures.items():
            if room not in params or not isinstance(item, dict):
                raise ValueError("Temperature must reference a configured room")
            value = _number(item.get("value"), "temperature")
            if not -30 <= value <= 60 or item.get("quality") not in ("good", "suspect", "stale", "bad"):
                raise ValueError("Invalid temperature or quality")
            if sensor_rooms.get(item.get("source_device_id")) != room:
                raise ValueError("Temperature source must be a device assigned to the room")
            clean_temp[room] = {"value": value, "quality": item["quality"],
                                "source_device_id": item["source_device_id"]}
        for room, inputs in heat_inputs.items():
            if room not in sources or not isinstance(inputs, dict):
                raise ValueError("Heat input must reference a configured room")
            clean_heat[room] = {}
            for source, value in inputs.items():
                if source not in sources[room] or not isinstance(value, dict):
                    raise ValueError("Heat source is not assigned to the room")
                watts = _number(value.get("watts"), "delivered watts")
                if watts < 0 or value.get("classification") not in ("measured", "estimated"):
                    raise ValueError("Heat input needs nonnegative watts and provenance")
                clean_heat[room][source] = {"watts": watts, "classification": value["classification"]}
        data = {"temperatures": clean_temp, "outdoor_c": outdoor, "heat_inputs": clean_heat}
        with self.engine.begin() as conn:
            conn.execute(insert(models.room_thermal_snapshots).values(
                observed_at=observed_at, data_json=json.dumps(data, allow_nan=False, sort_keys=True)))
            if clean_temp:
                conn.execute(insert(models.room_measurements), [
                    {"room_id": room, "quantity": "temperature", "value": item["value"],
                     "unit": "C", "observed_at": observed_at,
                     "received_at": datetime.now(timezone.utc),
                     "source_device_id": item["source_device_id"], "quality": item["quality"],
                     "classification": "measured"}
                    for room, item in clean_temp.items()
                ])
        return data

    def _usable(self, data, params, sources):
        known = {}
        for room, par in params.items():
            temp = data["temperatures"].get(room)
            inputs = data["heat_inputs"].get(room, {})
            known[room] = bool(
                temp and temp["quality"] == "good" and data["outdoor_c"] is not None
                and "capacity_j_per_k" in par and "ua_w_per_k" in par
                and sources[room].issubset(inputs)
            )
        # A coupled component needs every room's state and heat input.
        changed = True
        while changed:
            changed = False
            for room, par in params.items():
                if known[room] and any(not known[n] for n in par["conductances_w_per_k"]):
                    known[room] = False
                    changed = True
        return known

    def predict(self, observed_at, data, *, record=True, response_seconds=3600):
        """Forecast constant outdoor temperature and delivered heat for six hours."""
        observed_at = _utc(observed_at)
        response_seconds = _number(response_seconds, "response_seconds")
        if response_seconds <= 0:
            raise ValueError("response_seconds must be positive")
        params, sources, targets = self._configuration()
        usable = self._usable(data, params, sources)
        temperatures = {r: data["temperatures"][r]["value"] for r in params if usable[r]}
        selected = {r: params[r] for r in temperatures}
        heat = {r: sum(data["heat_inputs"].get(r, {}).get(source, {}).get("watts", 0)
                       for source in sources[r]) for r in temperatures}
        result = {r: {"classification": "unknown",
                      "temperatures_c": {horizon: None for horizon in HORIZONS},
                      "expected_heating_demand_w": None,
                      "maintenance_heating_demand_w": None,
                      "cooling_rate_c_per_hour": None,
                      "initial_rate_c_per_hour": None,
                      "time_to_target_seconds": None,
                      "reason": "missing temperature, neighbor, heat input or parameters"}
                  for r in params}
        trajectory = {r: {} for r in temperatures}
        current = dict(temperatures)
        target_times = {r: (0 if targets.get(r) is not None and current[r] >= targets[r] else None)
                        for r in temperatures}
        for seconds in range(300, max(HORIZONS) + 1, 300) if temperatures else ():
            current = simulate_step(current, selected, heat, data["outdoor_c"], 300)
            for room, value in current.items():
                target = targets.get(room)
                if target is not None and target_times[room] is None and value >= target:
                    target_times[room] = seconds
                if seconds in HORIZONS:
                    trajectory[room][seconds] = value
        for room in temperatures:
            par = params[room]
            cooling = simulate_step(temperatures, selected, {r: 0.0 for r in temperatures},
                                    data["outdoor_c"], 300)[room]
            demand = None
            maintenance = None
            if targets.get(room) is not None:
                demand = room_heat_demand_w(
                    temperatures[room], targets[room], data["outdoor_c"],
                    par["capacity_j_per_k"], par["ua_w_per_k"],
                    par["conductances_w_per_k"], temperatures, response_seconds)
                target = targets[room]
                maintenance = max(0.0, par["ua_w_per_k"] * (target - data["outdoor_c"])
                                  - sum(g * (temperatures[n] - target)
                                        for n, g in par["conductances_w_per_k"].items()))
            result[room] = {
                "classification": "predicted",
                "temperatures_c": trajectory[room],
                "expected_heating_demand_w": demand,
                "maintenance_heating_demand_w": maintenance,
                "cooling_rate_c_per_hour": max(0.0, (temperatures[room] - cooling) * 12),
                "initial_rate_c_per_hour": (simulate_step(
                    temperatures, selected, heat, data["outdoor_c"], 300)[room]
                    - temperatures[room]) * 12,
                "time_to_target_seconds": target_times[room],
                "source_heat_w": {source: data["heat_inputs"][room][source]["watts"]
                                  for source in sources[room]},
                "heat_transfer_w": {n: g * (temperatures[n] - temperatures[room])
                                    for n, g in par["conductances_w_per_k"].items()},
                "capacity_j_per_k": par["capacity_j_per_k"],
                "heat_loss_w_per_k": par["ua_w_per_k"],
                "assumptions": "constant outdoor temperature and delivered heat",
            }
        if record:
            recorded_at = _utc(self._now())
            rows = [{"room_id": room, "created_at": observed_at,
                     "recorded_at": recorded_at,
                     "target_at": observed_at + timedelta(seconds=horizon),
                     "horizon_seconds": horizon, "predicted_c": value}
                    for room in temperatures for horizon, value in trajectory[room].items()
                    if observed_at + timedelta(seconds=horizon) > recorded_at]
            if rows:
                with self.engine.begin() as conn:
                    conn.execute(insert(models.room_thermal_predictions), rows)
        return result

    def latest_snapshot(self):
        with self.engine.connect() as conn:
            row = conn.execute(select(models.room_thermal_snapshots).order_by(
                models.room_thermal_snapshots.c.observed_at.desc()).limit(1)).first()
        if row is None:
            return None
        return _timestamp(row.observed_at), json.loads(row.data_json)

    def import_legacy_history(self, database, sensibo_room_id, sensibo_device_id):
        """Import legacy Sensibo/outdoor observations without inferring heat delivery.

        Legacy collection timestamps are not provider observation timestamps, so
        imported indoor temperatures are marked suspect.
        """
        params, _, _ = self._configuration()
        with self.engine.connect() as conn:
            device = conn.execute(select(models.devices).where(
                (models.devices.c.id == sensibo_device_id)
                & (models.devices.c.room_id == sensibo_room_id)
                & (models.devices.c.adapter == "sensibo")
                & models.devices.c.removed_at.is_(None)
            )).first()
            existing = {_timestamp(row.observed_at) for row in conn.execute(
                select(models.room_thermal_snapshots.c.observed_at))}
        if sensibo_room_id not in params or device is None:
            raise ValueError("An explicitly configured Sensibo sensor and room are required")
        count = 0
        for row in database.history():
            timestamp = _utc(datetime.fromisoformat(row["timestamp"]))
            if timestamp in existing:
                continue
            temperatures = {}
            if row.get("indoor_c") is not None:
                temperatures[sensibo_room_id] = {
                    "value": row["indoor_c"], "source_device_id": sensibo_device_id,
                    "quality": "suspect",
                }
            self.save_snapshot(timestamp, temperatures, row.get("outdoor_c"), {})
            existing.add(timestamp)
            count += 1
        return count

    def evaluate_predictions(self, tolerance_seconds=300, as_of=None):
        """Match forecasts to good measured room temperatures and report room/horizon MAE."""
        if not isinstance(tolerance_seconds, int) or tolerance_seconds < 0:
            raise ValueError("tolerance_seconds must be a non-negative integer")
        as_of = _utc(as_of) if as_of is not None else datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            pending = conn.execute(select(models.room_thermal_predictions).where(
                models.room_thermal_predictions.c.measurement_id.is_(None))).all()
            for forecast in pending:
                target = _timestamp(forecast.target_at)
                if target + timedelta(seconds=tolerance_seconds) > as_of:
                    continue
                rows = conn.execute(select(models.room_measurements).where(
                    (models.room_measurements.c.room_id == forecast.room_id)
                    & (models.room_measurements.c.quantity == "temperature")
                    & (models.room_measurements.c.unit == "C")
                    & (models.room_measurements.c.quality == "good")
                    & (models.room_measurements.c.classification == "measured")
                    & (models.room_measurements.c.observed_at >= target - timedelta(seconds=tolerance_seconds))
                    & (models.room_measurements.c.observed_at <= target + timedelta(seconds=tolerance_seconds))
                    & (models.room_measurements.c.observed_at > forecast.created_at)
                    & (models.room_measurements.c.observed_at > forecast.recorded_at)
                    & (models.room_measurements.c.observed_at <= as_of)
                )).all()
                if rows:
                    match = min(rows, key=lambda row: abs((_timestamp(row.observed_at) - target).total_seconds()))
                    conn.execute(update(models.room_thermal_predictions).where(
                        models.room_thermal_predictions.c.id == forecast.id).values(
                            measured_c=match.value, measured_at=match.observed_at, measurement_id=match.id))
            scored = conn.execute(select(models.room_thermal_predictions).where(
                models.room_thermal_predictions.c.measurement_id.is_not(None))).all()
        grouped = {}
        for row in scored:
            key = (row.room_id, row.horizon_seconds)
            grouped.setdefault(key, []).append(row.predicted_c - row.measured_c)
        return {room: {horizon: {"samples": len(errors),
                                "mae_c": sum(abs(e) for e in errors) / len(errors),
                                "bias_c": sum(errors) / len(errors)}
                       for (r, horizon), errors in grouped.items() if r == room}
                for room in {key[0] for key in grouped}}

    def learn(self, minimum_intervals=60):
        """Fit room heat balances from measured delivered watts, with a holdout.

        A common inverse capacity multiplies all measured heat sources. Parameters
        remain unchanged when excitation or measured heat is insufficient.
        """
        if not isinstance(minimum_intervals, int) or minimum_intervals < 30:
            raise ValueError("At least 30 intervals are required")
        params, sources, _ = self._configuration()
        with self.engine.connect() as conn:
            rows = conn.execute(select(models.room_thermal_snapshots).order_by(
                models.room_thermal_snapshots.c.observed_at)).all()
        snapshots = [(_timestamp(row.observed_at), json.loads(row.data_json)) for row in rows]
        fits, report = {}, {}
        for room, par in params.items():
            neighbors = sorted(par["conductances_w_per_k"])
            x, y = [], []
            if not sources[room] or "capacity_j_per_k" not in par or "ua_w_per_k" not in par:
                report[room] = {"status": "insufficient_parameters_or_heat_sources"}
                continue
            for (start, before), (end, after) in zip(snapshots, snapshots[1:]):
                seconds = (end - start).total_seconds()
                if not 60 <= seconds <= 900:
                    continue
                readings = before["temperatures"]
                next_reading = after["temperatures"].get(room)
                required = [room] + neighbors
                if (before["outdoor_c"] is None or next_reading is None
                        or next_reading["quality"] != "good"
                        or any(r not in readings or readings[r]["quality"] != "good" for r in required)):
                    continue
                inputs = before["heat_inputs"].get(room, {})
                next_inputs = after["heat_inputs"].get(room, {})
                if (not sources[room].issubset(inputs) or not sources[room].issubset(next_inputs)
                        or any(inputs[s]["classification"] != "measured"
                               or next_inputs[s]["classification"] != "measured"
                               or inputs[s]["watts"] != next_inputs[s]["watts"]
                               for s in sources[room])):
                    continue
                temp = readings[room]["value"]
                x.append([before["outdoor_c"] - temp]
                         + [readings[n]["value"] - temp for n in neighbors]
                         + [sum(inputs[s]["watts"] for s in sources[room]) / 1000])
                y.append((next_reading["value"] - temp) * 3600 / seconds)
            if len(x) < minimum_intervals:
                report[room] = {"status": "insufficient_samples", "samples": len(x)}
                continue
            matrix, labels = np.asarray(x), np.asarray(y)
            split = int(len(x) * 0.8)
            train, test = matrix[:split - 1], matrix[split:]
            train_y, test_y = labels[:split - 1], labels[split:]
            scale = np.linalg.norm(train, axis=0)
            if (np.any(scale < 1e-8) or np.linalg.matrix_rank(train / scale) < matrix.shape[1]
                    or np.linalg.cond(train / scale) > 100):
                report[room] = {"status": "unidentifiable", "samples": len(x)}
                continue
            candidate = lsq_linear(train / scale, train_y, bounds=(0, np.inf))
            coefficients = candidate.x / scale
            if not candidate.success or coefficients[-1] <= 0:
                report[room] = {"status": "fit_failed", "samples": len(x)}
                continue
            capacity = 3600000 / coefficients[-1]
            ua = coefficients[0] * capacity / 3600
            conductance = {neighbor: coefficients[i + 1] * capacity / 3600
                           for i, neighbor in enumerate(neighbors)}
            baseline = np.array([par["ua_w_per_k"] * 3600 / par["capacity_j_per_k"]]
                                + [par["conductances_w_per_k"][n] * 3600 / par["capacity_j_per_k"]
                                   for n in neighbors]
                                + [3600000 / par["capacity_j_per_k"]])
            mae = float(np.mean(np.abs(test @ coefficients - test_y)))
            baseline_mae = float(np.mean(np.abs(test @ baseline - test_y)))
            if (not 1e4 <= capacity <= 1e10 or not 0 < ua <= 10000
                    or any(not 0 < g <= 10000 for g in conductance.values())
                    or mae >= baseline_mae):
                report[room] = {"status": "validation_rejected", "samples": len(x),
                                "mae_c_per_hour": mae, "baseline_mae_c_per_hour": baseline_mae}
                continue
            fits[room] = (capacity, ua, conductance, len(x))
            report[room] = {"status": "validated", "samples": len(x),
                            "mae_c_per_hour": mae, "baseline_mae_c_per_hour": baseline_mae}
        eligible = set(fits)
        changed = True
        while changed:
            changed = False
            for room in tuple(eligible):
                for neighbor, value in fits[room][2].items():
                    if neighbor not in eligible:
                        eligible.remove(room)
                        changed = True
                        break
                    other = fits[neighbor][2].get(room)
                    if other is None or abs(value - other) > 0.5 * max(value, other):
                        eligible.remove(room)
                        changed = True
                        break
        for room in set(fits) - eligible:
            report[room]["status"] = "pending_symmetric_link_validation"
        fits = {room: fits[room] for room in eligible}
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            for room, (capacity, ua, _, count) in fits.items():
                for name, value in (("capacity_j_per_k", capacity), ("ua_w_per_k", ua)):
                    table = models.thermal_parameters
                    conn.execute(update(table).where(
                        (table.c.room_id == room) & (table.c.name == name)
                    ).values(value=value, sample_count=count, updated_at=now))
                    conn.execute(insert(models.thermal_parameter_events).values(
                        room_id=room, name=name, old_value=params[room][name],
                        new_value=value, reason="learned", occurred_at=now))
            for room, (_, _, values, _) in fits.items():
                for neighbor, value in values.items():
                    if room >= neighbor or neighbor not in fits:
                        continue
                    other = fits[neighbor][2].get(room)
                    if other is None or abs(value - other) > 0.5 * max(value, other):
                        continue
                    learned = (value + other) / 2
                    table = models.thermal_links
                    conn.execute(update(table).where(
                        (table.c.room_a_id == room) & (table.c.room_b_id == neighbor)
                        & table.c.removed_at.is_(None)
                    ).values(conductance_w_per_k=learned, updated_at=now))
                    table = models.thermal_parameters
                    conn.execute(update(table).where(
                        (table.c.room_id == room) & (table.c.name == "conductance_w_per_k:" + neighbor)
                    ).values(value=learned, sample_count=min(fits[room][3], fits[neighbor][3]),
                             updated_at=now))
                    conn.execute(insert(models.thermal_parameter_events).values(
                        room_id=room, name="conductance_w_per_k:" + neighbor,
                        old_value=params[room]["conductances_w_per_k"][neighbor],
                        new_value=learned, reason="learned", occurred_at=now))
        return report
