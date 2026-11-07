import os
import unittest
from unittest.mock import Mock, patch

from test_heat_switch import main


class FanCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock()
        self.response.json.return_value = {
            "result": {"remoteCapabilities": {"modes": {"fan": {
                "fanLevels": ["quiet", "low", "medium", "high", "auto", "strong"]
            }}}}
        }

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(main.sensibo.requests, "get")
    def test_selects_strong_over_high_and_auto(self, get):
        get.return_value = self.response
        self.assertEqual(main.sensibo.get_fan_level(), "strong")
        self.assertEqual(get.call_args.kwargs["params"]["fields"], "remoteCapabilities")

    @patch.dict(os.environ, {"SENSIBO_MAX_FAN_LEVEL": "high"})
    @patch.object(main.sensibo.requests, "get")
    def test_configured_level_must_be_supported(self, get):
        get.return_value = self.response
        self.assertEqual(main.sensibo.get_fan_level(), "high")
        os.environ["SENSIBO_MAX_FAN_LEVEL"] = "invalid"
        self.assertIsNone(main.sensibo.get_fan_level())

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(main.sensibo.requests, "get")
    def test_unknown_capabilities_do_not_guess(self, get):
        get.return_value = self.response
        levels = self.response.json.return_value["result"]["remoteCapabilities"]["modes"]["fan"]["fanLevels"]
        levels.append("custom")
        self.assertIsNone(main.sensibo.get_fan_level())
        os.environ["SENSIBO_MAX_FAN_LEVEL"] = "custom"
        self.assertEqual(main.sensibo.get_fan_level(), "custom")

    @patch.object(main.sensibo, "send_post_request", return_value={"result": "ok"})
    def test_fan_command_includes_power_mode_and_level(self, post):
        main.sensibo.fan_on("strong")
        post.assert_called_once_with({
            "acState": {"on": True, "mode": "fan", "fanLevel": "strong"}
        })


class FanAssistTests(unittest.TestCase):
    def setUp(self):
        self.assist = main.FanAssist()
        self.off_state = {"on": False, "mode": "heat"}
        self.fan_state = {"on": True, "mode": "fan", "fanLevel": "strong"}

    def test_start_maintain_and_stop(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "get_status", side_effect=[
                    {"mode": "HÖGEFFEKT"}, {"mode": "HÖGEFFEKT"}, {"mode": "VILAR..."}
                ]), \
                patch.object(main.sensibo, "get_fan_level", return_value="strong"), \
                patch.object(main.sensibo, "get_fan_state", side_effect=[
                    self.off_state, self.fan_state, self.fan_state
                ]), \
                patch.object(main.sensibo, "fan_on", return_value={"result": "ok"}) as on, \
                patch.object(main.sensibo, "off", return_value={"result": "ok"}) as off:
            self.assist.poll()
            self.assertTrue(self.assist.active)
            self.assist.poll()
            self.assist.poll()
            self.assertFalse(self.assist.active)
            on.assert_called_once_with("strong")
            off.assert_called_once()

    def test_unknown_kmp_and_unavailable_sensibo_leave_settings_unchanged(self):
        self.assist.active = True
        self.assist.fan_level = "strong"
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "get_status", side_effect=[
                    None, {"mode": "unknown"}, {"mode": "VILAR..."}
                ]), \
                patch.object(main.sensibo, "get_fan_state", return_value=None), \
                patch.object(main.sensibo, "fan_on") as on, \
                patch.object(main.sensibo, "off") as off:
            for _ in range(3):
                self.assist.poll()
            self.assertTrue(self.assist.active)
            on.assert_not_called()
            off.assert_not_called()

    def test_does_not_turn_off_heat_or_a_changed_fan_setting(self):
        for state in [
            {"on": True, "mode": "heat", "fanLevel": "strong"},
            {"on": True, "mode": "fan", "fanLevel": "low"},
            {"on": False, "mode": "fan", "fanLevel": "strong"},
        ]:
            with self.subTest(state=state), \
                    patch.object(main, "TEST_MODE", False), \
                    patch.object(main.kmp, "get_status", return_value={"mode": "AV"}), \
                    patch.object(main.sensibo, "get_fan_state", return_value=state), \
                    patch.object(main.sensibo, "off") as off:
                self.assist.active = True
                self.assist.fan_level = "strong"
                self.assist.poll()
                self.assertFalse(self.assist.active)
                off.assert_not_called()

    def test_failed_commands_are_retried_on_next_poll(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "get_status", return_value={"mode": "HÖGEFFEKT"}), \
                patch.object(main.sensibo, "get_fan_level", return_value="strong"), \
                patch.object(main.sensibo, "get_fan_state", return_value=self.off_state), \
                patch.object(main.sensibo, "fan_on", side_effect=[None, {"result": "ok"}]) as on:
            self.assist.poll()
            self.assertFalse(self.assist.active)
            self.assist.poll()
            self.assertTrue(self.assist.active)
            self.assertEqual(on.call_count, 2)
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main.kmp, "get_status", return_value={"mode": "VILAR..."}), \
                patch.object(main.sensibo, "get_fan_state", return_value=self.fan_state), \
                patch.object(main.sensibo, "off", side_effect=[None, {"result": "ok"}]) as off:
            self.assist.poll()
            self.assertTrue(self.assist.active)
            self.assist.poll()
            self.assertFalse(self.assist.active)
            self.assertEqual(off.call_count, 2)

    def test_test_mode_never_commands_devices(self):
        with patch.object(main, "TEST_MODE", True), \
                patch.object(main.kmp, "get_status", side_effect=[
                    {"mode": "HÖGEFFEKT"}, {"mode": "VILAR..."}
                ]), \
                patch.object(main.sensibo, "get_fan_level", return_value="strong"), \
                patch.object(main.sensibo, "get_fan_state", return_value=self.off_state), \
                patch.object(main.sensibo, "fan_on") as on, \
                patch.object(main.sensibo, "off") as off:
            self.assist.poll()
            self.assist.poll()
            self.assertFalse(self.assist.active)
            on.assert_not_called()
            off.assert_not_called()

    def test_pellet_transition_does_not_disable_active_assist(self):
        with patch.object(main, "TEST_MODE", False), \
                patch.object(main, "FAN_ASSIST_ENABLED", True), \
                patch.object(main.fan_assist, "active", True), \
                patch.object(main.fan_assist, "fan_level", "strong"), \
                patch.object(main.kmp, "on", return_value=True), \
                patch.object(main.sensibo, "get_fan_state", return_value=self.fan_state), \
                patch.object(main.sensibo, "off") as off:
            self.assertTrue(main.turn_on_pellet_stove())
            off.assert_not_called()

    def test_pellet_transition_rechecks_assist_state(self):
        for state, expected, off_calls in [
            (None, False, 0),
            ({"on": True, "mode": "heat"}, True, 1),
        ]:
            with self.subTest(state=state), \
                    patch.object(main, "TEST_MODE", False), \
                    patch.object(main, "FAN_ASSIST_ENABLED", True), \
                    patch.object(main.fan_assist, "active", True), \
                    patch.object(main.fan_assist, "fan_level", "strong"), \
                    patch.object(main.kmp, "on", return_value=True), \
                    patch.object(main.sensibo, "get_fan_state", return_value=state), \
                    patch.object(main.sensibo, "off", return_value={"result": "ok"}) as off:
                self.assertEqual(main.turn_on_pellet_stove(), expected)
                self.assertEqual(off.call_count, off_calls)

    def test_monitor_runs_every_minute_without_waiting_for_quarter_hour(self):
        clock = Mock(side_effect=[0, 0, 0, 0, 60, 60, 60])
        with patch.object(main.time, "monotonic", clock), \
                patch.object(main.time, "sleep") as sleep, \
                patch.object(main.fan_assist, "poll", side_effect=[None, StopIteration]) as poll:
            with self.assertRaises(StopIteration):
                main.fan_monitor_loop()
            self.assertEqual(poll.call_count, 2)
            sleep.assert_called_once_with(60)

    def test_main_starts_monitor_only_when_enabled(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), \
                    patch.object(main, "FAN_ASSIST_ENABLED", enabled), \
                    patch.object(main.threading, "Thread") as thread, \
                    patch.object(main, "get_spot_price_safe", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    main.main_loop()
                names = [c.kwargs["name"] for c in thread.call_args_list]
                self.assertEqual("pellet-fan-monitor" in names, enabled)


if __name__ == "__main__":
    unittest.main()
