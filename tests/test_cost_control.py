import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_heat_switch import main

model = main.heating_model
COP = ((10, 4), (0, 3), (-10, 2))
DEMAND = ((10, 1), (0, 4), (-10, 8))


class CostModelTests(unittest.TestCase):
    def select(self, temp, price, capacity=5, pellet_capacity=6, pellet_cost=1,
               current=None, hysteresis=0.1, spot_limit=0):
        return model.select(temp, price, capacity, pellet_capacity, pellet_cost,
                            COP, DEMAND, hysteresis, current, spot_limit)

    def test_interpolation_and_endpoint_clamping(self):
        self.assertEqual(model.interpolate(COP, 5), 3.5)
        self.assertEqual(model.interpolate(COP, 20), 4)
        self.assertEqual(model.interpolate(COP, -20), 2)

    def test_cost_per_delivered_kwh_and_negative_prices(self):
        self.assertEqual(self.select(0, 2), ("heatpump", 5))
        self.assertEqual(self.select(0, 4), ("pelletstove", 6))
        self.assertEqual(self.select(0, -1), ("heatpump", 5))

    def test_combined_when_capacity_or_cost_requires_it(self):
        self.assertEqual(self.select(-10, 2, capacity=5, pellet_capacity=4), ("combined", 9))
        self.assertEqual(self.select(-10, 5, capacity=5, pellet_capacity=4), ("combined", 9))
        self.assertEqual(self.select(0, 1, capacity=3, pellet_capacity=6), ("combined", 9))
        self.assertEqual(self.select(0, 5, capacity=3, pellet_capacity=6), ("pelletstove", 6))
        self.assertEqual(self.select(-10, 2, capacity=2, pellet_capacity=4), ("combined", 6))

    def test_zero_demand_and_override_capacity(self):
        self.assertEqual(model.select(0, 2, 5, 6, 1, COP, ((0, 0),), 0.1), ("none", 0))
        self.assertEqual(self.select(0, 2, capacity=3, spot_limit=3), ("combined", 9))
        self.assertEqual(self.select(0, 2, spot_limit=2), ("heatpump", 5))
        self.assertEqual(self.select(0, 3, spot_limit=2), ("pelletstove", 6))

    def test_hysteresis_both_directions_and_insufficient_capacity(self):
        self.assertEqual(self.select(0, 2.85, current="pelletstove")[0], "pelletstove")
        self.assertEqual(self.select(0, 2.6, current="pelletstove")[0], "heatpump")
        self.assertEqual(self.select(0, 3.15, current="heatpump")[0], "heatpump")
        self.assertEqual(self.select(0, 3.5, current="heatpump")[0], "pelletstove")
        self.assertEqual(self.select(-10, 3, capacity=3, pellet_capacity=6,
                                     current="pelletstove")[0], "combined")

    def test_return_from_combined_when_one_source_is_sufficient(self):
        self.assertEqual(self.select(0, 2, current="combined"), ("heatpump", 5))
        self.assertEqual(self.select(0, 4, current="combined"), ("pelletstove", 6))


class CostControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = main.CostController()
        self.settings = patch.multiple(
            main.config, MINIMUM_RUNTIME_SECONDS=120,
            MINIMUM_SWITCH_INTERVAL_SECONDS=60, COST_CONTROL_ENABLED=True,
            create=True,
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.mode = patch.object(main, "TEST_MODE", False)
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_minimum_intervals_and_start_before_stop(self):
        c = self.controller
        c.synchronize({"heatpump": True, "pelletstove": False}, 0)
        with patch.object(main.kmp, "on", return_value=True) as on, \
                patch.object(main.sensibo, "off", return_value={"ok": True}) as off:
            self.assertFalse(c.transition("pelletstove", 59))
            on.assert_not_called()
            self.assertFalse(c.transition("pelletstove", 60))
            on.assert_called_once()
            off.assert_not_called()
            self.assertFalse(c.transition("pelletstove", 119))
            off.assert_not_called()
            self.assertTrue(c.transition("pelletstove", 120))
            off.assert_called_once()

    def test_failed_or_protected_shutdown_does_not_change_state(self):
        c = self.controller
        c.synchronize({"heatpump": False, "pelletstove": True}, 0)
        with patch.object(main.kmp, "get_status", return_value={"mode": "VILAR..."}), \
                patch.object(main.kmp, "off", return_value=False), \
                patch.object(main.sensibo, "on", return_value={"ok": True}):
            self.assertFalse(c.transition("heatpump", 120))
            self.assertFalse(c.transition("heatpump", 240))
            self.assertEqual(c.current(), "combined")

    def test_running_stove_defers_heat_pump_only_transition_but_allows_combined(self):
        c = self.controller
        c.synchronize({"heatpump": False, "pelletstove": True}, 0)
        with patch.object(main.kmp, "get_status", return_value={"mode": "HÖGEFFEKT"}), \
                patch.object(main.sensibo, "on", return_value={"ok": True}) as on:
            self.assertFalse(c.transition("heatpump", 120))
            on.assert_not_called()
            self.assertTrue(c.transition("combined", 120))
            on.assert_called_once()

    def test_unknown_status_prevents_actions_and_resets_timers(self):
        c = self.controller
        c.synchronize({"heatpump": True, "pelletstove": False}, 0)
        with patch.object(main.kmp, "get_status", return_value=None), \
                patch.object(main.sensibo, "getSystemStatus", return_value={"is_on": True, "mode": "heat"}):
            self.assertIsNone(c.observe())
        c.synchronize({"heatpump": True, "pelletstove": False}, 500)
        with patch.object(main.kmp, "on") as on:
            self.assertFalse(c.transition("combined", 550))
            on.assert_not_called()

    def test_test_mode_never_commands_and_models_state(self):
        c = self.controller
        c.synchronize({"heatpump": False, "pelletstove": False}, 0)
        with patch.object(main, "TEST_MODE", True), \
                patch.object(main.kmp, "get_status", return_value={"mode": "AV"}), \
                patch.object(main.kmp, "on") as stove, \
                patch.object(main.sensibo, "on") as pump:
            self.assertTrue(c.transition("combined", 120))
            self.assertEqual(c.current(), "combined")
            stove.assert_not_called()
            pump.assert_not_called()

    def test_fan_does_not_replace_heat_or_unavailable_status(self):
        with patch.object(main.config, "COST_CONTROL_ENABLED", True), \
                patch.object(main, "advanced_heat_required", True), \
                patch.object(main.kmp, "get_status", return_value={"mode": "HÖGEFFEKT"}), \
                patch.object(main.fan_assist, "_start_or_maintain") as start:
            main.fan_assist.poll()
            start.assert_not_called()

    def test_complete_cycle_combined_safeguards_and_confirmed_replacement(self):
        c = self.controller
        current = {"stove": "AV", "pump": False}

        def stove_status():
            return {"mode": current["stove"]}

        def pump_status():
            return {"is_on": current["pump"], "mode": "heat"}

        def stove_on():
            current["stove"] = "HÖGEFFEKT"
            return True

        def pump_on():
            current["pump"] = True
            return {"ok": True}

        with patch.multiple(
                main.config, PELLET_COST_PER_KWH=1.0, COP_BY_TEMPERATURE=COP,
                DEMAND_BY_TEMPERATURE=DEMAND, COST_HYSTERESIS=0.1,
                PELLET_STOVE_CAPACITY=4.0, spotPriceLimit=0, create=True), \
                patch.object(main, "get_effective_heating_capacity", return_value=5), \
                patch.object(main.kmp, "get_status", side_effect=stove_status), \
                patch.object(main.sensibo, "getSystemStatus", side_effect=pump_status), \
                patch.object(main.kmp, "on", side_effect=stove_on) as stove, \
                patch.object(main.sensibo, "on", side_effect=pump_on) as pump, \
                patch.object(main.kmp, "off", return_value=False) as stove_off, \
                patch.object(main.time, "monotonic", side_effect=[0, 60, 180]):
            self.assertIsNone(main.cost_control_cycle(c, -10, 2))
            self.assertEqual(main.cost_control_cycle(c, -10, 2), ("combined", 9.0))
            stove.assert_called_once()
            pump.assert_called_once()
            self.assertIsNone(main.cost_control_cycle(c, 0, 2))
            stove_off.assert_called_once()
            self.assertEqual(c.current(), "combined")

    def test_replacement_is_not_stopped_if_new_source_is_not_observed(self):
        c = self.controller
        c.synchronize({"heatpump": True, "pelletstove": False}, 0)
        with patch.object(main.kmp, "on", return_value=True), \
                patch.object(main.sensibo, "off") as off:
            self.assertFalse(c.transition("pelletstove", 120))
            c.synchronize({"heatpump": True, "pelletstove": False}, 240)
            self.assertFalse(c.transition("pelletstove", 240))
            off.assert_not_called()


class CostConfigurationTests(unittest.TestCase):
    def test_requires_measured_inputs_and_accepts_valid_configuration(self):
        source = Path(__file__).resolve().parents[1] / "heatautomation"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copyfile(source / "config.py", target / "config.py")
            shutil.copyfile(source / "heating_model.py", target / "heating_model.py")
            contents = (source / "heatautomation.conf").read_text(encoding="utf-8")
            for name in ("pellet_energy_kwh_per_kg", "pellet_price_per_bag",
                         "cop_by_temperature", "demand_by_temperature"):
                contents = re.sub(
                    r"(?m)^" + name + r"[ \t]*=[^\n]*(?:\n[ \t]+[^\n]*)*",
                    name + " =", contents,
                )
            path = target / "heatautomation.conf"
            path.write_text(contents, encoding="utf-8")
            env = os.environ.copy()
            env.pop("SPOT_PRICE_LIMIT", None)
            result = subprocess.run(
                [sys.executable, "-c", "import config; assert not config.COST_CONTROL_ENABLED"],
                cwd=directory, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            contents = contents.replace("\nenabled = false\n", "\nenabled = true\n")
            path.write_text(contents, encoding="utf-8")
            command = [sys.executable, "-c", "import config; assert config.COST_CONTROL_ENABLED"]
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            contents = contents.replace(
                "pellet_energy_kwh_per_kg =", "pellet_energy_kwh_per_kg = 5"
            )
            contents = contents.replace(
                "pellet_price_per_bag =", "pellet_price_per_bag = 75.2"
            )
            contents = contents.replace("cop_by_temperature =", "cop_by_temperature =\n    10, 4\n    0, 3")
            contents = contents.replace("demand_by_temperature =", "demand_by_temperature =\n    10, 1\n    0, 4")
            path.write_text(contents, encoding="utf-8")
            command = [
                sys.executable, "-c",
                "import config; assert config.COST_CONTROL_ENABLED; "
                "assert abs(config.PELLET_COST_PER_KWH - 1.0) < 1e-12"
            ]
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for old, new in [
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = 0"),
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = nan"),
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = not-a-number"),
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = 1e-320"),
                ("pellet_energy_kwh_per_kg = 5", "pellet_energy_kwh_per_kg = -1"),
                ("pellet_energy_kwh_per_kg = 5", "pellet_energy_kwh_per_kg = inf"),
                ("pellet_price_per_bag = 75.2", "pellet_price_per_bag = 0"),
                ("pellet_price_per_bag = 75.2", "pellet_price_per_bag = 5e-324"),
                ("stove_efficiency_percent = 94", "stove_efficiency_percent = 0"),
                ("stove_efficiency_percent = 94", "stove_efficiency_percent = 101"),
                ("stove_efficiency_percent = 94", "stove_efficiency_percent = nan"),
                ("stove_efficiency_percent = 94", "stove_efficiency_percent = 1e-320"),
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = 1e308"),
                ("cost_hysteresis = 0.10", "cost_hysteresis = 1"),
                ("minimum_runtime_minutes = 30", "minimum_runtime_minutes = -1"),
                ("10, 4\n    0, 3", "0, 0\n    10, 3"),
                ("10, 1\n    0, 4", "10, -1\n    0, 4"),
            ]:
                with self.subTest(invalid=new):
                    path.write_text(contents.replace(old, new), encoding="utf-8")
                    result = subprocess.run(command, cwd=directory, env=env,
                                            capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ValueError", result.stderr)

            path.write_text(contents.replace("stove_efficiency_percent = 94",
                                             "stove_efficiency_percent = 100"), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", "import config; assert abs(config.PELLET_COST_PER_KWH - 0.94) < 1e-12"],
                cwd=directory, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            for old, new in [
                ("pellet_bag_size_kg = 16", "pellet_bag_size_kg = 20"),
                ("pellet_energy_kwh_per_kg = 5", "pellet_energy_kwh_per_kg = 6"),
                ("pellet_price_per_bag = 75.2", "pellet_price_per_bag = 50"),
                ("stove_efficiency_percent = 94", "stove_efficiency_percent = 100"),
            ]:
                with self.subTest(changed=new):
                    path.write_text(contents.replace(old, new), encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-c",
                         "import config, heating_model; "
                         "assert heating_model.select(0, 3, 5, 6, "
                         "config.PELLET_COST_PER_KWH, config.COP_BY_TEMPERATURE, "
                         "config.DEMAND_BY_TEMPERATURE, 0)[0] == 'pelletstove'"],
                        cwd=directory, env=env, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
