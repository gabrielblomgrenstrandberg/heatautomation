import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, insert, select, update

from heatautomation.control.mpc import Interval, PlanningUnavailable, _Problem
from heatautomation.control.room_mpc import (
    Forecast, Room, Source, forecasts_from_legacy, optimize_rooms, rooms_from_database,
    save_simulation,
)
from heatautomation.house import models
from heatautomation.house.config import load_house_config
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def forecasts(prices=None):
    return [Forecast(START + timedelta(minutes=15 * i),
                     prices[i] if prices else 1.0, 0, 3) for i in range(144)]


def room(name, initial=20, target=20, links=None, capacity=2e6, ua=100):
    return Room(name, initial, capacity, ua, links or {}, 18, 23, [target] * 144, 0)


def circuit(rooms, capacity=10):
    return Source("radiator", "radiator", {r: 4 for r in rooms}, capacity)


class RoomMpcTests(unittest.TestCase):
    def test_exact_36_hours_and_preheat_reduces_cost(self):
        steps = forecasts([0.05] * 8 + [3.0] * 136)
        heater = circuit(["room"])
        preheat = optimize_rooms([room("room")], [heater], steps)
        constrained = Room("room", 20, 2e6, 100, {}, 18, 20, [20] * 144, 0)
        baseline = optimize_rooms([constrained], [heater], steps)
        self.assertEqual(preheat["mode"], "simulation")
        self.assertEqual(len(preheat["intervals"]), 144)
        self.assertGreater(max(i["rooms"][0]["predicted_temperature_c"]
                               for i in preheat["intervals"][:8]), 20.1)
        self.assertLess(preheat["expected_total_cost"], baseline["expected_total_cost"])
        for item in preheat["intervals"]:
            self.assertLessEqual(item["rooms"][0]["predicted_temperature_c"], 22 + 1e-5)
            self.assertGreaterEqual(item["rooms"][0]["predicted_temperature_c"], 20 - 1e-5)

    def test_room_specific_preheat_limit(self):
        steps = forecasts([0.05] * 8 + [3.0] * 136)
        limited = Room("room", 20, 2e6, 100, {}, 18, 23, [20] * 144, 0,
                       max_preheat_c=0.25)
        plan = optimize_rooms([limited], [circuit(["room"])], steps)
        self.assertTrue(all(step["rooms"][0]["predicted_temperature_c"] <= 20.25 + 1e-5
                            for step in plan["intervals"]))
        self.assertGreater(max(step["rooms"][0]["predicted_temperature_c"]
                               for step in plan["intervals"][:8]), 20.1)

    def test_kmp_transfer_reduces_kitchen_radiator_and_independent_office(self):
        rooms = [room("living", links={"kitchen": 200}),
                 room("kitchen", links={"living": 200}), room("office")]
        radiator = circuit(["living", "kitchen", "office"], 12)
        with_stove = Source("kmp", "pellet", {"living": 2.5}, 2.5, 0.01,
                            initial_on=True, initial_elapsed_steps=0, min_on_steps=144)
        plan = optimize_rooms(rooms, [radiator, with_stove], forecasts())
        baseline = optimize_rooms(rooms, [radiator], forecasts())
        first = {r["room_id"]: r for r in plan["intervals"][0]["rooms"]}
        self.assertGreater(first["kitchen"]["inter_room_transfer_kw"]["living"], -0.01)
        self.assertTrue(any(r["room_id"] == "living" and "kmp" in r["selected_sources"]
                            for interval in plan["intervals"] for r in interval["rooms"]))
        self.assertTrue(all("kmp" not in r["planned_heat_kw"] for interval in plan["intervals"]
                            for r in interval["rooms"] if r["room_id"] != "living"))
        self.assertGreater(first["office"]["planned_heat_kw"]["radiator"], 0)
        self.assertTrue(all(not r["inter_room_transfer_kw"] for interval in plan["intervals"]
                            for r in interval["rooms"] if r["room_id"] == "office"))
        def total(plan, name):
            return sum(r["planned_heat_kw"]["radiator"] for interval in plan["intervals"]
                       for r in interval["rooms"] if r["room_id"] == name)
        self.assertLess(total(plan, "kitchen"), total(baseline, "kitchen") - 10)
        self.assertAlmostEqual(total(plan, "office"), total(baseline, "office"), places=4)

    def test_mill_dwell_maximum_and_cost_accounting(self):
        storage = room("storage", ua=20, capacity=1e6)
        mill = Source("mill", "electric", {"storage": 0.8}, 0.8,
                      initial_on=False, initial_elapsed_steps=10,
                      min_on_steps=2, min_off_steps=2, max_on_steps=4)
        plan = optimize_rooms([storage], [mill], forecasts(), solver_seconds=30)
        states = [s["rooms"][0]["planned_activity"]["mill"] for s in plan["intervals"]]
        self.assertTrue(any(states))
        for start in range(140):
            self.assertLessEqual(sum(states[start:start + 5]), 4)
        for i in range(1, len(states) - 1):
            if states[i] != states[i - 1]:
                self.assertEqual(states[i], states[i + 1])
        self.assertAlmostEqual(plan["expected_total_cost"], sum(
            step["rooms"][0]["planned_heat_kw"]["mill"] * 0.25
            for step in plan["intervals"]), places=5)

    def test_bad_inputs_infeasibility_and_solver_result_rejected(self):
        with self.assertRaises(ValueError):
            optimize_rooms([room("r")], [circuit(["r"])], forecasts()[:143])
        with self.assertRaises(ValueError):
            optimize_rooms([room("a", links={"b": 10}), room("b")],
                           [circuit(["a", "b"])], forecasts())
        with self.assertRaises(PlanningUnavailable):
            optimize_rooms([room("r", initial=0)], [circuit(["r"])], forecasts())
        with patch.object(_Problem, "solve") as solve:
            solve.return_value.success = False
            solve.return_value.message = "time limit"
            with self.assertRaises(PlanningUnavailable):
                optimize_rooms([room("r")], [circuit(["r"])], forecasts())

    def test_existing_forecast_adapter_requires_full_coverage(self):
        steps = [Interval(START + timedelta(minutes=15 * i), 1, 0, 5, 3, 4)
                 for i in range(144)]
        converted, capacity = forecasts_from_legacy(steps)
        self.assertEqual(len(converted), 144)
        self.assertEqual(capacity, [5] * 144)
        with self.assertRaises(PlanningUnavailable):
            forecasts_from_legacy(steps[:-1])

    def test_time_varying_circuit_capacity_and_uncertainty(self):
        heater = Source("radiator", "radiator", {"r": 4}, 4,
                        capacity_forecast_kw=[2.5] * 144)
        cautious = Room("r", 20.5, 2e6, 100, {}, 18, 23, [20] * 144, 0.2)
        plan = optimize_rooms([cautious], [heater], forecasts())
        for step in plan["intervals"]:
            row = step["rooms"][0]
            self.assertLessEqual(row["planned_heat_kw"]["radiator"], 2.5 + 1e-5)
            self.assertGreaterEqual(row["predicted_temperature_c"], 20.2 - 1e-5)
            self.assertLessEqual(row["predicted_temperature_c"], 21.8 + 1e-5)
        insufficient = Source("radiator", "radiator", {"r": 4}, 4,
                              capacity_forecast_kw=[0] * 144)
        with self.assertRaises(PlanningUnavailable):
            optimize_rooms([cautious], [insufficient], forecasts())

    def test_database_targets_paths_and_plan_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "room.sqlite3"
            migrate_database(path, backup=False)
            engine = create_engine("sqlite:///" + str(path))
            try:
                house = load_house_config(Path(__file__).resolve().parents[2] / "config" / "house.example.json")
                reconcile_house(engine, house)
                with engine.begin() as conn:
                    conn.execute(update(models.room_settings).values(
                        min_c=18, max_c=23, target_c=20))
                    rid = str(house.rooms[0].id)
                    conn.execute(update(models.room_settings).where(
                        models.room_settings.c.room_id == rid).values(max_preheat_c=0.5))
                    conn.execute(insert(models.room_schedules).values(
                        room_id=rid, weekday=3, start_min=60, end_min=120, target_c=21))
                    conn.execute(insert(models.room_overrides).values(
                        room_id=rid, target_c=22,
                        starts_at=START + timedelta(hours=2),
                        expires_at=START + timedelta(hours=3), source="manual"))
                hp = str(house.heat_paths[0].generator_id)
                kmp = str(house.heat_paths[1].generator_id)
                mill = str(house.heat_paths[2].generator_id)
                radiator_rooms = {str(r.id): 4 for r in house.rooms[:-1]}
                sources = [Source(hp, "radiator", radiator_rooms, 40),
                           Source(kmp, "pellet", {rid: 4}, 4, 1, False, 2),
                           Source(mill, "electric", {str(house.rooms[-1].id): 1},
                                  1, initial_on=False, initial_elapsed_steps=2)]
                sensor_ids = {str(d.room_id): str(d.id) for d in house.devices
                              if d.kind in ("thermostat", "sensor", "actuator_sensor")}
                readings = {str(r.id): {"value": 20, "observed_at": START,
                                       "quality": "good", "classification": "measured",
                                       "source_device_id": sensor_ids[str(r.id)],
                                       "uncertainty_c": 0.1} for r in house.rooms}
                resolved = rooms_from_database(engine, forecasts(), readings, sources,
                                               timezone_name="UTC")
                first = next(r for r in resolved if r.id == rid)
                self.assertEqual(first.max_preheat_c, 0.5)
                self.assertEqual(first.targets_c[:13],
                                 [20] * 4 + [21] * 4 + [22] * 4 + [20])
                with engine.begin() as conn:
                    conn.execute(update(models.room_settings).where(
                        models.room_settings.c.room_id == rid).values(max_preheat_c=2))
                with self.assertRaises(PlanningUnavailable):
                    rooms_from_database(engine, forecasts(), {}, sources, timezone_name="UTC")
                wrong = dict(readings)
                wrong[rid] = dict(readings[rid], source_device_id=mill)
                with self.assertRaises(PlanningUnavailable):
                    rooms_from_database(engine, forecasts(), wrong, sources, timezone_name="UTC")
                wrong[rid] = dict(readings[rid], observed_at=START - timedelta(minutes=16))
                with self.assertRaises(PlanningUnavailable):
                    rooms_from_database(engine, forecasts(), wrong, sources, timezone_name="UTC")
                wrong_sources = [sources[0], Source(
                    kmp, "pellet", {str(house.rooms[4].id): 4}, 4, 1, False, 2), sources[2]]
                with self.assertRaises(PlanningUnavailable):
                    rooms_from_database(engine, forecasts(), readings, wrong_sources, timezone_name="UTC")
                readings = {room_id: dict(value, value=21, uncertainty_c=0)
                            for room_id, value in readings.items()}
                sources = [
                    sources[0],
                    Source(kmp, "pellet", {rid: 4}, 4, 1, False, 0,
                           min_off_steps=144),
                    Source(mill, "electric", {str(house.rooms[-1].id): 0.8}, 0.8,
                           initial_on=True, initial_elapsed_steps=0, min_on_steps=144),
                ]
                resolved = rooms_from_database(engine, forecasts(), readings, sources,
                                               timezone_name="UTC")
                plan = optimize_rooms(resolved, sources, forecasts(), solver_seconds=15)
                self.assertEqual(len(plan["intervals"][0]["rooms"]), 11)
                self.assertTrue(all(step["rooms"][-1]["selected_sources"] == [mill]
                                    for step in plan["intervals"]))
                plan_id = save_simulation(engine, plan)
                with engine.connect() as conn:
                    self.assertEqual(len(conn.execute(select(models.optimizer_plan_intervals).where(
                        models.optimizer_plan_intervals.c.plan_id == plan_id)).all()), 144 * 11)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
