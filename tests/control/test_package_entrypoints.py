import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from heatautomation import main
from heatautomation.learning import collection, database, profile, training


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

    def test_reset_precedes_service_startup(self):
        events = []
        with patch("heatautomation.house.startup_reset.reset_mismatched_house",
                   side_effect=lambda *args: events.append("reset")), \
                patch("heatautomation.discovery.service.start_worker",
                      side_effect=lambda *args: events.append("discovery")), \
                patch("heatautomation.web.startup.start_web_server",
                      side_effect=lambda: events.append("web")), \
                patch("heatautomation.web.startup.stop_web_server"), \
                patch.object(main, "main_loop", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.run_application()
        self.assertEqual(events, ["reset", "discovery", "web"])

    def test_monitor_uses_relocated_modules(self):
        store = Mock()
        selected = {
            "indoor_source": "sensibo", "tado_room_id": "", "tado_zone": "",
            "heatpump_absent": False, "pelletstove_absent": False,
            "other_heating_absent": False,
        }
        with patch.object(database, "ThermalDatabase", return_value=store), \
                patch.object(profile, "current", return_value=selected), \
                patch.object(main.config, "MPC_ENABLED", False), \
                patch.object(collection, "collect") as collect, \
                patch.object(training, "train") as train, \
                patch.object(main.time, "monotonic", return_value=0), \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.thermal_monitor_loop()
        collect.assert_called_once_with(store, main.get_effective_heating_capacity, None, selected)
        train.assert_called_once_with(store, main.config.THERMAL_MODEL_PATH, selected)

    def test_tado_profile_is_shared_with_shadow_planner(self):
        from heatautomation.control import mpc_monitor
        from heatautomation.house import config as house_config
        from heatautomation.integrations import tado

        selected = {
            "indoor_source": "tado", "tado_room_id": "room", "tado_zone": "1:2",
            "heatpump_absent": True, "pelletstove_absent": False,
            "other_heating_absent": True,
        }
        store, service, house = Mock(), Mock(), Mock()
        with patch.object(database, "ThermalDatabase", return_value=store), \
                patch.object(profile, "current", return_value=selected), \
                patch.object(main.config, "TADO_ENABLED", True), \
                patch.object(main.config, "MPC_ENABLED", True), \
                patch.object(house_config, "load_house_config", return_value=house), \
                patch.object(tado, "TadoService", return_value=service), \
                patch.object(collection, "collect") as collect, \
                patch.object(training, "train") as train, \
                patch.object(mpc_monitor, "run_shadow_cycle") as run, \
                patch.object(main.time, "monotonic", return_value=0), \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.thermal_monitor_loop()
        collect.assert_called_once_with(store, main.get_effective_heating_capacity, service, selected)
        train.assert_called_once_with(store, main.config.THERMAL_MODEL_PATH, selected)
        run.assert_called_once_with(store, main.get_effective_heating_capacity,
                                    selected_profile=selected)


if __name__ == "__main__":
    unittest.main()
