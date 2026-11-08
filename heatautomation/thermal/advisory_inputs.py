"""Validate independently supplied, time-bounded equipment scenario inputs."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator

from .radiators import Circuit


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class HeatScenario(StrictInput):
    low: StrictFloat = Field(ge=0)
    nominal: StrictFloat = Field(ge=0)
    high: StrictFloat = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if not self.low <= self.nominal <= self.high:
            raise ValueError("Heat scenarios must be ordered")
        return self


class RoomTemperatureInput(StrictInput):
    device_id: str = Field(min_length=1)
    value_c: StrictFloat = Field(ge=-30, le=60)
    quality: Literal["good", "suspect"]
    classification: Literal["measured"]
    connection_method: Literal["CLOUD", "LOCAL_HTTP", "LOCAL_HTTPS", "UNKNOWN"]


class CircuitInput(StrictInput):
    generator_id: str = Field(min_length=1)
    generator_available: bool = Field(strict=True)
    available_w: StrictFloat = Field(ge=0)
    supply_c: StrictFloat
    return_c: StrictFloat
    exponent: StrictFloat = Field(gt=0)
    delivery_factors: Tuple[StrictFloat, StrictFloat, StrictFloat]

    @model_validator(mode="after")
    def validate_physics(self):
        if self.return_c > self.supply_c:
            raise ValueError("Return temperature cannot exceed supply temperature")
        if (any(not 0 <= value <= 1 for value in self.delivery_factors)
                or sorted(self.delivery_factors) != list(self.delivery_factors)):
            raise ValueError("Delivery factors must be ordered values between zero and one")
        return self

    def circuit(self):
        return Circuit(self.generator_available, self.available_w, self.supply_c, self.return_c,
                       self.exponent, self.delivery_factors, self.generator_id)


class EquipmentInputs(StrictInput):
    observed_at: datetime
    classification: Literal["measured", "estimated"]
    provenance: str = Field(min_length=1, max_length=256)
    circuit: Optional[CircuitInput] = None
    room_temperatures: Dict[str, RoomTemperatureInput] = Field(default_factory=dict)
    direct_heat_w: Dict[str, Dict[str, HeatScenario]] = Field(default_factory=dict)
    outdoor_uncertainty_c: Optional[StrictFloat] = Field(default=None, ge=0)
    cost_per_delivered_kwh: Dict[str, StrictFloat] = Field(default_factory=dict)
    cost_currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_inputs(self):
        if self.observed_at.utcoffset() is None:
            raise ValueError("Equipment observation must have a timezone")
        if any(value < 0 for value in self.cost_per_delivered_kwh.values()):
            raise ValueError("Energy costs must be nonnegative")
        if self.cost_per_delivered_kwh and not self.cost_currency:
            raise ValueError("Cost currency is required when rates are supplied")
        return self

    def require_fresh(self, now, freshness_seconds=600):
        if now.utcoffset() is None:
            raise ValueError("Current time must have a timezone")
        age = (now - self.observed_at).total_seconds()
        if not 0 <= age <= freshness_seconds:
            raise ValueError("Equipment observations are stale or future-dated")
        return self


def load_equipment_inputs(path, now, freshness_seconds=600):
    with Path(path).open(encoding="utf-8") as file:
        data = json.load(file)
    return EquipmentInputs.model_validate(data).require_fresh(
        now.astimezone(timezone.utc), freshness_seconds)
