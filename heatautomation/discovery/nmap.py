"""Bounded Nmap scanning of explicitly trusted IPv4 LANs."""

import ipaddress
import math
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Tuple


_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


class DiscoveryError(Exception):
    """A scan could not be completed safely."""


@dataclass(frozen=True)
class ServiceFingerprint:
    port: int
    name: Optional[str]
    product: Optional[str]
    version: Optional[str]


@dataclass(frozen=True)
class DeviceFingerprint:
    ip_address: str
    hostname: Optional[str]
    mac_address: Optional[str]
    services: Tuple[ServiceFingerprint, ...]


def trusted_networks(cidrs):
    """Validate explicitly authorized, small RFC 1918 IPv4 scan ranges."""
    if not isinstance(cidrs, (list, tuple)) or not cidrs:
        raise ValueError("At least one trusted LAN CIDR is required")
    networks = []
    for cidr in cidrs:
        if not isinstance(cidr, str) or "/" not in cidr:
            raise ValueError("Trusted networks must be IPv4 CIDR ranges")
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except ValueError as error:
            raise ValueError("Invalid trusted LAN CIDR") from error
        if (network.version != 4 or network.num_addresses > 256
                or not any(network.subnet_of(private) for private in _PRIVATE_NETWORKS)):
            raise ValueError("Trusted LAN CIDRs must be RFC 1918 IPv4 ranges of at most 256 addresses")
        if network not in networks:
            networks.append(network)
    if sum(network.num_addresses for network in networks) > 1024:
        raise ValueError("Trusted LAN ranges may contain at most 1024 addresses")
    return tuple(networks)


class NmapScanner:
    """Return unverified candidates; an application-level probe is still required."""

    def __init__(self, cidrs, executable="nmap", timeout=120):
        self._networks = trusted_networks(cidrs)
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("Nmap executable must not be empty")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Nmap timeout must be a positive finite number")
        self._executable = executable
        self._timeout = timeout

    def scan(self):
        arguments = [
            self._executable, "-n", "-sT", "-sV", "--version-light",
            "--max-retries", "1", "--host-timeout", "15s",
            "-p", "80,443", "-oX", "-",
        ] + [str(network) for network in self._networks]
        try:
            result = subprocess.run(
                arguments, capture_output=True, timeout=self._timeout, check=False,
            )
        except FileNotFoundError as error:
            raise DiscoveryError("Nmap executable not found") from error
        except subprocess.TimeoutExpired as error:
            raise DiscoveryError("Nmap discovery timed out") from error
        except OSError as error:
            raise DiscoveryError("Nmap could not be started") from error
        if result.returncode:
            raise DiscoveryError("Nmap discovery failed (exit code %s)" % result.returncode)
        try:
            root = ET.fromstring(result.stdout)
        except (ET.ParseError, ValueError) as error:
            raise DiscoveryError("Nmap returned invalid XML") from error
        if root.tag != "nmaprun" or root.find("./runstats/finished[@exit='success']") is None:
            raise DiscoveryError("Nmap did not report a successful scan")
        candidates = []
        for host in root.findall("host"):
            status = host.find("status")
            if status is None or status.get("state") != "up":
                continue
            address = host.find("address[@addrtype='ipv4']")
            if address is None:
                continue
            try:
                ip = ipaddress.ip_address(address.get("addr", ""))
            except ValueError as error:
                raise DiscoveryError("Nmap returned an invalid host address") from error
            if ip.version != 4 or not any(ip in network for network in self._networks):
                raise DiscoveryError("Nmap returned an address outside trusted LANs")
            mac = host.find("address[@addrtype='mac']")
            hostname = host.find("./hostnames/hostname")
            services = []
            for port in host.findall("./ports/port"):
                state = port.find("state")
                if port.get("protocol") != "tcp" or state is None or state.get("state") != "open":
                    continue
                try:
                    port_number = int(port.get("portid", ""))
                except ValueError as error:
                    raise DiscoveryError("Nmap returned an invalid port") from error
                if port_number not in (80, 443):
                    raise DiscoveryError("Nmap returned an unexpected port")
                service = port.find("service")
                services.append(ServiceFingerprint(
                    port_number, service.get("name") if service is not None else None,
                    service.get("product") if service is not None else None,
                    service.get("version") if service is not None else None,
                ))
            candidates.append(DeviceFingerprint(
                str(ip), hostname.get("name") if hostname is not None else None,
                mac.get("addr") if mac is not None else None, tuple(services),
            ))
        return candidates
