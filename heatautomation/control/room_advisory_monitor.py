"""Collect Tado observations and produce read-only, configuration-bound advisories."""

import json
import logging
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from heatautomation.control.room_advisory import MODEL_VERSION, predict_scenarios, save_advisory
from heatautomation.control.mpc import PlanningUnavailable
from heatautomation.control.room_mpc import Room, resolve_room_target
from heatautomation.house import models
from heatautomation.house.config import config_hash
from heatautomation.integrations.tado import TadoError
from heatautomation.integrations.weather import current_forecast
from heatautomation.thermal.measurements import (
    Classification, Measurement, MeasurementStore, Quality, collect_tado_room,
)
from heatautomation.thermal.radiators import Radiator
from heatautomation.thermal.rooms import RoomThermalService


LOGGER = logging.getLogger(__name__)


def run_cycle(engine, house, tado_service, outdoor_c, *, circuit=None,
              other_heat_w=None, equipment=None, equipment_error=None,
              suspect_uncertainty_c, now=None, timezone_name="UTC", outdoor_forecast=None):
    """Run one read-only advisory cycle; missing physical data remains unknown.

    Explicit circuit and source scenarios are caller-supplied assumptions,
    not automatically inferred from thermostat demand or device connectivity.
    """
    requested_time = now
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Advisory timestamp must include a timezone")
    now = now.astimezone(timezone.utc)
    with engine.connect() as conn:
        stored_hash = conn.execute(select(models.houses.c.config_hash).where(
            models.houses.c.id == str(house.house.id))).scalar_one_or_none()
    if stored_hash != config_hash(house):
        raise ValueError("Reconcile the current house configuration before room advisory collection")
    store = MeasurementStore(engine)
    mapping = {str(item.room_id): item for item in house.tado_rooms if item.home_id is not None}
    devices = {str(device.id): device for device in house.devices}
    demand_rooms = {str(devices[str(emitter)].room_id)
                    for path in house.heat_paths for emitter in (path.emitter_ids or [])}
    warnings = []
    if equipment_error:
        warnings.append(equipment_error)
    for room_id in mapping:
        if tado_service is None:
            warnings.append("%s: Tado service unavailable" % room_id)
            continue
        try:
            temperature, _, demand = collect_tado_room(tado_service, store, room_id, now=requested_time)
            if temperature.quality not in (Quality.GOOD, Quality.SUSPECT):
                warnings.append("%s: current Tado temperature unavailable" % room_id)
            if room_id in demand_rooms and demand.quality not in (Quality.GOOD, Quality.SUSPECT):
                warnings.append("%s: current Tado heating demand unavailable" % room_id)
        except TadoError as error:
            LOGGER.warning("Tado observation unavailable for room %s: %s", room_id, error)
            warnings.append("%s: Tado observation unavailable" % room_id)
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("Invalid Tado observation for room %s", room_id)
            warnings.append("%s: invalid Tado observation" % room_id)
    # Collection may finish after the cycle began; use a fresh decision timestamp.
    if requested_time is None:
        now = datetime.now(timezone.utc)
    forecast_evidence = None
    if outdoor_forecast is not None:
        forecast_evidence = current_forecast(outdoor_forecast, now, timezone_name)
        outdoor_c = forecast_evidence["temperature_c"] if forecast_evidence else None
    costs = None
    cost_currency = None
    outdoor_uncertainty_c = None
    connection_warnings = []
    if equipment is not None:
        if circuit is not None or other_heat_w is not None:
            raise ValueError("Provide either equipment inputs or separate assumptions")
        try:
            equipment.require_fresh(now)
        except ValueError:
            warnings.append("Equipment observations are stale or future-dated")
        else:
            circuit = equipment.circuit.circuit() if equipment.circuit else None
            other_heat_w = {
                room: {source: scenario.model_dump() for source, scenario in sources.items()}
                for room, sources in equipment.direct_heat_w.items()
            }
            costs = equipment.cost_per_delivered_kwh
            cost_currency = equipment.cost_currency
            outdoor_uncertainty_c = equipment.outdoor_uncertainty_c
            for room_id, observation in equipment.room_temperatures.items():
                device = devices.get(observation.device_id)
                if (room_id in mapping or device is None or str(device.room_id) != room_id
                        or device.kind not in ("sensor", "thermostat", "actuator_sensor")
                        or "read_temperature" not in device.capabilities):
                    warnings.append("%s: supplied temperature source does not match an unmapped room sensor" % room_id)
                    continue
                measurement = Measurement(
                    None, observation.device_id, room_id, "temperature", observation.value_c, "C",
                    equipment.observed_at, now, device.adapter, observation.connection_method,
                    Quality(observation.quality), Classification.MEASURED, observation.device_id,
                )
                try:
                    store.save(measurement)
                except ValueError:
                    LOGGER.warning("Conflicting supplied temperature for room %s", room_id)
                    warnings.append("%s: supplied temperature conflicts with stored observation" % room_id)
    if house.lan_discovery.enabled:
        with engine.connect() as conn:
            discovered = {row.id: row for row in conn.execute(
                select(models.lan_devices).where(models.lan_devices.c.device_type == "mill"))}
        for device_id, device in devices.items():
            if (device.adapter != "mill" or not device.external_id
                    or device.room_id is None or str(device.room_id) in mapping
                    or device.kind not in ("sensor", "thermostat", "actuator_sensor")
                    or "read_temperature" not in device.capabilities):
                continue
            row = discovered.get(device.external_id)
            if row is None:
                continue
            if row.connection_state != "CONNECTED_LOCAL" or row.last_error:
                connection_warnings.append("%s: Mill connection or latest poll unavailable" % device_id)
            try:
                payload = json.loads(row.measurements_json).get("ambient_temperature")
                if payload is None:
                    continue
                stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
                if (payload["source"] != "mill" or payload["unit"] != "C"
                        or payload["classification"] != "measured"
                        or payload["quality"] not in ("good", "suspect")
                        or payload["connection_method"] not in ("LOCAL_HTTP", "LOCAL_HTTPS")):
                    raise ValueError("Invalid Mill temperature metadata")
                measurement = Measurement(
                    None, device_id, str(device.room_id), "temperature", payload["value"], "C",
                    stamp, stamp, "mill", payload["connection_method"],
                    Quality(payload["quality"]), Classification.MEASURED, device.external_id,
                ).at(now, 600)
                if (measurement.quality in (Quality.GOOD, Quality.SUSPECT)
                        and -30 <= measurement.value <= 60):
                    store.save(measurement)
            except (KeyError, TypeError, ValueError, AttributeError):
                LOGGER.warning("Invalid stored Mill temperature for device %s", device_id)
                connection_warnings.append("%s: invalid Mill temperature observation" % device_id)
    if (isinstance(outdoor_c, bool) or not isinstance(outdoor_c, (int, float))
            or not math.isfinite(outdoor_c)):
        warnings.append("Outdoor temperature forecast unavailable")
    temperatures, room_demands = {}, {}
    for room_id, item in mapping.items():
        source_ref = "tado:{}:{}".format(item.home_id, item.zone_id)
        for quantity, target in (("temperature", temperatures), ("heating_demand", room_demands)):
            value = store.latest(room_id, quantity, now=now,
                                 source_integration="tado", source_ref=source_ref)
            if value is not None:
                target[room_id] = value
    for room in house.rooms:
        room_id = str(room.id)
        if room_id in mapping:
            continue
        candidates = []
        for device_id, device in devices.items():
            if (str(device.room_id) != room_id
                    or device.kind not in ("sensor", "thermostat", "actuator_sensor")
                    or "read_temperature" not in device.capabilities):
                continue
            value = store.latest(room_id, "temperature", now=now,
                                 source_integration=device.adapter, device_id=device_id)
            if value is not None and value.quality in (Quality.GOOD, Quality.SUSPECT):
                candidates.append(value)
        if len(candidates) == 1:
            temperatures[room_id] = candidates[0]
        elif len(candidates) > 1:
            warnings.append("%s: multiple current temperature sensors require an explicit selection" % room_id)
    parameters, assigned, _ = RoomThermalService(engine)._configuration()
    with engine.connect() as conn:
        settings = {row.room_id: row for row in conn.execute(select(models.room_settings))}
        schedules = conn.execute(select(models.room_schedules)).all()
        overrides = conn.execute(select(models.room_overrides)).all()
    zone = ZoneInfo(timezone_name)
    rooms = []
    for room in house.rooms:
        room_id = str(room.id)
        par = parameters.get(room_id, {})
        setting = settings.get(room_id)
        try:
            target = (resolve_room_target(
                room_id, setting, [s for s in schedules if s.room_id == room_id],
                [o for o in overrides if o.room_id == room_id], now, zone)
                if setting else None)
        except PlanningUnavailable as error:
            warnings.append(str(error))
            target = None
        if "capacity_j_per_k" not in par or "ua_w_per_k" not in par or target is None:
            warnings.append("%s: thermal parameters or target missing" % room_id)
            continue
        rooms.append(Room(room_id, 0, par["capacity_j_per_k"], par["ua_w_per_k"],
                          par["conductances_w_per_k"], -30, 60, [target], 0,
                          floor_id=str(room.floor_id)))
    circuits = [path for path in house.heat_paths if path.emitter_ids]
    radiators, demands = [], {}
    generator_ids = [str(path.generator_id) for path in house.heat_paths]
    if len(generator_ids) != len(set(generator_ids)):
        warnings.append("Generator is assigned to multiple heat paths; delivery cannot be separated safely")
    if len(circuits) > 1:
        warnings.append("Multiple radiator circuits are not supported by this advisory")
    for path in circuits:
        for emitter_id in path.emitter_ids:
            emitter = devices[str(emitter_id)]
            room_id = str(emitter.room_id)
            demand = room_demands.get(room_id)
            temp = temperatures.get(room_id)
            controller = devices.get(str(emitter.controlled_by))
            if controller is None or controller.adapter != "tado" or str(controller.room_id) != room_id:
                warnings.append("%s: radiator controller is not a mapped Tado thermostat" % emitter_id)
            radiators.append(Radiator(str(emitter_id), room_id, emitter.rated_w_at_dt50,
                                      demand.value if demand else None, temp.value if temp else None))
            if demand is not None:
                demands[str(emitter_id)] = demand
    if circuits and (circuit is None or circuit.generator_id != str(circuits[0].generator_id)):
        warnings.append("Radiator generator availability, capacity and water conditions unavailable")
    if not circuits and circuit is not None:
        warnings.append("Equipment specifies a radiator circuit not present in the house configuration")
    other_heat_w = other_heat_w if other_heat_w is not None else {}
    if set(other_heat_w) - {str(room.id) for room in house.rooms}:
        warnings.append("Direct heat inputs contain unconfigured rooms")
    for room_id, sources in assigned.items():
        circuit_ids = {str(path.generator_id) for path in circuits}
        required = sources - circuit_ids
        supplied = set(other_heat_w.get(room_id, {}))
        if supplied != required:
            warnings.append("%s: direct heat delivery unknown or does not match configured sources" % room_id)
    if len(rooms) != len(house.rooms):
        warnings.append("Not all configured rooms have usable thermal parameters and targets")
    if warnings:
        result = {"available": False, "mode": "advisory", "model_version": MODEL_VERSION,
                  "created_at": now.isoformat(), "reasons": warnings}
    else:
        complete_other = {str(room.id): other_heat_w.get(str(room.id), {}) for room in house.rooms}
        result = predict_scenarios(rooms, temperatures, demands, circuit, radiators, complete_other,
                                   outdoor_c, now=now, suspect_uncertainty_c=suspect_uncertainty_c,
                                   cost_per_delivered_kwh=costs, cost_currency=cost_currency,
                                   outdoor_uncertainty_c=outdoor_uncertainty_c)
        if not result["available"]:
            result.update(mode="advisory", created_at=now.isoformat(), model_version=MODEL_VERSION)
    if equipment is not None:
        result["equipment_evidence"] = {
            "observed_at": equipment.observed_at.isoformat(),
            "classification": equipment.classification,
            "provenance": equipment.provenance,
        }
    if forecast_evidence is not None:
        result["outdoor_forecast_evidence"] = forecast_evidence
    result["connection_warnings"] = connection_warnings
    if result["available"]:
        save_advisory(engine, result)
    else:
        with engine.begin() as conn:
            conn.execute(insert(models.optimizer_plans).values(
                created_at=now, mode="advisory",
                details_json=json.dumps(result, allow_nan=False, sort_keys=True),
            ))
    return result
