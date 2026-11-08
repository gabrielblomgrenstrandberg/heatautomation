import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import select

from heatautomation.discovery import service
from heatautomation.discovery.nmap import DeviceFingerprint, DiscoveryError, ServiceFingerprint
from heatautomation.house import models
from heatautomation.house.config import LanDiscoveryConfig
from heatautomation.house.migrate import migrate_database
from heatautomation.integrations.mill_discovery import (
    MillAuthenticatedAdapter, MillLocalAdapter, MillProbeError,
)


IDENTITY = {"status": "ok", "name": "panel heater gen. 3", "version": "0x210927",
            "mac_address": "00:11:22:33:44:55", "custom_name": "Bedroom"}


def response(data, code=200):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.status_code = code
    result.iter_content.return_value = [json.dumps(data).encode()]
    return result


class MillProbeTests(unittest.TestCase):
    def test_identity_and_optional_measurements(self):
        session = Mock()
        session.get.side_effect = [response(IDENTITY), response({
            "status": "ok", "ambient_temperature": 20.5, "set_temperature": None,
            "current_power": 42, "switched_on": True,
        })]
        client = MillLocalAdapter("192.168.10.3", session=session)
        self.assertEqual(client.identify()["id"], "mill:001122334455")
        measurements = client.measurements()
        self.assertEqual(measurements["ambient_temperature"]["value"], 20.5)
        self.assertNotIn("set_temperature", measurements)
        self.assertEqual(measurements["current_power"]["classification"], "reported")
        self.assertFalse(session.trust_env)
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])

    def test_rejects_non_mill_and_changed_identity(self):
        for bad in ({"status": "ok", "name": "web server"}, dict(IDENTITY, version=""),
                    dict(IDENTITY, mac_address="FF:FF:FF:FF:FF:FF")):
            session = Mock()
            session.get.return_value = response(bad)
            with self.subTest(bad=bad), self.assertRaises(MillProbeError):
                MillLocalAdapter("192.168.10.3", session=session).identify()
        session = Mock()
        session.get.return_value = response(IDENTITY)
        with self.assertRaisesRegex(MillProbeError, "identity changed"):
            MillLocalAdapter("192.168.10.3", session=session).identify("00:11:22:33:44:56")

    def test_https_uses_existing_client_and_normalizes_measurements(self):
        client = Mock()
        client.get_status.return_value = IDENTITY
        client.get_control_status.return_value = {
            "status": "ok", "ambient_temperature": 19, "switched_on": True,
        }
        with patch("heatautomation.integrations.mill.MillLocalDevice", return_value=client) as factory:
            adapter = MillAuthenticatedAdapter("192.168.10.3", "secret", "trusted.pem")
            self.assertEqual(adapter.identify()["id"], "mill:001122334455")
            self.assertEqual(adapter.measurements()["ambient_temperature"]["connection_method"],
                             "LOCAL_HTTPS")
            adapter.close()
        factory.assert_called_once_with("https://192.168.10.3", "secret", "trusted.pem", timeout=3)
        client.close.assert_called_once()


class DiscoveryServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "db.sqlite"
        migrate_database(path, backup=False)
        self.engine = service.engine_for(path)
        self.addCleanup(self.engine.dispose)
        self.settings = LanDiscoveryConfig(enabled=True, allowed_cidrs=["192.168.10.0/24"],
                                           allow_unauthenticated_mill_http=True)
        self.candidate = DeviceFingerprint(
            "192.168.10.3", None, None, (ServiceFingerprint(80, "http", None, None),))
        self.identity = {"id": "mill:001122334455", "name": "Bedroom",
                         "mac_address": "00:11:22:33:44:55"}

    def test_scan_verifies_and_updates_ip_without_erasing_measurements(self):
        client = Mock()
        client.identify.return_value = self.identity
        measurement = {"ambient_temperature": {
            "value": 20, "timestamp": "2020-01-01T00:00:00+00:00", "unit": "C",
            "quality": "good", "classification": "measured", "source": "mill",
            "connection_method": "LOCAL_HTTP"}}
        client.measurements.side_effect = [measurement, {}]
        scanner = Mock()
        scanner.scan.side_effect = [[self.candidate], [
            DeviceFingerprint("192.168.10.4", None, None, self.candidate.services)]]
        with patch.object(service, "MillLocalAdapter", return_value=client):
            service.scan(self.engine, self.settings, scanner=scanner)
            service.scan(self.engine, self.settings, scanner=scanner)
        with self.engine.connect() as conn:
            row = conn.execute(select(models.lan_devices)).one()
            self.assertEqual(row.id, self.identity["id"])
            self.assertEqual(row.ip_address, "192.168.10.4")
            self.assertEqual(json.loads(row.measurements_json), measurement)
            self.assertEqual(conn.execute(select(models.lan_scan_runs)).all()[-1].verified_count, 1)

    def test_failed_probe_is_not_registered_and_nmap_failure_reported(self):
        client = Mock()
        client.identify.side_effect = MillProbeError("invalid")
        scanner = Mock()
        scanner.scan.return_value = [self.candidate]
        with patch.object(service, "MillLocalAdapter", return_value=client):
            service.scan(self.engine, self.settings, scanner=scanner)
        scanner.scan.side_effect = DiscoveryError("Nmap executable not found")
        service.scan(self.engine, self.settings, scanner=scanner)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.lan_devices)).all(), [])
            run = conn.execute(select(models.lan_scan_runs).order_by(
                models.lan_scan_runs.c.id.desc())).first()
            self.assertEqual(run.state, "FAILED")
            self.assertEqual(run.error, "Nmap executable not found")

    def test_poll_uses_cached_ip_and_keeps_old_values_on_error(self):
        client = Mock()
        client.identify.return_value = self.identity
        client.measurements.return_value = {}
        scanner = Mock()
        scanner.scan.return_value = [self.candidate]
        with patch.object(service, "MillLocalAdapter", return_value=client), \
                patch.object(service, "NmapScanner") as nmap:
            service.scan(self.engine, self.settings, scanner=scanner)
            client.identify.side_effect = MillProbeError("Local API unavailable")
            service.poll(self.engine, self.settings)
            nmap.assert_not_called()
        with self.engine.connect() as conn:
            row = conn.execute(select(models.lan_devices)).one()
            self.assertEqual(row.connection_state, "LOCAL_UNAVAILABLE")
            self.assertEqual(row.last_error, "Local API unavailable")

    def test_poll_does_not_contact_a_previously_known_ip_outside_new_allowlist(self):
        client = Mock()
        client.identify.return_value = self.identity
        client.measurements.return_value = {}
        scanner = Mock()
        scanner.scan.return_value = [self.candidate]
        with patch.object(service, "MillLocalAdapter", return_value=client) as factory:
            service.scan(self.engine, self.settings, scanner=scanner)
            settings = self.settings.model_copy(update={"allowed_cidrs": ["192.168.11.0/24"]})
            factory.reset_mock()
            service.poll(self.engine, settings)
            factory.assert_not_called()
        with self.engine.connect() as conn:
            row = conn.execute(select(models.lan_devices)).one()
            self.assertEqual(row.connection_state, "LOCAL_UNAVAILABLE")
            self.assertIn("no longer authorized", row.last_error)

    def test_protected_device_poll_and_mac_matched_discovery_never_use_http(self):
        settings = LanDiscoveryConfig(
            enabled=True, allowed_cidrs=["192.168.10.0/24"],
            protected_mill=[{"ip_address": "192.168.10.3",
                             "mac_address": "00:11:22:33:44:55"}])
        secure = Mock()
        secure.identify.return_value = self.identity
        secure.measurements.return_value = {}
        candidate = DeviceFingerprint("192.168.10.4", None, "00:11:22:33:44:55",
                                      (ServiceFingerprint(443, "https", None, None),))
        scanner = Mock()
        scanner.scan.return_value = [candidate]
        environment = {"HEATAUTOMATION_MILL_001122334455_API_KEY": "secret",
                       "HEATAUTOMATION_MILL_001122334455_CA_BUNDLE": "cert.pem"}
        with patch.dict("os.environ", environment), \
                patch.object(service, "MillAuthenticatedAdapter", return_value=secure) as factory, \
                patch.object(service, "MillLocalAdapter") as plain:
            service.poll(self.engine, settings)
            service.scan(self.engine, settings, scanner=scanner)
            service.poll(self.engine, settings)
            self.assertEqual(factory.call_args_list[0].args[0], "192.168.10.3")
            self.assertEqual(factory.call_args_list[-1].args[0], "192.168.10.4")
            plain.assert_not_called()
        with self.engine.connect() as conn:
            row = conn.execute(select(models.lan_devices)).one()
            self.assertEqual(row.connection_method, "LOCAL_HTTPS")
            self.assertEqual(row.ip_address, "192.168.10.4")

    def test_protected_identity_cannot_be_downgraded_to_http(self):
        settings = LanDiscoveryConfig(
            enabled=True, allowed_cidrs=["192.168.10.0/24"],
            allow_unauthenticated_mill_http=True,
            protected_mill=[{"ip_address": "192.168.10.5",
                             "mac_address": "00:11:22:33:44:55"}])
        plain = Mock()
        plain.identify.return_value = self.identity
        scanner = Mock()
        scanner.scan.return_value = [self.candidate]
        with patch.object(service, "MillLocalAdapter", return_value=plain):
            service.scan(self.engine, settings, scanner=scanner)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.lan_devices)).all(), [])

    def test_protected_missing_credentials_do_not_open_connection(self):
        settings = LanDiscoveryConfig(
            enabled=True, allowed_cidrs=["192.168.10.0/24"],
            protected_mill=[{"ip_address": "192.168.10.3",
                             "mac_address": "00:11:22:33:44:55"}])
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(service, "MillAuthenticatedAdapter") as client:
            service.poll(self.engine, settings)
            client.assert_not_called()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.lan_devices)).all(), [])
            error = conn.execute(select(models.lan_probe_errors)).one()
            self.assertEqual(error.device_id, self.identity["id"])
            self.assertIn("not configured", error.error)
        secure = Mock()
        secure.identify.return_value = self.identity
        secure.measurements.return_value = {}
        environment = {"HEATAUTOMATION_MILL_001122334455_API_KEY": "secret",
                       "HEATAUTOMATION_MILL_001122334455_CA_BUNDLE": "cert.pem"}
        with patch.dict("os.environ", environment), \
                patch.object(service, "MillAuthenticatedAdapter", return_value=secure):
            service.poll(self.engine, settings)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(models.lan_probe_errors)).all(), [])

    def test_protected_retries_manually_updated_ip_after_cached_ip_fails(self):
        settings = LanDiscoveryConfig(
            enabled=True, allowed_cidrs=["192.168.10.0/24"],
            protected_mill=[{"ip_address": "192.168.10.3",
                             "mac_address": "00:11:22:33:44:55"}])
        secure = Mock()
        secure.identify.return_value = self.identity
        secure.measurements.return_value = {}
        environment = {"HEATAUTOMATION_MILL_001122334455_API_KEY": "secret",
                       "HEATAUTOMATION_MILL_001122334455_CA_BUNDLE": "cert.pem"}
        with patch.dict("os.environ", environment), \
                patch.object(service, "MillAuthenticatedAdapter", return_value=secure) as factory:
            service.poll(self.engine, settings)
            settings = LanDiscoveryConfig(
                enabled=True, allowed_cidrs=["192.168.10.0/24"],
                protected_mill=[{"ip_address": "192.168.10.4",
                                 "mac_address": "00:11:22:33:44:55"}])
            secure.identify.side_effect = [MillProbeError("Local API unavailable"), self.identity]
            factory.reset_mock()
            service.poll(self.engine, settings)
            self.assertEqual([call.args[0] for call in factory.call_args_list],
                             ["192.168.10.3", "192.168.10.4"])
        with self.engine.connect() as conn:
            row = conn.execute(select(models.lan_devices)).one()
            self.assertEqual(row.ip_address, "192.168.10.4")
            self.assertEqual(row.connection_state, "CONNECTED_LOCAL")


if __name__ == "__main__":
    unittest.main()
