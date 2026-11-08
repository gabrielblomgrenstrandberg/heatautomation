"""Collect Tado observations and produce read-only, configuration-bound advisories."""

import json
import logging
import math
from datetime import datetime, timezone

from sqlalchemy import insert, select

from heatautomation.control.room_advisory import MODEL_VERSION, predict_scenarios, save_advisory
from heatautomation.control.room_mpc import Room
from heatautomation.house import models
from heatautomation.house.config import config_hash
from heatautomation.integrations.tado import TadoError
from heatautomation.thermal.measurements import MeasurementStore, Quality, collect_tado_room
from heatautomation.thermal.radiators import Radiator
from heatautomation.thermal.rooms import RoomThermalService


LOGGER = logging.getLogger(__name__)


def run_cycle(engine, house, tado_service, outdoor_c, *, circuit=None,
              other_heat_w=None, equipment=None, suspect_uncertainty_c, now=None):
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
    warnings = []
    for room_id in mapping:
        if tado_service is None:
            warnings.append("%s: Tado service unavailable" % room_id)
            continue
        try:
            temperature, _, demand = collect_tado_room(tado_service, store, room_id, now=requested_time)
            if temperature.quality not in (Quality.GOOD, Quality.SUSPECT):
                warnings.append("%s: current Tado temperature unavailable" % room_id)
            if demand.quality not in (Quality.GOOD, Quality.SUSPECT):
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
    costs = None
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
    parameters, assigned, targets = RoomThermalService(engine)._configuration()
    rooms = []
    for room in house.rooms:
        room_id = str(room.id)
        par = parameters.get(room_id, {})
        target = targets.get(room_id)
        if "capacity_j_per_k" not in par or "ua_w_per_k" not in par or target is None:
            warnings.append("%s: thermal parameters or target missing" % room_id)
            continue
        rooms.append(Room(room_id, 0, par["capacity_j_per_k"], par["ua_w_per_k"],
                          par["conductances_w_per_k"], -30, 60, [target], 0,
                          floor_id=str(room.floor_id)))
    devices = {str(device.id): device for device in house.devices}
    circuits = [path for path in house.heat_paths if path.emitter_ids]
    radiators, demands = [], {}
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
    other_heat_w = other_heat_w if other_heat_w is not None else {}
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
                                   cost_per_delivered_kwh=costs)
        if not result["available"]:
            result.update(mode="advisory", created_at=now.isoformat(), model_version=MODEL_VERSION)
    if equipment is not None:
        result["equipment_evidence"] = {
            "observed_at": equipment.observed_at.isoformat(),
            "classification": equipment.classification,
            "provenance": equipment.provenance,
        }
    if result["available"]:
        save_advisory(engine, result)
    else:
        with engine.begin() as conn:
            conn.execute(insert(models.optimizer_plans).values(
                created_at=now, mode="advisory",
                details_json=json.dumps(result, allow_nan=False, sort_keys=True),
            ))
    return result
