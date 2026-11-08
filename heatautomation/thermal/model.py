"""Physics-based room heat balance and room-specific heat demand."""

import math
from typing import Dict, Mapping, Optional


def _finite(value, name):
    if not math.isfinite(value):
        raise ValueError("%s must be finite" % name)


def room_temperature_after_step(
    room_id,
    temperatures_c,
    capacity_j_per_k,
    ua_w_per_k,
    conductances_w_per_k,
    heat_input_w,
    outdoor_c,
    seconds,
    internal_gains_w=0.0,
):
    """Apply one explicit-Euler heat-balance step for a room."""
    for value, name in (
        (capacity_j_per_k, "capacity_j_per_k"),
        (ua_w_per_k, "ua_w_per_k"),
        (heat_input_w, "heat_input_w"),
        (outdoor_c, "outdoor_c"),
        (seconds, "seconds"),
        (internal_gains_w, "internal_gains_w"),
    ):
        _finite(value, name)
    if capacity_j_per_k <= 0 or ua_w_per_k < 0 or seconds <= 0:
        raise ValueError("Capacity and step must be positive; UA cannot be negative")
    if room_id not in temperatures_c or temperatures_c[room_id] is None:
        raise ValueError("A known current temperature is required for the room")

    temperature = temperatures_c[room_id]
    _finite(temperature, "room temperature")
    transfer_w = 0.0
    for neighbor_id, conductance in conductances_w_per_k.items():
        if conductance < 0 or not math.isfinite(conductance):
            raise ValueError("Thermal conductance must be finite and non-negative")
        neighbor_temperature = temperatures_c.get(neighbor_id)
        if neighbor_temperature is None:
            raise ValueError(
                "A known current temperature is required for neighbor %s" % neighbor_id
            )
        _finite(neighbor_temperature, "neighbor temperature")
        transfer_w += conductance * (neighbor_temperature - temperature)

    heat_loss_w = ua_w_per_k * (temperature - outdoor_c)
    derivative = (
        heat_input_w + transfer_w - heat_loss_w + internal_gains_w
    ) / capacity_j_per_k
    result = temperature + seconds * derivative
    _finite(result, "predicted temperature")
    return result


def room_heat_demand_w(
    current_c,
    target_c,
    outdoor_c,
    capacity_j_per_k,
    ua_w_per_k,
    conductance_by_neighbor,
    temperature_by_room,
    response_seconds,
):
    """Estimate room-specific heating power needed over a response horizon."""
    values = (current_c, target_c, outdoor_c, capacity_j_per_k,
              ua_w_per_k, response_seconds)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Demand inputs must be finite")
    if capacity_j_per_k <= 0 or ua_w_per_k < 0 or response_seconds <= 0:
        raise ValueError("Capacity and response time must be positive; UA cannot be negative")

    inter_room_w = 0.0
    for neighbor_id, conductance in conductance_by_neighbor.items():
        neighbor_temperature = temperature_by_room.get(neighbor_id)
        if neighbor_temperature is None:
            raise ValueError(
                "A known current temperature is required for neighbor %s" % neighbor_id
            )
        if conductance < 0 or not math.isfinite(conductance):
            raise ValueError("Thermal conductance must be finite and non-negative")
        _finite(neighbor_temperature, "neighbor temperature")
        inter_room_w += conductance * (neighbor_temperature - current_c)

    required_w = (
        capacity_j_per_k * (target_c - current_c) / response_seconds
        + ua_w_per_k * (current_c - outdoor_c)
        - inter_room_w
    )
    return max(0.0, required_w)


def simulate_step(
    temperatures_c: Mapping[str, Optional[float]],
    parameters: Mapping[str, dict],
    heat_input_w: Mapping[str, float],
    outdoor_c: float,
    seconds: float,
    internal_gains_w: Optional[Mapping[str, float]] = None,
):
    """Advance all known rooms synchronously, preserving unknown temperatures."""
    internal_gains_w = internal_gains_w or {}
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be finite and positive")
    if not math.isfinite(outdoor_c):
        raise ValueError("outdoor_c must be finite")

    max_rate = 0.0
    for room_id, current_c in temperatures_c.items():
        if current_c is None:
            continue
        room_parameters = parameters[room_id]
        capacity = room_parameters["capacity_j_per_k"]
        ua = room_parameters["ua_w_per_k"]
        conductances = room_parameters.get("conductances_w_per_k", {})
        if capacity <= 0 or ua < 0:
            raise ValueError("Capacity must be positive and UA cannot be negative")
        if any(value < 0 or not math.isfinite(value)
               for value in conductances.values()):
            raise ValueError("Thermal conductance must be finite and non-negative")
        rate = (ua + 2.0 * sum(conductances.values())) / capacity
        max_rate = max(max_rate, rate)

    subdivisions = max(1, math.ceil(seconds * max_rate))
    step_seconds = seconds / subdivisions
    current_temperatures = dict(temperatures_c)
    for _ in range(subdivisions):
        next_temperatures: Dict[str, Optional[float]] = {}
        for room_id, current_c in current_temperatures.items():
            if current_c is None:
                next_temperatures[room_id] = None
                continue
            room_parameters = parameters[room_id]
            next_temperatures[room_id] = room_temperature_after_step(
                room_id=room_id,
                temperatures_c=current_temperatures,
                capacity_j_per_k=room_parameters["capacity_j_per_k"],
                ua_w_per_k=room_parameters["ua_w_per_k"],
                conductances_w_per_k=room_parameters.get("conductances_w_per_k", {}),
                heat_input_w=heat_input_w.get(room_id, 0.0),
                outdoor_c=outdoor_c,
                seconds=step_seconds,
                internal_gains_w=internal_gains_w.get(room_id, 0.0),
            )
        current_temperatures = next_temperatures
    return current_temperatures
