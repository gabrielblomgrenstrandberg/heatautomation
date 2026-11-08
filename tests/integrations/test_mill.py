import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from sqlalchemy import create_engine, select

from heatautomation.devices.command_gate import CommandGate
from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.reconcile import reconcile_house
from heatautomation.integrations.mill import MillError, MillHeatingSource, MillLocalDevice, MillPolicy


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = "09963c36-c69b-4472-be21-4d6095292759"
OUTLET = "c1565fc6-f3e9-40db-a83d-a4dc4cd195c1"


class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status

    def json(self):
        return self.value


class Session:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        pass


class FakeDevice:
    def __init__(self):
        self.mode = "OFF"
        self.operation_error = ""
        self.failure = False
        self.commands = []

    def get_status(self):
        if self.failure:
            raise MillError("unavailable")
        return {"name": "Mill", "status": "ok", "operation_key": self.operation_error}

    def get_control_status(self):
        return {"status": "ok", "operation_mode": self.mode,
                "switched_on": self.mode != "OFF", "current_power": 500}

    def _set_operation_mode(self, enabled):
        self.commands.append(enabled)
        self.mode = "Control individually" if enabled else "OFF"


class Alive:
    def is_alive(self):
        return True


class MillTests(unittest.TestCase):
    def setUp(self):
        self.house = load_house_config(ROOT / "config" / "house.example.json")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine("sqlite:///" + str(Path(self.directory.name) / "test.sqlite3"))
        models.metadata.create_all(self.engine)
        reconcile_house(self.engine, self.house)
        self.addCleanup(self.engine.dispose)
        self.device = FakeDevice()
        self.now = 0.0
        self.patch = patch("heatautomation.integrations.mill.config.TEST_MODE", False)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.gate = CommandGate(self.engine, test_mode=False, automatic_control_enabled=True)
        self.source = MillHeatingSource(
            self.house, self.device, GENERATOR, OUTLET,
            MillPolicy(10, 20, 100, 5), self.gate, clock=lambda: self.now)

    def test_local_api_auth_tls_errors_and_commands(self):
        session = Session()
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "cert.pem"
            cert.write_text("test", encoding="utf-8")
            client = MillLocalDevice("https://mill.local", "secret", cert, session=session)
            session.responses = [Response({"status": "ok", "name": "Mill"}),
                                 Response({"status": "ok"}),
                                 Response({"status": "Failed to execute the request"}),
                                 requests.Timeout("secret")]
            self.assertEqual(client.get_status()["name"], "Mill")
            client._set_operation_mode(False)
            self.assertEqual(session.calls[1][2]["json"], {"mode": "OFF"})
            self.assertEqual(session.calls[0][2]["headers"]["Authentication"], "secret")
            self.assertEqual(session.calls[0][2]["verify"], str(cert))
            self.assertFalse(session.calls[0][2]["allow_redirects"])
            self.assertFalse(session.trust_env)
            with self.assertRaises(MillError):
                client.get_status()
            with self.assertRaises(MillError) as error:
                client.get_status()
            self.assertNotIn("secret", str(error.exception))
            with self.assertRaises(ValueError):
                MillLocalDevice("http://mill.local", "secret", cert)

    def test_configured_storage_and_unknown_telemetry(self):
        status = self.source.get_status(2)
        self.assertEqual(status["room_id"], "d981f1f6-bf20-40e4-8526-c008ad0f30b2")
        for field in ("actual_outlet_power", "actual_electrical_consumption_kw",
                      "actual_heat_output_kw", "energy_consumption_kwh",
                      "estimated_heating_cost"):
            self.assertIsNone(status[field])
        self.assertIsNone(status["estimated_heating_capacity_kw"])
        self.assertEqual(status["provider_current_power_w"], 500)
        self.assertEqual(self.source.get_devices()[0]["device_id"], OUTLET)

    def test_cost_is_explicit_sample_based_estimate(self):
        self.source.capacity_kw = 2
        self.source._thread = Alive()
        self.source._monitor_once()
        self.now = 20
        self.source.set_power(True)
        self.source._monitor_once()
        self.now = 25
        status = self.source.get_status(electricity_price_per_kwh=3)
        self.assertAlmostEqual(status["estimated_energy_kwh"], 2 * 5 / 3600)
        self.assertAlmostEqual(status["estimated_heating_cost"], 6 * 5 / 3600)
        self.assertIsNone(status["energy_consumption_kwh"])
        self.assertIsNone(status["operating_hours"])
        self.assertIn("sampled enabled mode", status["estimate_basis"])

    def test_minimum_times_monitor_and_maximum_run(self):
        self.source._thread = Alive()
        self.source._monitor_once()
        self.assertEqual(self.source.set_power(True).outcome, "rejected")
        self.now = 20
        self.assertEqual(self.source.set_power(True).outcome, "sent")
        self.assertEqual(self.source.set_power(True).outcome, "rejected")
        self.assertEqual(self.source.set_power(False).outcome, "rejected")
        self.now = 30
        self.assertEqual(self.source.set_power(False).outcome, "sent")
        self.source._monitor_once()
        self.now = 50
        self.assertEqual(self.source.set_power(True).outcome, "sent")
        self.source._monitor_once()
        self.now = 150
        self.source._monitor_once()
        self.assertEqual(self.device.commands, [True, False, True, False])
        self.assertTrue(self.source.get_health()["safety_latched"])
        self.source._monitor_once()
        self.assertFalse(self.source.get_health()["safety_latched"])
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(models.command_log)).all()), 4)

    def test_startup_on_and_lost_connection_fail_closed(self):
        self.source._thread = Alive()
        self.device.mode = "Control individually"
        self.source._monitor_once()
        self.assertEqual(self.device.commands, [False])
        self.source._monitor_once()
        self.device.failure = True
        self.source._monitor_once()
        self.assertTrue(self.source.get_health()["safety_latched"])
        with self.assertRaises(MillError):
            self.source.set_power(True)
        with self.engine.connect() as conn:
            value = conn.execute(select(models.device_runtime_state.c.connectivity).where(
                models.device_runtime_state.c.device_id == OUTLET)).scalar_one()
        self.assertEqual(value, "unknown")

    def test_external_on_and_operation_error_trigger_off(self):
        self.source._thread = Alive()
        self.source._monitor_once()
        self.device.mode = "Control individually"
        self.device.operation_error = "sensor failure"
        self.source._monitor_once()
        self.assertEqual(self.device.commands, [False])
        self.source._monitor_once()
        self.assertTrue(self.source.get_health()["safety_latched"])
        self.now = 30
        self.assertEqual(self.source.set_power(True).outcome, "rejected")

    def test_override_expires_and_cannot_bypass_safety(self):
        self.source._thread = Alive()
        self.source._monitor_once()
        self.now = 20
        self.assertEqual(self.source.set_manual_override(True, 40).outcome, "sent")
        self.now = 40
        self.assertEqual(self.source.set_power(False).outcome, "rejected")
        self.now = 61
        self.assertEqual(self.source.set_power(False).outcome, "sent")

    def test_maximum_runtime_overrides_manual_on(self):
        self.source._thread = Alive()
        self.source._monitor_once()
        self.now = 20
        self.assertEqual(self.source.set_manual_override(True, 1000).outcome, "sent")
        self.source._monitor_once()
        self.now = 120
        self.source._monitor_once()
        self.assertEqual(self.device.commands, [True, False])

    def test_failed_command_latches_and_preserves_requested_state(self):
        self.source._thread = Alive()
        self.source._monitor_once()
        self.now = 20
        def fail(enabled):
            raise MillError("uncertain command result")
        self.device._set_operation_mode = fail
        with self.assertRaises(MillError):
            self.source.set_power(True)
        health = self.source.get_health()
        self.assertTrue(health["safety_latched"])
        self.assertEqual(self.source._requested, True)

    def test_direct_gate_dispatch_cannot_bypass_guard(self):
        self.source._monitor_once()
        with self.assertRaises(MillError):
            self.gate.request(OUTLET, "mill_set_power", {"enabled": True})
        self.assertEqual(self.device.commands, [])

    def test_test_mode_never_sends_command(self):
        self.gate.test_mode = True
        with patch("heatautomation.integrations.mill.config.TEST_MODE", True):
            result = self.source.set_power(True)
        self.assertEqual(result.outcome, "simulated")
        self.assertEqual(self.device.commands, [])

    def test_monitor_starts_polls_and_stops(self):
        received = threading.Event()
        original = self.device.get_control_status

        def capture():
            received.set()
            return original()

        self.device.get_control_status = capture
        self.source.start_monitor()
        self.assertTrue(received.wait(3))
        self.assertTrue(self.source.get_health()["monitor_running"])
        self.source.stop_monitor()
        self.assertFalse(self.source.get_health()["monitor_running"])
        self.assertTrue(self.source.get_health()["safety_latched"])

    def test_policy_validation(self):
        for values in ((20, 1, 10, 1), (0, 0, 1, 1), (0, 0, float("nan"), 1)):
            with self.assertRaises(ValueError):
                MillPolicy(*values)


if __name__ == "__main__":
    unittest.main()
