import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select

from heatautomation.control.room_advisory import predict_scenarios, save_advisory
from heatautomation.control.room_mpc import Room
from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.integrations.tado import TadoService
from heatautomation.thermal.measurements import (
    Classification, Measurement, MeasurementStore, Quality, collect_tado_room,
)
from heatautomation.thermal.radiators import Circuit, Radiator, estimate_delivery
from tests.integrations.test_tado import Response, Session, Store, tokens


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROOM = "6f1282eb-23f7-4c3e-8905-e056c1a35220"
ROOT = Path(__file__).resolve().parents[2]


def reading(room, value=20, quality=Quality.SUSPECT):
    return Measurement(None, None, room, "temperature", value, "C", NOW, NOW,
                       "tado", "CLOUD", quality, Classification.MEASURED, "tado:1:2")


def demands(radiators):
    return {r.id: Measurement(None, None, r.room_id, "heating_demand",
                              r.demand_percent, "%", NOW, NOW, "tado", "CLOUD",
                              Quality.SUSPECT, Classification.MEASURED, "tado:1:2")
            for r in radiators}


def room(name, temp=20, links=None):
    return Room(name, temp, 2e6, 100, links or {}, 18, 23, [20] * 144, 0.1)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "thermal.sqlite3"
        migrate_database(path, backup=False)
        self.engine = create_engine("sqlite:///" + str(path))
        self.addCleanup(self.engine.dispose)
        reconcile_house(self.engine, load_house_config(ROOT / "config" / "house.example.json"))
        self.store = MeasurementStore(self.engine)

    def test_tado_observation_preserves_quality_and_is_not_heat(self):
        status = {
            "room_id": ROOM, "home_id": 1, "zone_id": 2, "temperature_reading": {
                "value": 20.5, "timestamp": NOW.isoformat(), "received_at": NOW.isoformat(),
                "quality": "suspect"},
            "requested_temperature_c": 21, "heating_demand_percent": 60,
            "heating_demand_observed_at": NOW.isoformat(),
        }
        service = Mock()
        service.get_room_status.return_value = status
        values = collect_tado_room(service, self.store, ROOM, now=NOW)
        self.assertEqual([v.quality for v in values],
                         [Quality.SUSPECT, Quality.UNKNOWN, Quality.SUSPECT])
        self.assertEqual(values[2].quantity, "heating_demand")
        self.assertIsNone(values[0].device_id)
        self.assertEqual(values[0].source_integration, "tado")
        self.assertEqual(values[0].connection_method, "CLOUD")
        self.assertEqual(values[0].source_ref, "tado:1:2")
        collect_tado_room(service, self.store, ROOM, now=NOW)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(models.room_measurements)).all()), 2)
        service.get_room_status.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            collect_tado_room(service, self.store, ROOM, now=NOW)
        last = self.store.latest(ROOM, "temperature", now=NOW + timedelta(minutes=11))
        self.assertEqual(last.value, 20.5)
        self.assertEqual(last.quality, Quality.STALE)
        self.assertEqual(last.classification, Classification.MEASURED)
        self.assertIsNone(self.store.latest(ROOM, "temperature", now=NOW,
                                            source_integration="tado", source_ref="tado:1:3"))
        service.set_room_temperature.assert_not_called()
        service.set_heating_enabled.assert_not_called()

    def test_invalid_missing_stale_and_future_not_persisted(self):
        status = {
            "room_id": ROOM, "home_id": 1, "zone_id": 2, "temperature_reading": {
                "value": None, "timestamp": NOW.isoformat(), "received_at": NOW.isoformat(),
                "quality": "bad"},
            "requested_temperature_c": None, "heating_demand_percent": None,
            "heating_demand_observed_at": None,
        }
        service = Mock()
        service.get_room_status.return_value = status
        self.assertTrue(all(v.quality == Quality.UNKNOWN for v in
                            collect_tado_room(service, self.store, ROOM, now=NOW)))
        status["temperature_reading"].update(value=20, quality="stale")
        self.assertEqual(collect_tado_room(service, self.store, ROOM, now=NOW)[0].quality,
                         Quality.STALE)
        status["temperature_reading"].update(quality="good", timestamp=(NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(collect_tado_room(service, self.store, ROOM, now=NOW)[0].quality,
                         Quality.INVALID)
        self.assertIsNone(self.store.latest(ROOM, "temperature", now=NOW))

    def test_real_tado_adapter_response_can_be_stored(self):
        now = datetime.now(timezone.utc)
        house = SimpleNamespace(
            tado_rooms=[SimpleNamespace(room_id=ROOM, home_id=1, zone_id=2)],
            devices=[],
        )
        session = Session()
        session.responses = [tokens(), Response({"zoneStates": {"2": {
            "sensorDataPoints": {"insideTemperature": {
                "celsius": 19.5, "timestamp": now.isoformat()}},
            "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 21}},
            "activityDataPoints": {"heatingPower": {
                "percentage": 70, "timestamp": now.isoformat()}},
            "link": {"state": "ONLINE"},
        }}})]
        service = TadoService(house, Store(), session=session)
        with patch("heatautomation.integrations.tado.config.TADO_ENABLED", True):
            values = collect_tado_room(service, self.store, ROOM,
                                       now=now + timedelta(seconds=2))
        self.assertEqual(values[0].value, 19.5)
        self.assertEqual(values[0].quality, Quality.SUSPECT)
        self.assertEqual(values[2].value, 70)
        self.assertEqual(values[2].quantity, "heating_demand")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[-1][0], "GET")

    def test_advisory_result_and_assumptions_persist_separately(self):
        radiator = Radiator("radiator-1", ROOM, 2000, 60, 20)
        circuit = Circuit(True, 1000, 70, 60, 1.3, (0.25, 0.5, 1.0), "generator")
        temp = self.store.save(reading(ROOM))
        demand = self.store.save(demands([radiator])[radiator.id])
        result = predict_scenarios([room(ROOM)], {ROOM: temp}, {radiator.id: demand},
                                   circuit, [radiator], {ROOM: {}}, 5, now=NOW,
                                   suspect_uncertainty_c=0.5)
        plan_id = save_advisory(self.engine, result)
        with self.engine.connect() as conn:
            plan = conn.execute(select(models.optimizer_plans).where(
                models.optimizer_plans.c.id == plan_id)).one()
            interval = conn.execute(select(models.optimizer_plan_intervals).where(
                models.optimizer_plan_intervals.c.plan_id == plan_id)).one()
        import json
        self.assertEqual(plan.mode, "advisory")
        self.assertEqual(json.loads(plan.details_json)["model_version"], "room-advisory-1")
        self.assertEqual(json.loads(interval.data_json)["temperature_measurement_id"], temp.id)


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.rads = [Radiator("r1", "living", 2000, 100, 20),
                     Radiator("r2", "kitchen", 2000, 100, 20)]
        self.circuit = Circuit(True, 1000, 70, 60, 1.3, (0.25, 0.5, 1.0), "sprsun")

    def test_shared_capacity_unknown_and_no_demand_as_measurement(self):
        result = estimate_delivery(self.circuit, self.rads)
        self.assertEqual(result["classification"], "estimated")
        for scenario in result["scenarios"].values():
            self.assertLessEqual(sum(scenario.values()), 1000.0001)
            self.assertAlmostEqual(scenario["r1"], scenario["r2"])
        self.assertEqual(estimate_delivery(Circuit(None, 1000, 70, 60, 1.3, (0, .5, 1), "sprsun"),
                                           self.rads)["classification"], "unknown")
        self.assertIsNone(estimate_delivery(self.circuit, [
            Radiator("x", "living", None, 100, 20)])["scenarios"])
        off = estimate_delivery(Circuit(False, 1000, 70, 60, 1.3, (0, .5, 1), "sprsun"), self.rads)
        self.assertEqual(sum(off["scenarios"]["high"].values()), 0)

    def test_prediction_propagates_stove_heat_and_uncertainty(self):
        rooms = [room("living", links={"kitchen": 100}),
                 room("kitchen", links={"living": 100})]
        inputs = {"living": {"kmp": {"low": 1000, "nominal": 2000, "high": 3000}},
                  "kitchen": {}}
        temps = {"living": reading("living", quality=Quality.GOOD),
                 "kitchen": reading("kitchen")}
        result = predict_scenarios(rooms, temps, demands(self.rads), self.circuit,
                                   self.rads, inputs, 5, now=NOW, suspect_uncertainty_c=0.5)
        self.assertTrue(result["available"])
        living, kitchen = result["rooms"]
        self.assertEqual(living["estimated_other_heat_w"]["nominal"], 2000)
        self.assertEqual(kitchen["estimated_other_heat_w"]["nominal"], 0)
        self.assertGreater(kitchen["predicted_temperature_c"]["high"],
                           kitchen["predicted_temperature_c"]["low"])
        without = predict_scenarios(rooms, temps, demands(self.rads), self.circuit,
                                    self.rads, {"living": {}, "kitchen": {}}, 5,
                                    now=NOW, suspect_uncertainty_c=0.5)
        self.assertGreater(kitchen["predicted_temperature_c"]["nominal"],
                           without["rooms"][1]["predicted_temperature_c"]["nominal"])

    def test_missing_temperature_source_and_weather_block_predictions(self):
        rooms = [room("living"), room("storage")]
        rads = [self.rads[0]]
        with patch("heatautomation.thermal.model.simulate_step") as command:
            result = predict_scenarios(rooms, {"living": reading("living")}, demands(rads), self.circuit, rads,
                                       {"living": {}, "storage": {}}, 5, now=NOW,
                                       suspect_uncertainty_c=0.5)
            self.assertFalse(result["available"])
            self.assertIn("storage: temperature missing", result["reasons"])
            command.assert_not_called()
        self.assertFalse(predict_scenarios(rooms, {}, demands(rads), self.circuit, rads, {}, None, now=NOW,
                                           suspect_uncertainty_c=0.5)["available"])
        stale = reading("living").at(NOW + timedelta(minutes=11), 600)
        result = predict_scenarios([rooms[0]], {"living": stale}, demands(rads), self.circuit, rads,
                                   {"living": {}}, 5, now=NOW + timedelta(minutes=11),
                                   suspect_uncertainty_c=0.5)
        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
