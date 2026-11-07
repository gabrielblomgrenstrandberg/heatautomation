import json
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from heatautomation import kmp, sensibo, tibber


class TibberGridReadingsTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock()
        self.response.json.return_value = {
            "data": {
                "viewer": {
                    "websocketSubscriptionUrl": "wss://example.test/subscriptions",
                    "homes": [{
                        "id": "home-id",
                        "features": {"realTimeConsumptionEnabled": True},
                    }],
                }
            }
        }

    @patch.object(tibber, "TIBBER_API_KEY", "test-token")
    @patch.object(tibber.websocket, "create_connection")
    @patch.object(tibber.requests, "post")
    def test_authenticates_and_returns_measurement(
        self, post, create_connection
    ):
        post.return_value = self.response
        socket = Mock()
        socket.recv.side_effect = [
            json.dumps({"type": "connection_ack"}),
            json.dumps({
                "type": "next",
                "payload": {
                    "data": {
                        "liveMeasurement": {
                            "power": 1200,
                            "powerProduction": 50,
                        }
                    }
                },
            }),
        ]
        create_connection.return_value = socket

        readings = tibber.get_grid_import_export()

        self.assertEqual(
            readings,
            {"grid_import_w": 1200, "grid_export_w": 50},
        )
        self.assertEqual(
            json.loads(socket.send.call_args_list[0].args[0]),
            {
                "type": "connection_init",
                "payload": {"token": "test-token"},
            },
        )
        create_connection.assert_called_once_with(
            "wss://example.test/subscriptions",
            header={"User-Agent": "heatautomation/1.0"},
            subprotocols=["graphql-transport-ws"],
            timeout=10,
        )
        socket.close.assert_called_once()

    @patch.object(tibber, "TIBBER_API_KEY", "test-token")
    @patch.object(tibber.websocket, "create_connection")
    @patch.object(tibber.requests, "post")
    def test_empty_websocket_frame_returns_unavailable(
        self, post, create_connection
    ):
        post.return_value = self.response
        socket = Mock()
        socket.recv.return_value = ""
        create_connection.return_value = socket

        self.assertIsNone(tibber.get_grid_import_export())
        socket.close.assert_called_once()

    @patch.object(tibber, "TIBBER_API_KEY", "test-token")
    @patch.object(tibber.websocket, "create_connection")
    @patch.object(tibber.requests, "post")
    def test_disabled_pulse_does_not_open_websocket(self, post, create_connection):
        self.response.json.return_value["data"]["viewer"]["homes"][0]["features"][
            "realTimeConsumptionEnabled"
        ] = False
        post.return_value = self.response

        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(tibber.get_grid_import_export())
        self.assertIn("real-time consumption is not enabled", logs.output[0])
        create_connection.assert_not_called()

    @patch.object(tibber, "TIBBER_API_KEY", "test-token")
    @patch.object(tibber.websocket, "create_connection")
    @patch.object(tibber.requests, "post")
    def test_reports_graphql_subscription_errors(self, post, create_connection):
        post.return_value = self.response
        socket = Mock()
        socket.recv.side_effect = [
            json.dumps({"type": "connection_ack"}),
            json.dumps({
                "type": "next",
                "payload": {"errors": [{"message": "Pulse unavailable"}]},
            }),
        ]
        create_connection.return_value = socket

        with self.assertLogs(level="ERROR") as logs:
            self.assertIsNone(tibber.get_grid_import_export())
        self.assertIn("Pulse unavailable", logs.output[0])
        socket.close.assert_called_once()


class SensiboStatusTests(unittest.TestCase):
    @patch.object(sensibo.requests, "get")
    def test_reads_current_pod_status_from_pod_endpoint(self, get):
        get.return_value.json.return_value = {
            "result": {
                "acState": {
                    "on": True, "mode": "heat", "targetTemperature": 21
                },
                "mainMeasurementsSensor": {
                    "measurements": {"temperature": 19.5}
                },
            }
        }

        self.assertEqual(sensibo.getSystemStatus(), {
            "is_on": True, "mode": "heat", "target_temperature": 21
        })
        self.assertEqual(sensibo.getTemp(), 19.5)
        self.assertEqual(get.call_count, 2)
        get.assert_called_with(
            sensibo.POD_URL,
            params={
                "fields": "acState,mainMeasurementsSensor",
                "apiKey": sensibo.SENSIBO_API_KEY,
            },
            timeout=10,
        )

    @patch.object(sensibo.requests, "post")
    def test_commands_still_use_ac_states_endpoint(self, post):
        post.return_value.status_code = 200
        sensibo.on()
        self.assertEqual(post.call_args.args[0], sensibo.URL)

    @patch.object(sensibo.requests, "get")
    def test_connection_check_uses_pod_endpoint(self, get):
        get.return_value.status_code = 200
        self.assertTrue(sensibo.check_connection())
        self.assertEqual(get.call_args.args[0], sensibo.POD_URL)

    @patch.object(sensibo, "status", return_value={"result": []})
    def test_empty_ac_states_return_unavailable(self, _status):
        self.assertIsNone(sensibo.getSystemStatus())

    @patch.object(sensibo, "status", return_value={"result": []})
    def test_empty_temperature_states_return_unavailable(self, _status):
        self.assertIsNone(sensibo.getTemp())

    @patch.object(
        sensibo,
        "status",
        return_value={
            "result": [
                {
                    "acState": {
                        "on": True,
                        "mode": "heat",
                        "targetTemperature": 21,
                    },
                    "mainMeasurementsSensor": {
                        "measurements": {"temperature": 19.5}
                    },
                }
            ]
        },
    )
    def test_reads_sensibo_status_and_temperature(self, _status):
        self.assertEqual(
            sensibo.getSystemStatus(),
            {
                "is_on": True,
                "mode": "heat",
                "target_temperature": 21,
            },
        )
        self.assertEqual(sensibo.getTemp(), 19.5)


class KmpStatusTests(unittest.TestCase):
    @patch("selenium.webdriver.support.wait.time.sleep")
    def test_waits_until_mode_has_loaded(self, _sleep):
        driver = Mock()
        element = Mock()
        element.text = ""
        loaded = Mock()
        loaded.text = " HÖGEFFEKT "
        driver.find_element.side_effect = [element, loaded]

        self.assertEqual(kmp.get_mode(driver), "HÖGEFFEKT")
        self.assertEqual(driver.find_element.call_count, 2)

    @patch.object(kmp, "WebDriverWait")
    def test_timeout_returns_unavailable(self, wait):
        wait.return_value.until.side_effect = TimeoutException()
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(kmp.get_mode(Mock()))

    @patch.object(kmp, "click_start")
    @patch.object(kmp, "get_mode", return_value=None)
    @patch.object(kmp, "login")
    @patch.object(kmp, "start_chrome")
    def test_off_does_not_toggle_when_mode_is_unavailable(
        self, start_chrome, _login, _get_mode, click_start
    ):
        with self.assertLogs(level="WARNING"):
            kmp.off()
        click_start.assert_not_called()
        start_chrome.return_value.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
