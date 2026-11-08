"""Load and validate application settings."""

import configparser
import logging
import math
import os
from pathlib import Path

from dotenv import load_dotenv

_directory = Path(__file__).resolve().parent
load_dotenv(dotenv_path=_directory / ".env")
_path = _directory / "heatautomation.conf"
_settings = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
try:
    with _path.open(encoding="utf-8") as _file:
        _settings.read_file(_file)
except (OSError, configparser.Error) as error:
    raise RuntimeError("Could not load configuration {}: {}".format(_path, error)) from error


def _value(section, name, environment=None):
    if environment and environment in os.environ:
        return os.environ[environment].strip()
    try:
        return _settings.get(section, name).strip()
    except configparser.Error as error:
        raise ValueError("Missing or invalid configuration [{}] {}: {}".format(
            section, name, error
        )) from error


def _number(section, name, environment=None, positive=False, non_negative=False):
    value = _value(section, name, environment)
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError("Configuration [{}] {} must be a number".format(section, name)) from error
    if (not math.isfinite(number) or (positive and number <= 0)
            or (non_negative and number < 0)):
        raise ValueError("Configuration [{}] {} is out of range".format(section, name))
    return number


def _integer(section, name):
    value = _value(section, name)
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError("Configuration [{}] {} must be an integer".format(section, name)) from error
    if result <= 0:
        raise ValueError("Configuration [{}] {} must be positive".format(section, name))
    return result


def _boolean(section, name, environment=None):
    value = _value(section, name, environment).lower()
    if value in ("true", "1", "yes", "on"):
        return True
    if value in ("false", "0", "no", "off"):
        return False
    raise ValueError("Configuration [{}] {} must be true or false".format(section, name))


def _integration_enabled(name):
    environment = "HEATAUTOMATION_" + name.upper() + "_ENABLED"
    if environment not in os.environ and not _settings.has_option("integrations", name):
        return True
    return _boolean("integrations", name, environment)


def _capacities():
    rows = _value("heating", "heat_pump_capacity_by_temperature").splitlines()
    result = []
    for row in rows:
        try:
            parts = row.split(",")
            if len(parts) != 2:
                raise ValueError("expected temperature, capacity")
            temperature, capacity = (float(part.strip()) for part in parts)
        except ValueError as error:
            raise ValueError("Invalid heat pump capacity row: {!r}".format(row)) from error
        if (not math.isfinite(temperature) or not math.isfinite(capacity)
                or capacity <= 0 or (result and temperature >= result[-1][0])):
            raise ValueError("Heat pump capacity rows must have descending temperatures "
                             "and positive finite capacities")
        result.append((temperature, capacity))
    if not result:
        raise ValueError("At least one heat pump capacity row is required")
    return tuple(result)


TIBBER_API_KEY = os.getenv("TIBBER_API_KEY")
SENSIBO_DEVICE_ID = os.getenv("SENSIBO_DEVICE_ID")
SENSIBO_API_KEY = os.getenv("SENSIBO_API_KEY")
KMP_USERNAME = os.getenv("KMP_USERNAME")
KMP_PASSWORD = os.getenv("KMP_PASSWORD")
SOLIS_KEY_ID = os.getenv("SOLIS_KEY_ID", "")
SOLIS_KEY_SECRET = os.getenv("SOLIS_KEY_SECRET", "")
SOLIS_STATION_ID = os.getenv("SOLIS_STATION_ID", "")
SOLIS_BASE_URL = _value("connections", "solis_base_url", "BASE_URL")
CHROME_BINARY_PATH = _value("connections", "chrome_binary_path", "CHROME_BINARY_PATH") or None
CHROMEDRIVER_PATH = _value("connections", "chromedriver_path", "CHROMEDRIVER_PATH") or None
SENSIBO_ENABLED = _integration_enabled("sensibo")
KMP_ENABLED = _integration_enabled("kmp")
TIBBER_ENABLED = _integration_enabled("tibber")
WEATHER_ENABLED = _integration_enabled("weather")

WEATHER_LATITUDE = _number("weather", "latitude")
WEATHER_LONGITUDE = _number("weather", "longitude")
if not (-90 <= WEATHER_LATITUDE <= 90 and -180 <= WEATHER_LONGITUDE <= 180):
    raise ValueError("Weather latitude or longitude is out of range")
WEATHER_TIMEZONE = _value("weather", "timezone")
FORECAST_HOURS = _integer("weather", "forecast_hours")
LOG_FILE = _value("operation", "log_file")
if not WEATHER_TIMEZONE or not LOG_FILE:
    raise ValueError("Weather timezone and log file must not be empty")

if "HEATAUTOMATION_TEST_MODE" in os.environ:
    logging.warning(
        "HEATAUTOMATION_TEST_MODE is ignored; set [operation] test_mode in %s instead.",
        _path,
    )
TEST_MODE = _boolean("operation", "test_mode")
FAN_ASSIST_ENABLED = _boolean("operation", "fan_assist_enabled", "HEATAUTOMATION_FAN_ASSIST")
SENSIBO_MAX_FAN_LEVEL = _value(
    "operation", "sensibo_max_fan_level", "SENSIBO_MAX_FAN_LEVEL"
) or None
GRID_POLL_INTERVAL_SECONDS = _integer("operation", "grid_poll_interval_seconds")
FAN_POLL_INTERVAL_SECONDS = _integer("operation", "fan_poll_interval_seconds")
THERMAL_LEARNING_ENABLED = _boolean("thermal_learning", "enabled")
THERMAL_DATABASE_PATH = _value("thermal_learning", "database_path")
THERMAL_MODEL_PATH = _value("thermal_learning", "model_path")
if not THERMAL_DATABASE_PATH or not THERMAL_MODEL_PATH:
    raise ValueError("[thermal_learning] database_path and model_path must not be empty")

MPC_ENABLED = _boolean("mpc", "enabled")
if MPC_ENABLED and not THERMAL_LEARNING_ENABLED:
    raise ValueError("[mpc] requires [thermal_learning] enabled")

spotPriceLimit = _number("heating", "spotPriceLimit", "SPOT_PRICE_LIMIT", non_negative=True)
MAX_PRICE_THRESHOLD = _number("heating", "max_price_threshold")
PELLET_PRICE_ADJUSTMENT = _number("heating", "pellet_price_adjustment")
HEAT_PUMP_SCOP = _number("heating", "heat_pump_scop", positive=True)
MIN_HEAT_PUMP_CAPACITY = _number("heating", "min_heat_pump_capacity", non_negative=True)
PELLET_STOVE_CAPACITY = _number("heating", "pellet_stove_capacity", positive=True)
HEAT_PUMP_CAPACITY_BELOW_MINIMUM = _number(
    "heating", "heat_pump_capacity_below_minimum", positive=True
)
HEAT_PUMP_CAPACITY_BY_TEMPERATURE = _capacities()


def _curve(name, allow_zero=False):
    rows = _value("cost_control", name).splitlines()
    result = []
    for row in rows:
        try:
            parts = row.split(",")
            if len(parts) != 2:
                raise ValueError("expected temperature, value")
            temperature, value = (float(part.strip()) for part in parts)
        except ValueError as error:
            raise ValueError("Invalid [{}] {} row: {!r}".format(
                "cost_control", name, row
            )) from error
        if (not math.isfinite(temperature) or not math.isfinite(value)
                or value < 0 or (not allow_zero and value == 0)
                or (result and temperature >= result[-1][0])):
            raise ValueError("Invalid or unordered [cost_control] {} row".format(name))
        result.append((temperature, value))
    if not result:
        raise ValueError("[cost_control] {} requires at least one row".format(name))
    return tuple(result)


COST_CONTROL_ENABLED = _boolean("cost_control", "enabled")
if COST_CONTROL_ENABLED:
    PELLET_BAG_SIZE_KG = _number("cost_control", "pellet_bag_size_kg", positive=True)
    PELLET_ENERGY_KWH_PER_KG = _number(
        "cost_control", "pellet_energy_kwh_per_kg", positive=True
    )
    PELLET_PRICE_PER_BAG = _number("cost_control", "pellet_price_per_bag", positive=True)
    STOVE_EFFICIENCY_PERCENT = _number(
        "cost_control", "stove_efficiency_percent", positive=True
    )
    if STOVE_EFFICIENCY_PERCENT > 100:
        raise ValueError("[cost_control] stove_efficiency_percent must not exceed 100")
    delivered_energy = (
        PELLET_BAG_SIZE_KG * PELLET_ENERGY_KWH_PER_KG * (STOVE_EFFICIENCY_PERCENT / 100)
    )
    if not math.isfinite(delivered_energy) or delivered_energy <= 0:
        raise ValueError("[cost_control] delivered energy per bag must be finite and positive")
    PELLET_COST_PER_KWH = PELLET_PRICE_PER_BAG / delivered_energy
    if not math.isfinite(PELLET_COST_PER_KWH) or PELLET_COST_PER_KWH <= 0:
        raise ValueError("[cost_control] pellet cost per kWh must be finite and positive")
    COP_BY_TEMPERATURE = _curve("cop_by_temperature")
    DEMAND_BY_TEMPERATURE = _curve("demand_by_temperature", allow_zero=True)
    COST_HYSTERESIS = _number("cost_control", "cost_hysteresis", non_negative=True)
    if COST_HYSTERESIS >= 1:
        raise ValueError("[cost_control] cost_hysteresis must be less than 1")
    MINIMUM_RUNTIME_SECONDS = _number(
        "cost_control", "minimum_runtime_minutes", non_negative=True
    ) * 60
    MINIMUM_SWITCH_INTERVAL_SECONDS = _number(
        "cost_control", "minimum_switch_interval_minutes", non_negative=True
    ) * 60
