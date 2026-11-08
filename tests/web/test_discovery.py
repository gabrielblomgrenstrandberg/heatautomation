import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from heatautomation.discovery.service import engine_for
from heatautomation.house import models
from heatautomation.house.migrate import migrate_database
from heatautomation.house.config import load_house_config
from heatautomation.house.reconcile import reconcile_house
from heatautomation.web.app import create_app


ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "a sufficiently long test password 123456"
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "house.example.json"


class DiscoveryWebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.database = path / "test.sqlite"
        migrate_database(self.database, backup=False)
        self.config = path / "house.json"
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        data["lan_discovery"].update(enabled=True, allowed_cidrs=["192.168.10.0/24"],
                                     allow_unauthenticated_mill_http=True)
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with patch.dict(os.environ, {"HEATAUTOMATION_HOUSE_CONFIG": str(self.config)}):
            self.client = TestClient(create_app(
                password=PASSWORD, database_path=self.database, origin=ORIGIN))
        self.addCleanup(self.client.close)
        self.engine = engine_for(self.database)
        self.addCleanup(self.engine.dispose)

    def test_authentication_origin_and_manual_queue(self):
        self.assertEqual(self.client.get("/api/discovery").status_code, 401)
        self.client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        self.assertEqual(self.client.post("/api/discovery").status_code, 403)
        self.assertEqual(self.client.post("/api/discovery", headers={"origin": ORIGIN}).status_code, 202)
        self.assertEqual(self.client.post("/api/discovery", headers={"origin": ORIGIN}).status_code, 202)
        data = self.client.get("/api/discovery").json()
        self.assertTrue(data["scan_requested"])
        self.assertEqual(data["devices"], [])
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(models.lan_scan_requests.select()).all()), 1)

    def test_stale_measurements_and_last_error(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(seconds=901)).isoformat()
        with self.engine.begin() as conn:
            conn.execute(insert(models.lan_devices).values(
                id="mill:001122334455", device_type="mill", name="Mill", ip_address="192.168.10.5",
                mac_address="00:11:22:33:44:55", connection_method="LOCAL_HTTP",
                connection_state="LOCAL_UNAVAILABLE", last_error="Local API unavailable",
                capabilities_json="[]", measurements_json=json.dumps({
                    "ambient_temperature": {"value": 20, "timestamp": timestamp}})))
        self.client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        device = self.client.get("/api/discovery").json()["devices"][0]
        self.assertTrue(device["measurements"]["ambient_temperature"]["stale"])
        self.assertEqual(device["measurements"]["ambient_temperature"]["value"], 20)
        self.assertEqual(device["last_error"], "Local API unavailable")

    def test_configured_probe_failure_is_visible_without_registering_device(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["lan_discovery"]["protected_mill"] = [
            {"ip_address": "192.168.10.5", "mac_address": "00:11:22:33:44:55"}]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.engine.begin() as conn:
            conn.execute(insert(models.lan_probe_errors).values(
                device_id="mill:001122334455", checked_at=datetime.now(timezone.utc).isoformat(),
                error="Mill API key or trusted certificate is not configured"))
        self.client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        data = self.client.get("/api/discovery").json()
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["probe_errors"][0]["device_id"], "mill:001122334455")

    def test_old_health_check_preserves_last_error(self):
        timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.engine.begin() as conn:
            conn.execute(insert(models.lan_devices).values(
                id="mill:001122334455", device_type="mill", name="Mill",
                ip_address="192.168.10.5", mac_address="00:11:22:33:44:55",
                connection_method="LOCAL_HTTP", connection_state="LOCAL_UNAVAILABLE",
                last_connection=timestamp, last_error="Local API unavailable",
                capabilities_json="[]", measurements_json="{}"))
        self.client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        device = self.client.get("/api/discovery").json()["devices"][0]
        self.assertTrue(device["monitoring_stale"])
        self.assertEqual(device["connection_state"], "LOCAL_UNAVAILABLE")
        self.assertEqual(device["last_error"], "Local API unavailable")
        with self.engine.begin() as conn:
            conn.execute(update(models.lan_devices).values(
                connection_state="CONNECTED_LOCAL", last_error=None))
        device = self.client.get("/api/discovery").json()["devices"][0]
        self.assertTrue(device["monitoring_stale"])
        self.assertEqual(device["connection_state"], "UNKNOWN")
        self.assertIsNone(device["last_error"])

    def test_explicit_reconciled_identity_maps_device_across_ip_changes(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        configured = next(device for device in config["devices"] if device["adapter"] == "mill")
        configured["external_id"] = "mill:001122334455"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        reconcile_house(self.engine, load_house_config(self.config))
        with self.engine.begin() as conn:
            conn.execute(insert(models.lan_devices).values(
                id="mill:001122334455", device_type="mill", name="Mill",
                ip_address="192.168.10.5", mac_address="00:11:22:33:44:55",
                connection_method="LOCAL_HTTP", connection_state="CONNECTED_LOCAL",
                capabilities_json="[]", measurements_json="{}"))
        self.client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        first = self.client.get("/api/discovery").json()["devices"][0]
        self.assertEqual(first["house_device"]["id"], configured["id"])
        with self.engine.begin() as conn:
            conn.execute(update(models.lan_devices).values(ip_address="192.168.10.6"))
        second = self.client.get("/api/discovery").json()["devices"][0]
        self.assertEqual(second["house_device"], first["house_device"])
        self.assertEqual(second["ip_address"], "192.168.10.6")


if __name__ == "__main__":
    unittest.main()
