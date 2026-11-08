"""Mill Generation 3 local integration, independent of the room optimizer."""

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from heatautomation import config
from heatautomation.devices.capabilities import CommandResult
from heatautomation.house.models import device_runtime_state


LOGGER = logging.getLogger(__name__)


class MillError(Exception):
    """Mill communication, configuration or safety failure."""


@dataclass(frozen=True)
class MillPolicy:
    min_on_seconds: float
    min_off_seconds: float
    max_on_seconds: float
    poll_seconds: float = 30

    def __post_init__(self):
        for value in (self.min_on_seconds, self.min_off_seconds,
                      self.max_on_seconds, self.poll_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Mill timing values must be finite numbers")
            if not math.isfinite(value) or value < 0:
                raise ValueError("Mill timing values must be non-negative and finite")
        if self.max_on_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("Mill maximum runtime and polling interval must be positive")
        if self.min_on_seconds > self.max_on_seconds:
            raise ValueError("Minimum on time cannot exceed maximum on time")
        if self.poll_seconds >= self.max_on_seconds:
            raise ValueError("Polling interval must be shorter than maximum on time")


class MillLocalDevice:
    """Authenticated local Gen 3 client for one explicitly configured device."""

    def __init__(self, base_url, api_key, ca_bundle, *, session=None, timeout=10):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("Mill URL must be an HTTPS device origin without credentials")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("A Mill local API key is required")
        if not isinstance(ca_bundle, (str, Path)) or not Path(ca_bundle).is_file():
            raise ValueError("A trusted Mill certificate/CA bundle is required")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Timeout must be positive and finite")
        self._url = base_url.rstrip("/")
        self._key = api_key
        self._ca_bundle = str(ca_bundle)
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._timeout = timeout

    def close(self):
        self._session.close()

    def _request(self, method, path, body=None):
        try:
            response = self._session.request(
                method, self._url + path, headers={"Authentication": self._key},
                json=body, timeout=self._timeout, verify=self._ca_bundle,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            LOGGER.warning("Mill connection failed: %s", type(error).__name__)
            raise MillError("Mill device unavailable") from None
        if not 200 <= response.status_code < 300:
            raise MillError("Mill request failed (HTTP %s)" % response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise MillError("Mill returned invalid JSON") from None
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise MillError("Mill reported an error or invalid response")
        return data

    def get_status(self):
        return self._request("GET", "/status")

    def get_control_status(self):
        return self._request("GET", "/control-status")

    def _set_operation_mode(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        return self._request("POST", "/operation-mode", {
            "mode": "Control individually" if enabled else "OFF",
        })


class MillHeatingSource:
    """Monitored local heating-source service with audited mode commands.

    Durations and accumulated values cover only this process's observed session.
    """

    def __init__(self, house, device, generator_id, outlet_id, policy, gate,
                 *, clock=time.monotonic):
        self.generator_id = str(generator_id)
        self.outlet_id = str(outlet_id)
        generators = [d for d in house.devices if str(d.id) == self.generator_id]
        outlets = [d for d in house.devices if str(d.id) == self.outlet_id]
        if (len(generators) != 1 or len(outlets) != 1
                or generators[0].kind != "heat_generator"
                or outlets[0].kind != "actuator_sensor"
                or outlets[0].adapter != "mill"
                or generators[0].room_id is None
                or generators[0].room_id != outlets[0].room_id):
            raise ValueError("Mill generator and outlet must be assigned to the same room")
        if not any(path.generator_id == generators[0].id
                   and path.target_room_id == generators[0].room_id
                   for path in house.heat_paths):
            raise ValueError("Mill generator needs a direct heat path to its room")
        if config.TEST_MODE and not gate.test_mode:
            raise MillError("Application TEST_MODE requires a simulated command gate")
        if "mill_set_power" in gate.dispatchers:
            raise MillError("A Mill dispatcher is already registered")
        gate.dispatchers["mill_set_power"] = self._dispatch
        self.room_id = str(generators[0].room_id)
        self.capacity_kw = generators[0].rated_kw
        self._device = device
        self._policy = policy
        self._gate = gate
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._mode_enabled = None
        self._mode_since = None
        self._observed_at = None
        self._last_control = None
        self._device_info = None
        self._requested = None
        self._pending = None
        self._pending_at = None
        self._dispatch_thread = None
        self._override = None
        self._override_until = None
        self._latched = False
        self._last_error = None
        self._session_started = datetime.now(timezone.utc)
        self._observed_on_seconds = 0.0
        self._covered_intervals = 0
        self._previous_sample = None

    def get_devices(self):
        """Return the configured local device; local API has no account discovery."""
        with self._lock:
            self._sample()
            info = self._device_info
            return [{"device_id": self.outlet_id, "room_id": self.room_id,
                     "name": info.get("name"), "version": info.get("version"),
                     "operation_error": info.get("operation_key") or None}]

    def _sample(self):
        try:
            info = self._device.get_status()
            data = self._device.get_control_status()
            mode = data.get("operation_mode")
            if not isinstance(mode, str) or mode.lower() not in (
                    "off", "control individually", "weekly program", "independent device"):
                raise MillError("Mill operation mode is unavailable or unrecognized")
            enabled = mode.lower() != "off"
            switched = data.get("switched_on")
            if switched is not None and not isinstance(switched, bool):
                raise MillError("Mill returned an invalid switched_on value")
            now = self._clock()
            was_enabled = self._mode_enabled
            expected_on = self._pending is True and was_enabled is False
            if self._previous_sample is not None:
                previous_time, previous_enabled = self._previous_sample
                gap = now - previous_time
                if 0 < gap <= self._policy.poll_seconds * 2:
                    self._covered_intervals += 1
                    if previous_enabled and enabled:
                        self._observed_on_seconds += gap
            self._previous_sample = (now, enabled)
            if self._mode_enabled is None or self._mode_enabled != enabled:
                self._mode_enabled = enabled
                self._mode_since = self._pending_at if enabled and expected_on else now
                if enabled and not expected_on:
                    # The true start is unknown when first observed or after a gap.
                    self._latched = True
            if self._pending == enabled:
                self._pending = None
                self._pending_at = None
            self._last_control = data
            self._device_info = info
            if info.get("operation_key"):
                self._latched = True
            self._observed_at = datetime.now(timezone.utc)
            self._last_error = None
            self._update_connectivity("ok")
        except MillError as error:
            self._mode_enabled = None
            self._mode_since = None
            self._previous_sample = None
            self._last_control = None
            self._pending = None
            self._pending_at = None
            self._latched = True
            self._last_error = str(error)
            self._update_connectivity("unknown")
            raise

    def _update_connectivity(self, connectivity):
        table = device_runtime_state
        with self._gate.engine.begin() as connection:
            exists = connection.execute(select(table.c.device_id).where(
                table.c.device_id == self.outlet_id)).first()
            if exists:
                connection.execute(update(table).where(table.c.device_id == self.outlet_id).values(
                    connectivity=connectivity, reported_at=self._observed_at))
            else:
                connection.execute(insert(table).values(
                    device_id=self.outlet_id, connectivity=connectivity,
                    reported_at=self._observed_at))

    def get_status(self, electricity_price_per_kwh=None):
        if electricity_price_per_kwh is not None and (
                isinstance(electricity_price_per_kwh, bool)
                or not isinstance(electricity_price_per_kwh, (int, float))
                or not math.isfinite(electricity_price_per_kwh)
                or electricity_price_per_kwh < 0):
            raise ValueError("Electricity price must be non-negative and finite")
        with self._lock:
            self._sample()
            data = self._last_control
            estimated_energy = None
            if self.capacity_kw is not None and self._covered_intervals:
                estimated_energy = self.capacity_kw * self._observed_on_seconds / 3600
            return {
                "room_id": self.room_id,
                "generator_id": self.generator_id,
                "device_id": self.outlet_id,
                "connected": True,
                "operation_error": self._device_info.get("operation_key") or None,
                "operating_mode": data.get("operation_mode"),
                "reported_device_switched_on": data.get("switched_on"),
                "actual_outlet_power": None,
                "requested_power": self._requested,
                "actual_heat_output_kw": None,
                "estimated_heating_capacity_kw": self.capacity_kw,
                "actual_electrical_consumption_kw": None,
                "energy_consumption_kwh": None,
                "estimated_energy_kwh": estimated_energy,
                "estimated_heating_cost": (
                    estimated_energy * electricity_price_per_kwh
                    if estimated_energy is not None and electricity_price_per_kwh is not None
                    else None
                ),
                "estimate_basis": "sampled enabled mode at rated capacity; gaps omitted",
                "observed_enabled_hours": self._observed_on_seconds / 3600,
                "operating_hours": None,
                "statistics_since": self._session_started.isoformat(),
                "observed_at": self._observed_at.isoformat(),
                "provider_current_power_w": data.get("current_power"),
                "temperature_c": data.get("ambient_temperature"),
                "cloud_connected": data.get("connected_to_cloud"),
                "safety_latched": self._latched,
                "monitor_running": bool(self._thread and self._thread.is_alive()),
            }

    def get_health(self):
        """Return the last connectivity failure without performing a network request."""
        with self._lock:
            return {
                "last_error": self._last_error,
                "last_observed_at": self._observed_at.isoformat() if self._observed_at else None,
                "connected": False if self._last_error else (
                    True if self._observed_at else None),
                "safety_latched": self._latched,
                "monitor_running": bool(self._thread and self._thread.is_alive()),
            }

    def set_manual_override(self, enabled, duration_seconds):
        if not isinstance(enabled, bool):
            raise ValueError("Override state must be boolean")
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
                or not math.isfinite(duration_seconds) or duration_seconds <= 0):
            raise ValueError("Override duration must be positive and finite")
        with self._lock:
            self._override = enabled
            self._override_until = self._clock() + duration_seconds
            return self.set_power(enabled, manual=True)

    def clear_manual_override(self):
        with self._lock:
            self._override = None
            self._override_until = None

    def set_power(self, enabled, *, manual=False):
        if not isinstance(enabled, bool) or not isinstance(manual, bool):
            raise ValueError("enabled and manual must be boolean")
        with self._lock:
            if config.TEST_MODE and not self._gate.test_mode:
                raise MillError("Application TEST_MODE requires a simulated command gate")
            if self._gate.test_mode:
                return self._gate_request(enabled)
            self._sample()
            now = self._clock()
            if self._override_until is not None and now >= self._override_until:
                self.clear_manual_override()
            if not manual and self._override is not None and enabled != self._override:
                return CommandResult("rejected", "Active manual override")
            if enabled and (self._latched or not self._thread or not self._thread.is_alive()):
                return CommandResult("rejected", "Safety latch active or monitor not running")
            if self._pending is not None:
                return CommandResult("rejected", "Previous command awaits confirmation")
            if self._mode_enabled == enabled:
                return CommandResult("rejected", "Device already reports requested mode")
            minimum = self._policy.min_off_seconds if enabled else self._policy.min_on_seconds
            if now - self._mode_since < minimum:
                return CommandResult("rejected", "Minimum switching time has not elapsed")
            return self._gate_request(enabled)

    def _gate_request(self, enabled):
        self._dispatch_thread = threading.get_ident()
        try:
            return self._gate.request(self.outlet_id, "mill_set_power", {"enabled": enabled})
        finally:
            self._dispatch_thread = None

    def _dispatch(self, device_id, payload):
        if (device_id != self.outlet_id or not isinstance(payload.get("enabled"), bool)
                or self._gate.test_mode or not self._gate.automatic_control_enabled
                or config.TEST_MODE or self._dispatch_thread != threading.get_ident()):
            raise MillError("Mill command dispatch is not permitted")
        self._requested = payload["enabled"]
        requested_at = self._clock()
        try:
            self._device._set_operation_mode(payload["enabled"])
        except MillError:
            self._latched = True
            self._pending = None
            self._pending_at = None
            raise
        self._pending = payload["enabled"]
        self._pending_at = requested_at
        return CommandResult("sent", "Mill accepted the operation mode request")

    def _monitor_once(self):
        with self._lock:
            try:
                self._sample()
                if (self._mode_enabled and
                        (self._latched or self._clock() - self._mode_since
                         >= self._policy.max_on_seconds)):
                    self._latched = True
                    if (self._pending is not False or self._pending_at is None
                            or self._clock() - self._pending_at >= self._policy.poll_seconds * 2):
                        result = self._gate_request(False)
                        if result.outcome not in ("sent", "simulated"):
                            LOGGER.error("Mill safety shutdown rejected: %s", result.detail)
                elif not self._mode_enabled and self._latched and not self._device_info.get("operation_key"):
                    self._latched = False
            except MillError as error:
                LOGGER.error("Mill monitor cannot verify or stop heater: %s", error)

    def start_monitor(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run_monitor, daemon=True,
                                            name="mill-safety-monitor")
            self._thread.start()

    def _run_monitor(self):
        try:
            while not self._stop.is_set():
                self._monitor_once()
                self._stop.wait(self._policy.poll_seconds)
        except SQLAlchemyError:
            with self._lock:
                self._latched = True
                self._last_error = "Mill monitor stopped because database access failed"
            LOGGER.exception("Mill monitor stopped because database access failed")

    def stop_monitor(self):
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join()
        with self._lock:
            self._thread = None
            self._latched = True
