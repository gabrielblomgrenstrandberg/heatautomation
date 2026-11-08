"""Synthetic, prediction-only multi-room integration scenarios."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


import requests
from scipy.integrate import solve_ivp
from sqlalchemy import create_engine, insert, select, update

from heatautomation.control.mpc import PlanningUnavailable
from heatautomation.control.room_mpc import (
    Forecast, Room, Source, optimize_rooms, rooms_from_database,
)
from heatautomation.devices.command_gate import CommandGate
from heatautomation.house import models
from heatautomation.house.config import HouseConfig
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.integrations.mill import MillError, MillHeatingSource, MillPolicy
from heatautomation.integrations.tado import TadoError, TadoService
from heatautomation.learning import collection
from heatautomation.learning.database import ThermalDatabase, metadata, observations, sensors
from heatautomation.thermal.model import room_heat_demand_w
from heatautomation.thermal.rooms import RoomThermalService
from heatautomation.web import startup


ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class MockWorld:
    """Separately integrated synthetic plant; inputs are not real device observations."""

    def __init__(self, house):
        self.house = house
        self.ids = {r.name: str(r.id) for r in house.rooms}
        self.temperature = {str(r.id): 20.0 for r in house.rooms}
        self.sources = {
            "sensibo": Mock(name="sensibo"),
            "kmp": Mock(name="kmp"),
            "tibber": Mock(name="tibber"),
            "weather": Mock(name="weather"),
        }
        self.sources["weather"].get_forecast.return_value = [{"temperature_c": -5}]
        self.sources["tibber"].prices = [0.1] * 8 + [3.0] * 136
        self.sources["tibber"].get_spot_price.return_value = 0.8
        self.sources["sensibo"].status.return_value = {
            "result": {"acState": {"on": True, "mode": "heat", "targetTemperature": 21},
                       "mainMeasurementsSensor": {"measurements": {"temperature": 19.5}}}}
        self.sources["kmp"].get_status.return_value = {"mode": "LADDAR"}

    def integrate(self, params, delivered, outdoor, seconds):
        rooms = list(self.temperature)
        position = {room: i for i, room in enumerate(rooms)}
        def derivative(_time, state):
            return [
                (delivered.get(room, 0)
                 + sum(g * (state[position[neighbor]] - state[position[room]])
                       for neighbor, g in params[room]["conductances_w_per_k"].items())
                 - params[room]["ua_w_per_k"] * (state[position[room]] - outdoor))
                / params[room]["capacity_j_per_k"]
                for room in rooms
            ]
        result = solve_ivp(derivative, (0, seconds),
                           [self.temperature[room] for room in rooms],
                           rtol=1e-10, atol=1e-10)
        if not result.success:
            raise RuntimeError(result.message)
        return dict(zip(rooms, result.y[:, -1]))


class Response:
    def __init__(self, data):
        self.status_code = 200
        self.headers = {}
        self.content = b"data"
        self.data = data

    def json(self):
        return self.data


class SimulatedTadoSession:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.fail = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if self.fail:
            raise requests.ConnectionError("simulated Tado outage")
        if "oauth" in url:
            return Response({"access_token": "test", "refresh_token": "rotated", "expires_in": 3600})
        return Response({"zoneStates": {str(zone): {
            "sensorDataPoints": {"insideTemperature": {"celsius": 18 + zone / 10}},
            "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 20 + zone / 10}},
            "activityDataPoints": {"heatingPower": {"percentage": zone}},
            "link": {"state": "ONLINE"},
        } for zone in range(1, 11)}})

    def close(self):
        pass


class SimulatedMillDevice:
    def __init__(self):
        self.fail = False
        self.commands = []

    def get_status(self):
        if self.fail:
            raise MillError("simulated Mill outage")
        return {"name": "Mill", "status": "ok", "operation_key": ""}

    def get_control_status(self):
        return {"status": "ok", "operation_mode": "OFF", "switched_on": False}

    def _set_operation_mode(self, enabled):
        self.commands.append(enabled)


class MultifloorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "simulation.sqlite3"
        migrate_database(self.path, backup=False)
        self.engine = create_engine("sqlite:///" + str(self.path))
        self.addCleanup(self.engine.dispose)
        data = json.loads((ROOT / "config" / "house.example.json").read_text(encoding="utf-8"))
        for index, mapping in enumerate(data["tado_rooms"]):
            mapping.update(home_id=1, zone_id=index + 1)
        self.house = HouseConfig.model_validate(data)
        reconcile_house(self.engine, self.house)
        self.world = MockWorld(self.house)
        self.ids = self.world.ids
        self.gate = CommandGate(self.engine, test_mode=True, automatic_control_enabled=False)
        self.session = SimulatedTadoSession()
        store = Mock()
        store.load.return_value = "test-refresh"
        self.tado = TadoService(self.house, store, session=self.session, command_gate=self.gate)
        self.addCleanup(self.tado.close)
        self.mill_device = SimulatedMillDevice()
        self.mill = MillHeatingSource(
            self.house, self.mill_device, str(self.house.heat_paths[2].generator_id),
            next(str(d.id) for d in self.house.devices if d.adapter == "mill"),
            MillPolicy(10, 20, 100, 5), self.gate)
        self.service = RoomThermalService(self.engine, now=lambda: START)
        with self.engine.begin() as conn:
            conn.execute(update(models.room_settings).values(min_c=18, max_c=23, target_c=20))

    def sources(self):
        radiator, kmp, heater = [str(path.generator_id) for path in self.house.heat_paths]
        room_ids = [str(room.id) for room in self.house.rooms]
        return [
            Source(radiator, "radiator", {room: 4 for room in room_ids[:-1]}, 40),
            Source(kmp, "pellet", {self.ids["Living room"]: 4}, 4, 1, False, 2),
            Source(heater, "electric", {self.ids["Storage"]: 0.8}, 0.8,
                   initial_on=False, initial_elapsed_steps=2),
        ]

    def readings(self):
        sensors_by_room = {str(d.room_id): str(d.id) for d in self.house.devices
                           if d.kind in ("thermostat", "sensor", "actuator_sensor")}
        return {room: {"value": value, "observed_at": START, "quality": "good",
                       "classification": "measured", "source_device_id": sensors_by_room[room],
                       "uncertainty_c": 0} for room, value in self.world.temperature.items()}

    def forecasts(self, outdoor=0):
        return [Forecast(START + timedelta(minutes=15 * i),
                         self.world.sources["tibber"].prices[i], outdoor, 3)
                for i in range(144)]

    def test_graph_heat_transfer_and_independent_floor_with_independent_plant(self):
        params, assigned, _ = self.service._configuration()
        heater = str(self.house.heat_paths[1].generator_id)
        living = self.ids["Living room"]
        for outdoor in (-10, 5):
            with self.subTest(outdoor=outdoor):
                heated = self.world.integrate(params, {living: 4000}, outdoor, 3600)
                baseline = self.world.integrate(params, {}, outdoor, 3600)
                self.assertGreater(heated[living], baseline[living])
                for name in ("Kitchen", "Nursery", "Bathroom"):
                    room = self.ids[name]
                    self.assertGreater(heated[room], baseline[room] + 0.005)
                    demand = lambda temps: room_heat_demand_w(
                        temps[room], 21, outdoor, params[room]["capacity_j_per_k"],
                        params[room]["ua_w_per_k"], params[room]["conductances_w_per_k"],
                        temps, 3600)
                    self.assertLess(demand(heated), demand(baseline))
                for name in ("Office", "Hall", "Toilet", "Wardrobe", "Bedroom", "Washroom", "Storage"):
                    self.assertAlmostEqual(heated[self.ids[name]], baseline[self.ids[name]], places=8)
                    self.assertNotIn(heater, assigned[self.ids[name]])

    def test_prediction_against_independent_plant_all_rooms(self):
        params, assigned, _ = self.service._configuration()
        readings = self.readings()
        temps = {room: {"value": value["value"], "quality": "good",
                        "source_device_id": value["source_device_id"]}
                 for room, value in readings.items()}
        heat = {room: {source: {"watts": 300 if room == self.ids["Storage"] else 500,
                                "classification": "measured"} for source in sources}
                for room, sources in assigned.items()}
        delivered = {room: sum(item["watts"] for item in inputs.values())
                     for room, inputs in heat.items()}
        data = self.service.save_snapshot(START, temps, -5, heat)
        prediction = self.service.predict(START, data)
        for horizon in (900, 1800, 3600, 7200, 10800, 21600):
            actual = self.world.integrate(params, delivered, -5, horizon)
            for room in actual:
                self.assertEqual(prediction[room]["classification"], "predicted")
                self.assertAlmostEqual(prediction[room]["temperatures_c"][horizon], actual[room], delta=0.03)

    def test_independent_tado_and_mill_are_simulated_only(self):
        for index, mapping in enumerate(self.house.tado_rooms):
            room_id = str(mapping.room_id)
            status = self.tado.get_room_status(room_id)
            self.assertAlmostEqual(status["actual_temperature_c"], 18 + (index + 1) / 10)
            self.assertAlmostEqual(status["requested_temperature_c"], 20 + (index + 1) / 10)
            self.assertIsNone(status["radiator_heating_activity"])
            self.assertEqual(self.tado.set_room_temperature(room_id, 20 + index / 10).outcome, "simulated")
        self.assertEqual(self.mill.room_id, self.ids["Storage"])
        self.assertIsNone(self.mill.get_status()["actual_heat_output_kw"])
        self.assertEqual(self.mill.set_power(True).outcome, "simulated")
        self.assertNotIn("PUT", [method for method, _ in self.session.calls])
        self.assertEqual(self.mill_device.commands, [])
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(models.command_log)).all()), 11)

    def test_missing_sensor_and_provider_failure_fail_closed(self):
        readings = self.readings()
        del readings[self.ids["Nursery"]]
        with self.assertRaises(PlanningUnavailable):
            rooms_from_database(self.engine, self.forecasts(), readings, self.sources(), timezone_name="UTC")
        temps = {room: {"value": value["value"], "quality": "good",
                        "source_device_id": value["source_device_id"]}
                 for room, value in readings.items()}
        _, assigned, _ = self.service._configuration()
        heat = {room: {source: {"watts": 0, "classification": "measured"}
                       for source in sources} for room, sources in assigned.items()}
        snapshot = self.service.save_snapshot(START, temps, 0, heat)
        result = self.service.predict(START, snapshot)
        self.assertEqual(result[self.ids["Nursery"]]["classification"], "unknown")
        self.assertEqual(result[self.ids["Living room"]]["classification"], "unknown")
        self.session.fail = True
        with self.assertRaises(TadoError):
            self.tado.get_room_status(str(self.house.tado_rooms[0].room_id))
        self.mill_device.fail = True
        with self.assertRaises(MillError):
            self.mill.get_status()
        self.assertEqual(self.mill_device.commands, [])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.command_log)).all(), [])

    def test_mock_provider_collection_preserves_legacy_observations(self):
        database = ThermalDatabase(self.path)
        self.addCleanup(database.engine.dispose)
        providers = self.world.sources
        with patch.object(collection.config, "TIBBER_API_KEY", "test"), \
                patch.object(collection.weather, "get_forecast", providers["weather"].get_forecast), \
                patch.object(collection.tibber, "get_spot_price", providers["tibber"].get_spot_price), \
                patch.object(collection.sensibo, "status", providers["sensibo"].status), \
                patch.object(collection.kmp, "get_status", providers["kmp"].get_status):
            collection.collect(database, lambda outdoor: 4 if outdoor < 0 else 5)
        reading = database.history()[0]
        self.assertEqual(reading["indoor_c"], 19.5)
        self.assertEqual(reading["outdoor_c"], -5)
        self.assertEqual(reading["price_sek_kwh"], 0.8)
        self.assertEqual(reading["heatpump_capacity_kw"], 4)
        self.assertIsNone(reading["pelletstove_capacity_kw"])
        for provider, method in (("weather", "get_forecast"), ("tibber", "get_spot_price"),
                                 ("sensibo", "status"), ("kmp", "get_status")):
            getattr(providers[provider], method).assert_called_once_with()

    def test_adapter_reconnect_preserves_safe_mode_and_independent_monitoring(self):
        room_id = str(self.house.tado_rooms[0].room_id)
        self.session.fail = True
        self.mill_device.fail = True
        with self.assertRaises(TadoError):
            self.tado.get_room_status(room_id)
        with self.assertRaises(MillError):
            self.mill.get_status()
        database = ThermalDatabase(self.path)
        self.addCleanup(database.engine.dispose)
        providers = self.world.sources
        with patch.object(collection.config, "TIBBER_API_KEY", "test"), \
                patch.object(collection.weather, "get_forecast", providers["weather"].get_forecast), \
                patch.object(collection.tibber, "get_spot_price", providers["tibber"].get_spot_price), \
                patch.object(collection.sensibo, "status", providers["sensibo"].status), \
                patch.object(collection.kmp, "get_status", providers["kmp"].get_status):
            collection.collect(database, lambda outdoor: 4)
        self.assertEqual(database.history()[0]["indoor_c"], 19.5)
        self.session.fail = False
        self.mill_device.fail = False
        self.assertIsNotNone(self.tado.get_room_temperature(room_id))
        recovered = self.mill.get_status()
        self.assertTrue(recovered["connected"])
        self.assertTrue(recovered["safety_latched"])
        self.assertIsNone(recovered["actual_heat_output_kw"])
        self.assertEqual(self.mill_device.commands, [])
        self.assertFalse(any(method == "PUT" for method, _ in self.session.calls))

    def test_override_applies_only_to_selected_room(self):
        room = self.ids["Kitchen"]
        with self.engine.begin() as conn:
            conn.execute(insert(models.room_overrides).values(
                room_id=room, target_c=22, starts_at=START + timedelta(minutes=30),
                expires_at=START + timedelta(hours=1), source="simulation"))
        resolved = rooms_from_database(self.engine, self.forecasts(), self.readings(),
                                       self.sources(), timezone_name="UTC")
        by_room = {item.id: item for item in resolved}
        self.assertEqual(by_room[room].targets_c[:5], [20, 20, 22, 22, 20])
        self.assertEqual(by_room[self.ids["Office"]].targets_c[:5], [20] * 5)

    def test_simulation_never_dispatches_devices(self):
        room = Room("storage", 20, 2e6, 50, {}, 18, 23, [20] * 144, 0)
        source = Source("electric", "electric", {"storage": 1}, 1,
                        initial_on=True, initial_elapsed_steps=1, min_on_steps=144)
        plan = optimize_rooms([room], [source], self.forecasts(), solver_seconds=15)
        self.assertEqual(plan["mode"], "simulation")
        self.assertEqual(len(plan["intervals"]), 144)
        self.assertEqual(self.mill_device.commands, [])
        self.assertEqual(self.session.calls, [])

    def test_kmp_and_radiators_coordinate_across_neighbors(self):
        names = ["Living room", "Kitchen", "Nursery", "Bathroom", "Office"]
        living = self.ids[names[0]]
        rooms = []
        for name in names:
            room_id = self.ids[name]
            links = ({self.ids[neighbor]: 80 for neighbor in names[1:4]}
                     if name == "Living room" else
                     {living: 80} if name in names[1:4] else {})
            rooms.append(Room(room_id, 20, 2e6, 100, links, 18, 23, [20] * 144, 0))
        radiator = Source("radiator", "radiator", {room.id: 4 for room in rooms}, 20)
        pellet = Source("kmp", "pellet", {living: 2.5}, 2.5, 0.01,
                        initial_on=True, initial_elapsed_steps=0, min_on_steps=144)
        baseline = optimize_rooms(rooms, [radiator], self.forecasts(), solver_seconds=15)
        coupled = optimize_rooms(rooms, [radiator, pellet], self.forecasts(), solver_seconds=15)
        def total(plan, room_id):
            return sum(next(r for r in interval["rooms"] if r["room_id"] == room_id)
                       ["planned_heat_kw"]["radiator"] for interval in plan["intervals"])
        for name in names[1:4]:
            self.assertLess(total(coupled, self.ids[name]), total(baseline, self.ids[name]) - 1)
        self.assertAlmostEqual(total(coupled, self.ids["Office"]),
                               total(baseline, self.ids["Office"]), places=4)
        office_rows = [next(r for r in interval["rooms"] if r["room_id"] == self.ids["Office"])
                       for interval in coupled["intervals"]]
        self.assertTrue(all(row["predicted_temperature_c"] >= row["target_c"] - 1e-5
                            and "kmp" not in row["selected_sources"] for row in office_rows))
        living_rows = [next(r for r in interval["rooms"] if r["room_id"] == living)
                       for interval in coupled["intervals"]]
        self.assertLess(sum(row["planned_heat_kw"]["radiator"] for row in living_rows),
                        total(baseline, living) - 1)
        self.assertTrue(all("kmp" in row["selected_sources"] for row in living_rows))

    def test_mill_plan_prefers_cheaper_electricity(self):
        room = Room("storage", 20, 1e6, 20, {}, 18, 23, [20] * 144, 0)
        source = Source("electric", "electric", {"storage": 0.8}, 0.8,
                        initial_on=False, initial_elapsed_steps=10)
        cheap_first = self.forecasts()
        flat = [Forecast(step.start, 1.0, step.outdoor_c, step.radiator_cop) for step in cheap_first]
        flexible = optimize_rooms([room], [source], cheap_first, solver_seconds=15)
        baseline = optimize_rooms([room], [source], flat, solver_seconds=15)
        def early(plan):
            return sum(step["rooms"][0]["planned_heat_kw"]["electric"]
                       for step in plan["intervals"][:8])
        self.assertGreater(early(flexible), early(baseline) + 1)
        self.assertTrue(all(step["rooms"][0]["selected_sources"] in ([], ["electric"])
                            for step in flexible["intervals"]))

    def test_entire_house_plans_separate_floor_zero_sources(self):
        readings = {room: dict(value, value=21) for room, value in self.readings().items()}
        radiator, pellet, heater = self.sources()
        pellet = Source(pellet.id, "pellet", pellet.room_capacity_kw, 4, 1, False, 0,
                        min_off_steps=144)
        heater = Source(heater.id, "electric", heater.room_capacity_kw, 0.8,
                        initial_on=True, initial_elapsed_steps=0, min_on_steps=144)
        sources = [radiator, pellet, heater]
        rooms = rooms_from_database(self.engine, self.forecasts(), readings, sources, timezone_name="UTC")
        plan = optimize_rooms(rooms, sources, self.forecasts(), solver_seconds=15)
        floor_zero = {str(room.id) for room in self.house.rooms if room.floor_id == self.house.floors[1].id}
        for interval in plan["intervals"]:
            for row in interval["rooms"]:
                if row["room_id"] in floor_zero:
                    self.assertGreaterEqual(row["predicted_temperature_c"], row["target_c"] - 1e-5)
                    self.assertNotIn(pellet.id, row["selected_sources"])
                    if row["room_id"] == self.ids["Storage"]:
                        self.assertEqual(row["selected_sources"], [heater.id])
                    else:
                        self.assertNotIn(heater.id, row["selected_sources"])
        params, _, _ = self.service._configuration()
        self.world.temperature = {room: 21.0 for room in self.world.temperature}
        for interval in plan["intervals"][:8]:
            delivered = {row["room_id"]: 1000 * sum(row["planned_heat_kw"].values())
                         for row in interval["rooms"]}
            actual = self.world.integrate(params, delivered, 0, 900)
            for row in interval["rooms"]:
                self.assertAlmostEqual(row["predicted_temperature_c"],
                                       actual[row["room_id"]], delta=0.001)
            self.world.temperature = actual


class SystemBoundaryTests(unittest.TestCase):
    def test_web_restart_does_not_stop_controller(self):
        stop = threading.Event()
        ticks = []
        def controller_loop():
            while not stop.wait(0.01):
                ticks.append(time.monotonic())
        thread = threading.Thread(target=controller_loop)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        def await_ready(server):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1)
                except HTTPError as error:
                    self.assertEqual(error.code, 401)
                    return
                except URLError:
                    if server[0].poll() is not None:
                        self.fail("Web server exited during startup")
                    time.sleep(0.05)
            self.fail("Web server failed to become ready")
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": "x" * 40,
                                     "HEATAUTOMATION_WEB_AUTOSTART": "true"}):
            first = startup.start_web_server(port=port)
            self.assertIsNotNone(first)
            try:
                await_ready(first)
                count = len(ticks)
            finally:
                startup.stop_web_server(first)
            second = startup.start_web_server(port=port)
            self.assertIsNotNone(second)
            try:
                await_ready(second)
                self.assertGreater(len(ticks), count)
                self.assertTrue(thread.is_alive())
            finally:
                startup.stop_web_server(second)

    def test_legacy_migration_and_backup_preserve_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            engine = create_engine("sqlite:///" + str(path))
            metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(insert(observations).values(id=1, timestamp=START.isoformat()))
                conn.execute(insert(sensors).values(observation_id=1, name="indoor_c", value=19.5))
            engine.dispose()
            backup = migrate_database(path)
            for candidate in (path, backup):
                engine = create_engine("sqlite:///" + str(candidate))
                try:
                    with engine.connect() as conn:
                        self.assertEqual(conn.execute(select(sensors.c.value)).scalar_one(), 19.5)
                finally:
                    engine.dispose()


if __name__ == "__main__":
    unittest.main()
