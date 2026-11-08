"""Read the latest controller observation without modifying its database."""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError

from .. import config
from ..control.heating_model import interpolate
from ..learning.database import observations, sensors, sources

logger = logging.getLogger(__name__)
STALE_SECONDS = 900


class StatusUnavailable(Exception):
    """The observation store cannot be read."""


def read_status(database_path, now=None):
    path = Path(database_path).resolve()
    # SQLite's read-only URI prevents accidental creation of an empty database.
    engine = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2),
    )
    try:
        with engine.connect() as connection:
            row = connection.execute(
                select(observations).order_by(observations.c.timestamp.desc()).limit(1)
            ).first()
            if row is None:
                return {"state": "unavailable", "message": "No observations have been recorded."}
            values = {
                item.name: item.value for item in connection.execute(
                    select(sensors).where(sensors.c.observation_id == row.id)
                )
            }
            devices = {
                item.name: {"on": item.is_on, "mode": item.mode, "capacity_kw": item.capacity_kw}
                for item in connection.execute(
                    select(sources).where(sources.c.observation_id == row.id)
                )
            }
    except (SQLAlchemyError, sqlite3.Error) as error:
        logger.warning("Cannot read thermal observation database: %s", error)
        raise StatusUnavailable("Observation database is unavailable.") from error
    finally:
        engine.dispose()

    try:
        timestamp = datetime.fromisoformat(row.timestamp)
        if timestamp.utcoffset() is None:
            raise ValueError("Observation timestamp has no timezone")
    except (ValueError, TypeError) as error:
        logger.warning("Invalid observation timestamp: %s", error)
        raise StatusUnavailable("Observation timestamp is invalid.") from error
    now = now or datetime.now(timezone.utc)
    age = (now - timestamp).total_seconds()
    state = "current" if 0 <= age <= STALE_SECONDS else "stale"
    outdoor = values.get("outdoor_c")
    price = values.get("price_sek_kwh")
    cop = demand = cost = None
    if config.COST_CONTROL_ENABLED and outdoor is not None:
        cop = interpolate(config.COP_BY_TEMPERATURE, outdoor)
        demand = interpolate(config.DEMAND_BY_TEMPERATURE, outdoor)
        if price is not None:
            cost = price / cop
    return {
        "state": state,
        "message": "Cached provider observations, not live physical measurements.",
        "observed_at": timestamp.isoformat(),
        "indoor_c": values.get("indoor_c"),
        "outdoor_c": outdoor,
        "target_c": values.get("target_c"),
        "price_sek_kwh": price,
        "heatpump": devices.get("heatpump"),
        "pelletstove": devices.get("pelletstove"),
        "estimated_cop": cop,
        "estimated_demand_kw": demand,
        "estimated_pump_cost_sek_kwh": cost,
    }
