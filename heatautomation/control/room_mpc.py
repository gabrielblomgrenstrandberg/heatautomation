"""Simulation-only, coupled-room 36-hour mixed-integer heating planner."""

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from scipy.linalg import expm
from sqlalchemy import insert, select

from heatautomation.house import models
from heatautomation.thermal.model import room_heat_demand_w
from heatautomation.thermal.rooms import RoomThermalService
from .mpc import PlanningUnavailable, _Problem


INTERVAL_SECONDS = 900
INTERVAL_COUNT = 144
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Room:
    id: str
    initial_c: float
    capacity_j_per_k: float
    heat_loss_w_per_k: float
    conductances_w_per_k: Mapping[str, float]
    minimum_c: float
    maximum_c: float
    targets_c: Sequence[float]
    uncertainty_c: float
    floor_id: Optional[str] = None
    max_preheat_c: float = 2.0


@dataclass(frozen=True)
class Source:
    id: str
    kind: str  # radiator circuit, pellet stove, or electric heater
    room_capacity_kw: Mapping[str, float]
    capacity_kw: float
    cost_per_kwh: float = 0.0  # pellet cost per kWh delivered
    initial_on: Optional[bool] = None
    initial_elapsed_steps: Optional[int] = None
    min_on_steps: int = 1
    min_off_steps: int = 1
    max_on_steps: Optional[int] = None
    capacity_forecast_kw: Optional[Sequence[float]] = None


@dataclass(frozen=True)
class Forecast:
    start: datetime
    electricity_price: float
    outdoor_c: float
    radiator_cop: float


def forecasts_from_legacy(intervals):
    """Adapt the existing MPC provider intervals without shortening the horizon."""
    intervals = list(intervals)
    if len(intervals) != INTERVAL_COUNT:
        raise PlanningUnavailable("Price and weather must cover all 144 intervals")
    return ([Forecast(step.start, step.price, step.outdoor_c, step.pump_cop)
             for step in intervals],
            [step.pump_capacity_kw for step in intervals])


def _real(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number" % name)
    return float(value)


def _validate(rooms, sources, forecasts, solver_seconds):
    _real(solver_seconds, "solver_seconds")
    if solver_seconds <= 0 or len(forecasts) != INTERVAL_COUNT or not rooms or not sources:
        raise ValueError("A positive solver time, rooms, sources and exactly 144 intervals are required")
    room_ids = {room.id for room in rooms}
    if (len(room_ids) != len(rooms) or any(not isinstance(r, str) or not r for r in room_ids)
            or len({source.id for source in sources}) != len(sources)
            or any(not isinstance(source.id, str) or not source.id for source in sources)):
        raise ValueError("Room and source IDs must be unique nonempty strings")
    for room in rooms:
        for value, name in ((room.initial_c, "initial temperature"), (room.capacity_j_per_k, "capacity"),
                            (room.heat_loss_w_per_k, "heat loss"), (room.minimum_c, "minimum"),
                            (room.maximum_c, "maximum"), (room.uncertainty_c, "uncertainty"),
                            (room.max_preheat_c, "maximum preheating")):
            _real(value, name)
        if (not -30 <= room.initial_c <= 60 or room.capacity_j_per_k <= 0
                or room.heat_loss_w_per_k < 0 or room.uncertainty_c < 0
                or not 0 <= room.max_preheat_c <= 2
                or not -30 <= room.minimum_c <= room.maximum_c <= 60
                or len(room.targets_c) != INTERVAL_COUNT):
            raise ValueError("Invalid room parameters, comfort bounds or targets")
        for target in room.targets_c:
            _real(target, "target")
            if not room.minimum_c <= target <= room.maximum_c:
                raise ValueError("Room target must be within its comfort limits")
            if target + room.uncertainty_c > min(room.maximum_c, target + room.max_preheat_c) - room.uncertainty_c:
                raise ValueError("Room uncertainty leaves no feasible comfort band")
        for neighbor, value in room.conductances_w_per_k.items():
            _real(value, "conductance")
            if neighbor == room.id or neighbor not in room_ids or value < 0:
                raise ValueError("Invalid thermal relationship")
    lookup = {r.id: r for r in rooms}
    for room in rooms:
        for neighbor, value in room.conductances_w_per_k.items():
            if not math.isclose(value, lookup[neighbor].conductances_w_per_k.get(room.id, -1),
                                rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Thermal relationships must be symmetric")
    for source in sources:
        if source.kind not in ("radiator", "pellet", "electric"):
            raise ValueError("Unsupported heating source kind")
        _real(source.capacity_kw, "source capacity")
        _real(source.cost_per_kwh, "source cost")
        if source.capacity_kw <= 0 or source.cost_per_kwh < 0 or not source.room_capacity_kw:
            raise ValueError("Source capacity and room assignments are required")
        for room, value in source.room_capacity_kw.items():
            _real(value, "room capacity")
            if room not in room_ids or value <= 0 or value > source.capacity_kw:
                raise ValueError("Invalid source room capacity")
        if source.capacity_forecast_kw is not None:
            if source.kind != "radiator" or len(source.capacity_forecast_kw) != INTERVAL_COUNT:
                raise ValueError("Only radiator circuits accept 144 forecast capacities")
            for value in source.capacity_forecast_kw:
                _real(value, "forecast capacity")
                if not 0 <= value <= source.capacity_kw:
                    raise ValueError("Forecast capacity must be within the circuit capacity")
        if source.kind != "radiator":
            if len(source.room_capacity_kw) != 1 or next(iter(source.room_capacity_kw.values())) != source.capacity_kw:
                raise ValueError("On/off generators must deliver their full capacity to one room")
            if type(source.initial_on) is not bool or type(source.initial_elapsed_steps) is not int:
                raise ValueError("On/off sources require known initial state and elapsed time")
            if (source.initial_elapsed_steps < 0 or type(source.min_on_steps) is not int
                    or type(source.min_off_steps) is not int or source.min_on_steps < 1
                    or source.min_off_steps < 1
                    or (source.max_on_steps is not None and (
                        type(source.max_on_steps) is not int or source.max_on_steps < source.min_on_steps
                        or (source.initial_on and source.initial_elapsed_steps > source.max_on_steps)))):
                raise ValueError("Invalid source dwell or maximum runtime")
    for i, step in enumerate(forecasts):
        if not isinstance(step.start, datetime) or step.start.utcoffset() is None:
            raise ValueError("Forecast times require a timezone")
        instant = step.start.astimezone(timezone.utc)
        if instant.minute % 15 or instant.second or instant.microsecond:
            raise ValueError("Forecast times must start on a quarter hour")
        if i and step.start.astimezone(timezone.utc) != (
                forecasts[i - 1].start.astimezone(timezone.utc) + timedelta(seconds=INTERVAL_SECONDS)):
            raise ValueError("Forecast intervals must be consecutive 15-minute periods")
        _real(step.electricity_price, "electricity price")
        _real(step.outdoor_c, "outdoor temperature")
        _real(step.radiator_cop, "radiator COP")
        if not -60 <= step.outdoor_c <= 60 or step.radiator_cop <= 0:
            raise ValueError("Invalid outdoor temperature or radiator COP")


def _dynamics(rooms):
    """Exact discretization of the linear heat-balance ODE."""
    count = len(rooms)
    index = {room.id: i for i, room in enumerate(rooms)}
    matrix = np.zeros((count, count))
    inputs = np.zeros((count, count + 1))
    for i, room in enumerate(rooms):
        matrix[i, i] = -(room.heat_loss_w_per_k + sum(room.conductances_w_per_k.values())) / room.capacity_j_per_k
        for neighbor, conductance in room.conductances_w_per_k.items():
            matrix[i, index[neighbor]] = conductance / room.capacity_j_per_k
        inputs[i, i] = 1000 / room.capacity_j_per_k
        inputs[i, count] = room.heat_loss_w_per_k / room.capacity_j_per_k
    augmented = np.zeros((2 * count + 1, 2 * count + 1))
    augmented[:count, :count] = matrix
    augmented[:count, count:] = inputs
    discrete = expm(augmented * INTERVAL_SECONDS)
    return discrete[:count, :count], discrete[:count, count:2 * count], discrete[:count, -1]


def _binary_dwell(problem, source, states):
    n = len(states)
    prior = int(source.initial_on)
    remaining = max(0, (source.min_on_steps if prior else source.min_off_steps)
                    - source.initial_elapsed_steps)
    for t in range(min(remaining, n)):
        problem.row({states[t]: 1}, low=prior, high=prior)
    starts = []
    for t, state in enumerate(states):
        start = problem.var(0, 1, integer=True)
        stop = problem.var(0, 1, integer=True)
        starts.append(start)
        terms = {state: 1, start: -1, stop: 1}
        if t:
            terms[states[t - 1]] = -1
            problem.row(terms, low=0, high=0)
        else:
            problem.row(terms, low=prior, high=prior)
        problem.row({start: 1, stop: 1}, high=1)
        # Do not plan switches that cannot satisfy their dwell within the horizon.
        if t + source.min_on_steps > n:
            problem.row({start: 1}, high=0)
        else:
            for j in range(t, t + source.min_on_steps):
                problem.row({states[j]: 1, start: -1}, low=0)
        if t + source.min_off_steps > n:
            problem.row({stop: 1}, high=0)
        else:
            for j in range(t, t + source.min_off_steps):
                problem.row({states[j]: 1, stop: 1}, high=1)
    if source.max_on_steps is not None:
        maximum = source.max_on_steps
        if prior:
            remaining_on = max(0, maximum - source.initial_elapsed_steps)
            if remaining_on < n:
                problem.row({states[remaining_on]: 1}, high=0)
        for t in range(n - maximum):
            problem.row({states[j]: 1 for j in range(t, t + maximum + 1)}, high=maximum)


def optimize_rooms(rooms, sources, forecasts, *, solver_seconds=30, max_solver_gap=0.1):
    """Optimize estimated delivered heat only. Never dispatches device commands."""
    rooms, sources, forecasts = list(rooms), list(sources), list(forecasts)
    _validate(rooms, sources, forecasts, solver_seconds)
    _real(max_solver_gap, "max_solver_gap")
    if not 0 <= max_solver_gap < 1:
        raise ValueError("max_solver_gap must be between zero and one")
    p = _Problem()
    a, b, d = _dynamics(rooms)
    n = len(forecasts)
    temperatures = {}
    heat = {}
    states = {}
    for room in rooms:
        temperatures[room.id] = [p.var(room.initial_c, room.initial_c)]
        for t in range(n):
            target = room.targets_c[t]
            lower = max(room.minimum_c, target) + room.uncertainty_c
            upper = min(room.maximum_c, target + room.max_preheat_c) - room.uncertainty_c
            temperatures[room.id].append(p.var(lower, upper))
    for source in sources:
        if source.kind != "radiator":
            states[source.id] = [p.var(0, 1, cost=(
                source.capacity_kw * 0.25 * (
                    source.cost_per_kwh if source.kind == "pellet"
                    else forecasts[t].electricity_price)), integer=True)
                for t in range(n)]
            _binary_dwell(p, source, states[source.id])
        for room_id, limit in source.room_capacity_kw.items():
            values = []
            for t, step in enumerate(forecasts):
                cost = step.electricity_price * 0.25 / step.radiator_cop if source.kind == "radiator" else 0
                value = p.var(0, limit, cost=cost)
                values.append(value)
                if source.kind != "radiator":
                    p.row({value: 1, states[source.id][t]: -source.capacity_kw}, low=0, high=0)
            heat[source.id, room_id] = values
        if source.kind == "radiator":
            for t in range(n):
                maximum = (source.capacity_forecast_kw[t] if source.capacity_forecast_kw is not None
                           else source.capacity_kw)
                p.row({heat[source.id, room][t]: 1 for room in source.room_capacity_kw},
                      high=maximum)
    for t, step in enumerate(forecasts):
        for i, room in enumerate(rooms):
            row = {temperatures[room.id][t + 1]: 1}
            for j, other in enumerate(rooms):
                row[temperatures[other.id][t]] = -a[i, j]
                for source in sources:
                    key = (source.id, other.id)
                    if key in heat:
                        row[heat[key][t]] = -b[i, j]
            rhs = d[i] * step.outdoor_c
            p.row(row, low=rhs, high=rhs)
    solution = p.solve(solver_seconds)
    gap = getattr(solution, "mip_gap", None)
    acceptable = solution.success or (
        solution.status == 1 and gap is not None and math.isfinite(gap) and gap <= max_solver_gap)
    if not acceptable or not p.feasible(solution.x):
        raise PlanningUnavailable("No verified room plan within solver gap: %s" % solution.message)
    if not solution.success:
        LOGGER.warning("Room MPC reached its time limit; accepting a feasible plan with gap %.2f%%",
                       gap * 100)
    x = solution.x
    plan = []
    for t, step in enumerate(forecasts):
        room_steps = []
        for i, room in enumerate(rooms):
            powers = {source.id: max(0.0, float(x[heat[source.id, room.id][t]]))
                      for source in sources if (source.id, room.id) in heat}
            active = {source: kw for source, kw in powers.items() if kw > 1e-6}
            costs = {}
            for source in sources:
                kw = powers.get(source.id, 0)
                rate = (source.cost_per_kwh if source.kind == "pellet"
                        else step.electricity_price / step.radiator_cop
                        if source.kind == "radiator" else step.electricity_price)
                costs[source.id] = kw * 0.25 * rate
            current = float(x[temperatures[room.id][t]])
            next_c = float(x[temperatures[room.id][t + 1]])
            transfers = {neighbor: conductance * (
                float(x[temperatures[neighbor][t]]) - current) / 1000
                for neighbor, conductance in room.conductances_w_per_k.items()}
            target = room.targets_c[t]
            maintenance = max(0.0, (room.heat_loss_w_per_k * (target - step.outdoor_c)
                                   - sum(g * (float(x[temperatures[n][t]]) - target)
                                         for n, g in room.conductances_w_per_k.items())) / 1000)
            current_by_room = {other.id: float(x[temperatures[other.id][t]]) for other in rooms}
            demand = room_heat_demand_w(
                current, target, step.outdoor_c, room.capacity_j_per_k,
                room.heat_loss_w_per_k, room.conductances_w_per_k, current_by_room,
                INTERVAL_SECONDS) / 1000
            room_steps.append({
                "room_id": room.id, "floor_id": room.floor_id,
                "predicted_temperature_c": next_c,
                "initial_temperature_c": current, "target_c": target,
                "heating_demand_kw": demand, "maintenance_demand_kw": maintenance,
                "planned_heat_kw": powers, "selected_sources": list(active),
                "planned_activity": {key: value > 1e-6 for key, value in powers.items()},
                "expected_cost": sum(costs.values()), "source_costs": costs,
                "inter_room_transfer_kw": transfers,
                "explanation": (
                    "Selected %s to offset heat loss and meet comfort or preheat constraints"
                    % ", ".join(sorted(active))
                    if active else "No direct heat needed in this interval; thermal state and transfers suffice"),
            })
        plan.append({"start": step.start.astimezone(timezone.utc).isoformat(),
                     "outdoor_c": step.outdoor_c, "electricity_price": step.electricity_price,
                     "rooms": room_steps, "expected_cost": sum(r["expected_cost"] for r in room_steps)})
    return {"mode": "simulation", "interval_seconds": INTERVAL_SECONDS,
            "solver_optimal": bool(solution.success), "solver_gap": float(gap) if gap is not None else None,
            "expected_total_cost": sum(row["expected_cost"] for row in plan),
            "assumptions": "planned heat delivery and costs are estimates; no commands are issued",
            "intervals": plan}


def rooms_from_database(engine, forecasts, readings, sources, *, timezone_name):
    """Resolve room targets and validated physical assignments from the house database.

    Readings are {room_id: {value, observed_at, quality, classification,
                           source_device_id, uncertainty_c}}.
    Target schedules are interpreted in the supplied house timezone.
    """
    forecasts = list(forecasts)
    if len(forecasts) != INTERVAL_COUNT:
        raise PlanningUnavailable("A complete 36-hour forecast is required")
    for i, forecast in enumerate(forecasts):
        if not isinstance(forecast.start, datetime) or forecast.start.utcoffset() is None:
            raise ValueError("Forecast times require a timezone")
        instant = forecast.start.astimezone(timezone.utc)
        if instant.minute % 15 or instant.second or instant.microsecond:
            raise ValueError("Forecast times must start on a quarter hour")
        if i and instant != forecasts[i - 1].start.astimezone(timezone.utc) + timedelta(
                seconds=INTERVAL_SECONDS):
            raise ValueError("Forecast intervals must be consecutive")
    start = forecasts[0].start.astimezone(timezone.utc)
    zone = ZoneInfo(timezone_name)
    params, assigned, _ = RoomThermalService(engine)._configuration()
    with engine.connect() as conn:
        settings = {row.room_id: row for row in conn.execute(select(models.room_settings))}
        schedules = conn.execute(select(models.room_schedules)).all()
        overrides = conn.execute(select(models.room_overrides)).all()
        floors = {row.id: row.floor_id for row in conn.execute(
            select(models.rooms.c.id, models.rooms.c.floor_id).where(models.rooms.c.removed_at.is_(None)))}
        paths = conn.execute(select(models.heat_paths).where(
            models.heat_paths.c.removed_at.is_(None))).all()
        sensors = {row.id: row.room_id for row in conn.execute(
            select(models.devices.c.id, models.devices.c.room_id).where(
                models.devices.c.removed_at.is_(None)
                & models.devices.c.kind.in_(("thermostat", "sensor", "actuator_sensor"))))}
    path_types = {}
    for path in paths:
        path_types.setdefault(path.generator_id, set()).add(path.path_type)
    coverage = {room: set() for room in params}
    for source in sources:
        expected = "circuit" if source.kind == "radiator" else "direct"
        if source.id not in path_types or path_types[source.id] != {expected}:
            raise PlanningUnavailable("Heating source %s does not match a configured heat path" % source.id)
        for room in source.room_capacity_kw:
            if room not in assigned or source.id not in assigned[room]:
                raise PlanningUnavailable("Heating source is not assigned to room %s" % room)
            coverage[room].add(source.id)
    rooms = []
    for room_id, par in params.items():
        if not assigned[room_id] or coverage[room_id] != assigned[room_id]:
            raise PlanningUnavailable("Missing source or capacity for room %s" % room_id)
        setting = settings.get(room_id)
        if (setting is None or setting.min_c is None or setting.max_c is None
                or setting.target_c is None or "capacity_j_per_k" not in par
                or "ua_w_per_k" not in par):
            raise PlanningUnavailable("Missing comfort settings or thermal parameters for room %s" % room_id)
        reading = readings.get(room_id)
        if not isinstance(reading, dict) or reading.get("quality") != "good" or reading.get("classification") != "measured":
            raise PlanningUnavailable("A good measured temperature is required for room %s" % room_id)
        if sensors.get(reading.get("source_device_id")) != room_id:
            raise PlanningUnavailable("Temperature source is not assigned to room %s" % room_id)
        stamp = reading.get("observed_at")
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            raise PlanningUnavailable("Temperature timestamp missing for room %s" % room_id)
        age = (start - stamp.astimezone(timezone.utc)).total_seconds()
        if not 0 <= age <= INTERVAL_SECONDS:
            raise PlanningUnavailable("Temperature is stale or future-dated for room %s" % room_id)
        uncertainty = reading.get("uncertainty_c")
        if uncertainty is None:
            raise PlanningUnavailable("Temperature uncertainty is required for room %s" % room_id)
        room_schedules = [s for s in schedules if s.room_id == room_id]
        if any(s.start_min % 15 or s.end_min % 15 for s in room_schedules):
            raise PlanningUnavailable("Room schedule boundaries must align to 15-minute intervals")
        room_overrides = [o for o in overrides if o.room_id == room_id]
        targets = []
        for forecast in forecasts:
            current = forecast.start.astimezone(timezone.utc)
            local = current.astimezone(zone)
            minute = local.hour * 60 + local.minute
            matches = [s for s in room_schedules
                       if s.weekday == local.weekday() and s.start_min <= minute < s.end_min]
            if len(matches) > 1:
                raise PlanningUnavailable("Overlapping room schedules for %s" % room_id)
            target = matches[0].target_c if matches else setting.target_c
            active = []
            for override in room_overrides:
                begin = override.starts_at.replace(tzinfo=timezone.utc) if override.starts_at.tzinfo is None else override.starts_at.astimezone(timezone.utc)
                end = override.expires_at.replace(tzinfo=timezone.utc) if override.expires_at.tzinfo is None else override.expires_at.astimezone(timezone.utc)
                if begin < current + timedelta(seconds=INTERVAL_SECONDS) and end > current:
                    if begin > current or end < current + timedelta(seconds=INTERVAL_SECONDS):
                        raise PlanningUnavailable("Override boundary falls inside a planning interval")
                    active.append(override)
            if len(active) > 1:
                raise PlanningUnavailable("Overlapping room overrides for %s" % room_id)
            targets.append(active[0].target_c if active else target)
        rooms.append(Room(room_id, reading.get("value"), par["capacity_j_per_k"],
                          par["ua_w_per_k"], par["conductances_w_per_k"],
                          setting.min_c, setting.max_c, targets, uncertainty, floors[room_id],
                          setting.max_preheat_c))
    return rooms


def save_simulation(engine, plan):
    """Persist a simulation using the existing room optimizer tables."""
    if plan.get("mode") != "simulation" or len(plan.get("intervals", ())) != INTERVAL_COUNT:
        raise ValueError("Only complete simulation plans may be saved")
    with engine.begin() as conn:
        result = conn.execute(insert(models.optimizer_plans).values(
            created_at=datetime.now(timezone.utc), mode="simulation",
            details_json=json.dumps({k: v for k, v in plan.items() if k != "intervals"},
                                    allow_nan=False, sort_keys=True)))
        plan_id = result.inserted_primary_key[0]
        rows = [{"plan_id": plan_id, "room_id": room["room_id"],
                 "starts_at": datetime.fromisoformat(interval["start"]),
                 "data_json": json.dumps(room, allow_nan=False, sort_keys=True)}
                for interval in plan["intervals"] for room in interval["rooms"]]
        conn.execute(insert(models.optimizer_plan_intervals), rows)
    return plan_id
