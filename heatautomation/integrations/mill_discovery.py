"""Read-only, explicitly enabled HTTP access to Mill Gen 3 devices."""

import ipaddress
import math
import re
import time
from datetime import datetime, timezone

import requests


class MillProbeError(Exception):
    """A device could not be verified or read."""


_MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
_VERSION = re.compile(r"^0x[0-9a-fA-F]{6,8}$")
_NAME = re.compile(r"^(?:panel heater|convection heater|oil heater|wi-?fi socket) gen\.? ?3$", re.I)


class MillLocalAdapter:
    """Read the documented unauthenticated API, without control operations."""

    def __init__(self, address, *, session=None, timeout=2):
        ip = ipaddress.ip_address(address)
        if ip.version != 4:
            raise ValueError("A numeric IPv4 address is required")
        self.address = str(ip)
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._timeout = timeout

    def close(self):
        self._session.close()

    def _get(self, path):
        deadline = time.monotonic() + self._timeout
        try:
            with self._session.get(
                    "http://" + self.address + path, timeout=self._timeout,
                    allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise MillProbeError("Local API returned HTTP %s" % response.status_code)
                content = bytearray()
                for chunk in response.iter_content(chunk_size=1):
                    if time.monotonic() > deadline:
                        raise MillProbeError("Local API response timed out")
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise MillProbeError("Local API response exceeds size limit")
                import json
                try:
                    data = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    raise MillProbeError("Local API returned invalid JSON") from None
        except requests.RequestException:
            raise MillProbeError("Local API unavailable") from None
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise MillProbeError("Local API returned an unsuccessful response")
        return data

    @staticmethod
    def _identity(data, expected_mac=None):
        name, version, mac = data.get("name"), data.get("version"), data.get("mac_address")
        if not isinstance(name, str) or not _NAME.fullmatch(name.strip()):
            raise MillProbeError("Device identity is not a verified Mill Gen 3 fingerprint")
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise MillProbeError("Device API version is invalid")
        if not isinstance(mac, str) or not _MAC.fullmatch(mac.upper()):
            raise MillProbeError("Device MAC address is invalid")
        mac = mac.upper()
        first = int(mac[:2], 16)
        if first & 1 or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            raise MillProbeError("Device MAC address is invalid")
        if expected_mac and mac != expected_mac.upper():
            raise MillProbeError("Device identity changed")
        display = data.get("custom_name")
        if not isinstance(display, str) or not display.strip():
            display = name.strip()
        return {"id": "mill:" + mac.replace(":", "").lower(),
                "name": display[:256], "mac_address": mac}

    def identify(self, expected_mac=None):
        return self._identity(self._get("/status"), expected_mac)

    @staticmethod
    def _measurements(data, method):
        now = datetime.now(timezone.utc).isoformat()
        result = {}
        for field, unit in (("ambient_temperature", "C"), ("set_temperature", "C"),
                            ("current_power", "W")):
            value = data.get(field)
            if (not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value)):
                result[field] = {"value": value, "unit": unit, "timestamp": now,
                                 "source": "mill", "connection_method": method,
                                 "quality": "good",
                                 "classification": "measured" if field == "ambient_temperature"
                                 else "reported"}
        value = data.get("switched_on")
        if isinstance(value, bool):
            result["switched_on"] = {
                "value": value, "unit": "bool", "timestamp": now, "source": "mill",
                "connection_method": method, "quality": "good",
                "classification": "reported",
            }
        return result

    def measurements(self):
        return self._measurements(self._get("/control-status"), "LOCAL_HTTP")


class MillAuthenticatedAdapter:
    """Read-only wrapper around the existing certificate-verified Mill client."""

    def __init__(self, address, api_key, ca_bundle):
        from .mill import MillLocalDevice
        ip = ipaddress.ip_address(address)
        if ip.version != 4:
            raise ValueError("A numeric IPv4 address is required")
        self._client = MillLocalDevice("https://" + str(ip), api_key, ca_bundle, timeout=3)

    def identify(self, expected_mac=None):
        from .mill import MillError
        try:
            return MillLocalAdapter._identity(self._client.get_status(), expected_mac)
        except MillError as error:
            raise MillProbeError(str(error)) from error

    def measurements(self):
        from .mill import MillError
        try:
            return MillLocalAdapter._measurements(
                self._client.get_control_status(), "LOCAL_HTTPS")
        except MillError as error:
            raise MillProbeError(str(error)) from error

    def close(self):
        self._client.close()
