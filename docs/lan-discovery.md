# LAN discovery and verification gaps

The opt-in discovery service currently supports **read-only Mill Gen 3 HTTP
and certificate-verified HTTPS**. It is not a complete KMP/Mill connectivity
feature. It does not change the existing control path or dispatch commands.

## Architecture

The controller owns equipment control. The authenticated web server is a
separate process whose process-local lock cannot protect controller commands.
The discovery worker runs in the controller process. The web process reads
results from SQLite and places manual scan requests in the database, after
authentication and origin checks. It does not access device interfaces directly.
The worker performs only GET requests; the existing command gate and KMP
safety checks are unchanged.

`heatautomation.discovery.nmap.NmapScanner` accepts explicitly authorized
RFC 1918 IPv4 CIDRs, each no larger than 256 addresses and at most 1024
addresses in total. It runs Nmap using an argument list, a bounded timeout,
TCP connect and light service detection on ports 80 and 443. Reverse DNS is
disabled; hostnames and MAC addresses may not be available. Errors are
reported rather than treated as an empty network. Scan results are candidates,
not identified devices. The worker probes up to 64 HTTP candidates, requires
a Mill Gen 3 name, version and unicast MAC from `/status`, and compares the
MAC to Nmap's MAC when available. For HTTP this is a fingerprint, not
cryptographic proof: an unauthenticated device can impersonate another one. Port and service
information alone is insufficient. The operator must explicitly authorize
each network; RFC 1918 address space is not automatically trusted.

Install [Nmap](https://nmap.org/download.html) separately and supply its
executable path if it is not on PATH. Scan privileges and service detection
vary by operating system and network policy. Tests do not scan actual networks.

Migrate the thermal database with
`python -m heatautomation.house.migrate --database <path>`, then set the
`lan_discovery` options shown in `config/house.example.json` in the active
house configuration. Set `enabled` and explicitly provide permitted CIDRs.
`allow_unauthenticated_mill_http` is a separate opt-in for devices without a
key: HTTP requests are unencrypted and unauthenticated. The worker reads configuration
changes on its next iteration. Set `HEATAUTOMATION_HOUSE_CONFIG` if the file
is not `config/house.json`. The controller must be running; a standalone web
server cannot execute queued scans. Scans are spaced by the configured
interval; manual scans are limited to one every five minutes per worker.
Polling known addresses does not run Nmap. Measurements retain their original
timestamps when later reads fail and are stale after `freshness_seconds`; they
are not used by the heating controller.

The dashboard uses authenticated `GET /api/discovery` and origin-checked
`POST /api/discovery`. Results and requests are stored in the thermal database,
separate from configured physical device and learned thermal tables. A
verified Mill's registry ID derives from its reported MAC, so an IP change
does not replace its registry row. To associate it with an existing house
device for display, set that Mill device's `external_id` to the exact registry
ID, such as `mill:001122334455`, then apply the house configuration. This
explicit association uses the reconciled database and preserves physical IDs
and learned parameters; it does not dispatch commands or feed measurements
into the heating controller. Duplicate assignments are rejected by house
configuration validation and are displayed as errors if already in the
database. Retention and multi-controller operation have
not been implemented; run only one controller per database.
The dashboard marks connection monitoring stale when there has been no recent
successful check or discovery is disabled. A stale previously connected state
is shown as unknown, while the last actual connection error remains available.

## Mill Gen 3

The [manufacturer's local API documentation](https://github.com/Mill-International-AS/Generation_3_REST_API)
lists Gen 3 panel, convection and oil heaters and Wi-Fi sockets. It documents
JSON `GET /status` for identity (`name`, `version`, `mac_address`) and
`GET /control-status` for fields including ambient and set temperatures,
`switched_on`, `current_power` and operation mode. Availability and physical
meaning must be verified per model and firmware; a reported control value is
not automatically measured electrical consumption or energy. The discovery
adapter reads fields present with valid types. It labels ambient temperature
as measured and other device-reported fields as reported, not verified power
consumption or physical heating state. The API also documents
`POST /operation-mode` and temperature controls, but these must not bypass the
existing gate.

The manufacturer describes HTTP before an API key is set. Setting a key
restarts the device in HTTPS mode with a self-signed certificate, and requests
then use the `Authentication` header. The existing `MillLocalDevice` requires
an API key, HTTPS and a trusted certificate or CA bundle; this work does not
relax that security requirement.

To configure a protected device, add
`{"ip_address": "192.168.10.3", "mac_address": "00:11:22:33:44:55"}`
to `lan_discovery.protected_mill`, using the actual address and MAC. Set
`HEATAUTOMATION_MILL_001122334455_API_KEY` and
`HEATAUTOMATION_MILL_001122334455_CA_BUNDLE` in the ignored
`heatautomation/.env` file or environment, substituting the upper-case MAC
without colons. The latter is a path to the device's trusted certificate or
CA bundle. The certificate must validate for the numeric IP address; a
self-signed certificate is not trusted automatically. Keys are not sent to
arbitrary scan candidates: HTTPS rediscovery only tries candidates whose Nmap
MAC matches a configured MAC, and validates both TLS and the API's MAC before
updating the address. If Nmap cannot obtain a MAC or a certificate does not
match the new IP, update the configuration and trust material manually.
Protected devices never downgrade to unauthenticated HTTP. A user reported selecting
"Connect to cloud and enable local API" in the app's advanced setup in
[Mill's issue tracker](https://github.com/Mill-International-AS/Generation_3_REST_API/issues/11);
activation, firmware and app versions require hardware verification.
Configuration or connection failures for protected devices appear in the
dashboard even if the device has never been verified; failed probes are not
registered as supported devices. The error clears after a successful probe.

The manufacturer documentation does not establish a specific mDNS service
type. The [Home Assistant integration](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mill/config_flow.py)
currently asks for an IP address for local setup, rather than demonstrating a
Mill mDNS discovery mechanism. Do not assume a generic HTTP advertisement
proves identity or that all Mill products advertise mDNS. The `mdns_enabled`
option logs that this feature is unavailable; it does not scan.

## KMP

The cloud adapter uses Selenium and reads the portal's `mode` element. Blank,
null and `--` mode values are rejected as incomplete; the adapter does not
currently read temperatures. KMP's documented local web interface and
access-point address `192.168.4.1` do not establish its response format, an
identity fingerprint, endpoints, fields or safe command semantics. We found no
authoritative local API specification or verified sample response for this
model. Cloud selectors must not be assumed to apply to its local UI. No KMP
local adapter or fallback is implemented. A sanitized local HTML and network
trace with firmware/model information, or vendor protocol documentation, is
needed before implementing one.

## Remaining work

KMP local identity and fallback, automatic active-subnet detection within
authorized networks, mDNS, cloud/local connection
preferences and fallback, integration
with controller measurements remain to be implemented. The current connection
model covers discovered Mill devices only. Hardware testing is needed for
identity fingerprints, TLS, local availability, measurements and
device-specific operations.
