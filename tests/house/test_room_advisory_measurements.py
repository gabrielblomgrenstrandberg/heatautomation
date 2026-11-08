import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import URL

from heatautomation.control.room_advisory import predict_scenarios, save_advisory
from heatautomation.control.room_advisory_monitor import run_cycle
from heatautomation.control.room_mpc import Room
from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.config import HouseConfig
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.integrations.tado import TadoService
from heatautomation.integrations.weather import current_forecast
from heatautomation.thermal.measurements import (
    Classification, Measurement, MeasurementStore, Quality, collect_tado_room,
)
from heatautomation.thermal.radiators import Circuit, Radiator, estimate_delivery
from heatautomation.thermal.advisory_inputs import EquipmentInputs, load_equipment_inputs
from heatautomation.web.rooms import read_advisory
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

    def test_historical_read_excludes_observations_not_yet_received(self):
        original = self.store.save(reading(ROOM))
        later = replace(reading(ROOM, value=21), observed_at=NOW + timedelta(minutes=5),
                        received_at=NOW + timedelta(minutes=10))
        self.store.save(later)
        replayed = self.store.latest(ROOM, "temperature", now=NOW + timedelta(minutes=7))
        self.assertEqual(replayed.id, original.id)
        self.assertEqual(later.at(NOW + timedelta(minutes=7), 600).quality, Quality.INVALID)
        live = self.store.latest(ROOM, "temperature", now=NOW + timedelta(minutes=11))
        self.assertEqual(live.value, 21)

    def test_upgrade_preserves_preexisting_room_measurements(self):
        path = Path(self.temp.name) / "old.sqlite3"
        alembic = Config(str(ROOT / "alembic.ini"))
        alembic.set_main_option("script_location", str(ROOT / "migrations"))
        alembic.set_main_option("sqlalchemy.url", URL.create("sqlite", database=str(path))
                                  .render_as_string(hide_password=False).replace("%", "%%"))
        command.upgrade(alembic, "0007")
        engine = create_engine("sqlite:///" + str(path))
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO room_measurements "
                    "(room_id, quantity, value, unit, observed_at, received_at, quality, classification) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ROOM, "temperature", 19.0, "C", NOW.isoformat(), NOW.isoformat(),
                     "suspect", "measured"))
            command.upgrade(alembic, "head")
            with engine.connect() as conn:
                old = conn.execute(select(models.room_measurements)).one()
            self.assertEqual(old.value, 19.0)
            self.assertIsNone(old.source_integration)
            self.assertIsNone(old.source_ref)
        finally:
            engine.dispose()

    def test_monitor_collects_tado_and_persists_unavailable_without_commands(self):
        data = self.house_data()
        data["tado_rooms"][0].update(home_id=1, zone_id=2)
        house = HouseConfig.model_validate(data)
        reconcile_house(self.engine, house)
        now = datetime.now(timezone.utc)
        status = {
            "room_id": ROOM, "home_id": 1, "zone_id": 2,
            "temperature_reading": {"value": 20, "timestamp": now.isoformat(),
                                    "received_at": now.isoformat(), "quality": "suspect"},
            "requested_temperature_c": 21, "heating_demand_percent": 60,
            "heating_demand_observed_at": now.isoformat(),
        }
        service = Mock()
        service.get_room_status.return_value = status
        result = run_cycle(self.engine, house, service, 5, suspect_uncertainty_c=0.5)
        self.assertFalse(result["available"])
        self.assertTrue(any("generator" in reason for reason in result["reasons"]))
        self.assertEqual(self.store.latest(ROOM, "temperature", now=datetime.now(timezone.utc),
                                           source_ref="tado:1:2").value, 20)
        self.assertEqual(read_advisory(str(Path(self.temp.name) / "thermal.sqlite3"))["mode"],
                         "advisory")
        service.get_room_status.assert_called_once_with(ROOM)
        service.set_room_temperature.assert_not_called()
        service.set_heating_enabled.assert_not_called()

    def test_monitor_produces_configured_tado_circuit_scenario(self):
        data = self.house_data()
        generator = "0eb76129-21c5-4e28-a5bd-264c9382c61c"
        thermostat = "89389cb1-1271-490b-9f91-de2cef5b7603"
        emitter = "6f24ec42-7652-4d6c-8941-8f8d97989332"
        data["rooms"] = [r for r in data["rooms"] if r["id"] == ROOM]
        data["floors"] = [f for f in data["floors"] if f["id"] == data["rooms"][0]["floor_id"]]
        data["devices"] = [d for d in data["devices"] if d["id"] in (generator, thermostat, emitter)]
        next(d for d in data["devices"] if d["id"] == emitter)["rated_w_at_dt50"] = 2000
        data["heat_paths"] = [p for p in data["heat_paths"] if p["id"] == "radiator-circuit"]
        data["heat_paths"][0]["emitter_ids"] = [emitter]
        data["room_priors"] = [p for p in data["room_priors"] if p["room_id"] == ROOM]
        data["thermal_links"] = []
        data["tado_rooms"] = [{"room_id": ROOM, "home_id": 1, "zone_id": 2}]
        house = HouseConfig.model_validate(data)
        path = Path(self.temp.name) / "single.sqlite3"
        migrate_database(path, backup=False)
        engine = create_engine("sqlite:///" + str(path))
        self.addCleanup(engine.dispose)
        reconcile_house(engine, house)
        with engine.begin() as conn:
            conn.execute(update(models.room_settings).where(
                models.room_settings.c.room_id == ROOM).values(target_c=21))
        now = datetime.now(timezone.utc)
        status = {
            "room_id": ROOM, "home_id": 1, "zone_id": 2,
            "temperature_reading": {"value": 20, "timestamp": now.isoformat(),
                                    "received_at": now.isoformat(), "quality": "suspect"},
            "requested_temperature_c": 21, "heating_demand_percent": 60,
            "heating_demand_observed_at": now.isoformat(),
        }
        service = Mock()
        service.get_room_status.return_value = status
        equipment_data = {
            "observed_at": now.isoformat(), "classification": "estimated", "provenance": "test",
            "circuit": {"generator_id": generator, "generator_available": True, "available_w": 1000,
                        "supply_c": 70, "return_c": 60, "exponent": 1.3,
                        "delivery_factors": [0.25, 0.5, 1.0]},
            "cost_per_delivered_kwh": {generator: 2},
            "cost_currency": "SEK",
        }
        equipment_path = Path(self.temp.name) / "equipment.json"
        equipment_path.write_text(json.dumps(equipment_data), encoding="utf-8")
        equipment = load_equipment_inputs(equipment_path, now)
        result = run_cycle(engine, house, service, 5, equipment=equipment,
                           suspect_uncertainty_c=0.5)
        self.assertTrue(result["available"])
        self.assertEqual(result["rooms"][0]["current_temperature_c"], 20)
        self.assertEqual(result["rooms"][0]["tado_demand_percent"][emitter], 60)
        self.assertLessEqual(result["rooms"][0]["estimated_radiator_w"]["high"], 1000)
        self.assertEqual(read_advisory(path)["rooms"][0]["room_id"], ROOM)
        self.assertEqual(read_advisory(path)["equipment_evidence"]["provenance"], "test")
        self.assertIsNotNone(result["house"]["estimated_heating_cost"])
        stale = equipment.model_copy(update={"observed_at": now - timedelta(minutes=11)})
        unavailable = run_cycle(engine, house, service, 5, equipment=stale,
                                suspect_uncertainty_c=0.5)
        self.assertFalse(unavailable["available"])
        self.assertIn("Equipment observations are stale or future-dated", unavailable["reasons"])
        invalid_room = equipment.model_copy(update={"direct_heat_w": {"not-a-room": {}}})
        unavailable = run_cycle(engine, house, service, 5, equipment=invalid_room,
                                suspect_uncertainty_c=0.5)
        self.assertIn("Direct heat inputs contain unconfigured rooms", unavailable["reasons"])
        duplicate_data = self.house_data()
        duplicate_data.update(data)
        duplicate_data["heat_paths"] = data["heat_paths"] + [{
            "id": "duplicated-generator", "generator_id": generator, "target_room_id": ROOM,
            "provenance": "test",
        }]
        duplicate_house = HouseConfig.model_validate(duplicate_data)
        reconcile_house(engine, duplicate_house)
        unavailable = run_cycle(engine, duplicate_house, service, 5, equipment=equipment,
                                suspect_uncertainty_c=0.5)
        self.assertIn("Generator is assigned to multiple heat paths; delivery cannot be separated safely",
                      unavailable["reasons"])
        service.set_room_temperature.assert_not_called()
        service.set_heating_enabled.assert_not_called()

    def test_equipment_inputs_require_freshness_and_calculate_conditional_cost(self):
        data = {
            "observed_at": NOW.isoformat(), "classification": "estimated",
            "provenance": "test", "circuit": {
                "generator_id": "generator", "generator_available": True, "available_w": 1000,
                "supply_c": 70, "return_c": 60, "exponent": 1.3,
                "delivery_factors": [0.25, 0.5, 1.0],
            },
            "direct_heat_w": {ROOM: {"kmp": {"low": 0, "nominal": 500, "high": 1000}}},
            "cost_per_delivered_kwh": {"generator": 2, "kmp": 1},
            "cost_currency": "SEK",
        }
        inputs = EquipmentInputs.model_validate(data)
        path = Path(self.temp.name) / "equipment.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = load_equipment_inputs(path, NOW)
        self.assertEqual(loaded, inputs)
        with self.assertRaisesRegex(ValueError, "stale"):
            load_equipment_inputs(path, NOW + timedelta(minutes=11))
        with self.assertRaisesRegex(ValueError, "future"):
            load_equipment_inputs(path, NOW - timedelta(seconds=1))
        for value in (True, float("nan"), "1000"):
            bad = dict(data, circuit=dict(data["circuit"], available_w=value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                EquipmentInputs.model_validate(bad)
        without_currency = dict(data)
        del without_currency["cost_currency"]
        with self.assertRaisesRegex(ValueError, "currency"):
            EquipmentInputs.model_validate(without_currency)
        with self.assertRaises(ValueError):
            EquipmentInputs.model_validate(dict(data, outdoor_uncertainty_c=-1))
        radiator = Radiator("radiator-1", ROOM, 2000, 60, 20)
        result = predict_scenarios(
            [room(ROOM)], {ROOM: reading(ROOM)}, demands([radiator]), loaded.circuit.circuit(),
            [radiator], {ROOM: {"kmp": loaded.direct_heat_w[ROOM]["kmp"].model_dump()}}, 5,
            now=NOW, suspect_uncertainty_c=0.5,
            cost_per_delivered_kwh=loaded.cost_per_delivered_kwh, cost_currency="SEK",
        )
        nominal_heat = result["rooms"][0]["estimated_radiator_w"]["nominal"]
        expected = 0.25 * (nominal_heat * 2 + 500) / 1000
        self.assertAlmostEqual(result["house"]["estimated_heating_cost"]["nominal"], expected)
        missing_cost = predict_scenarios(
            [room(ROOM)], {ROOM: reading(ROOM)}, demands([radiator]), loaded.circuit.circuit(),
            [radiator], {ROOM: {"kmp": loaded.direct_heat_w[ROOM]["kmp"].model_dump()}}, 5,
            now=NOW, suspect_uncertainty_c=0.5, cost_per_delivered_kwh={"generator": 2},
            cost_currency="SEK",
        )
        self.assertIsNone(missing_cost["house"]["estimated_heating_cost"])

    def test_external_room_sensor_is_bound_to_physical_device_and_expires(self):
        data = self.house_data()
        sensor = "f8103951-2df6-4442-9dd7-4295c44a99a9"
        data["rooms"] = [r for r in data["rooms"] if r["id"] == ROOM]
        data["floors"] = [f for f in data["floors"] if f["id"] == data["rooms"][0]["floor_id"]]
        data["devices"] = [{
            "id": sensor, "kind": "sensor", "name": "Room sensor",
            "room_id": ROOM, "adapter": "mill", "capabilities": ["read_temperature"],
            "provenance": "test",
        }]
        data["heat_paths"] = []
        data["room_priors"] = [p for p in data["room_priors"] if p["room_id"] == ROOM]
        data["thermal_links"] = []
        data["tado_rooms"] = []
        house = HouseConfig.model_validate(data)
        path = Path(self.temp.name) / "sensor.sqlite3"
        migrate_database(path, backup=False)
        engine = create_engine("sqlite:///" + str(path))
        self.addCleanup(engine.dispose)
        reconcile_house(engine, house)
        with engine.begin() as conn:
            conn.execute(update(models.room_settings).where(
                models.room_settings.c.room_id == ROOM).values(target_c=21))
        inputs = EquipmentInputs.model_validate({
            "observed_at": NOW.isoformat(), "classification": "estimated", "provenance": "test",
            "room_temperatures": {ROOM: {
                "device_id": sensor, "value_c": 19, "quality": "suspect",
                "classification": "measured", "connection_method": "LOCAL_HTTP",
            }},
        })
        result = run_cycle(engine, house, None, 5, equipment=inputs,
                           suspect_uncertainty_c=0.5, now=NOW)
        self.assertTrue(result["available"])
        self.assertEqual(result["rooms"][0]["current_temperature_c"], 19)
        self.assertEqual(result["rooms"][0]["temperature_quality"], "suspect")
        observed = MeasurementStore(engine).latest(
            ROOM, "temperature", now=NOW, source_integration="mill",
            source_ref=sensor, device_id=sensor)
        self.assertEqual(observed.connection_method, "LOCAL_HTTP")
        self.assertEqual(observed.classification, Classification.MEASURED)
        with engine.begin() as conn:
            conn.execute(models.room_schedules.insert().values(
                room_id=ROOM, weekday=NOW.weekday(), start_min=0, end_min=1440, target_c=22))
            conn.execute(models.room_overrides.insert().values(
                room_id=ROOM, starts_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=5), target_c=23, source="test"))
        with_override = run_cycle(engine, house, None, 5, equipment=inputs,
                                  suspect_uncertainty_c=0.5, now=NOW)
        self.assertEqual(with_override["rooms"][0]["target_c"], 23)
        with_schedule = run_cycle(engine, house, None, 5, equipment=inputs,
                                  suspect_uncertainty_c=0.5, now=NOW + timedelta(minutes=6))
        self.assertEqual(with_schedule["rooms"][0]["target_c"], 22)
        unmatched_circuit = inputs.model_copy(update={"circuit": EquipmentInputs.model_validate({
            "observed_at": NOW.isoformat(), "classification": "estimated", "provenance": "test",
            "circuit": {"generator_id": "unexpected", "generator_available": True, "available_w": 1000,
                        "supply_c": 50, "return_c": 40, "exponent": 1.3,
                        "delivery_factors": [0.2, 0.5, 0.8]},
        }).circuit})
        unavailable = run_cycle(engine, house, None, 5, equipment=unmatched_circuit,
                                suspect_uncertainty_c=0.5, now=NOW)
        self.assertIn("Equipment specifies a radiator circuit not present in the house configuration",
                      unavailable["reasons"])
        forecast_result = run_cycle(
            engine, house, None, None, equipment=inputs, suspect_uncertainty_c=0.5,
            now=NOW, timezone_name="Europe/Stockholm",
            outdoor_forecast=[{"time": "2026-01-01T01:00", "temperature_c": 4}])
        self.assertTrue(forecast_result["available"])
        self.assertEqual(forecast_result["outdoor_forecast_evidence"]["classification"], "predicted")
        self.assertEqual(forecast_result["inputs"]["outdoor_c"], 4)
        stale = run_cycle(engine, house, None, 5, equipment=inputs,
                          suspect_uncertainty_c=0.5, now=NOW + timedelta(minutes=11))
        self.assertFalse(stale["available"])
        self.assertIn("Equipment observations are stale or future-dated", stale["reasons"])
        bad = inputs.model_copy(update={"room_temperatures": {
            ROOM: inputs.room_temperatures[ROOM].model_copy(update={"device_id": "wrong"})}})
        result = run_cycle(engine, house, None, 5, equipment=bad,
                           suspect_uncertainty_c=0.5, now=NOW)
        self.assertFalse(result["available"])
        self.assertTrue(any("does not match" in reason for reason in result["reasons"]))
        data["devices"][0]["external_id"] = "mill:001122334455"
        data["lan_discovery"] = {"enabled": True, "allowed_cidrs": ["192.168.10.0/24"]}
        mill_house = HouseConfig.model_validate(data)
        reconcile_house(engine, mill_house)
        mill_time = NOW + timedelta(minutes=1)
        payload = {"ambient_temperature": {
            "value": 20.5, "unit": "C", "timestamp": mill_time.isoformat(), "source": "mill",
            "connection_method": "LOCAL_HTTP", "quality": "good", "classification": "measured",
        }}
        with engine.begin() as conn:
            conn.execute(models.lan_devices.insert().values(
                id="mill:001122334455", device_type="mill", name="Test sensor",
                mac_address="00:11:22:33:44:55", connection_method="LOCAL_HTTP",
                connection_state="LOCAL_UNAVAILABLE", last_error="Unavailable",
                capabilities_json="[]", measurements_json=json.dumps(payload)))
        mill_result = run_cycle(engine, mill_house, None, 5, equipment=inputs,
                                suspect_uncertainty_c=0.5, now=mill_time)
        self.assertTrue(mill_result["available"])
        self.assertEqual(mill_result["rooms"][0]["current_temperature_c"], 20.5)
        self.assertTrue(mill_result["connection_warnings"])
        mill_measurement = MeasurementStore(engine).latest(
            ROOM, "temperature", now=mill_time, source_integration="mill",
            source_ref="mill:001122334455", device_id=sensor)
        self.assertEqual(mill_measurement.value, 20.5)
        self.assertEqual(mill_measurement.observed_at, mill_time)
        payload["ambient_temperature"]["value"] = None
        payload["ambient_temperature"]["timestamp"] = (mill_time + timedelta(minutes=1)).isoformat()
        with engine.begin() as conn:
            conn.execute(update(models.lan_devices).where(
                models.lan_devices.c.id == "mill:001122334455").values(
                    measurements_json=json.dumps(payload)))
        retained = run_cycle(engine, mill_house, None, 5, equipment=inputs,
                             suspect_uncertainty_c=0.5, now=mill_time + timedelta(minutes=1))
        self.assertEqual(retained["rooms"][0]["current_temperature_c"], 20.5)
        expired = run_cycle(engine, mill_house, None, 5, suspect_uncertainty_c=0.5,
                            now=mill_time + timedelta(minutes=11))
        self.assertFalse(expired["available"])
        self.assertIn("%s: temperature missing" % ROOM, expired["reasons"])
        second_sensor = "6a0ad73d-40c3-4629-8111-c5709ff562f1"
        data["devices"].append({**data["devices"][0], "id": second_sensor, "external_id": None})
        expanded_house = HouseConfig.model_validate(data)
        reconcile_house(engine, expanded_house)
        second = inputs.model_copy(update={"room_temperatures": {
            ROOM: inputs.room_temperatures[ROOM].model_copy(update={"device_id": second_sensor})}})
        result = run_cycle(engine, expanded_house, None, 5, equipment=second,
                           suspect_uncertainty_c=0.5, now=NOW)
        self.assertFalse(result["available"])
        self.assertTrue(any("multiple current temperature sensors" in reason
                            for reason in result["reasons"]))

    def test_tado_temperature_only_room_does_not_require_radiator_demand(self):
        data = self.house_data()
        data["rooms"] = [r for r in data["rooms"] if r["id"] == ROOM]
        data["floors"] = [f for f in data["floors"] if f["id"] == data["rooms"][0]["floor_id"]]
        data["devices"] = []
        data["heat_paths"] = []
        data["room_priors"] = [p for p in data["room_priors"] if p["room_id"] == ROOM]
        data["thermal_links"] = []
        data["tado_rooms"] = [{"room_id": ROOM, "home_id": 1, "zone_id": 2}]
        house = HouseConfig.model_validate(data)
        path = Path(self.temp.name) / "tado-sensor.sqlite3"
        migrate_database(path, backup=False)
        engine = create_engine("sqlite:///" + str(path))
        self.addCleanup(engine.dispose)
        reconcile_house(engine, house)
        with engine.begin() as conn:
            conn.execute(update(models.room_settings).where(
                models.room_settings.c.room_id == ROOM).values(target_c=21))
        service = Mock()
        service.get_room_status.return_value = {
            "room_id": ROOM, "home_id": 1, "zone_id": 2,
            "temperature_reading": {"value": 20, "timestamp": NOW.isoformat(),
                                    "received_at": NOW.isoformat(), "quality": "suspect"},
            "requested_temperature_c": None, "heating_demand_percent": None,
            "heating_demand_observed_at": None,
        }
        result = run_cycle(engine, house, service, 5, suspect_uncertainty_c=0.5, now=NOW)
        self.assertTrue(result["available"])
        self.assertEqual(result["rooms"][0]["current_temperature_c"], 20)
        service.set_room_temperature.assert_not_called()
        service.set_heating_enabled.assert_not_called()

    @staticmethod
    def house_data():
        return load_house_config(ROOT / "config" / "house.example.json").model_dump(mode="json")


class AdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.rads = [Radiator("r1", "living", 2000, 100, 20),
                     Radiator("r2", "kitchen", 2000, 100, 20)]
        self.circuit = Circuit(True, 1000, 70, 60, 1.3, (0.25, 0.5, 1.0), "sprsun")

    def test_weather_requires_current_unambiguous_coverage(self):
        forecast = [{"time": "2026-01-01T01:00", "temperature_c": 4}]
        self.assertEqual(current_forecast(forecast, NOW, "Europe/Stockholm")["temperature_c"], 4)
        self.assertIsNone(current_forecast(forecast, NOW + timedelta(hours=1), "Europe/Stockholm"))
        self.assertIsNone(current_forecast(forecast * 2, NOW, "Europe/Stockholm"))
        ambiguous = [{"time": "2026-10-25T02:00", "temperature_c": 4}]
        self.assertIsNone(current_forecast(
            ambiguous, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), "Europe/Stockholm"))

    def test_explicit_forecast_uncertainty_widens_temperature_scenarios(self):
        arguments = ([room("living")], {"living": reading("living", quality=Quality.GOOD)},
                     {}, None, [], {"living": {}}, 5)
        fixed = predict_scenarios(*arguments, now=NOW, suspect_uncertainty_c=0.5)
        uncertain = predict_scenarios(*arguments, now=NOW, suspect_uncertainty_c=0.5,
                                      outdoor_uncertainty_c=5)
        fixed_temp = fixed["rooms"][0]["predicted_temperature_c"]
        predicted = uncertain["rooms"][0]["predicted_temperature_c"]
        self.assertLess(predicted["low"], fixed_temp["low"])
        self.assertGreater(predicted["high"], fixed_temp["high"])
        self.assertEqual(predicted["nominal"], fixed_temp["nominal"])
        self.assertIsNone(fixed["inputs"]["outdoor_uncertainty_c"])
        with self.assertRaises(ValueError):
            predict_scenarios(*arguments, now=NOW, suspect_uncertainty_c=0.5,
                              outdoor_uncertainty_c=float("nan"))

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
        with patch("heatautomation.control.room_advisory.simulate_step") as command:
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

    def test_independent_sensibo_and_mill_without_radiator_and_no_double_count(self):
        rooms = [room("living"), room("storage")]
        temperatures = {"living": reading("living", quality=Quality.GOOD),
                        "storage": reading("storage", quality=Quality.GOOD)}
        sources = {
            "living": {"sensibo": {"low": 500, "nominal": 1000, "high": 1500}},
            "storage": {"mill_heater": {"low": 0, "nominal": 500, "high": 1000}},
        }
        result = predict_scenarios(rooms, temperatures, {}, None, [], sources, 5,
                                   now=NOW, suspect_uncertainty_c=0.5)
        self.assertTrue(result["available"])
        self.assertEqual(result["rooms"][0]["estimated_other_heat_w"]["nominal"], 1000)
        self.assertEqual(result["rooms"][1]["estimated_other_heat_w"]["nominal"], 500)
        sources["storage"]["sensibo"] = sources["living"]["sensibo"]
        with self.assertRaisesRegex(ValueError, "only be assigned to one room"):
            predict_scenarios(rooms, temperatures, {}, None, [], sources, 5,
                              now=NOW, suspect_uncertainty_c=0.5)


if __name__ == "__main__":
    unittest.main()
