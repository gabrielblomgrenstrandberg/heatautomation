import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from heatautomation import main
from heatautomation.learning import collection, database, training


class PackageEntrypointTests(unittest.TestCase):
    def test_module_entrypoint_starts_controller(self):
        code = (
            "import runpy\n"
            "from unittest.mock import patch\n"
            "with patch('heatautomation.web.startup.start_web_server', return_value=None), "
            "patch('threading.Thread'), "
            "patch('heatautomation.integrations.tibber.get_spot_price', "
            "side_effect=KeyboardInterrupt):\n"
            "    try:\n"
            "        runpy.run_module('heatautomation.main', run_name='__main__')\n"
            "    except KeyboardInterrupt:\n"
            "        print('controller-started')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("controller-started", result.stdout)

    def test_application_starts_and_stops_web_server_with_controller(self):
        server = object()
        with patch("heatautomation.web.startup.start_web_server", return_value=server) as start, \
                patch("heatautomation.web.startup.stop_web_server") as stop, \
                patch.object(main, "main_loop", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.run_application()
        start.assert_called_once_with()
        stop.assert_called_once_with(server)

    def test_monitor_uses_relocated_modules(self):
        store = Mock()
        with patch.object(database, "ThermalDatabase", return_value=store), \
                patch.object(collection, "collect") as collect, \
                patch.object(training, "train") as train, \
                patch.object(main.time, "monotonic", return_value=0), \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.thermal_monitor_loop()
        collect.assert_called_once_with(store, main.get_effective_heating_capacity)
        train.assert_called_once_with(store, main.config.THERMAL_MODEL_PATH)


if __name__ == "__main__":
    unittest.main()
