import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from sqlalchemy import create_engine, insert, select

from heatautomation.devices.command_gate import CommandGate
from heatautomation.house import models
from heatautomation.house.config import HouseConfig, load_house_config
from heatautomation.integrations.tado import (
    TadoAuthenticationError, TadoCommandDisabledError, TadoError, TadoIntegrationDisabledError,
    TadoRateLimitError, TadoService, TadoUnsupportedError, TokenFile,
)


EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "house.example.json"


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}
        self.content = b"data" if data is not None else b""

    def json(self):
        return self.data


class Session:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


class Store:
    def __init__(self, token="old"):
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = token


def tokens(access="access", refresh="new"):
    return Response({"access_token": access, "refresh_token": refresh, "expires_in": 3600})


class TadoTests(unittest.TestCase):
    def setUp(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        for i, mapping in enumerate(data["tado_rooms"]):
            mapping["home_id"] = 1 if i < 5 else 2
            mapping["zone_id"] = i % 5 + 1
        self.house = HouseConfig.model_validate(data)
        self.rooms = [str(room.room_id) for room in self.house.tado_rooms]
        self.session = Session()
        self.store = Store()
        self.now = 100.0
        self.service = TadoService(self.house, self.store, session=self.session,
                                   monotonic=lambda: self.now)

    def test_example_mappings_and_validation(self):
        example = load_house_config(EXAMPLE)
        self.assertEqual(len(example.tado_rooms), 10)
        self.assertEqual({m.room_id for m in example.tado_rooms},
                         {d.room_id for d in example.devices if d.adapter == "tado"})
        data = self.house.model_dump(mode="json")
        data["tado_rooms"][1]["zone_id"] = data["tado_rooms"][0]["zone_id"]
        with self.assertRaises(ValueError):
            HouseConfig.model_validate(data)
        data["tado_rooms"][1]["home_id"] = None
        with self.assertRaises(ValueError):
            HouseConfig.model_validate(data)

    def test_disabled_blocks_cached_reads_authentication_and_commands(self):
        self.session.responses = [tokens(), Response({"homes": [{"id": 1}]})]
        self.assertEqual(self.service.get_homes(), [{"id": 1}])
        self.session.calls.clear()
        gate = unittest.mock.Mock()
        gate.test_mode = True
        gate.dispatchers = {}
        self.service.attach_command_gate(gate)
        with patch("heatautomation.integrations.tado.config.TADO_ENABLED", False):
            for operation in (
                self.service.get_homes, self.service.get_status,
                self.service.get_heating_activity, self.service.begin_authentication,
                self.service.poll_authentication,
                lambda: self.service.get_room_status(self.rooms[0]),
                lambda: self.service.set_room_temperature(self.rooms[0], 20),
                lambda: self.service.set_heating_enabled(self.rooms[0], False),
                lambda: self.service._write(self.rooms[0]),
            ):
                with self.subTest(operation=operation), self.assertRaises(TadoIntegrationDisabledError):
                    operation()
            gate.request.assert_not_called()
            self.assertEqual(self.session.calls, [])
        self.assertEqual(self.service.get_homes(), [{"id": 1}])

    def test_disabled_cannot_attach_gate(self):
        gate = unittest.mock.Mock()
        gate.test_mode = True
        gate.dispatchers = {}
        with patch("heatautomation.integrations.tado.config.TADO_ENABLED", False):
            with self.assertRaises(TadoIntegrationDisabledError):
                self.service.attach_command_gate(gate)
        self.assertEqual(gate.dispatchers, {})

    def test_device_authorization_pending_slowdown_and_token_rotation(self):
        self.session.responses = [
            Response({"device_code": "secret", "verification_uri": "https://example.test",
                      "user_code": "ABCD", "interval": 5, "expires_in": 100}),
            Response({"error": "authorization_pending"}, 400),
            Response({"error": "slow_down"}, 400),
            tokens(refresh="rotated"),
        ]
        details = self.service.begin_authentication()
        self.assertEqual(details["user_code"], "ABCD")
        self.assertNotIn("device_code", details)
        self.assertFalse(self.service.poll_authentication())
        self.now += 5
        self.assertFalse(self.service.poll_authentication())
        self.now += 5
        self.assertFalse(self.service.poll_authentication())
        self.now += 5
        self.assertFalse(self.service.poll_authentication())
        self.now += 5
        self.assertTrue(self.service.poll_authentication())
        self.assertEqual(self.store.token, "rotated")
        self.assertEqual(self.session.calls[-1][2]["params"]["grant_type"],
                         "urn:ietf:params:oauth:grant-type:device_code")

    def test_expired_device_authorization(self):
        self.session.responses = [Response({
            "device_code": "x", "verification_uri": "https://example.test",
            "user_code": "ABCD", "expires_in": 10, "interval": 5,
        })]
        self.service.begin_authentication()
        self.now += 10
        with self.assertRaises(TadoAuthenticationError):
            self.service.poll_authentication()

    def test_refresh_and_multi_home_bulk_reads_and_cache(self):
        state = {"sensorDataPoints": {"insideTemperature": {
            "celsius": 19.2, "timestamp": "2026-01-01T10:00:00Z"}},
            "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 21}},
            "activityDataPoints": {"heatingPower": {"percentage": 70}},
            "link": {"state": "ONLINE"}}
        self.session.responses = [
            tokens(), Response({"homes": [{"id": 1}, {"id": 2}]}),
            Response([{"id": 1, "name": "First", "type": "HEATING"}]),
            Response([{"id": 1, "name": "Second", "type": "HEATING"}]),
            Response({"zoneStates": {str(i): state for i in range(1, 6)}}),
            Response({"zoneStates": {str(i): state for i in range(1, 6)}}),
            Response([{"serialNo": "device1"}]),
            Response([{"serialNo": "device2"}]),
        ]
        status = self.service.get_status()
        self.assertEqual(self.store.token, "new")
        self.assertEqual([r["room_id"] for r in status["rooms"]],
                         [self.rooms[0], self.rooms[5]])
        self.assertEqual(len(status["heating_activity"]), 10)
        room = status["heating_activity"][self.rooms[0]]
        self.assertEqual(room["actual_temperature_c"], 19.2)
        self.assertEqual(room["requested_temperature_c"], 21)
        self.assertEqual(room["heating_demand_percent"], 70)
        self.assertIsNone(room["radiator_heating_activity"])
        self.assertEqual(room["temperature_reading"]["classification"], "measured")
        self.assertIsNone(room["temperature_reading"]["source_device_id"])
        self.assertEqual(self.service.get_room_temperature(self.rooms[1]), 19.2)
        self.assertEqual(len(self.session.calls), 8)
        self.now += 301
        self.session.responses = [Response({"zoneStates": {"2": state}})]
        self.service.get_room_temperature(self.rooms[1])
        self.assertEqual(len(self.session.calls), 9)

    def test_missing_and_offline_readings_are_not_fabricated(self):
        self.session.responses = [tokens(), Response({"zoneStates": {"1": {
            "link": {"state": "OFFLINE"}, "sensorDataPoints": {},
        }}})]
        status = self.service.get_room_status(self.rooms[0])
        self.assertIsNone(status["actual_temperature_c"])
        self.assertIsNone(status["heating_demand_percent"])
        self.assertEqual(status["temperature_reading"]["quality"], "bad")
        self.assertEqual(status["temperature_reading"]["classification"], "unknown")
        with self.assertRaises(TadoError):
            self.service.get_room_status("unmapped")

    def test_invalid_provider_measurements_are_rejected(self):
        self.session.responses = [tokens(), Response({"zoneStates": {"1": {
            "activityDataPoints": {"heatingPower": {"percentage": 101}},
        }}})]
        with self.assertRaises(TadoError):
            self.service.get_room_status(self.rooms[0])

    def test_401_refresh_retries_get_once_and_rotates(self):
        self.session.responses = [
            tokens(refresh="first"), Response({}, 401),
            tokens(access="second", refresh="second"), Response({"homes": []}),
        ]
        self.assertEqual(self.service.get_homes(), [])
        self.assertEqual(self.store.token, "second")
        self.assertEqual(self.session.calls[-1][2]["headers"]["Authorization"], "Bearer second")

    def test_rate_limit_blocks_requests_and_does_not_return_stale_data(self):
        self.session.responses = [tokens(), Response({"homes": []}),
                                  Response({}, 429, {"Retry-After": "60"}),
                                  Response({"homes": []})]
        self.service.get_homes()
        self.now += 301
        with self.assertRaises(TadoRateLimitError):
            self.service.get_homes()
        with self.assertRaises(TadoRateLimitError):
            self.service.get_homes()
        self.assertEqual(len(self.session.calls), 3)
        self.now += 60
        self.assertEqual(self.service.get_homes(), [])

    def test_network_and_auth_errors_are_explicit(self):
        self.session.responses = [requests.Timeout("do not log credentials")]
        with self.assertRaises(TadoError):
            self.service.get_homes()
        self.store.token = None
        with self.assertRaises(TadoAuthenticationError):
            self.service.get_homes()

    def test_rate_limit_header_blocks_new_uncached_requests(self):
        self.session.responses = [tokens(), Response({"homes": []}, headers={
            "RateLimit": '"perday";r=0',
        })]
        self.assertEqual(self.service.get_homes(), [])
        self.assertEqual(self.service.get_homes(), [])
        with self.assertRaises(TadoRateLimitError):
            self.service.get_devices(1)
        self.assertEqual(len(self.session.calls), 2)

    def make_gate(self, test_mode, enabled):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        models.metadata.create_all(engine)
        device_id = self.service._room_devices[self.rooms[0]][0]
        with engine.begin() as connection:
            connection.execute(insert(models.devices).values(
                id=device_id, kind="thermostat", name="Tado", provenance="test"))
            connection.execute(insert(models.device_runtime_state).values(
                device_id=device_id, connectivity="ok"))
        gate = CommandGate(engine, test_mode=test_mode, automatic_control_enabled=enabled)
        self.service.attach_command_gate(gate)
        return engine

    def test_commands_require_gate_and_simulation_is_audited(self):
        with self.assertRaises(TadoCommandDisabledError):
            self.service.set_room_temperature(self.rooms[0], 21)
        engine = self.make_gate(True, True)
        result = self.service.set_room_temperature(self.rooms[0], 21)
        self.assertEqual(result.outcome, "simulated")
        self.assertEqual(self.session.calls, [])
        with engine.connect() as connection:
            self.assertEqual(connection.execute(select(models.command_log.c.outcome)).scalar(), "simulated")

    def test_global_test_mode_rejects_live_gate(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", True):
            with self.assertRaises(TadoCommandDisabledError):
                self.make_gate(False, True)

    def test_live_gate_rejects_when_automatic_control_disabled(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", False):
            engine = self.make_gate(False, False)
            self.assertEqual(self.service.set_room_temperature(self.rooms[0], 21).outcome,
                             "rejected")
            self.assertEqual(self.session.calls, [])
            with engine.connect() as connection:
                self.assertEqual(connection.execute(select(models.command_log.c.outcome)).scalar(),
                                 "rejected")

    def test_live_gate_rejects_unconfirmed_connectivity(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", False):
            engine = self.make_gate(False, True)
            device_id = self.service._room_devices[self.rooms[0]][0]
            with engine.begin() as connection:
                connection.execute(models.device_runtime_state.update().where(
                    models.device_runtime_state.c.device_id == device_id
                ).values(connectivity="unknown"))
            self.assertEqual(self.service.set_room_temperature(self.rooms[0], 21).outcome,
                             "rejected")
            self.assertEqual(self.session.calls, [])

    def test_live_command_uses_capabilities_and_audited_gate(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", False):
            engine = self.make_gate(False, True)
            self.session.responses = [
                tokens(),
                Response({"type": "HEATING", "temperatures": {
                    "celsius": {"min": 5, "max": 25, "step": 0.5}}}),
                Response({"setting": {}}),
            ]
            result = self.service.set_room_temperature(self.rooms[0], 20.5)
            self.assertEqual(result.outcome, "sent")
            method, url, kwargs = self.session.calls[-1]
            self.assertEqual(method, "PUT")
            self.assertTrue(url.endswith("/homes/1/zones/1/overlay"))
            self.assertEqual(kwargs["json"]["setting"]["temperature"]["celsius"], 20.5)
            with engine.connect() as connection:
                self.assertEqual(connection.execute(select(models.command_log.c.outcome)).scalar(), "sent")
            with self.assertRaises(ValueError):
                self.service.set_room_temperature(self.rooms[0], 20.3)
            self.assertEqual(len(self.session.calls), 3)

    def test_unsupported_zone_and_mutation_not_retried_on_401(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", False):
            self.make_gate(False, True)
            self.session.responses = [tokens(), Response({"type": "AIR_CONDITIONING"})]
            with self.assertRaises(TadoUnsupportedError):
                self.service.set_room_temperature(self.rooms[0], 21)
            self.service._cache.clear()
            self.session.responses = [Response({"type": "HEATING"}), Response({}, 401)]
            with self.assertRaises(TadoAuthenticationError):
                self.service.set_heating_enabled(self.rooms[0], False)
            self.assertEqual(len(self.session.calls), 4)

    def test_live_disable_and_direct_gate_payload_validation(self):
        with patch("heatautomation.integrations.tado.config.TEST_MODE", False):
            self.make_gate(False, True)
            self.session.responses = [tokens(), Response({"type": "HEATING"}),
                                      Response({"setting": {}})]
            self.assertEqual(self.service.set_heating_enabled(self.rooms[0], False).outcome,
                             "sent")
            self.assertEqual(self.session.calls[-1][2]["json"]["setting"]["power"], "OFF")
            device_id = self.service._room_devices[self.rooms[0]][0]
            with self.assertRaises(ValueError):
                self.service._command_gate.request(
                    device_id, "tado_set_heating_enabled",
                    {"enabled": "false", "temperature": None})
            self.assertEqual(len(self.session.calls), 3)

    def test_token_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenFile(Path(directory) / "token.json")
            self.assertIsNone(store.load())
            store.save("first")
            store.save("rotated")
            self.assertEqual(store.load(), "rotated")


if __name__ == "__main__":
    unittest.main()
