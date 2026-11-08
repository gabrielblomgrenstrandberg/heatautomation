"""Read-only collection of observed device and weather states."""

import logging
import math
import time
from datetime import datetime, timezone

from .. import config
from ..integrations import kmp, sensibo, tibber, weather


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def collect(database, pump_capacity):
    start = time.monotonic()
    forecast = weather.get_forecast()
    outdoor = _number(forecast[0].get("temperature_c")) if forecast else None
    price = _number(tibber.get_spot_price()) if config.TIBBER_API_KEY else None
    pod = sensibo._first_result(sensibo.status(), "thermal observation")
    stove = kmp.get_status()
    state = pod.get("acState") if pod else None
    sensor = pod.get("mainMeasurementsSensor") if pod else None
    measurements = sensor.get("measurements") if isinstance(sensor, dict) else None
    state = state if isinstance(state, dict) else {}
    measurements = measurements if isinstance(measurements, dict) else {}
    pump_on = state.get("on") if isinstance(state.get("on"), bool) else None
    pump_mode = state.get("mode")
    if pump_on is False or (pump_on is True and pump_mode == "fan"):
        pump_kw = 0.0
    elif pump_on is True and pump_mode == "heat" and outdoor is not None:
        pump_kw = pump_capacity(outdoor)
    else:
        pump_kw = None

    stove_mode = stove.get("mode") if stove else None
    if stove_mode in kmp.OFF_MODES:
        stove_on = False
    elif stove_mode in kmp.RUNNING_MODES | kmp.IDLE_MODES:
        stove_on = True
    else:
        stove_on = None
    if stove_mode in kmp.OFF_MODES or stove_mode == "VILAR...":
        stove_kw = 0.0
    elif stove_mode == "HÖGEFFEKT":
        stove_kw = config.PELLET_STOVE_CAPACITY
    else:
        stove_kw = None

    if time.monotonic() - start > 120:
        logging.warning("Thermal observation took over two minutes; device capacities are unknown.")
        pump_kw = stove_kw = None
    sensor_values = {
        "indoor_c": _number(measurements.get("temperature")),
        "indoor_humidity_percent": _number(measurements.get("humidity")),
        "outdoor_c": outdoor,
        "price_sek_kwh": price,
        "target_c": _number(state.get("targetTemperature")),
    }
    missing = [name for name in ("indoor_c", "outdoor_c", "price_sek_kwh")
               if sensor_values[name] is None]
    if missing:
        logging.warning("Thermal observation missing readings: %s", ", ".join(missing))
    database.save(datetime.now(timezone.utc), sensor_values, {
        "heatpump": {"is_on": pump_on, "mode": pump_mode, "capacity_kw": pump_kw},
        "pelletstove": {"is_on": stove_on, "mode": stove_mode, "capacity_kw": stove_kw},
    })
