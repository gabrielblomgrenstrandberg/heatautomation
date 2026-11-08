import configparser
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from heatautomation import config
from heatautomation.control import mpc_monitor
from heatautomation.control.mpc import PlanningUnavailable, Settings
from heatautomation.learning import collection, profile, training
from tests.control.test_mpc import predictor
from tests.integrations.test_tado import EXAMPLE
from tests.learning.test_thermal_learning import history


TADO_PROFILE = {
    "indoor_source": "tado", "tado_room_id": "room-1",
    "tado_zone": "1:2",
    "heatpump_absent": True, "pelletstove_absent": False,
    "other_heating_absent": True,
}


class SelectedIntegrationTests(unittest.TestCase):
    def test_auto_and_disabled_sensibo_choose_unique_mapped_tado_room(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        mapped = data["tado_rooms"][0]
        mapped["home_id"], mapped["zone_id"] = 1, 2
        settings = configparser.ConfigParser()
        settings.read_dict({"mpc": {"indoor_source": "auto", "tado_room_id": ""}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "house.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(config, "_settings", settings), \
                    patch.object(config, "SENSIBO_ENABLED", False), \
                    patch.object(config, "TADO_ENABLED", True), \
                    patch.object(config, "KMP_ENABLED", True), \
                    patch.dict("os.environ", {"HEATAUTOMATION_HOUSE_CONFIG": str(path)}):
                result = profile.current()
                self.assertEqual(result["indoor_source"], "tado")
                self.assertEqual(result["tado_room_id"], mapped["room_id"])
                self.assertEqual(result["tado_zone"], "1:2")
                self.assertFalse(result["heatpump_absent"])
                self.assertFalse(result["other_heating_absent"])
                settings.set("mpc", "indoor_source", "sensibo")
                self.assertEqual(profile.current(), result)
                second = data["tado_rooms"][1]
                second["home_id"], second["zone_id"] = 1, 3
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "exactly one mapped room"):
                    profile.current()
                settings.set("mpc", "tado_room_id", mapped["room_id"])
                self.assertEqual(profile.current()["tado_zone"], "1:2")
                path.unlink()
                with self.assertRaisesRegex(ValueError, "valid house configuration"):
                    profile.current()

    def test_auto_prefers_enabled_sensibo_and_never_uses_disabled_tado(self):
        settings = configparser.ConfigParser()
        settings.read_dict({"mpc": {"indoor_source": "auto"}})
        with patch.object(config, "_settings", settings), \
                patch.object(config, "SENSIBO_ENABLED", True), \
                patch.object(config, "TADO_ENABLED", True):
            self.assertEqual(profile.current()["indoor_source"], "sensibo")
        with patch.object(config, "_settings", settings), \
                patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "TADO_ENABLED", False):
            with self.assertRaisesRegex(ValueError, "No enabled indoor"):
                profile.current()
            with self.assertRaisesRegex(PlanningUnavailable, "No enabled indoor"):
                mpc_monitor.run_shadow_cycle(Mock(), lambda _: 4)
            settings.set("mpc", "indoor_source", "tado")
            with self.assertRaisesRegex(ValueError, "Tado indoor source is disabled"):
                profile.current()

    def test_tado_collection_uses_only_recent_online_room_temperature(self):
        service = Mock()
        database = Mock()
        reading = {
            "temperature_reading": {
                "value": 20.5, "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "home_id": 1, "zone_id": 2,
            "connection": "ONLINE", "requested_temperature_c": 21,
        }
        service.get_room_status.return_value = reading
        with patch.object(profile, "current", return_value=TADO_PROFILE), \
                patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "TADO_ENABLED", True), \
                patch.object(config, "KMP_ENABLED", True), \
                patch.object(config, "TIBBER_ENABLED", False), \
                patch.object(config, "WEATHER_ENABLED", False), \
                patch.object(collection.sensibo, "status") as sensibo, \
                patch.object(collection.kmp, "get_status", return_value={"mode": "AV"}):
            collection.collect(database, lambda _: 4, service)
            sensors, sources = database.save.call_args.args[1:]
            self.assertEqual(sensors["indoor_c"], 20.5)
            self.assertEqual(sensors["thermal_profile"], profile.marker(TADO_PROFILE))
            self.assertEqual(sources["heatpump"], {
                "is_on": False, "mode": "absent", "capacity_kw": 0.0,
            })
            sensibo.assert_not_called()
            service.get_room_status.assert_called_with("room-1")

            for timestamp, connection in [
                ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(), "ONLINE"),
                (datetime.now(timezone.utc).isoformat(), "OFFLINE"),
                ("bad timestamp", "ONLINE"),
                (datetime.now().isoformat(), "ONLINE"),
            ]:
                reading["temperature_reading"]["timestamp"] = timestamp
                reading["connection"] = connection
                collection.collect(database, lambda _: 4, service)
                self.assertIsNone(database.save.call_args.args[1]["indoor_c"])

    def test_selected_tado_disabled_does_not_read(self):
        service = Mock()
        with patch.object(profile, "current", return_value=TADO_PROFILE), \
                patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "TADO_ENABLED", False), \
                patch.object(config, "KMP_ENABLED", False), \
                patch.object(config, "TIBBER_ENABLED", False), \
                patch.object(config, "WEATHER_ENABLED", False):
            database = Mock()
            collection.collect(database, lambda _: 4, service)
        service.get_room_status.assert_not_called()
        self.assertIsNone(database.save.call_args.args[1]["indoor_c"])

    def test_tado_kmp_only_plan_excludes_absent_heat_pump(self):
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        records = [{
            "timestamp": (now - timedelta(minutes=30 - i * 5)).isoformat(),
            "thermal_profile": profile.marker(TADO_PROFILE), "indoor_c": 20,
            "heatpump_is_on": False, "heatpump_mode": "absent",
            "pelletstove_is_on": False, "pelletstove_mode": "AV",
        } for i in range(7)]
        database = Mock()
        database.history.return_value = records
        database.save_plan.return_value = (1, [])
        model = predictor()
        model.artifact["models"]["heating"].coef_[4] = 0.8
        model.artifact.update(created_at=now.isoformat(), thermal_profile=TADO_PROFILE)
        start = now + timedelta(minutes=15)
        prices = [{"startsAt": (start + timedelta(minutes=i * 15)).isoformat(), "total": 1}
                  for i in range(16)]
        forecast = [{"time": "2026-01-01T{:02d}:00".format(i), "temperature_c": 0}
                    for i in range(1, 7)]
        settings = configparser.ConfigParser()
        settings.read_dict({"mpc": {
            "indoor_source": "auto", "heatpump_absent": "true",
            "other_heating_absent": "true",
        }})
        house = SimpleNamespace(tado_rooms=[
            SimpleNamespace(room_id="room-1", home_id=1, zone_id=2)
        ])
        with patch.object(config, "_settings", settings), \
                patch("heatautomation.house.config.load_house_config", return_value=house), \
                patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "TADO_ENABLED", True), \
                patch.object(config, "KMP_ENABLED", True), \
                patch.object(config, "TIBBER_ENABLED", True), \
                patch.object(config, "WEATHER_ENABLED", True), \
                patch.object(config, "WEATHER_TIMEZONE", "Europe/Stockholm"), \
                patch.object(mpc_monitor, "_settings",
                             return_value=Settings(18, 20, 22, 1, uncertainty_margin_c=0)), \
                patch.object(mpc_monitor, "ThermalPredictor", return_value=model), \
                patch.object(mpc_monitor.tibber, "get_price_forecast", return_value=prices), \
                patch.object(mpc_monitor.weather, "get_forecast", return_value=forecast), \
                patch.object(collection.kmp, "on") as stove_on, \
                patch.object(collection.kmp, "off") as stove_off, \
                patch.object(collection.sensibo, "on") as pump_on, \
                patch.object(collection.sensibo, "off") as pump_off:
            self.assertEqual(mpc_monitor.run_shadow_cycle(database, lambda _: 6, now), 1)
            for command in (stove_on, stove_off, pump_on, pump_off):
                command.assert_not_called()
            plan = database.save_plan.call_args.args[-1]
            self.assertTrue(plan)
            self.assertTrue(all(not row["mode"] & 1 and row["heatpump_kw"] == 0
                                for row in plan))
            model.artifact["thermal_profile"] = dict(TADO_PROFILE, tado_room_id="another")
            with self.assertRaisesRegex(PlanningUnavailable, "model does not match"):
                mpc_monitor.run_shadow_cycle(database, lambda _: 6, now)
            records[0]["thermal_profile"] = None
            with self.assertRaisesRegex(PlanningUnavailable, "observations do not match"):
                mpc_monitor.run_shadow_cycle(database, lambda _: 6, now)

    def test_planning_rejects_unmodeled_heating_and_unknown_source(self):
        with patch.object(config, "TIBBER_ENABLED", True), \
                patch.object(config, "WEATHER_ENABLED", True), \
                patch.object(config, "TADO_ENABLED", True), \
                patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "KMP_ENABLED", True):
            with patch.object(profile, "current", return_value=dict(
                    TADO_PROFILE, other_heating_absent=False)):
                with self.assertRaisesRegex(PlanningUnavailable, "not modeled"):
                    mpc_monitor.run_shadow_cycle(Mock(), lambda _: 4)
            with patch.object(profile, "current", return_value=dict(
                    TADO_PROFILE, heatpump_absent=False)):
                with self.assertRaisesRegex(PlanningUnavailable, "Heat pump state unavailable"):
                    mpc_monitor.run_shadow_cycle(Mock(), lambda _: 4)

    def test_training_does_not_mix_indoor_sources(self):
        records = history()
        database = SimpleNamespace(history=lambda: records, save_evaluation=Mock())
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "model.joblib")
            self.assertFalse(training.train(database, path, TADO_PROFILE))
            self.assertFalse(Path(path).exists())
            for record in records:
                record["thermal_profile"] = profile.marker(TADO_PROFILE)
            self.assertTrue(training.train(database, path, TADO_PROFILE))

    def test_profile_tracks_actual_home_and_zone_mapping(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        room = data["tado_rooms"][0]
        room["home_id"], room["zone_id"] = 1, 2
        settings = configparser.ConfigParser()
        settings.read_dict({"mpc": {
            "indoor_source": "tado", "tado_room_id": room["room_id"],
            "heatpump_absent": "true", "other_heating_absent": "true",
        }})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "house.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(config, "_settings", settings), \
                    patch.object(config, "SENSIBO_ENABLED", False), \
                    patch.object(config, "KMP_ENABLED", True), \
                    patch.dict("os.environ", {"HEATAUTOMATION_HOUSE_CONFIG": str(path)}):
                first = profile.current()
                self.assertEqual(first["tado_zone"], "1:2")
                room["zone_id"] = 3
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertNotEqual(profile.marker(first), profile.marker(profile.current()))
                room["home_id"] = room["zone_id"] = None
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "mapped home and zone"):
                    profile.current()


if __name__ == "__main__":
    unittest.main()
