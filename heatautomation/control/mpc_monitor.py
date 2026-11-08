"""Poll inputs and produce shadow plans, never device commands."""

import logging
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import config
from ..integrations import tibber, weather
from ..learning.prediction import ThermalPredictor
from ..learning import profile
from . import heating_model
from .mpc import Interval, PlanningUnavailable, Settings, optimize
from .mpc_simulation import compare_no_preheat


def _settings():
    if not config.COST_CONTROL_ENABLED:
        raise PlanningUnavailable("MPC requires configured cost-control COP and pellet costs")
    runtime = config._number("mpc", "minimum_runtime_minutes", positive=True)
    off = config._number("mpc", "minimum_off_minutes", positive=True)
    return Settings(
        config._number("mpc", "minimum_c"), config._number("mpc", "target_c"),
        config._number("mpc", "maximum_c"), config.PELLET_COST_PER_KWH,
        electricity_extra_per_kwh=config._number("mpc", "electricity_extra_per_kwh"),
        deviation_penalty=config._number("mpc", "deviation_penalty", non_negative=True),
        switch_penalty=config._number("mpc", "switch_penalty", non_negative=True),
        uncertainty_margin_c=config._number("mpc", "uncertainty_margin_c", non_negative=True),
        max_rate_rmse=config._number("mpc", "max_rate_rmse", positive=True),
        min_runtime_steps=int(math.ceil(runtime / 15)),
        min_off_steps=int(math.ceil(off / 15)),
    )


def build_intervals(prices, forecast, start, pump_capacity):
    """Construct consecutive UTC quarters until either provider's coverage ends."""
    if not prices or not forecast:
        raise PlanningUnavailable("Price or weather forecast is unavailable")
    try:
        times = pd.DatetimeIndex(pd.to_datetime([item["time"] for item in forecast]))
        times = times.tz_localize(config.WEATHER_TIMEZONE, ambiguous="NaT",
                                  nonexistent="NaT").tz_convert("UTC")
        if times.isna().any():
            raise ValueError("Weather timestamps are ambiguous or nonexistent in configured timezone")
        weather_points = sorted((t.to_pydatetime(), float(item["temperature_c"]))
                                for t, item in zip(times, forecast))
        price_points = {}
        for item in prices:
            time = datetime.fromisoformat(item["startsAt"].replace("Z", "+00:00"))
            if time.utcoffset() is None:
                raise ValueError("Tibber price has no timezone")
            time = time.astimezone(timezone.utc)
            if time in price_points:
                raise ValueError("Duplicate Tibber price timestamp")
            price_points[time] = float(item["total"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PlanningUnavailable("Invalid or ambiguous price/weather forecast: {}".format(error)) from error
    result = []
    for i in range(144):
        time = start + timedelta(minutes=15 * i)
        if time not in price_points:
            break
        matching = [(stamp, temp) for stamp, temp in weather_points
                    if stamp <= time < stamp + timedelta(hours=1)]
        if len(matching) != 1:
            break
        outdoor = matching[0][1]
        result.append(Interval(time, price_points[time], outdoor, pump_capacity(outdoor),
                               heating_model.interpolate(config.COP_BY_TEMPERATURE, outdoor),
                               config.PELLET_STOVE_CAPACITY))
    if not result:
        raise PlanningUnavailable("No contiguous future 15-minute price and weather coverage")
    return result


def run_shadow_cycle(database, pump_capacity, now=None, selected_profile=None):
    if selected_profile is None:
        try:
            selected_profile = profile.current()
        except ValueError as error:
            raise PlanningUnavailable(str(error)) from error
    settings_profile = selected_profile
    if not config.TIBBER_ENABLED or not config.WEATHER_ENABLED:
        raise PlanningUnavailable("Shadow MPC requires Tibber and weather forecasts")
    if settings_profile["indoor_source"] == "sensibo" and not config.SENSIBO_ENABLED:
        raise PlanningUnavailable("Shadow MPC indoor source Sensibo is disabled")
    if settings_profile["indoor_source"] == "tado" and not config.TADO_ENABLED:
        raise PlanningUnavailable("Shadow MPC indoor source Tado is disabled")
    if settings_profile["indoor_source"] == "tado" and not settings_profile["other_heating_absent"]:
        raise PlanningUnavailable("Tado radiator heating is not modeled; confirm other heating is absent before planning")
    if not config.SENSIBO_ENABLED and not settings_profile["heatpump_absent"]:
        raise PlanningUnavailable("Heat pump state unavailable: enable Sensibo or explicitly declare it absent")
    if not config.KMP_ENABLED and not settings_profile["pelletstove_absent"]:
        raise PlanningUnavailable("Pellet stove state unavailable: enable KMP or explicitly declare it absent")
    now = now or datetime.now(timezone.utc)
    history = database.history()
    if len(history) < 7:
        raise PlanningUnavailable(
            "Expected at least 7 recent indoor observations for shadow MPC; found {}"
            .format(len(history))
        )
    recent = history[-7:]
    if any(not profile.compatible(row, settings_profile) for row in recent):
        raise PlanningUnavailable("Recent observations do not match the selected thermal profile")
    timestamps = [datetime.fromisoformat(row["timestamp"]) for row in recent]
    problems = []
    naive = [index for index, timestamp in enumerate(timestamps, 1)
             if timestamp.utcoffset() is None]
    if naive:
        problems.append(
            "timestamps at observation positions {} have no timezone".format(
                ", ".join(str(index) for index in naive)
            )
        )
    if now.utcoffset() is None:
        problems.append("planning time has no timezone")
    elif timestamps[-1].utcoffset() is not None:
        age = (now - timestamps[-1]).total_seconds()
        if age < 0:
            problems.append(
                "newest observation at {} is {:.1f}s in the future".format(
                    timestamps[-1].isoformat(), -age
                )
            )
        elif age > 600:
            problems.append(
                "newest observation at {} is {:.1f}s old (maximum age: 600s)".format(
                    timestamps[-1].isoformat(), age
                )
            )
    for index, (start, end) in enumerate(zip(timestamps, timestamps[1:]), 1):
        if start.utcoffset() is None or end.utcoffset() is None:
            continue
        seconds = (end - start).total_seconds()
        if not 210 <= seconds <= 390:
            problems.append(
                "gap {}/6 from {} to {} is {:.1f}s (allowed: 210-390s)".format(
                    index, start.isoformat(), end.isoformat(), seconds
                )
            )
    if problems:
        raise PlanningUnavailable(
            "Recent indoor observations are stale or discontinuous: {}".format(
                "; ".join(problems)
            )
        )
    indoor = [row.get("indoor_c") for row in recent]
    latest = recent[-1]
    if any(value is None for value in indoor):
        raise PlanningUnavailable("Recent indoor temperature is missing")
    pump_on = latest.get("heatpump_is_on")
    stove_on = latest.get("pelletstove_is_on")
    if (type(pump_on) is not bool or type(stove_on) is not bool
            or (pump_on and latest.get("heatpump_mode") != "heat")
            or (stove_on and latest.get("pelletstove_mode") not in {"HÖGEFFEKT", "VILAR..."})
            or (not stove_on and latest.get("pelletstove_mode") not in {"AV", "absent"})):
        raise PlanningUnavailable("Device status is unknown or transitioning")
    settings = _settings()
    model = ThermalPredictor(config.THERMAL_MODEL_PATH)
    model_profile = model.artifact.get("thermal_profile")
    if model_profile != settings_profile and not (model_profile is None and profile.legacy(settings_profile)):
        raise PlanningUnavailable("Thermal model does not match the selected indoor source and heating configuration")
    prices = tibber.get_price_forecast()
    forecast = weather.get_forecast()
    start = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    start += timedelta(minutes=15 - start.minute % 15)
    intervals = build_intervals(prices, forecast, start,
                                (lambda _: 0.0) if settings_profile["heatpump_absent"] else pump_capacity)
    if settings_profile["pelletstove_absent"]:
        from dataclasses import replace
        intervals = [replace(step, stove_capacity_kw=0.0) for step in intervals]
    plan = optimize(intervals, indoor, {"heatpump": pump_on, "pelletstove": stove_on},
                    model, settings)
    metadata = asdict(settings)
    metadata["thermal_profile"] = settings_profile
    try:
        metadata["modeled_comparison"] = compare_no_preheat(
            intervals, indoor, {"heatpump": pump_on, "pelletstove": stove_on},
            model, settings, plan,
        )
    except PlanningUnavailable as error:
        logging.warning("No-preheat baseline unavailable: %s", error)
        metadata["modeled_comparison"] = None
    plan_id, changes = database.save_plan(
        now, model.artifact["created_at"], indoor[-1], metadata, plan,
    )
    logging.info("Shadow MPC plan %s: %s quarters, %s changed source intervals; no commands sent.",
                 plan_id, len(plan), len(changes))
    return plan_id
