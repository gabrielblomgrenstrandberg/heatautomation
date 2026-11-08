import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from heatautomation.learning.database import ThermalDatabase
from heatautomation.web.app import create_app
from heatautomation.web.status import StatusUnavailable, read_status


PASSWORD = "a sufficiently long test password 123456"
ORIGIN = "http://127.0.0.1:8000"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "thermal.sqlite3"
        self.db = ThermalDatabase(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.client = TestClient(create_app(password=PASSWORD, database_path=self.path, origin=ORIGIN))
        self.addCleanup(self.client.close)

    def save(self, timestamp):
        self.db.save(timestamp, {
            "indoor_c": 20.5, "outdoor_c": -2, "target_c": 21,
            "price_sek_kwh": 0.9,
        }, {
            "heatpump": {"is_on": True, "mode": "heat", "capacity_kw": 4.0},
            "pelletstove": {"is_on": None, "mode": None, "capacity_kw": None},
        })

    def login(self):
        return self.client.post("/api/session", json={"password": PASSWORD},
                                headers={"origin": ORIGIN})

    def test_authentication_origin_and_logout(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.post("/api/session", json={"password": PASSWORD}).status_code, 403)
        self.assertEqual(self.client.post("/api/session", json={"password": "wrong"},
                                          headers={"origin": ORIGIN}).status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 204)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/status").json()["state"], "unavailable")
        self.assertEqual(self.client.delete("/api/session", headers={"origin": ORIGIN}).status_code, 204)
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_login_throttling_and_password_policy(self):
        with self.assertRaises(RuntimeError):
            create_app(password="short", database_path=self.path)
        for _ in range(5):
            self.assertEqual(self.client.post("/api/session", json={"password": "wrong"},
                                              headers={"origin": ORIGIN}).status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_status_freshness_and_unknowns(self):
        now = datetime.now(timezone.utc)
        self.save(now)
        data = read_status(self.path, now=now + timedelta(seconds=900))
        self.assertEqual(data["state"], "current")
        self.assertEqual(data["indoor_c"], 20.5)
        self.assertTrue(data["heatpump"]["on"])
        self.assertIsNone(data["pelletstove"]["on"])
        self.assertIsNone(data["pelletstove"]["capacity_kw"])
        self.assertEqual(read_status(self.path, now=now + timedelta(seconds=901))["state"], "stale")
        self.assertEqual(read_status(self.path, now=now - timedelta(seconds=1))["state"], "stale")
        self.login()
        self.assertEqual(self.client.get("/api/status").json()["indoor_c"], 20.5)

    def test_missing_database_is_not_created_and_error_is_reported(self):
        path = Path(self.directory.name) / "missing.sqlite3"
        with self.assertRaises(StatusUnavailable):
            read_status(path)
        self.assertFalse(path.exists())
        client = TestClient(create_app(password=PASSWORD, database_path=path, origin=ORIGIN))
        self.addCleanup(client.close)
        client.post("/api/session", json={"password": PASSWORD}, headers={"origin": ORIGIN})
        self.assertEqual(client.get("/api/status").status_code, 503)

    def test_websocket_authentication_origin_and_snapshot(self):
        self.save(datetime.now(timezone.utc))
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/live", headers={"origin": ORIGIN}):
                pass
        self.login()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/live", headers={"origin": "http://evil.example"}):
                pass
        with self.client.websocket_connect("/api/live", headers={"origin": ORIGIN}) as websocket:
            message = websocket.receive_json()
            self.assertEqual(message["type"], "status")
            self.assertEqual(message["data"], self.client.get("/api/status").json())
            self.assertEqual(message["data"]["indoor_c"], 20.5)
            self.assertIsNone(message["data"]["pelletstove"]["on"])


if __name__ == "__main__":
    unittest.main()
