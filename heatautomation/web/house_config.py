"""Validated physical configuration editing and explicit reconciliation."""

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from heatautomation.house.config import HouseConfig
from heatautomation.house.reconcile import reconcile_house
from .rooms import _engine
from .status import StatusUnavailable


_lock = threading.Lock()


class ConfigurationConflict(ValueError):
    pass


def _read(path):
    try:
        content = Path(path).read_bytes()
        if len(content) > 1024 * 1024:
            raise ValueError("House configuration exceeds one megabyte")
        config = HouseConfig.model_validate_json(content)
        return config, hashlib.sha256(content).hexdigest()
    except (OSError, ValidationError) as error:
        raise StatusUnavailable("House configuration is missing, unreadable or invalid.") from error


def read_configuration(path):
    with _lock:
        config, revision = _read(path)
        return {"config": config.model_dump(mode="json"), "revision": revision}


def save_configuration(path, revision, config):
    path = Path(path)
    with _lock:
        current, current_revision = _read(path)
        if revision != current_revision:
            raise ConfigurationConflict("Configuration changed; reload before saving.")
        if config.house.id != current.house.id:
            raise ValueError("The house ID cannot be changed through the web editor.")
        payload = config.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(payload) > 1024 * 1024:
            raise ValueError("House configuration exceeds one megabyte")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise StatusUnavailable("Could not save house configuration.") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()


def apply_configuration(path, database_path, revision):
    with _lock:
        config, current_revision = _read(path)
        if revision != current_revision:
            raise ConfigurationConflict("Configuration changed; reload before applying.")
        engine = _engine(database_path, readonly=False)
        try:
            reconcile_house(engine, config)
        except SQLAlchemyError as error:
            raise StatusUnavailable("Reconciliation failed; configuration remains saved but unapplied.") from error
        finally:
            engine.dispose()
