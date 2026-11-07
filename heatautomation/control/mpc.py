"""Shadow-only mixed-integer thermal scheduling using scipy/HiGHS."""

import math
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix


class PlanningUnavailable(ValueError):
    """Input data, model quality, or feasible comfort constraints are unavailable."""


@dataclass(frozen=True)
class Interval:
    start: datetime
    price: float
    outdoor_c: float
    pump_capacity_kw: float
    pump_cop: float
    stove_capacity_kw: float


@dataclass(frozen=True)
class Settings:
    minimum_c: float
    target_c: float
    maximum_c: float
    pellet_cost_per_kwh: float
    electricity_extra_per_kwh: float = 0.0
    deviation_penalty: float = 0.05
    switch_penalty: float = 0.1
    min_runtime_steps: int = 2
    min_off_steps: int = 2
    uncertainty_margin_c: float = 0.25
    max_rate_rmse: float = 1.0
    min_support: int = 20
    solver_time_limit_seconds: float = 20.0
    max_solver_gap: float = 0.10


def _validate(intervals, recent, initial, settings):
    values = (settings.minimum_c, settings.target_c, settings.maximum_c,
              settings.pellet_cost_per_kwh, settings.electricity_extra_per_kwh,
              settings.deviation_penalty, settings.switch_penalty,
              settings.uncertainty_margin_c, settings.max_rate_rmse,
              settings.solver_time_limit_seconds, settings.max_solver_gap)
    if not all(math.isfinite(x) for x in values):
        raise ValueError("MPC settings must be finite")
    if not (-10 <= settings.minimum_c <= settings.target_c <= settings.maximum_c <= 50):
        raise ValueError("Invalid comfort range")
    if (settings.pellet_cost_per_kwh <= 0 or settings.deviation_penalty < 0
            or settings.switch_penalty < 0 or settings.uncertainty_margin_c < 0
            or settings.max_rate_rmse <= 0 or settings.min_support < 1
            or settings.solver_time_limit_seconds <= 0
            or not 0 <= settings.max_solver_gap < 1
            or settings.min_runtime_steps < 1 or settings.min_off_steps < 1):
        raise ValueError("Invalid cost, margin, model quality or timing setting")
    if settings.minimum_c + settings.uncertainty_margin_c > min(
            settings.maximum_c, settings.target_c + 2) - settings.uncertainty_margin_c:
        raise ValueError("Uncertainty margin leaves no comfort range")
    if not 1 <= len(intervals) <= 144 or len(recent) != 7:
        raise ValueError("Expected 1-144 intervals and seven recent temperatures")
    if any(not math.isfinite(x) or not -10 <= x <= 50 for x in recent):
        raise ValueError("Invalid recent temperature")
    if set(initial) != {"heatpump", "pelletstove"} or any(
            type(value) is not bool for value in initial.values()):
        raise ValueError("Initial state must contain boolean heatpump and pelletstove states")
    for i, step in enumerate(intervals):
        if step.start.tzinfo is None or step.start.utcoffset() is None:
            raise ValueError("Interval times must be timezone aware")
        if i and step.start.astimezone(timezone.utc) != (
                intervals[i - 1].start.astimezone(timezone.utc) + timedelta(minutes=15)):
            raise ValueError("Intervals must be consecutive 15-minute periods")
        if not all(math.isfinite(x) for x in (
                step.price, step.outdoor_c, step.pump_capacity_kw,
                step.pump_cop, step.stove_capacity_kw)):
            raise ValueError("Forecast values must be finite")
        if (not -60 <= step.outdoor_c <= 60 or step.pump_capacity_kw < 0
                or step.stove_capacity_kw < 0 or step.pump_cop <= 0):
            raise ValueError("Invalid capacity, COP or outdoor temperature")


class _Problem:
    def __init__(self):
        self.cost, self.lower, self.upper, self.integer = [], [], [], []
        self.rows, self.lb, self.ub = [], [], []

    def var(self, low, high, cost=0, integer=False):
        index = len(self.cost)
        self.cost.append(cost)
        self.lower.append(low)
        self.upper.append(high)
        self.integer.append(int(integer))
        return index

    def row(self, terms, low=-np.inf, high=np.inf):
        self.rows.append(terms)
        self.lb.append(low)
        self.ub.append(high)

    def solve(self, seconds):
        r, c, data = [], [], []
        for i, row in enumerate(self.rows):
            for col, value in row.items():
                r.append(i)
                c.append(col)
                data.append(value)
        matrix = coo_matrix((data, (r, c)), shape=(len(self.rows), len(self.cost))).tocsc()
        return milp(np.array(self.cost), integrality=np.array(self.integer),
                    bounds=Bounds(self.lower, self.upper),
                    constraints=LinearConstraint(matrix, self.lb, self.ub),
                    options={"time_limit": seconds})

    def feasible(self, values, tolerance=1e-5):
        if values is None or not np.isfinite(values).all():
            return False
        if any(value < low - tolerance or value > high + tolerance
               or (integer and abs(value - round(value)) > tolerance)
               for value, low, high, integer in zip(
                   values, self.lower, self.upper, self.integer)):
            return False
        for row, low, high in zip(self.rows, self.lb, self.ub):
            value = sum(values[index] * coefficient for index, coefficient in row.items())
            if value < low - tolerance or value > high + tolerance:
                return False
        return True


def _product(problem, x, binary, lower=-10, upper=50):
    """Exact binary-continuous product, including negative temperatures."""
    product = problem.var(lower, upper)
    problem.row({product: 1, binary: -lower}, low=0)
    problem.row({product: 1, binary: -upper}, high=0)
    problem.row({product: 1, x: -1, binary: -upper}, low=-upper)
    problem.row({product: 1, x: -1, binary: -lower}, high=-lower)
    return product


def optimize(intervals, recent_indoor_c, initial_state, predictor, settings):
    """Return a non-actuating 15-minute plan; initial state duration is unknown."""
    _validate(intervals, recent_indoor_c, initial_state, settings)
    artifact = predictor.artifact
    models = artifact.get("models", {})
    metrics = artifact.get("metrics", {})
    support = artifact.get("support", {})
    allowed = []
    for mode in range(4):
        regime = "cooling" if mode == 0 else "heating"
        metric = metrics.get(regime, {})
        model = models.get(regime)
        rmse = metric.get("rmse_c_per_hour")
        if (model is not None and rmse is not None and math.isfinite(rmse)
                and rmse <= settings.max_rate_rmse
                and metric.get("validation_samples", 0) >= 20
                and support.get(regime, {}).get(mode, 0) >= settings.min_support
                and len(model.coef_) == 5
                and np.isfinite(model.coef_).all() and math.isfinite(model.intercept_)):
            allowed.append(mode)
    initial_mode = int(initial_state["heatpump"]) + 2 * int(initial_state["pelletstove"])
    if 0 not in allowed or initial_mode not in allowed:
        raise PlanningUnavailable("Cooling or current source combination lacks a validated model")
    if any((mode & 1 and not all(s.pump_capacity_kw > 0 for s in intervals))
           or (mode & 2 and not all(s.stove_capacity_kw > 0 for s in intervals))
           for mode in allowed if mode == initial_mode):
        raise PlanningUnavailable("Current source has unavailable forecast capacity")

    p = _Problem()
    n = len(intervals)
    temperature = [p.var(recent_indoor_c[-1], recent_indoor_c[-1])]
    upper = min(settings.maximum_c, settings.target_c + 2) - settings.uncertainty_margin_c
    lower = settings.minimum_c + settings.uncertainty_margin_c
    temperature += [p.var(lower, upper) for _ in intervals]
    modes, switches = [], []
    for t, step in enumerate(intervals):
        choice = {}
        for mode in allowed:
            if ((mode & 1 and step.pump_capacity_kw <= 0)
                    or (mode & 2 and step.stove_capacity_kw <= 0)):
                continue
            kw_pump = step.pump_capacity_kw if mode & 1 else 0
            kw_stove = step.stove_capacity_kw if mode & 2 else 0
            cost = 0.25 * (kw_pump / step.pump_cop *
                           (step.price + settings.electricity_extra_per_kwh)
                           + kw_stove * settings.pellet_cost_per_kwh)
            choice[mode] = p.var(0, 1, cost, True)
        if not choice:
            raise PlanningUnavailable("No modeled source combination for interval")
        modes.append(choice)
        p.row({z: 1 for z in choice.values()}, low=1, high=1)
        deviation = p.var(0, 60, settings.deviation_penalty)
        p.row({deviation: 1, temperature[t + 1]: -1}, low=-settings.target_c)
        p.row({deviation: 1, temperature[t + 1]: 1}, low=settings.target_c)
        equation = {temperature[t + 1]: 1, temperature[t]: -1}
        for mode, z in choice.items():
            model = models["cooling" if mode == 0 else "heating"]
            coeff = model.coef_
            indoor_product = _product(p, temperature[t], z)
            lag_product = (_product(p, temperature[t - 2], z) if t >= 2 else None)
            lag = recent_indoor_c[3 if t == 1 else 0] if t < 2 else 0
            constant = (float(model.intercept_) + coeff[1] * step.outdoor_c
                        + coeff[3] * (step.pump_capacity_kw if mode & 1 else 0)
                        + coeff[4] * (step.stove_capacity_kw if mode & 2 else 0)
                        + coeff[2] * lag)
            equation[z] = -0.25 * constant
            equation[indoor_product] = -0.25 * coeff[0]
            if lag_product is not None:
                equation[lag_product] = -0.25 * coeff[2]
        p.row(equation, low=0, high=0)
        switches.append([p.var(0, 1, settings.switch_penalty, True) for _ in range(2)])

    for t in range(n):
        for source in range(2):
            bit = 1 << source
            current = {z: 1 for mode, z in modes[t].items() if mode & bit}
            previous = ({z: 1 for mode, z in modes[t - 1].items() if mode & bit}
                        if t else {})
            prior = int(initial_state[("heatpump", "pelletstove")[source]]) if t == 0 else 0
            delta = dict(current)
            for z in previous:
                delta[z] = delta.get(z, 0) - 1
            switch = switches[t][source]
            p.row({**delta, switch: -1}, high=prior)
            p.row({**{k: -v for k, v in delta.items()}, switch: -1}, high=-prior)
            # Unknown elapsed duration: conservatively hold the current state at startup.
            initial_on = initial_state[("heatpump", "pelletstove")[source]]
            if t < (settings.min_runtime_steps if initial_on else settings.min_off_steps):
                p.row(current, low=int(initial_on), high=int(initial_on))
            dwell_on = settings.min_runtime_steps
            dwell_off = settings.min_off_steps
            if t + dwell_on > n:
                p.row(delta, high=prior)
            if t + dwell_off > n:
                p.row(delta, low=prior)
            for j in range(t, min(n, t + max(dwell_on, dwell_off))):
                future = {z: 1 for mode, z in modes[j].items() if mode & bit}
                if j < t + dwell_on:
                    row = dict(future)
                    for z, value in delta.items():
                        row[z] = row.get(z, 0) - value
                    p.row(row, low=-prior)
                if j < t + dwell_off:
                    row = {z: -v for z, v in future.items()}
                    for z, value in delta.items():
                        row[z] = row.get(z, 0) + value
                    p.row(row, low=prior - 1)

    result = p.solve(settings.solver_time_limit_seconds)
    gap = getattr(result, "mip_gap", None)
    if not p.feasible(result.x):
        raise PlanningUnavailable("No feasible plan: {}".format(result.message))
    if not result.success:
        if result.status != 1 or gap is None or not math.isfinite(gap) or gap > settings.max_solver_gap:
            raise PlanningUnavailable("Solver did not meet the optimality-gap limit: {}".format(result.message))
        logging.warning("MPC time limit reached; using feasible plan with %.2f%% solver gap.",
                        100 * gap)
    plan = []
    for t, step in enumerate(intervals):
        mode = max(modes[t], key=lambda m: result.x[modes[t][m]])
        pump = step.pump_capacity_kw if mode & 1 else 0.0
        stove = step.stove_capacity_kw if mode & 2 else 0.0
        electricity = pump / step.pump_cop * 0.25
        pellet_heat = stove * 0.25
        plan.append({
            "start": step.start.astimezone(timezone.utc).isoformat(),
            "price": step.price, "outdoor_c": step.outdoor_c,
            "predicted_indoor_c": float(result.x[temperature[t + 1]]),
            "target_c": settings.target_c, "mode": mode,
            "heatpump_kw": pump, "pelletstove_kw": stove,
            "estimated_electricity_kwh": electricity,
            "estimated_pellet_heat_kwh": pellet_heat,
            "estimated_cost": electricity * (step.price + settings.electricity_extra_per_kwh)
                              + pellet_heat * settings.pellet_cost_per_kwh,
            "solver_gap": float(gap) if gap is not None and math.isfinite(gap) else None,
            "solver_optimal": bool(result.success),
        })
    return plan
