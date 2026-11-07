import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "heatautomation"))
import main


class KmpShutdownTests(unittest.TestCase):
    def test_running_modes_do_not_toggle(self):
        for mode in main.kmp.RUNNING_MODES:
            with self.subTest(mode=mode), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start") as click:
                self.assertFalse(main.kmp.off())
                click.assert_not_called()

    def test_idle_modes_toggle(self):
        for mode in main.kmp.IDLE_MODES:
            with self.subTest(mode=mode), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start", return_value=True) as click:
                self.assertTrue(main.kmp.off())
                click.assert_called_once()

    def test_off_modes_do_not_toggle(self):
        for mode in main.kmp.OFF_MODES:
            with self.subTest(mode=mode), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start") as click:
                self.assertTrue(main.kmp.off())
                click.assert_not_called()

    def test_unknown_mode_and_click_failure_are_not_success(self):
        with patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", side_effect=["unexpected", "VILAR..."]), \
                patch.object(main.kmp, "click_start", return_value=False) as click:
            self.assertFalse(main.kmp.off())
            click.assert_not_called()
            self.assertFalse(main.kmp.off())
            click.assert_called_once()


class HeatPumpSwitchTests(unittest.TestCase):
    def test_running_or_unavailable_stove_does_not_enable_heat_pump(self):
        for mode in list(main.kmp.RUNNING_MODES) + [None, "unexpected"]:
            with self.subTest(mode=mode), \
                    patch.object(main, "TEST_MODE", False), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start") as click, \
                    patch.object(main.sensibo, "on") as on:
                self.assertFalse(main.turn_on_heat_pump())
                click.assert_not_called()
                on.assert_not_called()

    def test_switch_turns_off_stove_before_turning_on_heat_pump(self):
        calls = Mock()
        calls.off.return_value = True
        calls.on.return_value = {"result": "ok"}
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "off", calls.off), \
                patch.object(main.sensibo, "on", calls.on):
            self.assertTrue(main.turn_on_heat_pump())
        self.assertEqual(calls.mock_calls, [call.off(), call.on()])

    def test_failure_does_not_mark_switch_successful(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "off", return_value=True), \
                patch.object(main.sensibo, "on", return_value=None):
            self.assertFalse(main.turn_on_heat_pump())

    def test_test_mode_reads_status_but_never_controls_devices(self):
        for mode, expected in [
            ("LADDAR", False), ("VILAR...", True), (None, False), ("unexpected", False)
        ]:
            status = {"mode": mode} if mode else None
            with self.subTest(mode=mode), \
                    patch.object(main, "TEST_MODE", True), \
                    patch.object(main.kmp, "get_status", return_value=status), \
                    patch.object(main.kmp, "off") as off, \
                    patch.object(main.sensibo, "on") as on:
                self.assertEqual(main.turn_on_heat_pump(), expected)
                off.assert_not_called()
                on.assert_not_called()

    def test_controller_rechecks_and_switches_only_when_idle(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.threading, "Thread"), \
                patch.object(main, "get_spot_price_safe", side_effect=[1, 1, StopIteration]), \
                patch.object(main.weather, "get_outdoor_temp", return_value=0), \
                patch.object(main, "decide_heating_source", return_value=("heatpump", 6.5)), \
                patch.object(main, "check_systems", return_value=(True, True)), \
                patch.object(main, "wait_until_next_quarter"), \
                patch.object(main, "log_data") as log, \
                patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", side_effect=["LADDAR", "VILAR..."]), \
                patch.object(main.kmp, "click_start", return_value=True) as click, \
                patch.object(main.sensibo, "on", return_value={"result": "ok"}) as on:
            with self.assertRaises(StopIteration):
                main.main_loop()
            click.assert_called_once()
            on.assert_called_once()
            self.assertEqual(log.call_count, 1)
            self.assertEqual(log.call_args.args[3], "heatpump")

    def test_changed_decision_cancels_pending_heat_pump_switch(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.threading, "Thread"), \
                patch.object(main, "get_spot_price_safe", side_effect=[1, 2, StopIteration]), \
                patch.object(main.weather, "get_outdoor_temp", return_value=0), \
                patch.object(main, "decide_heating_source", side_effect=[
                    ("heatpump", 6.5), ("pelletstove", 5.0)
                ]), \
                patch.object(main, "check_systems", return_value=(True, True)), \
                patch.object(main, "wait_until_next_quarter"), \
                patch.object(main, "log_data"), \
                patch.object(main.kmp, "off", return_value=False), \
                patch.object(main.sensibo, "on") as on, \
                patch.object(main, "turn_on_pellet_stove") as stove:
            with self.assertRaises(StopIteration):
                main.main_loop()
            on.assert_not_called()
            stove.assert_called_once()

    def test_existing_pellet_selection_is_preserved_when_switch_is_deferred(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.threading, "Thread"), \
                patch.object(main, "get_spot_price_safe", side_effect=[1, 1, 1, StopIteration]), \
                patch.object(main.weather, "get_outdoor_temp", return_value=0), \
                patch.object(main, "decide_heating_source", side_effect=[
                    ("pelletstove", 5.0), ("heatpump", 6.5), ("pelletstove", 5.0)
                ]), \
                patch.object(main, "check_systems", return_value=(True, True)), \
                patch.object(main, "wait_until_next_quarter"), \
                patch.object(main, "log_data") as log, \
                patch.object(main.kmp, "off", return_value=False), \
                patch.object(main.sensibo, "on") as on, \
                patch.object(main, "turn_on_pellet_stove") as stove:
            with self.assertRaises(StopIteration):
                main.main_loop()
            on.assert_not_called()
            stove.assert_called_once()
            self.assertEqual(log.call_count, 2)
            self.assertTrue(all(c.args[3] == "pelletstove" for c in log.call_args_list))


class PelletStoveSwitchTests(unittest.TestCase):
    def test_unknown_or_unavailable_mode_never_disables_heat_pump(self):
        for mode in [None, "", "unexpected"]:
            with self.subTest(mode=mode), \
                    patch.object(main, "TEST_MODE", False), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start") as click, \
                    patch.object(main.sensibo, "off") as off:
                self.assertFalse(main.turn_on_pellet_stove())
                click.assert_not_called()
                off.assert_not_called()

    def test_recognized_modes_and_failed_click(self):
        for mode in main.kmp.RUNNING_MODES | main.kmp.IDLE_MODES | main.kmp.OFF_MODES:
            with self.subTest(mode=mode), \
                    patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                    patch.object(main.kmp, "login"), \
                    patch.object(main.kmp, "get_mode", return_value=mode), \
                    patch.object(main.kmp, "click_start", return_value=True) as click:
                self.assertTrue(main.kmp.on())
                self.assertEqual(click.call_count, int(mode in main.kmp.OFF_MODES))
        with patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", return_value="AV"), \
                patch.object(main.kmp, "click_start", return_value=False):
            self.assertFalse(main.kmp.on())

    def test_test_mode_requires_known_stove_mode(self):
        for status, expected in [
            (None, False), ({"mode": None}, False), ({"mode": "unexpected"}, False),
            ({"mode": "AV"}, True), ({"mode": "HÖGEFFEKT"}, True)
        ]:
            with self.subTest(status=status), \
                    patch.object(main, "TEST_MODE", True), \
                    patch.object(main.kmp, "get_status", return_value=status), \
                    patch.object(main.kmp, "on") as on, \
                    patch.object(main.sensibo, "off") as off:
                self.assertEqual(main.turn_on_pellet_stove(), expected)
                on.assert_not_called()
                off.assert_not_called()

    def test_failed_heat_pump_off_does_not_report_success(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "on", return_value=True), \
                patch.object(main.sensibo, "off", return_value=None):
            self.assertFalse(main.turn_on_pellet_stove())

    def test_driver_or_start_button_failure_preserves_heat_pump(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "start_chrome", return_value=None), \
                patch.object(main.time, "sleep"), \
                patch.object(main.sensibo, "off") as off:
            self.assertFalse(main.turn_on_pellet_stove())
            off.assert_not_called()

        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", return_value="AV"), \
                patch.object(main.kmp, "click_start", return_value=False), \
                patch.object(main.sensibo, "off") as off:
            self.assertFalse(main.turn_on_pellet_stove())
            off.assert_not_called()

    def test_controller_retries_deferred_switch_next_cycle(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.threading, "Thread"), \
                patch.object(main, "get_spot_price_safe", side_effect=[1, 1, StopIteration]), \
                patch.object(main.weather, "get_outdoor_temp", return_value=0), \
                patch.object(main, "decide_heating_source", return_value=("pelletstove", 5.0)), \
                patch.object(main, "check_systems", return_value=(True, True)), \
                patch.object(main, "wait_until_next_quarter"), \
                patch.object(main, "log_data") as log, \
                patch.object(main.kmp, "start_chrome", return_value=Mock()), \
                patch.object(main.kmp, "login"), \
                patch.object(main.kmp, "get_mode", side_effect=[None, "AV"]), \
                patch.object(main.kmp, "click_start", return_value=True) as click, \
                patch.object(main.sensibo, "off", return_value={"result": "ok"}) as off:
            with self.assertRaises(StopIteration):
                main.main_loop()
            click.assert_called_once()
            off.assert_called_once()
            self.assertEqual(log.call_count, 1)
            self.assertEqual(log.call_args.args[3], "pelletstove")


if __name__ == "__main__":
    unittest.main()
