import os
import socket
import subprocess
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from heatautomation.web import startup


PASSWORD = "a sufficiently long test password 123456"


class WebStartupTests(unittest.TestCase):
    def test_missing_password_or_disabled_never_starts_process(self):
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": "",
                                     "HEATAUTOMATION_WEB_AUTOSTART": "true"}), \
                patch.object(startup.subprocess, "Popen") as popen:
            self.assertIsNone(startup.start_web_server())
            popen.assert_not_called()
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": PASSWORD,
                                     "HEATAUTOMATION_WEB_AUTOSTART": "false"}), \
                patch.object(startup.subprocess, "Popen") as popen:
            self.assertIsNone(startup.start_web_server())
            popen.assert_not_called()

    def test_launch_failure_does_not_raise(self):
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": PASSWORD,
                                     "HEATAUTOMATION_WEB_AUTOSTART": "true"}), \
                patch.object(startup.subprocess, "Popen", side_effect=OSError("test")):
            self.assertIsNone(startup.start_web_server())

    def test_monitor_failure_stops_child_without_stopping_controller(self):
        process = Mock()
        process.poll.return_value = None
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": PASSWORD,
                                     "HEATAUTOMATION_WEB_AUTOSTART": "true"}), \
                patch.object(startup.subprocess, "Popen", return_value=process), \
                patch.object(startup.threading.Thread, "start", side_effect=RuntimeError("test")):
            self.assertIsNone(startup.start_web_server())
        process.terminate.assert_called_once_with()

    def test_stop_only_terminates_owned_process(self):
        process = Mock()
        process.poll.return_value = None
        stopping = Mock()
        startup.stop_web_server((process, stopping))
        stopping.set.assert_called_once_with()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_stop_escalates_on_timeout(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("uvicorn", 5), 0]
        startup.stop_web_server((process, Mock()))
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_real_server_starts_and_stops(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with patch.dict(os.environ, {"HEATAUTOMATION_WEB_PASSWORD": PASSWORD,
                                     "HEATAUTOMATION_WEB_AUTOSTART": "true"}):
            server = startup.start_web_server(port=port)
            self.assertIsNotNone(server)
            self.addCleanup(startup.stop_web_server, server)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    urlopen("http://127.0.0.1:{}/api/status".format(port), timeout=1)
                except HTTPError as error:
                    self.assertEqual(error.code, 401)
                    break
                except URLError:
                    if server[0].poll() is not None:
                        self.fail("Web server exited before becoming ready")
                    time.sleep(0.1)
            else:
                self.fail("Web server did not become ready")
            startup.stop_web_server(server)
            self.assertIsNotNone(server[0].poll())


if __name__ == "__main__":
    unittest.main()
