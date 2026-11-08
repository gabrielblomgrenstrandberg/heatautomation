"""Controller-owned read-only LAN discovery and polling."""

import ipaddress
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..house import models
from ..house.config import load_house_config
from ..integrations.mill_discovery import MillAuthenticatedAdapter, MillLocalAdapter, MillProbeError
from .nmap import DeviceFingerprint, DiscoveryError, NmapScanner, trusted_networks


LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def engine_for(path):
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True, timeout=5))


def _permitted(address, config):
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in network for network in trusted_networks(config.allowed_cidrs))
    except ValueError:
        return False


def _store_device(engine, fingerprint, identity, measurements, discovered, measurement_error, method):
    stamp = now()
    table = models.lan_devices
    with engine.begin() as conn:
        previous = conn.execute(select(table).where(table.c.id == identity["id"])).first()
        stored = json.loads(previous.measurements_json) if previous else {}
        stored.update(measurements)
        values = {
            "device_type": "mill", "name": identity["name"],
            "ip_address": fingerprint.ip_address, "hostname": fingerprint.hostname,
            "mac_address": identity["mac_address"], "connection_method": method,
            "connection_state": "CONNECTED_LOCAL", "last_connection": stamp,
            "last_measurement": stamp if measurements else (previous.last_measurement if previous else None),
            "last_discovery": stamp if discovered else (previous.last_discovery if previous else None),
            "last_error": measurement_error, "measurements_json": json.dumps(stored, allow_nan=False),
            "capabilities_json": json.dumps(sorted(stored)),
        }
        if previous:
            conn.execute(update(table).where(table.c.id == identity["id"]).values(**values))
        else:
            conn.execute(insert(table).values(id=identity["id"], **values))
        conn.execute(delete(models.lan_probe_errors).where(
            models.lan_probe_errors.c.device_id == identity["id"]))


def _record_probe_error(engine, mac, error):
    table = models.lan_probe_errors
    device_id = "mill:" + mac.replace(":", "").lower()
    with engine.begin() as conn:
        exists = conn.execute(select(table.c.device_id).where(
            table.c.device_id == device_id)).first()
        values = {"checked_at": now(), "error": error[:256]}
        if exists:
            conn.execute(update(table).where(table.c.device_id == device_id).values(**values))
        else:
            conn.execute(insert(table).values(device_id=device_id, **values))


def _read_candidate(engine, fingerprint, expected_mac=None, discovered=False,
                    protected=None, protected_macs=()):
    method = "LOCAL_HTTPS" if protected else "LOCAL_HTTP"
    if protected:
        prefix = "HEATAUTOMATION_MILL_" + protected.mac_address.replace(":", "")
        key = os.environ.get(prefix + "_API_KEY")
        certificate = os.environ.get(prefix + "_CA_BUNDLE")
        if not key or not certificate:
            raise MillProbeError("Mill API key or trusted certificate is not configured")
        try:
            client = MillAuthenticatedAdapter(fingerprint.ip_address, key, certificate)
        except ValueError:
            raise MillProbeError("Mill trusted certificate is unavailable or invalid") from None
    else:
        client = MillLocalAdapter(fingerprint.ip_address)
    try:
        identity = client.identify(expected_mac=expected_mac)
        if not protected and identity["mac_address"] in protected_macs:
            raise MillProbeError("Protected Mill cannot be registered through HTTP")
        measurement_error = None
        try:
            measurements = client.measurements()
        except MillProbeError as error:
            measurements = {}
            measurement_error = str(error)
            LOG.warning("Mill %s control status unavailable: %s", identity["id"], error)
        _store_device(engine, fingerprint, identity, measurements, discovered, measurement_error, method)
        return True
    finally:
        client.close()


def scan(engine, settings, *, scanner=None):
    """Run one scan; failures and concurrent invocations are persisted explicitly."""
    stamp = now()
    table = models.lan_scan_runs
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with engine.begin() as conn:
        conn.execute(update(table).where((table.c.state == "RUNNING") &
                     (table.c.started_at < cutoff)).values(
                         state="FAILED", finished_at=stamp, error="Discovery worker interrupted"))
        try:
            run_id = conn.execute(insert(table).values(
                started_at=stamp, state="RUNNING")).inserted_primary_key[0]
        except IntegrityError:
            LOG.warning("LAN discovery already running; skipping scan")
            return
    candidates = []
    verified = 0
    failure = "Discovery did not complete"
    try:
        if not settings.allow_unauthenticated_mill_http and not settings.protected_mill:
            raise DiscoveryError("No Mill discovery method is configured")
        scanner = scanner or NmapScanner(settings.allowed_cidrs, settings.nmap_path)
        candidates = scanner.scan()
        protected = {device.mac_address: device for device in settings.protected_mill}
        protected_ips = {device.ip_address for device in settings.protected_mill}
        eligible = [candidate for candidate in candidates if _permitted(candidate.ip_address, settings)
                    and ((settings.allow_unauthenticated_mill_http
                          and candidate.ip_address not in protected_ips
                          and any(service.port == 80 for service in candidate.services))
                         or (candidate.mac_address and candidate.mac_address.upper() in protected
                             and any(service.port == 443 for service in candidate.services)))]
        if len(eligible) > 64:
            raise DiscoveryError("More than 64 HTTP candidates; narrow the trusted LAN range")
        for candidate in eligible:
            secure = protected.get(candidate.mac_address.upper()) if candidate.mac_address else None
            if secure and not any(port.port == 443 for port in candidate.services):
                continue
            try:
                if _read_candidate(engine, candidate, expected_mac=candidate.mac_address,
                                   discovered=True, protected=secure, protected_macs=protected):
                    verified += 1
            except MillProbeError as error:
                if secure:
                    LOG.warning("Protected Mill discovery failed: %s", error)
                continue
        failure = None
    except DiscoveryError as error:
        failure = str(error)
        LOG.warning("LAN discovery failed: %s", failure)
    except (SQLAlchemyError, sqlite3.Error):
        failure = "Discovery storage failed"
        raise
    finally:
        with engine.begin() as conn:
            conn.execute(update(table).where(table.c.id == run_id).values(
                state="FAILED" if failure else "COMPLETED", finished_at=now(),
                error=failure, candidate_count=len(candidates), verified_count=verified))


def poll(engine, settings):
    """Poll known addresses without running Nmap or using stale values for control."""
    table = models.lan_devices
    with engine.connect() as conn:
        devices = conn.execute(select(table)).all()
    by_mac = {device.mac_address: device for device in devices}
    protected = {device.mac_address: device for device in settings.protected_mill}
    for mac, configured in protected.items():
        previous = by_mac.get(mac)
        addresses = []
        if previous and previous.ip_address and _permitted(previous.ip_address, settings):
            addresses.append(previous.ip_address)
        if configured.ip_address not in addresses and _permitted(configured.ip_address, settings):
            addresses.append(configured.ip_address)
        failure = "No authorized local address"
        for address in addresses:
            fingerprint = DeviceFingerprint(address, None, mac, ())
            try:
                _read_candidate(engine, fingerprint, expected_mac=mac, protected=configured)
                failure = None
                break
            except MillProbeError as error:
                failure = str(error)
        if failure is not None:
            LOG.warning("Protected Mill polling failed: %s", failure)
            _record_probe_error(engine, mac, failure)
            if previous:
                with engine.begin() as conn:
                    conn.execute(update(table).where(table.c.id == previous.id).values(
                        connection_state="LOCAL_UNAVAILABLE", last_error=failure))
    for device in devices:
        if device.mac_address in protected:
            continue
        if device.connection_method == "LOCAL_HTTPS":
            with engine.begin() as conn:
                conn.execute(update(table).where(table.c.id == device.id).values(
                    connection_state="LOCAL_UNAVAILABLE",
                    last_error="Protected Mill configuration was removed"))
            continue
        if (not settings.allow_unauthenticated_mill_http or not device.ip_address
                or not _permitted(device.ip_address, settings)):
            with engine.begin() as conn:
                conn.execute(update(table).where(table.c.id == device.id).values(
                    connection_state="LOCAL_UNAVAILABLE",
                    last_error="Local address is no longer authorized for HTTP polling"))
            continue
        fingerprint = DeviceFingerprint(device.ip_address, device.hostname, device.mac_address, ())
        try:
            _read_candidate(engine, fingerprint, expected_mac=device.mac_address,
                            protected_macs=protected)
        except MillProbeError as error:
            with engine.begin() as conn:
                conn.execute(update(table).where(table.c.id == device.id).values(
                    connection_state="LOCAL_UNAVAILABLE", last_error=str(error)))


def run_worker(database_path, config_path):
    """Run in the controller process; a web request only enqueues a scan."""
    engine = engine_for(database_path)
    next_scan = next_poll = next_manual = 0.0
    mdns_warned = False
    schedule_initialized = False
    try:
        while True:
            try:
                settings = load_house_config(config_path).lan_discovery
                if settings.enabled:
                    if settings.mdns_enabled and not mdns_warned:
                        LOG.warning("Mill mDNS service is unverified; mDNS discovery is unavailable")
                        mdns_warned = True
                    elif not settings.mdns_enabled:
                        mdns_warned = False
                    with engine.begin() as conn:
                        pending = conn.execute(select(models.lan_scan_requests.c.id).where(
                            models.lan_scan_requests.c.handled_at.is_(None))).scalars().all()
                        if not schedule_initialized:
                            last = conn.execute(select(models.lan_scan_runs.c.started_at).order_by(
                                models.lan_scan_runs.c.id.desc()).limit(1)).scalar()
                            if last:
                                elapsed = max(0.0, (datetime.now(timezone.utc) -
                                              datetime.fromisoformat(last)).total_seconds())
                                next_scan = time.monotonic() + max(0, settings.interval_seconds - elapsed)
                                next_manual = time.monotonic() + max(0, 300 - elapsed)
                            schedule_initialized = True
                    current = time.monotonic()
                    if current >= next_scan or (pending and current >= next_manual):
                        if pending:
                            with engine.begin() as conn:
                                conn.execute(update(models.lan_scan_requests).where(
                                    models.lan_scan_requests.c.id.in_(pending)).values(handled_at=now()))
                        scan(engine, settings)
                        next_scan = time.monotonic() + settings.interval_seconds
                        next_manual = time.monotonic() + 300
                    if time.monotonic() >= next_poll:
                        poll(engine, settings)
                        next_poll = time.monotonic() + settings.poll_seconds
            except (OSError, ValueError, SQLAlchemyError, sqlite3.Error):
                LOG.exception("LAN discovery worker unavailable; retrying in 30 seconds")
            time.sleep(30)
    finally:
        engine.dispose()


def start_worker(database_path):
    import threading
    config_path = os.environ.get("HEATAUTOMATION_HOUSE_CONFIG", "config/house.json")
    if not Path(config_path).is_file():
        LOG.info("LAN discovery disabled: house configuration not found")
        return
    threading.Thread(target=run_worker, args=(database_path, config_path),
                     name="lan-discovery", daemon=True).start()
