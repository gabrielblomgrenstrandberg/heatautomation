"""Scenario estimates for a shared water circuit, never measured heat."""

import math
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Radiator:
    id: str
    room_id: str
    rated_w_at_dt50: Optional[float]
    demand_percent: Optional[float]
    room_temperature_c: Optional[float]


@dataclass(frozen=True)
class Circuit:
    generator_available: Optional[bool]
    available_w: Optional[float]
    supply_c: Optional[float]
    return_c: Optional[float]
    exponent: Optional[float]
    delivery_factors: Optional[Tuple[float, float, float]]
    generator_id: Optional[str] = None


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def estimate_delivery(circuit, radiators):
    """Estimate three assumed scenarios and enforce one shared generator limit.

    Demand is a reported control signal; delivery factors require independent
    calibration. The scenarios are not statistical confidence bounds.
    """
    radiators = list(radiators)
    if not radiators or len({r.id for r in radiators}) != len(radiators):
        raise ValueError("Unique radiators are required")
    missing = []
    if not isinstance(circuit.generator_id, str) or not circuit.generator_id:
        missing.append("generator identity")
    if type(circuit.generator_available) is not bool:
        missing.append("generator availability")
    if not _finite(circuit.available_w) or circuit.available_w < 0:
        missing.append("generator capacity")
    if not _finite(circuit.supply_c) or not _finite(circuit.return_c):
        missing.append("water temperatures")
    elif circuit.return_c > circuit.supply_c:
        missing.append("valid water temperatures")
    if not _finite(circuit.exponent) or circuit.exponent <= 0:
        missing.append("radiator exponent")
    factors = circuit.delivery_factors
    if (not isinstance(factors, (tuple, list)) or len(factors) != 3
            or any(not _finite(x) or not 0 <= x <= 1 for x in factors)
            or list(factors) != sorted(factors)):
        missing.append("calibrated delivery factors")
    for r in radiators:
        if not _finite(r.rated_w_at_dt50) or r.rated_w_at_dt50 <= 0:
            missing.append(r.id + ": rated output")
        if not _finite(r.demand_percent) or not 0 <= r.demand_percent <= 100:
            missing.append(r.id + ": Tado demand")
        if not _finite(r.room_temperature_c):
            missing.append(r.id + ": room temperature")
    if missing:
        return {"classification": "unknown", "missing": missing, "scenarios": None}
    mean_water = (circuit.supply_c + circuit.return_c) / 2
    capacity = {
        r.id: r.rated_w_at_dt50 * (max(0.0, mean_water - r.room_temperature_c) / 50) ** circuit.exponent
        for r in radiators
    }
    scenarios = {}
    for name, factor in zip(("low", "nominal", "high"), factors):
        requested = {r.id: capacity[r.id] * r.demand_percent / 100 * factor for r in radiators}
        total = sum(requested.values())
        limit = circuit.available_w if circuit.generator_available else 0.0
        scale = min(1.0, limit / total) if total else 1.0
        scenarios[name] = {r.id: requested[r.id] * scale for r in radiators}
    return {
        "classification": "estimated", "missing": [], "radiator_capacity_w": capacity,
        "scenarios": scenarios,
        "generator_available": circuit.generator_available,
        "generator_available_w": circuit.available_w if circuit.generator_available else 0.0,
        "assumptions": "Demand-scaled output and proportional sharing; factors are configured, not measured.",
    }
