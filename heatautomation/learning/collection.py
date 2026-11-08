"""Read-only collection of observed device and weather states."""

import logging
import math
import time
from datetime import datetime, timezone

from .. import config
from ..integrations import kmp, sensibo, tibber, weather
from . import profile


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def collect(database, pump_capacity, tado_service=None, selected_profile=None):
    start = time.monotonic()
    settings = selected_profile if selected_profile is not None else profile.current()
    forecast = weather.get_forecast() if config.WEATHER_ENABLED else None
    outdoor = _number(forecast[0].get("temperature_c")) if forecast else None
    price = _number(tibber.get_spot_price()) if config.TIBBER_ENABLED and config.TIBBER_API_KEY else None
    pod = (sensibo._first_result(sensibo.status(), "thermal observation")
           if config.SENSIBO_ENABLED else None)
    stove = kmp.get_status() if config.KMP_ENABLED else None
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

    if settings["heatpump_absent"]:
        pump_on, pump_mode, pump_kw = False, "absent", 0.0
    if settings["pelletstove_absent"]:
        stove_on, stove_mode, stove_kw = False, "absent", 0.0

    if time.monotonic() - start > 120:
        logging.warning("Thermal observation took over two minutes; device capacities are unknown.")
        pump_kw = stove_kw = None
    sensor_values = {
        "indoor_c": _number(measurements.get("temperature")),
        "indoor_humidity_percent": _number(measurements.get("humidity")),
        "outdoor_c": outdoor,
        "price_sek_kwh": price,
        "target_c": _number(state.get("targetTemperature")),
        "thermal_profile": profile.marker(settings),
    }
    if settings["indoor_source"] == "tado":
        sensor_values["indoor_c"] = None
        sensor_values["indoor_humidity_percent"] = None
        sensor_values["target_c"] = None
        if tado_service is not None and config.TADO_ENABLED:
            from ..integrations.tado import TadoError
            try:
                status = tado_service.get_room_status(settings["tado_room_id"])
                reading = status["temperature_reading"]
                observed = datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - observed).total_seconds()
                zone = "{}:{}".format(status["home_id"], status["zone_id"])
                if (observed.utcoffset() is not None and 0 <= age <= 600
                        and status["connection"] == "ONLINE" and zone == settings["tado_zone"]):
                    sensor_values["indoor_c"] = _number(reading["value"])
                    sensor_values["target_c"] = _number(status["requested_temperature_c"])
                else:
                    logging.warning("Tado room temperature is stale, offline or from the wrong zone.")
            except TadoError as error:
                logging.warning("Tado room temperature unavailable: %s", error)
            except (KeyError, TypeError, ValueError):
                logging.warning("Tado room temperature is unavailable or has no valid timestamp.")
        else:
            logging.warning("Tado temperature source is not configured or available.")
    expected = []
    if (settings["indoor_source"] == "sensibo" and config.SENSIBO_ENABLED) or settings["indoor_source"] == "tado":
        expected.append("indoor_c")
    if config.WEATHER_ENABLED:
        expected.append("outdoor_c")
    if config.TIBBER_ENABLED:
        expected.append("price_sek_kwh")
    missing = [name for name in expected
               if sensor_values[name] is None]
    if missing:
        logging.warning("Thermal observation missing readings: %s", ", ".join(missing))
    database.save(datetime.now(timezone.utc), sensor_values, {
        "heatpump": {"is_on": pump_on, "mode": pump_mode, "capacity_kw": pump_kw},
        "pelletstove": {"is_on": stove_on, "mode": stove_mode, "capacity_kw": stove_kw},
    })
