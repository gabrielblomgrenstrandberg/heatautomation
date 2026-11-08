"""Tado Classic cloud integration with explicit room mappings and guarded writes.

The consumer API is unofficial and does not establish actual heat delivery.
"""

import copy
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from heatautomation import config
from heatautomation.devices.capabilities import CommandResult


LOGGER = logging.getLogger(__name__)
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_URL = "https://my.tado.com/api/v2"


class TadoError(Exception):
    """Tado request, configuration, or response error."""


class TadoAuthenticationError(TadoError):
    """Authentication is missing, expired, or rejected."""


class TadoRateLimitError(TadoError):
    """Requests are blocked by the Tado rate limit."""


class TadoUnsupportedError(TadoError):
    """The device or capability is not supported or confirmed."""


class TadoCommandDisabledError(TadoError):
    """The safety gate rejected a write request."""


class TokenFile:
    """Persist rotating refresh tokens; callers must protect the parent directory."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TadoAuthenticationError("Cannot read Tado token file") from error
        if not isinstance(data, dict) or not isinstance(data.get("refresh_token"), str):
            raise TadoAuthenticationError("Tado token file is invalid")
        return data["refresh_token"]

    def save(self, token):
        if not isinstance(token, str) or not token:
            raise TadoAuthenticationError("Tado did not provide a refresh token")
        name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(self.path.parent),
                prefix=".tado-token-", delete=False
            ) as file:
                name = file.name
                os.chmod(name, 0o600)
                json.dump({"refresh_token": token}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        except OSError as error:
            if name and os.path.exists(name):
                os.unlink(name)
            raise TadoAuthenticationError("Cannot persist Tado refresh token") from error


class TadoService:
    """Synchronous Tado Classic client. No network calls occur during construction."""

    def __init__(self, house_config, token_store, *, session=None, cache_seconds=300,
                 command_gate=None, timeout=10,
                 monotonic=time.monotonic):
        if not math.isfinite(cache_seconds) or cache_seconds <= 0:
            raise ValueError("cache_seconds must be positive and finite")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self._room_map = {
            str(mapping.room_id): (mapping.home_id, mapping.zone_id)
            for mapping in house_config.tado_rooms if mapping.home_id is not None
        }
        self._tokens = token_store
        self._session = session or requests.Session()
        self._cache_seconds = cache_seconds
        self._timeout = timeout
        self._clock = monotonic
        self._room_devices = {}
        self._device_rooms = {}
        for device in house_config.devices:
            if device.adapter == "tado" and device.kind == "thermostat":
                room_id = str(device.room_id)
                self._room_devices.setdefault(room_id, []).append(str(device.id))
                self._device_rooms[str(device.id)] = room_id
        self._command_gate = None
        self._access_token = None
        self._expires_at = 0.0
        self._blocked_until = 0.0
        self._rate_limit_remaining = None
        self._cache = {}
        self._lock = threading.RLock()
        self._device_flow = None
        self._session.headers.update({
            "User-Agent": "heatautomation/1.0", "Referer": "https://app.tado.com/",
        })
        if command_gate is not None:
            self.attach_command_gate(command_gate)

    def attach_command_gate(self, gate):
        """Register dispatchers with the existing audited command gate."""
        if self._command_gate is not None:
            raise TadoCommandDisabledError("A Tado command gate is already attached")
        if config.TEST_MODE and not gate.test_mode:
            raise TadoCommandDisabledError("Application TEST_MODE requires a simulated gate")
        dispatchers = {
            "tado_set_temperature": self._dispatch_temperature,
            "tado_set_heating_enabled": self._dispatch_power,
        }
        if any(name in gate.dispatchers for name in dispatchers):
            raise TadoCommandDisabledError("Tado command dispatcher is already registered")
        gate.dispatchers.update(dispatchers)
        self._command_gate = gate

    def close(self):
        self._session.close()

    def _send(self, method, url, **kwargs):
        if self._clock() < self._blocked_until:
            raise TadoRateLimitError("Tado requests are temporarily rate-limited")
        try:
            response = self._session.request(method, url, timeout=self._timeout, **kwargs)
        except requests.RequestException as error:
            LOGGER.warning("Tado connection failed: %s", type(error).__name__)
            raise TadoError("Tado connection failed") from None
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            delay = 1800.0
            if retry_after:
                try:
                    parsed_delay = float(retry_after)
                    if not math.isfinite(parsed_delay):
                        raise ValueError("invalid delay")
                    delay = max(0.0, parsed_delay)
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry_after)
                        delay = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        LOGGER.warning("Invalid Tado Retry-After header; using 30 minutes")
            self._blocked_until = self._clock() + max(delay, 1.0)
            LOGGER.warning("Tado rate limit reached; retry after %.0f seconds", delay)
            raise TadoRateLimitError("Tado rate limit reached")
        rate = response.headers.get("RateLimit", "")
        remaining = re.search(r"(?:^|;)\s*r=(\d+)", rate)
        if remaining:
            self._rate_limit_remaining = int(remaining.group(1))
            if self._rate_limit_remaining == 0:
                self._blocked_until = max(self._blocked_until, self._clock() + 1800)
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as error:
            raise TadoError("Tado returned invalid JSON") from error

    def begin_authentication(self):
        """Start OAuth device authorization and return the user verification details."""
        with self._lock:
            response = self._send("POST", AUTH_URL, params={
                "client_id": CLIENT_ID, "scope": "offline_access",
            }, json={})
            if response.status_code != 200:
                raise TadoAuthenticationError(
                    "Tado device authorization failed (HTTP %s)" % response.status_code
                )
            data = self._json(response)
            try:
                device_code = data["device_code"]
                uri = data["verification_uri"]
                user_code = data["user_code"]
                interval = max(float(data.get("interval", 5)), 1.0)
                expires_in = float(data["expires_in"])
                if not all(isinstance(value, str) and value
                           for value in (device_code, uri, user_code)):
                    raise ValueError("missing device flow fields")
                if not math.isfinite(interval) or not math.isfinite(expires_in) or expires_in <= 0:
                    raise ValueError("invalid device flow timing")
            except (KeyError, TypeError, ValueError) as error:
                raise TadoAuthenticationError("Invalid device authorization response") from error
            self._device_flow = {
                "code": device_code, "interval": interval,
                "expires_at": self._clock() + expires_in,
                "next_poll": self._clock() + interval,
            }
            return {
                "verification_uri": uri,
                "verification_uri_complete": data.get("verification_uri_complete"),
                "user_code": user_code,
                "expires_in": expires_in,
                "interval": interval,
            }

    def poll_authentication(self):
        """Poll once; returns False while authorization is pending, True when complete."""
        with self._lock:
            flow = self._device_flow
            if flow is None:
                raise TadoAuthenticationError("Start device authorization first")
            now = self._clock()
            if now >= flow["expires_at"]:
                self._device_flow = None
                raise TadoAuthenticationError("Tado device authorization expired")
            if now < flow["next_poll"]:
                return False
            flow["next_poll"] = now + flow["interval"]
            response = self._send("POST", TOKEN_URL, params={
                "client_id": CLIENT_ID,
                "device_code": flow["code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            data = self._json(response)
            if response.status_code == 400 and isinstance(data, dict):
                if data.get("error") == "authorization_pending":
                    return False
                if data.get("error") == "slow_down":
                    flow["interval"] += 5
                    flow["next_poll"] = now + flow["interval"]
                    return False
            if response.status_code != 200:
                self._device_flow = None
                raise TadoAuthenticationError(
                    "Tado device authorization failed (HTTP %s)" % response.status_code
                )
            self._accept_token(data)
            self._device_flow = None
            self._cache.clear()
            return True

    def _accept_token(self, data):
        try:
            access = data["access_token"]
            refresh = data["refresh_token"]
            expires = float(data["expires_in"])
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
                raise ValueError("missing tokens")
            if not math.isfinite(expires) or expires <= 0:
                raise ValueError("invalid token lifetime")
        except (KeyError, TypeError, ValueError) as error:
            raise TadoAuthenticationError("Invalid Tado token response") from error
        self._tokens.save(refresh)
        self._access_token = access
        self._expires_at = self._clock() + max(0, expires - 30)

    def _refresh(self):
        refresh = self._tokens.load()
        if not refresh:
            raise TadoAuthenticationError("Tado authorization is required")
        response = self._send("POST", TOKEN_URL, params={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }, json={})
        if response.status_code != 200:
            raise TadoAuthenticationError(
                "Tado token refresh failed (HTTP %s); reauthorize if necessary"
                % response.status_code
            )
        self._accept_token(self._json(response))

    def _request(self, method, path, body=None):
        if not self._access_token or self._clock() >= self._expires_at:
            self._refresh()
        response = self._send(
            method, API_URL + path,
            headers={"Authorization": "Bearer " + self._access_token},
            **({"json": body} if body is not None else {})
        )
        if response.status_code == 401 and method == "GET":
            self._refresh()
            response = self._send(
                method, API_URL + path,
                headers={"Authorization": "Bearer " + self._access_token},
            )
        if response.status_code in (401, 403):
            raise TadoAuthenticationError("Tado access denied (HTTP %s)" % response.status_code)
        if not 200 <= response.status_code < 300:
            raise TadoError("Tado API request failed (HTTP %s)" % response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return self._json(response)

    def _cached_get(self, path):
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None and self._clock() < entry[0]:
                return copy.deepcopy(entry[1])
            value = self._request("GET", path)
            self._cache[path] = (
                self._clock() + self._cache_seconds, value,
                datetime.now(timezone.utc).isoformat(),
            )
            return copy.deepcopy(value)

    def get_homes(self):
        data = self._cached_get("/me")
        if not isinstance(data, dict) or not isinstance(data.get("homes"), list):
            raise TadoError("Tado returned invalid home information")
        return copy.deepcopy(data["homes"])

    def get_rooms(self):
        """Return all Tado zones, including unmapped zones, grouped by home ID."""
        result = []
        for home in self.get_homes():
            if not isinstance(home, dict):
                raise TadoError("Tado returned invalid home information")
            home_id = self._valid_id(home.get("id"))
            zones = self._cached_get("/homes/%d/zones" % home_id)
            if not isinstance(zones, list):
                raise TadoError("Tado returned invalid zone information")
            for zone in zones:
                if not isinstance(zone, dict):
                    raise TadoError("Tado returned an invalid zone")
                zone_id = self._valid_id(zone.get("id"))
                room_id = next((key for key, value in self._room_map.items()
                                if value == (home_id, zone_id)), None)
                result.append({"home_id": home_id, "zone_id": zone_id,
                               "room_id": room_id, "name": zone.get("name"),
                               "type": zone.get("type"), "devices": zone.get("devices")})
        return result

    @staticmethod
    def _valid_id(value):
        if isinstance(value, bool):
            raise TadoError("Invalid Tado home or zone ID")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise TadoError("Invalid Tado home or zone ID") from error
        if result <= 0 or str(result) != str(value):
            raise TadoError("Invalid Tado home or zone ID")
        return result

    def _mapped(self, room_id):
        try:
            return self._room_map[str(room_id)]
        except KeyError as error:
            raise TadoError("Room has no configured Tado home/zone mapping") from error

    def get_room_status(self, room_id):
        home, zone = self._mapped(room_id)
        path = "/homes/%d/zoneStates" % home
        with self._lock:
            data = self._cached_get(path)
            received_at = self._cache[path][2]
        if not isinstance(data, dict) or not isinstance(data.get("zoneStates"), dict):
            raise TadoError("Tado returned invalid zone states")
        state = data["zoneStates"].get(str(zone))
        if not isinstance(state, dict):
            raise TadoError("Mapped Tado zone is absent from zone states")
        sensors = state.get("sensorDataPoints") or {}
        setting = state.get("setting") or {}
        activity = state.get("activityDataPoints") or {}
        if not all(isinstance(part, dict) for part in (sensors, setting, activity)):
            raise TadoError("Tado returned invalid zone state")
        measured = sensors.get("insideTemperature") or {}
        target = setting.get("temperature") or {}
        heating = activity.get("heatingPower") or {}
        if not all(isinstance(part, dict) for part in (measured, target, heating)):
            raise TadoError("Tado returned invalid zone data points")
        for value in (measured.get("celsius"), target.get("celsius"), heating.get("percentage")):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise TadoError("Tado returned an invalid numeric data point")
        demand = heating.get("percentage")
        if demand is not None and not 0 <= demand <= 100:
            raise TadoError("Tado returned an invalid heating demand percentage")
        link = state.get("link") or {}
        online = link.get("state") if isinstance(link, dict) else None
        return {
            "room_id": str(room_id), "home_id": home, "zone_id": zone,
            "actual_temperature_c": measured.get("celsius"),
            "temperature_observed_at": measured.get("timestamp"),
            "temperature_reading": {
                "value": measured.get("celsius"), "unit": "C",
                "timestamp": measured.get("timestamp"),
                "received_at": received_at,
                "source_device_id": None,
                "source_zone": {"home_id": home, "zone_id": zone},
                "classification": "measured" if measured.get("celsius") is not None else "unknown",
                "quality": "bad" if measured.get("celsius") is None
                else ("stale" if online == "OFFLINE" else "suspect"),
            },
            "requested_temperature_c": target.get("celsius"),
            "heating_demand_percent": demand,
            "heating_demand_observed_at": heating.get("timestamp"),
            "radiator_heating_activity": None,
            "zone_power": setting.get("power"),
            "zone_type": setting.get("type"),
            "connection": online,
            "overlay_type": state.get("overlayType"),
        }

    def get_room_temperature(self, room_id):
        return self.get_room_status(room_id)["actual_temperature_c"]

    def get_heating_activity(self):
        return {room_id: self.get_room_status(room_id) for room_id in self._room_map}

    def get_devices(self, home_id):
        home_id = self._valid_id(home_id)
        result = self._cached_get("/homes/%d/devices" % home_id)
        if not isinstance(result, list):
            raise TadoError("Tado returned invalid device information")
        return result

    def get_status(self):
        homes = self.get_homes()
        return {
            "connected": True,
            "rate_limit_remaining": self._rate_limit_remaining,
            "homes": homes,
            "rooms": self.get_rooms(),
            "heating_activity": self.get_heating_activity(),
            "devices": {str(home["id"]): self.get_devices(home["id"]) for home in homes},
        }

    def _write(self, room_id, temperature=None, power="ON"):
        if (self._command_gate is None or self._command_gate.test_mode
                or not self._command_gate.automatic_control_enabled or config.TEST_MODE):
            raise TadoCommandDisabledError("Tado dispatch is disabled in TEST_MODE")
        home, zone = self._mapped(room_id)
        base = "/homes/%d/zones/%d" % (home, zone)
        with self._lock:
            capability = self._cached_get(base + "/capabilities")
            if not isinstance(capability, dict) or capability.get("type") != "HEATING":
                raise TadoUnsupportedError("Tado heating zone capability is not confirmed")
            setting = {"type": "HEATING", "power": power}
            if temperature is not None:
                if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                    raise ValueError("Temperature must be a finite number")
                if not math.isfinite(temperature):
                    raise ValueError("Temperature must be a finite number")
                temperatures = capability.get("temperatures")
                limits = temperatures.get("celsius", {}) if isinstance(temperatures, dict) else {}
                if not isinstance(limits, dict) or not all(
                        isinstance(limits.get(k), (int, float)) for k in ("min", "max", "step")):
                    raise TadoUnsupportedError("Tado temperature limits are unavailable")
                minimum, maximum, step = limits["min"], limits["max"], limits["step"]
                if (not all(math.isfinite(x) for x in (minimum, maximum, step))
                        or step <= 0 or minimum > maximum):
                    raise TadoUnsupportedError("Tado temperature limits are invalid")
                if not minimum <= temperature <= maximum or not math.isclose(
                        (temperature - minimum) / step,
                        round((temperature - minimum) / step), abs_tol=1e-7):
                    raise ValueError("Temperature is outside the Tado zone limits or step")
                setting["temperature"] = {"celsius": temperature}
            try:
                self._request("PUT", base + "/overlay", {
                    "setting": setting,
                    "termination": {"typeSkillBasedApp": "NEXT_TIME_BLOCK"},
                })
            finally:
                self._cache.pop("/homes/%d/zoneStates" % home, None)
            return CommandResult(outcome="sent", detail="Tado accepted the overlay request")

    def set_room_temperature(self, room_id, temperature):
        self._validate_temperature(temperature)
        return self._request_command(room_id, "tado_set_temperature",
                                     {"temperature": temperature})

    @staticmethod
    def _validate_temperature(temperature):
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("Temperature must be a finite number")
        if not math.isfinite(temperature):
            raise ValueError("Temperature must be a finite number")

    def set_heating_enabled(self, room_id, enabled, temperature=None):
        self._validate_power(enabled, temperature)
        return self._request_command(room_id, "tado_set_heating_enabled",
                                     {"enabled": enabled, "temperature": temperature})

    @classmethod
    def _validate_power(cls, enabled, temperature):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if enabled and temperature is None:
            raise ValueError("Enabling heating requires an explicit target temperature")
        if not enabled and temperature is not None:
            raise ValueError("Do not provide a target when disabling heating")
        if temperature is not None:
            cls._validate_temperature(temperature)

    def _request_command(self, room_id, command, payload):
        self._mapped(room_id)
        devices = self._room_devices.get(str(room_id), [])
        if len(devices) != 1:
            raise TadoCommandDisabledError("Room needs exactly one configured Tado thermostat")
        gate = self._command_gate
        if gate is None:
            raise TadoCommandDisabledError("No Tado command gate is attached")
        if config.TEST_MODE and not gate.test_mode:
            raise TadoCommandDisabledError("Application TEST_MODE requires a simulated gate")
        return gate.request(devices[0], command, payload)

    def _dispatch_temperature(self, device_id, payload):
        room_id = self._device_rooms.get(device_id)
        if room_id is None:
            raise TadoCommandDisabledError("Device is not a configured Tado thermostat")
        self._validate_temperature(payload["temperature"])
        return self._write(room_id, temperature=payload["temperature"])

    def _dispatch_power(self, device_id, payload):
        room_id = self._device_rooms.get(device_id)
        if room_id is None:
            raise TadoCommandDisabledError("Device is not a configured Tado thermostat")
        self._validate_power(payload["enabled"], payload["temperature"])
        return self._write(room_id, temperature=payload["temperature"],
                           power="ON" if payload["enabled"] else "OFF")
