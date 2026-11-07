import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_heat_switch import main


class SpotPriceLimitTests(unittest.TestCase):
    def test_limit_zero_preserves_cost_and_capacity_rules(self):
        with patch.object(main.config, "spotPriceLimit", 0):
            self.assertEqual(main.decide_heating_source(0, 0.5), ("heatpump", 6.5))
            self.assertEqual(main.decide_heating_source(0, 3.0409), ("pelletstove", 5.0))
            self.assertEqual(main.decide_heating_source(-20, 0.01), ("pelletstove", 5.0))

    def test_override_uses_price_not_cost_or_capacity(self):
        with patch.object(main.config, "spotPriceLimit", 1.0):
            self.assertEqual(main.decide_heating_source(-20, 0.9999), ("heatpump", 2.6))
            self.assertEqual(main.decide_heating_source(0, 1.0), ("heatpump", 6.5))
            self.assertEqual(main.decide_heating_source(0, 1.0001), ("pelletstove", 5.0))
            self.assertEqual(main.decide_heating_source(-20, -0.5), ("heatpump", 2.6))

    def test_default_model_capacity_boundaries(self):
        for temperature, capacity in [
            (0, 6.5), (-0.1, 4.0), (-12.5, 4.0),
            (-12.6, 3.6), (-17.5, 3.6), (-17.6, 2.6)
        ]:
            with self.subTest(temperature=temperature):
                self.assertEqual(main.get_effective_heating_capacity(temperature), capacity)

    def test_price_override_still_respects_stove_shutdown_gate(self):
        with patch.object(main.config, "spotPriceLimit", 1.0), \
                patch.object(main, "TEST_MODE", False), \
                patch.object(main.threading, "Thread"), \
                patch.object(main, "get_spot_price_safe", side_effect=[0.5, StopIteration]), \
                patch.object(main.weather, "get_outdoor_temp", return_value=-20), \
                patch.object(main, "check_systems", return_value=(True, True)), \
                patch.object(main, "wait_until_next_quarter"), \
                patch.object(main, "log_data") as log, \
                patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", return_value="HÖGEFFEKT"), \
                patch.object(main.kmp, "click_start") as click, \
                patch.object(main.sensibo, "on") as on:
            with self.assertRaises(StopIteration):
                main.main_loop()
            click.assert_not_called()
            on.assert_not_called()
            log.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_inline_comments_on_settings_and_capacity_rows(self):
        source = Path(__file__).resolve().parents[1] / "heatautomation"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copyfile(source / "config.py", target / "config.py")
            contents = (source / "heatautomation.conf").read_text(encoding="utf-8")
            contents = contents.replace(
                "pellet_price_adjustment = 0.94875 # SEK/kWh",
                "pellet_price_adjustment = 0.94875"
            ).replace(
                "pellet_price_adjustment = 0.94875",
                "pellet_price_adjustment = 0.94875 # SEK/kWh"
            ).replace(
                "forecast_hours = 36", "forecast_hours = 36 ; hours"
            ).replace(
                "test_mode = true", "test_mode = true # read only"
            ).replace(
                "0, 6.5", "0, 6.5 # kW"
            ).replace(
                "log_file = heating_data_log.csv",
                "log_file = heating_data_log.csv # output"
            )
            (target / "heatautomation.conf").write_text(contents, encoding="utf-8")
            env = os.environ.copy()
            env.pop("HEATAUTOMATION_TEST_MODE", None)
            result = subprocess.run(
                [sys.executable, "-c",
                 "import config; assert config.PELLET_PRICE_ADJUSTMENT == 0.94875; "
                 "assert config.FORECAST_HOURS == 36; "
                 "assert config.TEST_MODE is True; "
                 "assert config.HEAT_PUMP_CAPACITY_BY_TEMPERATURE[0] == (0, 6.5); "
                 "assert config.LOG_FILE == 'heating_data_log.csv'"],
                cwd=directory, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_config(self, **variables):
        env = os.environ.copy()
        env.update(variables)
        return subprocess.run(
            [sys.executable, "-c",
             "import config; print(config.spotPriceLimit, config.TEST_MODE, config.FAN_ASSIST_ENABLED)"],
            cwd=str(Path(__file__).resolve().parents[1] / "heatautomation"),
            env=env, capture_output=True, text=True,
        )

    def test_environment_settings_override_defaults(self):
        result = self.run_config(
            SPOT_PRICE_LIMIT="2.25",
            HEATAUTOMATION_FAN_ASSIST="true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2.25 True True")

    def test_invalid_price_limit_fails_explicitly(self):
        for value in ("-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value):
                result = self.run_config(SPOT_PRICE_LIMIT=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ValueError", result.stderr)

    def test_standalone_modules_load_configuration_without_tibber(self):
        env = os.environ.copy()
        env.update({
            "KMP_USERNAME": "test-user",
            "SENSIBO_DEVICE_ID": "test-device",
            "SOLIS_STATION_ID": "test-station",
            "SPOT_PRICE_LIMIT": "0",
        })
        result = subprocess.run(
            [sys.executable, "-c",
             "import kmp, sensibo, solis; "
             "assert kmp.USERNAME == 'test-user'; "
             "assert sensibo.DEVICE_ID == 'test-device'; "
             "assert solis.STATION_ID == 'test-station'"],
            cwd=str(Path(__file__).resolve().parents[1] / "heatautomation"),
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conf_file_drives_settings_and_environment_takes_precedence(self):
        source = Path(__file__).resolve().parents[1] / "heatautomation"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copyfile(source / "config.py", target / "config.py")
            contents = (source / "heatautomation.conf").read_text(encoding="utf-8")
            contents = contents.replace("spotPriceLimit = 0", "spotPriceLimit = 1.75")
            contents = contents.replace("test_mode = true", "test_mode = false")
            contents = contents.replace("fan_assist_enabled = false", "fan_assist_enabled = true")
            contents = contents.replace("latitude = 60.12345", "latitude = 58.0")
            contents = contents.replace("0, 6.5", "1, 7.5")
            (target / "heatautomation.conf").write_text(contents, encoding="utf-8")
            env = os.environ.copy()
            for key in ("SPOT_PRICE_LIMIT", "HEATAUTOMATION_TEST_MODE",
                        "HEATAUTOMATION_FAN_ASSIST"):
                env.pop(key, None)
            command = [
                sys.executable, "-c",
                "import config; print(config.spotPriceLimit, config.TEST_MODE, "
                "config.FAN_ASSIST_ENABLED, config.WEATHER_LATITUDE, "
                "config.HEAT_PUMP_CAPACITY_BY_TEMPERATURE[0])"
            ]
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1.75 False True 58.0 (1.0, 7.5)")
            for path in source.glob("*.py"):
                shutil.copyfile(path, target / path.name)
            decision = subprocess.run(
                [sys.executable, "-c",
                 "import main; assert main.decide_heating_source(0, 1.75)[0] == 'heatpump'; "
                 "assert main.decide_heating_source(0, 1.76)[0] == 'pelletstove'"],
                cwd=directory, env=env, capture_output=True, text=True,
            )
            self.assertEqual(decision.returncode, 0, decision.stderr)
            env.update(SPOT_PRICE_LIMIT="2.25", HEATAUTOMATION_TEST_MODE="true")
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "2.25 False True 58.0 (1.0, 7.5)")
            self.assertIn("HEATAUTOMATION_TEST_MODE is ignored", result.stderr)

    def test_test_mode_conf_overrides_legacy_env_and_dotenv(self):
        source = Path(__file__).resolve().parents[1] / "heatautomation"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for path in source.glob("*.py"):
                shutil.copyfile(path, target / path.name)
            contents = (source / "heatautomation.conf").read_text(encoding="utf-8")
            env = os.environ.copy()
            env.pop("HEATAUTOMATION_TEST_MODE", None)
            for value, expected, legacy in [("true", "True", "false"), ("false", "False", "true")]:
                with self.subTest(value=value):
                    (target / "heatautomation.conf").write_text(
                        contents.replace("test_mode = true", "test_mode = " + value),
                        encoding="utf-8",
                    )
                    (target / ".env").write_text(
                        "HEATAUTOMATION_TEST_MODE=" + legacy, encoding="utf-8"
                    )
                    for use_env in (False, True):
                        test_env = env.copy()
                        if use_env:
                            test_env["HEATAUTOMATION_TEST_MODE"] = legacy
                        result = subprocess.run(
                            [sys.executable, "-c", "import config; print(config.TEST_MODE)"],
                            cwd=directory, env=test_env, capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)
                        self.assertIn("HEATAUTOMATION_TEST_MODE is ignored", result.stderr)
                        command = (
                            "from unittest.mock import patch; import main; "
                            "k=patch.object(main.kmp, 'on', return_value=True); "
                            "s=patch.object(main.sensibo, 'off', return_value={{'ok': True}}); "
                            "r=patch.object(main.kmp, 'get_status', return_value={{'mode': 'AV'}}); "
                            "on=k.start(); off=s.start(); r.start(); "
                            "assert main.turn_on_pellet_stove(); "
                            "assert on.call_count == {}; assert off.call_count == {}"
                        ).format(0 if value == "true" else 1, 0 if value == "true" else 1)
                        integration = subprocess.run(
                            [sys.executable, "-c", command], cwd=directory,
                            env=test_env, capture_output=True, text=True,
                        )
                        self.assertEqual(integration.returncode, 0, integration.stderr)

    def test_missing_and_invalid_conf_fail_at_startup(self):
        source = Path(__file__).resolve().parents[1] / "heatautomation"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copyfile(source / "config.py", target / "config.py")
            env = os.environ.copy()
            env.pop("SPOT_PRICE_LIMIT", None)
            command = [sys.executable, "-c", "import config"]
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not load configuration", result.stderr)
            contents = (source / "heatautomation.conf").read_text(encoding="utf-8")
            for old, new in [
                ("spotPriceLimit = 0", "spotPriceLimit = -1"),
                ("test_mode = true", "test_mode = maybe"),
                ("fan_poll_interval_seconds = 60", "fan_poll_interval_seconds = 0"),
                ("heat_pump_scop = 3.8", "heat_pump_scop = 0"),
                ("-12.5, 4.0", "5, 4.0"),
            ]:
                with self.subTest(setting=new):
                    (target / "heatautomation.conf").write_text(
                        contents.replace(old, new), encoding="utf-8"
                    )
                    clean_env = env.copy()
                    clean_env.pop("HEATAUTOMATION_TEST_MODE", None)
                    result = subprocess.run(
                        command, cwd=directory, env=clean_env, capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ValueError", result.stderr)


if __name__ == "__main__":
    unittest.main()
