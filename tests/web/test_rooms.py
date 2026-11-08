import tempfile
import unittest
import json
import os
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.learning.database import ThermalDatabase
from heatautomation.web.app import create_app


ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "a sufficiently long test password 123456"
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "house.example.json"


class RoomWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "house.sqlite3"
        migrate_database(self.path, backup=False)
        self.db = ThermalDatabase(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.config = load_house_config(EXAMPLE)
        reconcile_house(self.db.engine, self.config)
        self.room_id = str(self.config.rooms[0].id)
        self.client = TestClient(create_app(password=PASSWORD, database_path=self.path, origin=ORIGIN))
        self.addCleanup(self.client.close)

    def login(self):
        self.assertEqual(self.client.post("/api/session", json={"password": PASSWORD},
                                          headers={"origin": ORIGIN}).status_code, 204)

    def test_auth_csrf_settings_and_input_validation(self):
        path = f"/api/rooms/{self.room_id}/settings"
        data = {"target_c": 21, "min_c": 16, "max_c": 24}
        self.assertEqual(self.client.get("/api/house").status_code, 401)
        self.assertEqual(self.client.put(path, json=data, headers={"origin": ORIGIN}).status_code, 401)
        self.login()
        self.assertEqual(self.client.put(path, json=data).status_code, 403)
        self.assertEqual(self.client.put(path, json=data, headers={"origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.client.put(path, json={**data, "target_c": 100}, headers={"origin": ORIGIN}).status_code, 422)
        self.assertEqual(self.client.put(path, content='{"target_c":NaN,"min_c":16,"max_c":24}',
                                         headers={"origin": ORIGIN, "content-type": "application/json"}).status_code, 422)
        self.assertEqual(self.client.put(path, json={**data, "target_c": 25}, headers={"origin": ORIGIN}).status_code, 400)
        self.assertEqual(self.client.put(path, json=data, headers={"origin": ORIGIN}).status_code, 204)
        room = next(r for r in self.client.get("/api/house").json()["rooms"] if r["id"] == self.room_id)
        self.assertEqual(room["target_c"], 21)
        self.assertIsNone(room["heating_activity"])
        self.assertEqual(self.client.get("/api/rooms/missing/history").status_code, 404)

    def test_override_alignment_conflicts_and_removed_rooms(self):
        self.login()
        settings = f"/api/rooms/{self.room_id}/settings"
        override = f"/api/rooms/{self.room_id}/override"
        self.client.put(settings, json={"target_c": 21, "min_c": 16, "max_c": 24}, headers={"origin": ORIGIN})
        self.assertEqual(self.client.post(override, json={"target_c": 22, "minutes": 17},
                                          headers={"origin": ORIGIN}).status_code, 204)
        with self.db.engine.connect() as conn:
            row = conn.execute(select(models.room_overrides)).one()
        self.assertEqual(row.starts_at.minute % 15, 0)
        self.assertEqual(row.starts_at.second, 0)
        self.assertEqual(row.expires_at - row.starts_at, timedelta(minutes=30))
        self.assertEqual(self.client.post(override, json={"target_c": 23, "minutes": 30},
                                          headers={"origin": ORIGIN}).status_code, 400)
        self.assertEqual(self.client.put(settings, json={"target_c": 21, "min_c": 16, "max_c": 21},
                                         headers={"origin": ORIGIN}).status_code, 400)
        with self.db.engine.begin() as conn:
            conn.execute(update(models.rooms).where(models.rooms.c.id == self.room_id).values(
                removed_at=datetime.now(timezone.utc)))
        self.assertEqual(self.client.put(settings, json={"target_c": 21, "min_c": 16, "max_c": 24},
                                         headers={"origin": ORIGIN}).status_code, 400)

    def test_freshness_and_websocket_house_message(self):
        self.login()
        now = datetime.now(timezone.utc)
        device_id = str(next(d.id for d in self.config.devices if str(d.room_id) == self.room_id))
        with self.db.engine.begin() as conn:
            conn.execute(insert(models.room_measurements).values(
                room_id=self.room_id, quantity="temperature", value=19.5, unit="C",
                observed_at=now, received_at=now, source_device_id=device_id,
                quality="good", classification="measured"))
            conn.execute(insert(models.device_runtime_state).values(
                device_id=device_id, connectivity="online", reported_at=now - timedelta(hours=1)))
            conn.execute(insert(models.room_thermal_predictions).values(
                room_id=self.room_id, created_at=now - timedelta(hours=1), recorded_at=now,
                target_at=now + timedelta(hours=1), horizon_seconds=7200, predicted_c=20))
        house = self.client.get("/api/house").json()
        room = next(r for r in house["rooms"] if r["id"] == self.room_id)
        self.assertEqual(room["temperature_c"], 19.5)
        self.assertIsNone(room["predicted_c"])
        self.assertEqual(next(d for d in room["devices"] if d["id"] == device_id)["connectivity"], "unknown")
        with self.client.websocket_connect("/api/live", headers={"origin": ORIGIN}) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "status")
            self.assertEqual(websocket.receive_json()["type"], "house")

    def test_missing_database_is_reported_without_creation(self):
        path = Path(self.directory.name) / "missing.sqlite3"
        client = TestClient(create_app(password=PASSWORD, database_path=path, origin=ORIGIN))
        self.addCleanup(client.close)
        client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        self.assertEqual(client.get("/api/house").status_code, 503)
        self.assertFalse(path.exists())

    def test_schedule_unknown_and_simulation_plan(self):
        self.login()
        now = datetime.now(timezone.utc)
        with self.db.engine.begin() as conn:
            conn.execute(insert(models.room_schedules).values(
                room_id=self.room_id, weekday=0, start_min=0, end_min=60, target_c=20))
            plan_id = conn.execute(insert(models.optimizer_plans).values(
                created_at=now, mode="simulation", details_json="{}")).inserted_primary_key[0]
            conn.execute(insert(models.optimizer_plan_intervals).values(
                plan_id=plan_id, room_id=self.room_id, starts_at=now,
                data_json=json.dumps({"predicted_temperature_c": 20, "target_c": 21,
                                      "selected_sources": [], "expected_cost": 0,
                                      "planned_heat_kw": {}, "inter_room_transfer_kw": {},
                                      "explanation": "No direct heat needed"})))
        room = next(r for r in self.client.get("/api/house").json()["rooms"] if r["id"] == self.room_id)
        self.assertIsNone(room["target_c"])
        plan = self.client.get("/api/room-plan").json()
        self.assertEqual(plan["mode"], "simulation")
        self.assertEqual(plan["intervals"][0]["data"]["target_c"], 21)

    def test_physical_configuration_save_apply_and_conflicts(self):
        config_path = Path(self.directory.name) / "house.json"
        config_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        with patch.dict(os.environ, {"HEATAUTOMATION_HOUSE_CONFIG": str(config_path)}):
            client = TestClient(create_app(password=PASSWORD, database_path=self.path, origin=ORIGIN))
        self.addCleanup(client.close)
        endpoint = "/api/house-configuration"
        self.assertEqual(client.get(endpoint).status_code, 401)
        client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        initial = client.get(endpoint).json()
        document = initial["config"]
        document["rooms"][0]["name"] = "Updated living room"
        payload = {"revision": initial["revision"], "config": document}
        self.assertEqual(client.put(endpoint, json=payload).status_code, 403)
        invalid = json.loads(json.dumps(payload))
        invalid["config"]["rooms"][0]["floor_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.assertEqual(client.put(endpoint, json=invalid, headers={"origin": ORIGIN}).status_code, 422)
        response = client.put(endpoint, json=payload, headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 200)
        revision = response.json()["revision"]
        self.assertEqual(client.put(endpoint, json=payload, headers={"origin": ORIGIN}).status_code, 409)
        before = client.get("/api/house").json()
        self.assertNotEqual(before["rooms"][0]["name"], "Updated living room")
        self.assertEqual(client.post(endpoint + "/apply", json={"revision": initial["revision"]},
                                     headers={"origin": ORIGIN}).status_code, 409)
        with self.db.engine.begin() as conn:
            conn.execute(update(models.thermal_parameters).where(
                (models.thermal_parameters.c.room_id == self.room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k")).values(
                    value=12345678, sample_count=7))
        self.assertEqual(client.post(endpoint + "/apply", json={"revision": revision},
                                     headers={"origin": ORIGIN}).status_code, 204)
        after = client.get("/api/house").json()
        self.assertEqual(next(r for r in after["rooms"] if r["id"] == self.room_id)["name"],
                         "Updated living room")
        with self.db.engine.connect() as conn:
            value = conn.execute(select(models.thermal_parameters.c.value).where(
                (models.thermal_parameters.c.room_id == self.room_id)
                & (models.thermal_parameters.c.name == "capacity_j_per_k"))).scalar_one()
        self.assertEqual(value, 12345678)


if __name__ == "__main__":
    unittest.main()
