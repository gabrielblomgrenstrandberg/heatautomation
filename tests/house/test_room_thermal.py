import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select, update

from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.config import HouseConfig
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.thermal.rooms import HORIZONS, RoomThermalService


ROOT = Path(__file__).resolve().parents[2]
STORAGE = "d981f1f6-bf20-40e4-8526-c008ad0f30b2"
HEATER = "09963c36-c69b-4472-be21-4d6095292759"
SENSOR = "c1565fc6-f3e9-40db-a83d-a4dc4cd195c1"


class RoomThermalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        path = Path(self.dir.name) / "thermal.sqlite3"
        migrate_database(path, backup=False)
        self.engine = create_engine("sqlite:///" + str(path))
        self.addCleanup(self.engine.dispose)
        self.house = load_house_config(ROOT / "config" / "house.example.json")
        reconcile_house(self.engine, self.house)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.service = RoomThermalService(self.engine, now=lambda: self.start)

    def storage_isolated(self):
        with self.engine.begin() as conn:
            conn.execute(update(models.thermal_links).where(
                (models.thermal_links.c.room_a_id == STORAGE)
                | (models.thermal_links.c.room_b_id == STORAGE)
            ).values(removed_at=self.start))
            conn.execute(update(models.room_settings).where(
                models.room_settings.c.room_id == STORAGE).values(target_c=21))

    def save(self, when, temperature, outdoor, watts):
        return self.service.save_snapshot(
            when, {STORAGE: {"value": temperature, "source_device_id": SENSOR,
                             "quality": "good"}}, outdoor,
            {STORAGE: {HEATER: {"watts": watts, "classification": "measured"}}})

    def test_graph_excludes_kmp_office_and_floor_zero(self):
        params, sources, _ = self.service._configuration()
        living = "6f1282eb-23f7-4c3e-8905-e056c1a35220"
        self.assertEqual(len(params[living]["conductances_w_per_k"]), 3)
        self.assertNotIn("7b290880-803b-422a-aea0-9c333237a32b",
                         params[living]["conductances_w_per_k"])
        self.assertEqual(sources[STORAGE], {HEATER})

    def test_forecast_unknown_and_evaluation_by_room_and_horizon(self):
        self.storage_isolated()
        data = self.save(self.start, 18, 5, 2000)
        result = self.service.predict(self.start, data)
        storage = result[STORAGE]
        self.assertEqual(set(storage["temperatures_c"]), set(HORIZONS))
        self.assertGreater(storage["temperatures_c"][900], 18)
        self.assertGreater(storage["expected_heating_demand_w"], 0)
        self.assertGreater(storage["cooling_rate_c_per_hour"], 0)
        self.assertEqual(result["6f1282eb-23f7-4c3e-8905-e056c1a35220"]["classification"],
                         "unknown")
        measured = storage["temperatures_c"][900] + 0.5
        self.save(self.start + timedelta(minutes=15), measured, 5, 2000)
        metrics = self.service.evaluate_predictions()
        self.assertAlmostEqual(metrics[STORAGE][900]["mae_c"], 0.5)
        self.assertEqual(metrics[STORAGE][900]["samples"], 1)
        self.assertEqual(self.service.evaluate_predictions()[STORAGE][900]["samples"], 1)

    def test_unknown_heat_or_neighbor_not_assumed_zero(self):
        data = self.service.save_snapshot(self.start, {
            STORAGE: {"value": 20, "source_device_id": SENSOR, "quality": "good"}
        }, 0, {})
        self.assertEqual(self.service.predict(self.start, data)[STORAGE]["classification"], "unknown")
        self.storage_isolated()
        self.assertEqual(self.service.predict(self.start, data)[STORAGE]["classification"], "unknown")

    def test_retrospective_forecasts_do_not_count_as_live_accuracy(self):
        self.storage_isolated()
        data = self.save(self.start, 18, 5, 2000)
        late = RoomThermalService(self.engine, now=lambda: self.start + timedelta(days=1))
        late.predict(self.start, data)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.room_thermal_predictions)).all(), [])

    def test_learning_identifiable_data_and_preserving_learned_values(self):
        self.storage_isolated()
        temp = 19.0
        for i in range(150):
            outdoor = 2 + 5 * math.sin(i / 17)
            watts = 1500 if (i // 5) % 2 else 0
            self.save(self.start + timedelta(minutes=5 * i), temp, outdoor, watts)
            temp += 300 * (watts - 90 * (temp - outdoor)) / 5000000
        report = self.service.learn()
        self.assertEqual(report[STORAGE]["status"], "validated")
        with self.engine.connect() as conn:
            rows = {r.name: r.value for r in conn.execute(select(models.thermal_parameters).where(
                models.thermal_parameters.c.room_id == STORAGE))}
        self.assertAlmostEqual(rows["capacity_j_per_k"], 5000000, delta=1)
        self.assertAlmostEqual(rows["ua_w_per_k"], 90, delta=0.01)
        reconcile_house(self.engine, self.house)
        with self.engine.connect() as conn:
            value = conn.execute(select(models.thermal_parameters.c.value).where(
                (models.thermal_parameters.c.room_id == STORAGE)
                & (models.thermal_parameters.c.name == "ua_w_per_k"))).scalar_one()
        self.assertAlmostEqual(value, 90, delta=0.01)

    def test_learning_rejects_estimated_heat_and_insufficient_data(self):
        self.storage_isolated()
        for i in range(70):
            self.service.save_snapshot(
                self.start + timedelta(minutes=i * 5),
                {STORAGE: {"value": 20, "quality": "good", "source_device_id": SENSOR}}, 5,
                {STORAGE: {HEATER: {"watts": 1000, "classification": "estimated"}}})
        report = self.service.learn()
        self.assertEqual(report[STORAGE]["status"], "insufficient_samples")

    def test_symmetric_transfer_is_learned_from_both_rooms(self):
        living = "6f1282eb-23f7-4c3e-8905-e056c1a35220"
        kitchen = "be1dd3a2-ed38-473b-8c60-8d90a7b2aea4"
        params, sources, _ = self.service._configuration()
        with self.engine.begin() as conn:
            for row in conn.execute(select(models.thermal_links)).all():
                if {row.room_a_id, row.room_b_id} != {living, kitchen}:
                    conn.execute(update(models.thermal_links).where(
                        (models.thermal_links.c.room_a_id == row.room_a_id)
                        & (models.thermal_links.c.room_b_id == row.room_b_id)
                    ).values(removed_at=self.start))
        sensors = {str(device.room_id): str(device.id) for device in self.house.devices
                   if device.adapter == "tado"}
        temperatures = {living: 20.0, kitchen: 18.0}
        for i in range(240):
            outdoor = 3 + 5 * math.sin(i / 19)
            powers = {living: 1600 if (i // 8) % 2 else 0,
                      kitchen: 900 if (i // 13) % 2 else 0}
            heat = {}
            for room in (living, kitchen):
                source_list = sorted(sources[room])
                heat[room] = {source: {"watts": powers[room] if j == 0 else 0,
                                       "classification": "measured"}
                              for j, source in enumerate(source_list)}
            self.service.save_snapshot(
                self.start + timedelta(minutes=5 * i),
                {room: {"value": temperatures[room], "quality": "good",
                        "source_device_id": sensors[room]} for room in temperatures},
                outdoor, heat)
            previous = temperatures.copy()
            for room, other, capacity, ua in (
                    (living, kitchen, 5e6, 90), (kitchen, living, 4e6, 70)):
                temperatures[room] += 300 * (
                    powers[room] - ua * (previous[room] - outdoor)
                    + 55 * (previous[other] - previous[room])) / capacity
        report = self.service.learn()
        self.assertEqual(report[living]["status"], "validated")
        self.assertEqual(report[kitchen]["status"], "validated")
        params, _, _ = self.service._configuration()
        self.assertAlmostEqual(params[living]["conductances_w_per_k"][kitchen], 55, delta=0.01)
        self.assertEqual(params[living]["conductances_w_per_k"][kitchen],
                         params[kitchen]["conductances_w_per_k"][living])

    def test_sensor_must_be_assigned_to_room(self):
        with self.assertRaises(ValueError):
            self.service.save_snapshot(self.start, {
                STORAGE: {"value": 20, "quality": "good", "source_device_id": HEATER}
            }, 5, {STORAGE: {HEATER: {"watts": 0, "classification": "measured"}}})

    def test_explicit_legacy_sensibo_import_is_suspect_and_idempotent(self):
        data = self.house.model_dump(mode="json")
        sensibo_id = "00000000-0000-4000-8000-000000000001"
        data["devices"].append({
            "id": sensibo_id, "kind": "sensor", "name": "Sensibo",
            "room_id": STORAGE, "adapter": "sensibo", "capabilities": ["read_temperature"],
            "provenance": "config",
        })
        reconcile_house(self.engine, HouseConfig.model_validate(data))

        class Legacy:
            def history(self):
                return [{"timestamp": "2026-01-01T00:00:00+00:00",
                         "indoor_c": 20, "outdoor_c": 2,
                         "pelletstove_capacity_kw": 8}]
        self.assertEqual(self.service.import_legacy_history(Legacy(), STORAGE, sensibo_id), 1)
        self.assertEqual(self.service.import_legacy_history(Legacy(), STORAGE, sensibo_id), 0)
        _, snapshot = self.service.latest_snapshot()
        self.assertEqual(snapshot["temperatures"][STORAGE]["quality"], "suspect")
        self.assertEqual(snapshot["heat_inputs"], {})


if __name__ == "__main__":
    unittest.main()
