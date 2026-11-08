import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from heatautomation import config, main
from heatautomation.control.mpc import PlanningUnavailable
from heatautomation.control.mpc_monitor import run_shadow_cycle
from heatautomation.learning import collection


ROOT = Path(__file__).resolve().parents[2]


class IntegrationSelectionTests(unittest.TestCase):
    def test_tado_environment_override_and_invalid_values(self):
        env = os.environ.copy()
        env["HEATAUTOMATION_TADO_ENABLED"] = "false"
        result = subprocess.run(
            [sys.executable, "-c", "from heatautomation import config; print(config.TADO_ENABLED)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
        env["HEATAUTOMATION_TADO_ENABLED"] = "invalid"
        result = subprocess.run(
            [sys.executable, "-c", "from heatautomation import config"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be true or false", result.stderr)

    def test_environment_overrides_and_invalid_values(self):
        env = os.environ.copy()
        env["HEATAUTOMATION_SENSIBO_ENABLED"] = "false"
        result = subprocess.run(
            [sys.executable, "-c", "from heatautomation import config; print(config.SENSIBO_ENABLED)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
        env["HEATAUTOMATION_SENSIBO_ENABLED"] = "invalid"
        result = subprocess.run(
            [sys.executable, "-c", "from heatautomation import config"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be true or false", result.stderr)

    def test_no_sensibo_does_not_read_or_command_it(self):
        database = Mock()
        with patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "KMP_ENABLED", True), \
                patch.object(config, "TIBBER_ENABLED", False), \
                patch.object(config, "WEATHER_ENABLED", False), \
                patch.object(main.sensibo, "status") as status, \
                patch.object(main.sensibo, "getSystemStatus") as pump_status, \
                patch.object(main.sensibo, "on") as pump_on, \
                patch.object(main.sensibo, "off") as pump_off, \
                patch.object(main.kmp, "get_status", return_value={"mode": "AV"}) as stove, \
                patch.object(main.kmp, "on") as stove_on, \
                patch.object(main.tibber, "get_spot_price") as price, \
                patch.object(main.weather, "get_forecast") as forecast:
            collection.collect(database, lambda _: 4)
            self.assertEqual(database.save.call_args.args[2]["heatpump"],
                             {"is_on": None, "mode": None, "capacity_kw": None})
            self.assertEqual(main.check_systems(), (False, True))
            self.assertFalse(main.turn_on_heat_pump())
            self.assertFalse(main.turn_on_pellet_stove())
            self.assertIsNone(main.CostController().observe())
            main.FanAssist().poll()
            with self.assertRaises(PlanningUnavailable):
                run_shadow_cycle(database, lambda _: 4)
            for mocked in (status, pump_status, pump_on, pump_off, stove_on, price, forecast):
                mocked.assert_not_called()
            self.assertEqual(stove.call_count, 2)

    def test_disabled_kmp_cannot_be_bypassed_by_direct_commands(self):
        with patch.object(config, "KMP_ENABLED", False), \
                patch.object(config, "SENSIBO_ENABLED", True), \
                patch.object(main.kmp, "get_status") as stove, \
                patch.object(main.sensibo, "on") as pump, \
                patch.object(main, "TEST_MODE", False):
            self.assertFalse(main.turn_on_heat_pump())
            self.assertFalse(main.turn_on_pellet_stove())
            controller = main.CostController()
            self.assertFalse(controller.transition("heatpump", 0))
            stove.assert_not_called()
            pump.assert_not_called()

    def test_collection_does_not_poll_any_disabled_provider(self):
        database = Mock()
        with patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "KMP_ENABLED", False), \
                patch.object(config, "TIBBER_ENABLED", False), \
                patch.object(config, "WEATHER_ENABLED", False), \
                patch.object(collection.sensibo, "status") as pump, \
                patch.object(collection.kmp, "get_status") as stove, \
                patch.object(collection.tibber, "get_spot_price") as price, \
                patch.object(collection.weather, "get_forecast") as forecast:
            collection.collect(database, lambda _: 4)
            for mocked in (pump, stove, price, forecast):
                mocked.assert_not_called()
            sensors, sources = database.save.call_args.args[1:]
            self.assertTrue(all(value is None for name, value in sensors.items()
                                if name != "thermal_profile"))
            self.assertTrue(all(value is None for source in sources.values()
                                for value in source.values()))

    def test_disabled_integrations_keep_monitoring_but_pause_control(self):
        with patch.object(config, "SENSIBO_ENABLED", False), \
                patch.object(config, "TIBBER_ENABLED", False), \
                patch.object(config, "THERMAL_LEARNING_ENABLED", False), \
                patch.object(main.threading, "Thread") as thread, \
                patch.object(main, "check_systems") as checks, \
                patch.object(main, "wait_until_next_quarter", side_effect=StopIteration), \
                patch.object(main, "get_spot_price_safe") as price, \
                patch.object(main, "turn_on_pellet_stove") as control:
            with self.assertRaises(StopIteration):
                main.main_loop()
            checks.assert_called_once()
            thread.assert_not_called()
            price.assert_not_called()
            control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
