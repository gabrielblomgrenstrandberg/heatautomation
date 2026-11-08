"""Device capability contracts independent of any vendor API."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Literal, Optional, Protocol, Union


CapabilityName = Literal[
    "read_temperature",
    "read_setpoint",
    "write_setpoint",
    "read_status",
    "set_power",
    "read_open_state",
]


@dataclass(frozen=True)
class Reading:
    value: Union[float, str, bool, None]
    unit: str
    observed_at: datetime
    received_at: datetime
    source_device_id: str
    quality: Literal["good", "suspect", "stale", "bad"]
    classification: Literal["measured", "estimated", "predicted", "unknown"]


@dataclass(frozen=True)
class CommandResult:
    outcome: Literal["simulated", "sent", "rejected", "failed", "timeout"]
    detail: Optional[str] = None


class TemperatureSensor(Protocol):
    def read_temperature(self) -> Reading:
        ...


class SetpointController(Protocol):
    def read_setpoint(self) -> Reading:
        ...

    def write_setpoint(self, celsius: float) -> CommandResult:
        ...


class HeatActuator(Protocol):
    def read_status(self) -> Reading:
        ...

    def set_power(self, on: bool) -> CommandResult:
        ...


class HeatGenerator(Protocol):
    def read_status(self) -> Reading:
        ...


class HeatingSource(Protocol):
    def get_status(self, electricity_price_per_kwh: Optional[float] = None) -> Dict[str, Any]:
        ...

    def set_power(self, enabled: bool, *, manual: bool = False) -> CommandResult:
        ...


class HeatEmitter(Protocol):
    def read_open_state(self) -> Reading:
        ...

    def rated_w_at_dt50(self) -> Optional[float]:
        ...
