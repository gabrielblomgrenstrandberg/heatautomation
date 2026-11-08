import subprocess
import unittest
from unittest.mock import patch

from heatautomation.discovery.nmap import DiscoveryError, NmapScanner, trusted_networks


XML = b"""<?xml version="1.0"?>
<nmaprun>
  <host><status state="up"/><address addr="192.168.10.12" addrtype="ipv4"/>
    <address addr="00:11:22:33:44:55" addrtype="mac"/>
    <hostnames><hostname name="heater.local"/></hostnames>
    <ports><port protocol="tcp" portid="80"><state state="open"/>
      <service name="http" product="example" version="1"/>
    </port><port protocol="tcp" portid="443"><state state="closed"/></port></ports>
  </host>
  <runstats><finished exit="success"/></runstats>
</nmaprun>"""


class NmapScannerTests(unittest.TestCase):
    def test_requires_explicit_small_private_ranges(self):
        for ranges in ([], ["0.0.0.0/0"], ["8.8.8.0/24"], ["192.168.0.0/16"],
                       ["127.0.0.0/24"], ["::1/128"], ["192.168.1.1"],
                       ["192.168.1.1/24"], ["192.168.1.0/24;echo bad"]):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                trusted_networks(ranges)
        self.assertEqual(len(trusted_networks(["10.2.3.0/24", "10.2.3.0/24"])), 1)

    @patch("heatautomation.discovery.nmap.subprocess.run")
    def test_returns_candidates_not_device_identity(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout=XML, stderr=b"")
        result = NmapScanner(["192.168.10.0/24"], executable="nmap path").scan()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ip_address, "192.168.10.12")
        self.assertEqual(result[0].hostname, "heater.local")
        self.assertEqual(result[0].mac_address, "00:11:22:33:44:55")
        self.assertEqual(result[0].services[0].port, 80)
        self.assertEqual(result[0].services[0].product, "example")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][0], "nmap path")
        self.assertEqual(args[0][-1], "192.168.10.0/24")
        self.assertEqual(kwargs["timeout"], 120)
        self.assertNotIn("shell", kwargs)

    @patch("heatautomation.discovery.nmap.subprocess.run")
    def test_failure_is_explicit(self, run):
        scanner = NmapScanner(["192.168.10.0/24"])
        for failure, message in (
            (FileNotFoundError(), "not found"),
            (PermissionError(), "could not be started"),
            (subprocess.TimeoutExpired("nmap", 1), "timed out"),
        ):
            run.side_effect = failure
            with self.subTest(message=message), self.assertRaisesRegex(DiscoveryError, message):
                scanner.scan()
        run.side_effect = None
        for output, code in ((b"invalid", 0), (XML, 1),
                             (XML.replace(b'exit="success"', b'exit="error"'), 0),
                             (XML.replace(b"192.168.10.12", b"8.8.8.8"), 0)):
            run.return_value = subprocess.CompletedProcess([], code, stdout=output, stderr=b"")
            with self.subTest(output=output[:20], code=code), self.assertRaises(DiscoveryError):
                scanner.scan()


if __name__ == "__main__":
    unittest.main()
