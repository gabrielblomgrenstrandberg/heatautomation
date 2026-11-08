import unittest
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from sqlalchemy import select

from heatautomation.control.mpc import Interval, PlanningUnavailable, Settings, _Problem, optimize
from heatautomation.control import mpc_monitor, mpc_simulation
from heatautomation.learning.database import ThermalDatabase, plans, plan_intervals, plan_evaluations
from heatautomation.integrations import kmp, sensibo, tibber


def predictor():
    cooling = SimpleNamespace(intercept_=0.0, coef_=np.array([-0.10, 0.10, 0, 0, 0]))
    heating = SimpleNamespace(intercept_=0.0, coef_=np.array([-0.10, 0.10, 0, 0.24, 0.20]))
    metric = {"validation_samples": 30, "rmse_c_per_hour": 0.1}
    return SimpleNamespace(artifact={
        "models": {"heating": heating, "cooling": cooling},
        "metrics": {"heating": metric, "cooling": metric},
        "support": {"cooling": {0: 100}, "heating": {1: 100, 2: 100, 3: 100}},
    })


def intervals(count=16):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Interval(start + timedelta(minutes=15 * i), 0.3 if i < 8 else 2.0,
                     0, 6, 3, 5) for i in range(count)]


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(18, 20, 22, 1.0, uncertainty_margin_c=0)

    def test_comfort_cost_and_conservative_initial_dwell(self):
        plan = optimize(intervals(), [20] * 7, {"heatpump": True, "pelletstove": False},
                        predictor(), self.settings)
        self.assertEqual(len(plan), 16)
        self.assertEqual(plan[0]["mode"], 1)
        self.assertEqual(plan[1]["mode"], 1)
        for step in plan:
            self.assertGreaterEqual(step["predicted_indoor_c"], 18 - 1e-6)
            self.assertLessEqual(step["predicted_indoor_c"], 22 + 1e-6)
            self.assertAlmostEqual(step["estimated_cost"],
                                   step["estimated_electricity_kwh"] * step["price"]
                                   + step["estimated_pellet_heat_kwh"])

    def test_refuses_unvalidated_model(self):
        model = predictor()
        model.artifact["metrics"]["cooling"]["rmse_c_per_hour"] = 2
        with self.assertRaises(PlanningUnavailable):
            optimize(intervals(), [20] * 7, {"heatpump": False, "pelletstove": False},
                     model, self.settings)

    def test_dwell_constraints_apply_across_the_horizon(self):
        model = predictor()
        model.artifact["models"]["heating"].coef_[3] = 0.5
        model.artifact["models"]["heating"].coef_[4] = 0.4
        settings = Settings(18, 20, 22, 10, deviation_penalty=0, switch_penalty=0,
                            min_runtime_steps=2, min_off_steps=2, uncertainty_margin_c=0)
        plan = optimize(intervals(20), [20] * 7,
                        {"heatpump": False, "pelletstove": False}, model, settings)
        for bit in (1, 2):
            states = [bool(step["mode"] & bit) for step in plan]
            for i in range(1, len(states)):
                if states[i] != states[i - 1]:
                    self.assertLess(i + 1, len(states))
                    self.assertEqual(states[i], states[i + 1])

    def test_solver_gap_and_candidate_are_checked(self):
        original = _Problem.solve

        def timed_out(problem, seconds):
            result = original(problem, seconds)
            result.success = False
            result.status = 1
            result.mip_gap = 0.05
            return result

        with patch.object(_Problem, "solve", timed_out):
            plan = optimize(intervals(), [20] * 7,
                            {"heatpump": True, "pelletstove": False},
                            predictor(), self.settings)
        self.assertFalse(plan[0]["solver_optimal"])
        self.assertEqual(plan[0]["solver_gap"], 0.05)

        def bad_candidate(problem, seconds):
            result = timed_out(problem, seconds)
            result.x[0] += 1
            return result

        with patch.object(_Problem, "solve", bad_candidate):
            with self.assertRaises(PlanningUnavailable):
                optimize(intervals(), [20] * 7,
                         {"heatpump": True, "pelletstove": False},
                         predictor(), self.settings)

    def test_refuses_gapped_forecast(self):
        steps = intervals()
        steps[1] = Interval(steps[1].start + timedelta(minutes=15), 1, 0, 6, 3, 5)
        with self.assertRaises(ValueError):
            optimize(steps, [20] * 7, {"heatpump": False, "pelletstove": False},
                     predictor(), self.settings)

    def test_preheat_lowers_modeled_cost_compared_with_capped_baseline(self):
        model = predictor()
        model.artifact["models"]["heating"].coef_[3] = 0.5
        model.artifact["models"]["heating"].coef_[4] = 0.4
        settings = Settings(19, 20, 22, 10, deviation_penalty=0, switch_penalty=0,
                            min_runtime_steps=1, min_off_steps=1, uncertainty_margin_c=0)
        initial = {"heatpump": False, "pelletstove": False}
        steps = intervals()
        plan = optimize(steps, [20] * 7, initial, model, settings)
        comparison = mpc_simulation.compare_no_preheat(
            steps, [20] * 7, initial, model, settings, plan)
        self.assertGreater(max(row["predicted_indoor_c"] for row in plan), 20.5)
        self.assertGreater(comparison["modeled_savings"], 1.0)

    def test_schedule_persistence_and_comparison(self):
        plan = optimize(intervals(), [20] * 7, {"heatpump": True, "pelletstove": False},
                        predictor(), self.settings)
        with tempfile.TemporaryDirectory() as directory:
            db = ThermalDatabase(Path(directory) / "history.sqlite3")
            try:
                now = datetime.now(timezone.utc)
                first, changes = db.save_plan(now, now.isoformat(), 20, {}, plan)
                self.assertEqual(changes, [])
                changed = [dict(step) for step in plan]
                changed[0]["mode"] = 2 if plan[0]["mode"] != 2 else 1
                second, changes = db.save_plan(now, now.isoformat(), 20, {}, changed)
                self.assertNotEqual(first, second)
                self.assertEqual(len(changes), 1)
                with db.engine.connect() as connection:
                    self.assertEqual(len(connection.execute(select(plans)).fetchall()), 2)
                    self.assertEqual(len(connection.execute(select(plan_intervals)).fetchall()), 32)
            finally:
                db.engine.dispose()

    def test_price_and_weather_alignment_and_missing_coverage(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        prices = [{"startsAt": (start + timedelta(minutes=15 * i)).isoformat(), "total": i}
                  for i in range(5)]
        weather = [{"time": "2026-01-01T01:00", "temperature_c": -2}]
        with patch.object(mpc_monitor.config, "WEATHER_TIMEZONE", "Europe/Stockholm"):
            result = mpc_monitor.build_intervals(prices, weather, start, lambda t: 4)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0].outdoor_c, -2)
        self.assertEqual(result[3].price, 3)

    def test_score_never_claims_actual_cost(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        plan = [{"start": start.isoformat(), "predicted_indoor_c": 20, "mode": 1}]
        observed = [{"timestamp": (start + timedelta(minutes=15)).isoformat(),
                     "indoor_c": 19.8, "heatpump_capacity_kw": 4,
                     "pelletstove_capacity_kw": 0}]
        scored = mpc_simulation.evaluate(plan, observed)
        self.assertAlmostEqual(scored[0]["temperature_error_c"], 0.2)
        self.assertTrue(scored[0]["source_match_at_end"])
        self.assertIsNone(scored[0]["actual_heating_cost"])
        early = [dict(observed[0], timestamp=(start + timedelta(minutes=13)).isoformat())]
        self.assertEqual(mpc_simulation.evaluate(plan, early), [])

    def test_simulation_rejects_future_trained_model(self):
        steps = intervals()
        model = predictor()
        model.artifact["created_at"] = (steps[0].start + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            mpc_simulation.simulate(steps, [20] * 7,
                                    {"heatpump": True, "pelletstove": False},
                                    model, self.settings, [])
        model.artifact["created_at"] = (steps[0].start - timedelta(hours=1)).isoformat()
        result = mpc_simulation.simulate(steps, [20] * 7,
                                         {"heatpump": True, "pelletstove": False},
                                         model, self.settings, [])
        self.assertEqual(len(result["plan"]), 16)

    def test_shadow_cycle_reports_discontinuous_observation_gap(self):
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        observation_times = [
            now - timedelta(minutes=30),
            now - timedelta(minutes=25),
            now - timedelta(minutes=20),
            now - timedelta(minutes=15),
            now - timedelta(minutes=10),
            now - timedelta(minutes=5),
            now - timedelta(minutes=4),
        ]
        history = [
            {"timestamp": timestamp.isoformat()}
            for timestamp in observation_times
        ]
        with self.assertRaisesRegex(
                PlanningUnavailable,
                r"gap 6/6 .* is 60\.0s \(allowed: 210-390s\)"):
            mpc_monitor.run_shadow_cycle(
                SimpleNamespace(history=lambda: history), lambda _: 6, now
            )

    def test_shadow_cycle_rejects_transitioning_stove_mode(self):
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        history = [
            {
                "timestamp": (now - timedelta(minutes=30 - i * 5)).isoformat(),
                "indoor_c": 20,
                "heatpump_is_on": False,
                "heatpump_mode": "heat",
                "pelletstove_is_on": True,
                "pelletstove_mode": "TÄNDNING",
            }
            for i in range(7)
        ]
        with self.assertRaisesRegex(
                PlanningUnavailable, "Device status is unknown or transitioning"):
            mpc_monitor.run_shadow_cycle(
                SimpleNamespace(history=lambda: history), lambda _: 6, now
            )

    def test_shadow_cycle_persists_and_evaluates_without_commands(self):
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        model = predictor()
        model.artifact["created_at"] = (now - timedelta(hours=1)).isoformat()
        start = now + timedelta(minutes=15)
        prices = [{"startsAt": (start + timedelta(minutes=i * 15)).isoformat(), "total": 1}
                  for i in range(16)]
        forecasts = [{"time": "2026-01-01T{:02d}:00".format(i), "temperature_c": 0}
                     for i in range(1, 7)]
        with tempfile.TemporaryDirectory() as directory:
            db = ThermalDatabase(Path(directory) / "history.sqlite3")
            try:
                for i in range(7):
                    db.save(now - timedelta(minutes=30 - i * 5), {"indoor_c": 20}, {
                        "heatpump": {"is_on": False, "mode": "heat", "capacity_kw": 0},
                        "pelletstove": {"is_on": True, "mode": "VILAR...", "capacity_kw": 0},
                    })
                with patch.object(mpc_monitor, "_settings", return_value=self.settings), \
                        patch.object(mpc_monitor, "ThermalPredictor", return_value=model), \
                        patch.object(mpc_monitor.config, "WEATHER_TIMEZONE", "Europe/Stockholm"), \
                        patch.object(mpc_monitor.tibber, "get_price_forecast", return_value=prices), \
                        patch.object(mpc_monitor.weather, "get_forecast", return_value=forecasts), \
                        patch.object(sensibo, "on") as pump_on, \
                        patch.object(sensibo, "off") as pump_off, \
                        patch.object(kmp, "on") as stove_on, \
                        patch.object(kmp, "off") as stove_off:
                    plan_id = mpc_monitor.run_shadow_cycle(db, lambda t: 6, now)
                    for command in (pump_on, pump_off, stove_on, stove_off):
                        command.assert_not_called()
                db.save(start + timedelta(minutes=15), {"indoor_c": 19.8}, {
                    "heatpump": {"is_on": True, "mode": "heat", "capacity_kw": 6},
                    "pelletstove": {"is_on": False, "mode": "AV", "capacity_kw": 0},
                })
                db.evaluate_plans(mpc_simulation.evaluate)
                with db.engine.connect() as connection:
                    rows = connection.execute(select(plan_evaluations).where(
                        plan_evaluations.c.plan_id == plan_id)).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                db.engine.dispose()

    def test_quarter_hour_tibber_forecast(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"viewer": {"homes": [
                {"currentSubscription": {"priceInfo": {
                    "today": [{"startsAt": "2026-01-01T00:00:00+00:00", "total": 1}],
                    "tomorrow": [],
                }}}
            ]}}}
        )
        with patch.object(tibber, "TIBBER_API_KEY", "test"), \
                patch.object(tibber.requests, "post", return_value=response) as post:
            result = tibber.get_price_forecast()
        self.assertEqual(result[0]["total"], 1)
        self.assertIn("QUARTER_HOURLY", post.call_args.kwargs["json"]["query"])


if __name__ == "__main__":
    unittest.main()
