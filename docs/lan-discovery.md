# LAN discovery: foundation and verification gaps

This is not yet a complete LAN connectivity feature. The application does not
start scans, expose discovery endpoints, persist discoveries, select a local
connection, or display discovery information. Do not rely on the scanner as a
device identification mechanism.

## Architecture

The controller owns equipment control. The authenticated web server is a
separate process that reads persisted observations; its process-local lock
cannot protect controller commands. A future discovery service should run on
the controller side, persist verified identities and connection health, and
provide an authenticated, origin-checked request mechanism for manual scans.
The web process should read those results, not access local device interfaces
directly. The existing command gate and KMP safety checks must remain effective
in all connection modes, including TEST_MODE.

`heatautomation.discovery.nmap.NmapScanner` is an isolated scanning primitive.
It accepts only explicitly specified RFC 1918 IPv4 CIDRs, each no larger than
256 addresses and at most 1024 addresses in total. It runs Nmap using an
argument list, a bounded timeout, TCP connect and light service detection on
ports 80 and 443. Reverse DNS is disabled; hostnames and MAC addresses may not
be available. It reports errors rather than treating a failed scan as an empty
network. The output contains *candidates*, not verified KMP or Mill devices.
No application component calls it automatically. Any future caller must
implement scheduling, concurrency and scan rate limits, as well as read-only
application-level identity verification. Port and service information alone is
insufficient. These range limits are conservative implementation limits, not a
claim that every private subnet is trusted; the operator must authorize each
network.

Install [Nmap](https://nmap.org/download.html) separately and supply its
executable path if it is not on PATH. Scan privileges, host discovery behavior
and service detection vary by operating system and network policy. No actual
network scans were used for the tests.

## Mill Gen 3

The [manufacturer's local API documentation](https://github.com/Mill-International-AS/Generation_3_REST_API)
lists Gen 3 panel, convection and oil heaters and Wi-Fi sockets. It documents
JSON `GET /status` for identity (`name`, `version`, `mac_address`) and
`GET /control-status` for fields including ambient and set temperatures,
`switched_on`, `current_power` and operation mode. Availability and physical
meaning must be verified per model and firmware; a reported control value is
not automatically a measured electrical consumption or energy counter.
The API also documents `POST /operation-mode` and temperature controls, but
these must not bypass the existing gate.

The manufacturer describes HTTP before an API key is set. Setting a key
restarts the device in HTTPS mode with a self-signed certificate, and requests
then use the `Authentication` header. The existing `MillLocalDevice` deliberately
requires an API key, HTTPS and a trusted certificate or CA bundle; this work
does not relax that security requirement. A user reported selecting
"Connect to cloud and enable local API" in the app's advanced setup in
[Mill's issue tracker](https://github.com/Mill-International-AS/Generation_3_REST_API/issues/11);
activation, firmware and app versions require hardware verification.

The manufacturer API documentation does not establish a specific mDNS service
type. The [Home Assistant integration](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mill/config_flow.py)
currently asks for an IP address for local setup, rather than demonstrating a
Mill mDNS discovery mechanism. Do not assume a generic HTTP advertisement
proves device identity or that all Mill products advertise mDNS.

## KMP

The existing cloud adapter uses Selenium and reads the portal's `mode` element.
Blank, null and `--` mode values are now rejected as incomplete; the adapter
does not currently read temperatures. KMP's documented local web interface
and access-point address `192.168.4.1` do not establish its response format,
an identity fingerprint, endpoints, fields or safe command semantics. We
found no authoritative local API specification or verified sample response
for this model. Cloud selectors must not be assumed to apply to its local UI.
No KMP local adapter or fallback is implemented. A sanitized local HTML and
network trace with firmware/model information, or vendor protocol documentation,
is needed before implementing one.

## Remaining work

The normalized measurement and connection model, stale-value retention,
registry, migrations, preferences, mDNS, verification, interval and manual
discovery, IP-change reconciliation, cloud/local fallback, API, GUI, and
end-to-end tests remain to be implemented. Hardware testing is also needed for
identity, TLS, local availability, measurements and device-specific operations.
The current scanner is not wired into the controller or web application.
