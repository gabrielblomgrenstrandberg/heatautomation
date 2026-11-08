"""Database-only discovery monitoring and manual scan requests."""

import json
from datetime import datetime, timezone

from sqlalchemy import insert, select

from heatautomation.house import models
from .house_config import read_configuration
from .rooms import _load
from .status import StatusUnavailable


def read_discovery(database_path, config_path):
    settings = read_configuration(config_path)["config"]["lan_discovery"]
    def query(conn):
        devices = []
        current = datetime.now(timezone.utc)
        assignments = {}
        duplicates = set()
        for configured in conn.execute(select(models.devices).where(
                (models.devices.c.adapter == "mill") &
                (models.devices.c.removed_at.is_(None)))):
            identity = configured.external_id
            if identity:
                if identity in assignments:
                    duplicates.add(identity)
                else:
                    assignments[identity] = {"id": configured.id, "name": configured.name}
        for row in conn.execute(select(models.lan_devices).order_by(models.lan_devices.c.name)):
            data = dict(row._mapping)
            data["house_device"] = assignments.get(row.id) if row.id not in duplicates else None
            data["mapping_error"] = (
                "Multiple house devices reference this identity" if row.id in duplicates else None
            )
            last_connection = data["last_connection"]
            data["monitoring_stale"] = False
            if not settings["enabled"]:
                data["connection_state"] = "LOCAL_UNAVAILABLE"
                data["monitoring_stale"] = True
            elif last_connection:
                age = (current - datetime.fromisoformat(last_connection)).total_seconds()
                if not 0 <= age <= max(2 * settings["poll_seconds"] + 30,
                                       settings["freshness_seconds"]):
                    data["monitoring_stale"] = True
                    if data["connection_state"] in ("CONNECTED_CLOUD", "CONNECTED_LOCAL"):
                        data["connection_state"] = "UNKNOWN"
            else:
                data["monitoring_stale"] = True
            data["capabilities"] = json.loads(data.pop("capabilities_json"))
            measurements = json.loads(data.pop("measurements_json"))
            for value in measurements.values():
                timestamp = datetime.fromisoformat(value["timestamp"])
                age = (current - timestamp).total_seconds()
                value["stale"] = not 0 <= age <= settings["freshness_seconds"]
            data["measurements"] = measurements
            devices.append(data)
        last_run = conn.execute(select(models.lan_scan_runs).order_by(
            models.lan_scan_runs.c.id.desc()).limit(1)).first()
        pending = conn.execute(select(models.lan_scan_requests.c.id).where(
            models.lan_scan_requests.c.handled_at.is_(None)).limit(1)).first()
        configured_ids = {"mill:" + device["mac_address"].replace(":", "").lower()
                          for device in settings["protected_mill"]}
        errors = [dict(row._mapping) for row in conn.execute(select(models.lan_probe_errors))
                  if row.device_id in configured_ids]
        return {"enabled": settings["enabled"], "devices": devices,
                "last_discovery": dict(last_run._mapping) if last_run else None,
                "scan_requested": pending is not None, "probe_errors": errors}
    try:
        return _load(database_path, query)
    except (ValueError, KeyError, TypeError) as error:
        raise StatusUnavailable("Stored LAN discovery data is invalid.") from error


def request_scan(database_path, config_path):
    settings = read_configuration(config_path)["config"]["lan_discovery"]
    if not settings["enabled"]:
        raise ValueError("LAN discovery is disabled in the house configuration.")
    if not settings["allow_unauthenticated_mill_http"] and not settings["protected_mill"]:
        raise ValueError("No Mill discovery method is configured.")
    def write(conn):
        pending = conn.execute(select(models.lan_scan_requests.c.id).where(
            models.lan_scan_requests.c.handled_at.is_(None)).limit(1)).first()
        if pending is None:
            conn.execute(insert(models.lan_scan_requests).values(
                requested_at=datetime.now(timezone.utc).isoformat()))
    _load(database_path, write, readonly=False)
