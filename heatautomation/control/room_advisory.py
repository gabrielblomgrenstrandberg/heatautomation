"""Read-only room forecasts for explicitly supplied heat-delivery scenarios."""

import math
import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import insert

from heatautomation.house import models
from heatautomation.thermal.measurements import Classification, Quality
from heatautomation.thermal.model import room_heat_demand_w, simulate_step
from heatautomation.thermal.radiators import estimate_delivery


MODEL_VERSION = "room-advisory-1"
SCENARIOS = ("low", "nominal", "high")


def predict_scenarios(rooms, temperatures, demands, circuit, radiators, other_heat_w,
                      outdoor_c, *, now, freshness_seconds=600,
                      suspect_uncertainty_c, seconds=900, cost_per_delivered_kwh=None,
                      cost_currency=None, outdoor_uncertainty_c=None):
    """Predict a single step from measured temperatures and assumed heat inputs.

    All rooms need an explicit other-source mapping; an empty mapping declares
    no other heat sources. A None circuit explicitly declares no radiator
    circuit. Scenario results are conditional, not confidence bounds.
    """
    rooms, radiators = list(rooms), list(radiators)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("Planning time must include a timezone")
    if (isinstance(suspect_uncertainty_c, bool)
            or not isinstance(suspect_uncertainty_c, (int, float))
            or not math.isfinite(suspect_uncertainty_c) or suspect_uncertainty_c <= 0):
        raise ValueError("Suspect uncertainty must be positive and finite")
    if not rooms or len({room.id for room in rooms}) != len(rooms):
        raise ValueError("Unique rooms are required")
    if not isinstance(outdoor_c, (int, float)) or isinstance(outdoor_c, bool) or not math.isfinite(outdoor_c):
        return {"available": False, "reasons": ["Outdoor temperature unavailable"]}
    if outdoor_uncertainty_c is not None and (
            isinstance(outdoor_uncertainty_c, bool)
            or not isinstance(outdoor_uncertainty_c, (int, float))
            or not math.isfinite(outdoor_uncertainty_c) or outdoor_uncertainty_c < 0):
        raise ValueError("Outdoor uncertainty must be nonnegative and finite")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError("Prediction duration must be positive and at most 24 hours")
    room_ids = {room.id for room in rooms}
    if any(r.room_id not in room_ids for r in radiators):
        raise ValueError("Radiator assigned to an unknown room")
    if len({r.id for r in radiators}) != len(radiators):
        raise ValueError("Radiators must have unique IDs")
    for room in rooms:
        for neighbor, conductance in room.conductances_w_per_k.items():
            peer = next((r for r in rooms if r.id == neighbor), None)
            if (peer is None or neighbor == room.id or not math.isfinite(conductance)
                    or conductance < 0 or not math.isclose(
                        conductance, peer.conductances_w_per_k.get(room.id, -1))):
                raise ValueError("Room conductances must be finite and symmetric")
    reasons = []
    usable = {}
    if circuit is None:
        if radiators:
            raise ValueError("A radiator circuit is required for radiators")
        radiator_result = {
            "classification": "estimated", "scenarios": {name: {} for name in SCENARIOS},
            "generator_available": None, "generator_available_w": None,
        }
    else:
        radiator_result = estimate_delivery(circuit, radiators)
    for room in rooms:
        reading = temperatures.get(room.id)
        if reading is None:
            reasons.append("%s: temperature missing" % room.id)
            continue
        reading = reading.at(now, freshness_seconds)
        if (reading.room_id != room.id or reading.quantity != "temperature" or reading.unit != "C"
                or reading.classification != Classification.MEASURED
                or reading.quality not in (Quality.GOOD, Quality.SUSPECT)
                or not -30 <= reading.value <= 60):
            reasons.append("%s: temperature unavailable, stale or invalid" % room.id)
            continue
        usable[room.id] = reading
    if radiator_result.get("classification") != "estimated":
        reasons.extend("radiator: " + name for name in radiator_result.get("missing", ["delivery unknown"]))
    for radiator in radiators:
        reading = usable.get(radiator.room_id)
        demand = demands.get(radiator.id)
        if reading is not None and radiator.room_temperature_c != reading.value:
            reasons.append("%s: radiator temperature does not match observation" % radiator.id)
        if demand is not None:
            demand = demand.at(now, freshness_seconds)
        if (demand is None or demand.room_id != radiator.room_id
                or demand.quantity != "heating_demand" or demand.unit != "%"
                or demand.classification != Classification.MEASURED
                or demand.quality not in (Quality.GOOD, Quality.SUSPECT)
                or demand.source_integration != "tado" or not demand.source_ref
                or (reading is not None and reading.source_integration == "tado"
                    and demand.source_ref != reading.source_ref)
                or demand.value != radiator.demand_percent):
            reasons.append("%s: current demand observation unavailable or mismatched" % radiator.id)
    for room in rooms:
        if room.id not in other_heat_w:
            reasons.append("%s: other heat sources unknown" % room.id)
    if reasons:
        return {"available": False, "model_version": MODEL_VERSION, "reasons": reasons}
    parameters = {room.id: {
        "capacity_j_per_k": room.capacity_j_per_k, "ua_w_per_k": room.heat_loss_w_per_k,
        "conductances_w_per_k": dict(room.conductances_w_per_k),
    } for room in rooms}
    if set(other_heat_w) != room_ids:
        raise ValueError("Other source inputs must refer only to configured rooms")
    radiator_ids = {r.id for r in radiators}
    if circuit is not None:
        radiator_ids.add(circuit.generator_id)
    source_ids = []
    for inputs in other_heat_w.values():
        if not isinstance(inputs, dict) or radiator_ids.intersection(inputs):
            raise ValueError("Other source inputs must be distinct from radiators")
        source_ids.extend(inputs)
        for heat in inputs.values():
            if (not isinstance(heat, dict) or set(heat) != set(SCENARIOS)
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or v < 0 for v in heat.values())
                    or not heat["low"] <= heat["nominal"] <= heat["high"]):
                raise ValueError("Other heat requires ordered finite nonnegative scenarios")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("A direct heat source may only be assigned to one room")
    costs = cost_per_delivered_kwh or {}
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in costs.values()):
        raise ValueError("Heat costs must be nonnegative and finite")
    if costs and (not isinstance(cost_currency, str) or len(cost_currency) != 3
                  or not cost_currency.isascii() or not cost_currency.isalpha()
                  or not cost_currency.isupper()):
        raise ValueError("Cost rates require a three-letter uppercase currency")
    predicted = {}
    for scenario in SCENARIOS:
        initial = {}
        heat_by_room = {room.id: 0.0 for room in rooms}
        for room in rooms:
            reading = usable[room.id]
            margin = room.uncertainty_c
            if reading.quality == Quality.SUSPECT:
                margin += suspect_uncertainty_c
            if not math.isfinite(margin) or margin < 0:
                raise ValueError("Room uncertainty must be nonnegative and finite")
            initial[room.id] = reading.value + (
                -margin if scenario == "low" else margin if scenario == "high" else 0)
            heat_by_room[room.id] += sum(item[scenario] for item in other_heat_w[room.id].values())
        for radiator in radiators:
            heat_by_room[radiator.room_id] += radiator_result["scenarios"][scenario][radiator.id]
        current_step = initial
        remaining = seconds
        while remaining > 0:
            step = min(300, remaining)
            weather_margin = outdoor_uncertainty_c if outdoor_uncertainty_c is not None else 0
            scenario_outdoor = outdoor_c + (
                -weather_margin if scenario == "low" else weather_margin if scenario == "high" else 0)
            current_step = simulate_step(current_step, parameters, heat_by_room, scenario_outdoor, step)
            remaining -= step
        predicted[scenario] = current_step
    current = {room.id: usable[room.id].value for room in rooms}
    result = []
    for room in rooms:
        reading = usable[room.id]
        par = parameters[room.id]
        target = room.targets_c[0]
        result.append({
            "room_id": room.id, "current_temperature_c": reading.value,
            "temperature_quality": reading.quality.value,
            "temperature_measurement_id": reading.id,
            "temperature_age_seconds": (now - reading.observed_at).total_seconds(),
            "target_c": target,
            "tado_demand_percent": {r.id: r.demand_percent for r in radiators if r.room_id == room.id},
            "tado_demand_measurement_ids": {
                r.id: demands[r.id].id for r in radiators if r.room_id == room.id},
            "estimated_radiator_w": {
                name: sum(radiator_result["scenarios"][name][r.id]
                          for r in radiators if r.room_id == room.id) for name in SCENARIOS},
            "estimated_other_heat_w": {
                name: sum(item[name] for item in other_heat_w[room.id].values()) for name in SCENARIOS},
            "estimated_heat_loss_w": par["ua_w_per_k"] * (reading.value - outdoor_c),
            "estimated_heat_demand_w": room_heat_demand_w(
                reading.value, target, outdoor_c, par["capacity_j_per_k"], par["ua_w_per_k"],
                par["conductances_w_per_k"], current, seconds),
            "predicted_temperature_c": {name: predicted[name][room.id] for name in SCENARIOS},
        })
    return {
        "available": True, "mode": "advisory", "model_version": MODEL_VERSION,
        "rooms": result, "reasons": [],
        "created_at": now.isoformat(),
        "inputs": {
            "outdoor_c": outdoor_c, "duration_seconds": seconds,
            "outdoor_uncertainty_c": outdoor_uncertainty_c,
            "circuit": asdict(circuit) if circuit is not None else None,
            "radiators": [asdict(r) for r in radiators],
            "other_heat_w": other_heat_w, "suspect_uncertainty_c": suspect_uncertainty_c,
            "cost_per_delivered_kwh": costs,
            "cost_currency": cost_currency,
            "freshness_seconds": freshness_seconds,
            "room_parameters": {r.id: {
                "capacity_j_per_k": r.capacity_j_per_k, "heat_loss_w_per_k": r.heat_loss_w_per_k,
                "conductances_w_per_k": dict(r.conductances_w_per_k),
                "uncertainty_c": r.uncertainty_c,
            } for r in rooms},
        },
        "house": {
            "generator_available": radiator_result["generator_available"],
            "available_generator_w": radiator_result["generator_available_w"],
            "estimated_total_heating_demand_w": sum(r["estimated_heat_demand_w"] for r in result),
            "estimated_heating_cost": (
                {name: seconds / 3600000 * (
                    sum(item[name] * costs[source] for inputs in other_heat_w.values()
                        for source, item in inputs.items())
                    + (sum(radiator_result["scenarios"][name].values())
                       * costs[circuit.generator_id] if circuit is not None else 0))
                 for name in SCENARIOS}
                if all(source in costs for source in source_ids
                       + ([circuit.generator_id] if circuit is not None else [])) else None),
            "estimated_heating_cost_currency": cost_currency,
            "warnings": ["Any heating costs are scenario estimates, not measured consumption."],
        },
        "assumptions": ("Fixed heat and scenario weather for one step; scenarios are not confidence bounds; "
                        "missing outdoor uncertainty is not quantified; no commands."),
    }


def save_advisory(engine, result):
    """Persist an available advisory separately from simulation plans."""
    if not result.get("available") or result.get("mode") != "advisory" or not result.get("rooms"):
        raise ValueError("An available advisory result is required")
    created = datetime.fromisoformat(result["created_at"])
    if created.utcoffset() is None:
        raise ValueError("Advisory timestamp must include a timezone")
    details = {key: value for key, value in result.items() if key != "rooms"}
    with engine.begin() as conn:
        inserted = conn.execute(insert(models.optimizer_plans).values(
            created_at=created, mode="advisory",
            details_json=json.dumps(details, allow_nan=False, sort_keys=True),
        ))
        plan_id = inserted.inserted_primary_key[0]
        conn.execute(insert(models.optimizer_plan_intervals), [
            {"plan_id": plan_id, "room_id": room["room_id"], "starts_at": created,
             "data_json": json.dumps(room, allow_nan=False, sort_keys=True)}
            for room in result["rooms"]
        ])
    return plan_id
