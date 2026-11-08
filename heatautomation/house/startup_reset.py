"""Reset a mismatched house database before starting application workers."""

import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from .config import config_hash, load_house_config
from .migrate import migrate_database
from .reconcile import reconcile_house


LOGGER = logging.getLogger(__name__)


def recorded_house(database_path):
    database_path = Path(database_path).resolve()
    uri = database_path.as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "houses" not in tables:
            return None
        columns = {row[1] for row in conn.execute("PRAGMA table_info(houses)")}
        if not {"id", "config_hash"}.issubset(columns):
            return None
        rows = conn.execute("SELECT id, config_hash FROM houses").fetchall()
        if len(rows) != 1 or not rows[0][1]:
            return None
        return rows[0]


def reset_mismatched_house(database_path, model_path, house_path):
    """Replace a proven mismatch with a migrated, reconciled empty database.

    An absent configuration or database, and a database without a recorded
    house hash, are not proof of a mismatch and are left unchanged.
    """
    database_path = Path(database_path).resolve()
    model_path = Path(model_path).resolve()
    house_path = Path(house_path).resolve()
    if not house_path.is_file() or not database_path.is_file():
        return False
    if database_path == model_path or house_path in (database_path, model_path):
        raise ValueError("Database, model and house configuration paths must be distinct")
    house = load_house_config(house_path)
    recorded = recorded_house(database_path)
    if recorded is None or recorded == (str(house.house.id), config_hash(house)):
        return False

    temporary = None
    saved_model = None
    try:
        with tempfile.NamedTemporaryFile(dir=database_path.parent, suffix=".sqlite3",
                                         delete=False) as handle:
            temporary = Path(handle.name)
        migrate_database(temporary, backup=False)
        engine = create_engine(URL.create("sqlite", database=str(temporary)))
        try:
            reconcile_house(engine, house)
        finally:
            engine.dispose()
        if config_hash(load_house_config(house_path)) != config_hash(house):
            raise RuntimeError("House configuration changed during database reset")
        if recorded_house(database_path) != recorded:
            raise RuntimeError("Database house configuration changed during reset")
        uri = database_path.as_uri() + "?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise RuntimeError("Cannot safely reset database with active WAL")
            conn.execute("BEGIN EXCLUSIVE")
            conn.rollback()
        if model_path.exists():
            with tempfile.NamedTemporaryFile(dir=model_path.parent, suffix=".reset",
                                             delete=False) as handle:
                pending_model = Path(handle.name)
            try:
                os.replace(model_path, pending_model)
            except OSError:
                pending_model.unlink(missing_ok=True)
                raise
            saved_model = pending_model
        try:
            os.replace(temporary, database_path)
            temporary = None
        except OSError:
            if saved_model is not None:
                os.replace(saved_model, model_path)
                saved_model = None
            raise
        if saved_model is not None:
            saved_model.unlink()
            saved_model = None
        LOGGER.warning(
            "House configuration mismatch: reset the thermal database and learned model; "
            "room settings, observations and plans were erased.")
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if saved_model is not None:
            LOGGER.error("Saved learning data could not be removed or restored: %s", saved_model)
