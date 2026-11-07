"""Score shadow forecasts against historical observations without hindsight retraining."""

import math
from bisect import bisect_left
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .mpc import optimize


def evaluate(plan, observations):
    """Score only observations close to interval ends; costs remain unmeasured."""
    measurements = []
    for row in observations:
        timestamp = datetime.fromisoformat(row["timestamp"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Historical observation timestamps must include a timezone")
        temp = row.get("indoor_c")
        if temp is not None and math.isfinite(temp):
            measurements.append((timestamp.astimezone(timezone.utc), row))
    measurements.sort(key=lambda pair: pair[0])
    times = [item[0] for item in measurements]
    result = []
    for step in plan:
        end = datetime.fromisoformat(step["start"]) + timedelta(minutes=15)
        if not times or times[-1] < end:
            continue
        index = bisect_left(times, end)
        candidates = [(abs((times[i] - end).total_seconds()), measurements[i][1])
                      for i in (index - 1, index) if 0 <= i < len(times)
                      and abs((times[i] - end).total_seconds()) <= 150]
        if not candidates:
            continue
        _, observed = min(candidates, key=lambda pair: pair[0])
        pump = observed.get("heatpump_capacity_kw")
        stove = observed.get("pelletstove_capacity_kw")
        compatible = (pump is not None and stove is not None
                      and (pump > 0) == bool(step["mode"] & 1)
                      and (stove > 0) == bool(step["mode"] & 2))
        result.append({
            "start": step["start"], "actual_indoor_c": observed["indoor_c"],
            "temperature_error_c": step["predicted_indoor_c"] - observed["indoor_c"],
            "source_match_at_end": compatible,
            "actual_heating_consumption_kwh": None,
            "actual_heating_cost": None,
        })
    return result


def simulate(intervals, recent_indoor_c, initial_state, predictor, settings, observations):
    """Replay with a model created before the horizon; supplied forecasts may be hindsight."""
    if not intervals:
        raise ValueError("Simulation requires at least one interval")
    created = datetime.fromisoformat(predictor.artifact["created_at"])
    if created.tzinfo is None or created.utcoffset() is None or created > intervals[0].start:
        raise ValueError("Simulation requires a model created before the planning horizon")
    plan = optimize(intervals, recent_indoor_c, initial_state, predictor, settings)
    return {"plan": plan, "evaluation": evaluate(plan, observations),
            "warning": "Results use the supplied forecast inputs; observed future inputs are hindsight."}


def compare_no_preheat(intervals, recent_indoor_c, initial_state, predictor, settings, plan):
    """Compare modeled costs against the same optimizer without above-target heating."""
    baseline = optimize(intervals, recent_indoor_c, initial_state, predictor,
                        replace(settings, maximum_c=settings.target_c))
    planned_cost = sum(row["estimated_cost"] for row in plan)
    baseline_cost = sum(row["estimated_cost"] for row in baseline)
    return {"modeled_baseline_cost": baseline_cost, "modeled_plan_cost": planned_cost,
            "modeled_savings": baseline_cost - planned_cost}
