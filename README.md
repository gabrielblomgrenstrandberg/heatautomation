# heatautomation
A script for selecting the best heat source based on spot price and temperature

## Project layout

* `heatautomation/integrations/`: Sensibo, KMP, Tibber, weather and Solis clients.
* `heatautomation/control/`: heating-source selection.
* `heatautomation/learning/`: collection, database, features, training and prediction.
* `heatautomation/storage/`: the existing CSV logger.
* `tests/`: tests grouped by the corresponding responsibilities.

Run the test suite from the repository root with
`python -m unittest discover -s tests`.

## Choose integrations

The legacy controller's enabled providers are selected in the `[integrations]`
section of `heatautomation/heatautomation.conf`. For a house without Sensibo,
set `sensibo = false` and restart the controller and web server. The
`HEATAUTOMATION_SENSIBO_ENABLED=false` environment variable or an entry in
the ignored `heatautomation/.env` file takes precedence. Equivalent settings
exist for `kmp`, `tibber`, and `weather`, using `HEATAUTOMATION_KMP_ENABLED`,
`HEATAUTOMATION_TIBBER_ENABLED`, and `HEATAUTOMATION_WEATHER_ENABLED`.
Values must be true or false. Defaults are enabled to preserve existing setups.

Disabled integrations are not polled or commanded by the controller or
thermal collector; their measurements remain unknown, not zero or off.
Automatic source switching and shadow MPC are paused unless all four are
enabled, because the existing two-source controller cannot safely infer the
state or capacity of an absent source. Enabled devices continue read-only
monitoring, including in test mode. This setting does not configure or enable
the separate Tado or Mill services or optional LAN discovery. The dashboard
shows the controller's configured selection; change settings locally and
restart both processes rather than treating the dashboard as a control channel.

Optional LAN discovery is disabled by default. It requires Nmap, explicitly
trusted CIDRs and a migrated thermal database. The current implementation
supports read-only Mill Gen 3 discovery only; KMP local connectivity and
cloud/local fallback are not implemented. See
[LAN discovery and verification gaps](./docs/lan-discovery.md) before enabling it.

The multi-floor simulation integration scenarios can be run separately:

```powershell
python -m unittest discover -s tests\integrations -p test_multifloor_simulation.py
```

They use synthetic thermal parameters and mocked integrations, compare model
forecasts with a separately integrated synthetic plant, and exercise the real
simulation planner and command gates. Passing does not validate real-world
equipment, sensor accuracy, prices or a production command path. Tado and
Mill are not yet wired into the main controller, so their outage tests verify
adapter errors and simulated dispatch safety, not a live controller recovery
loop. Automatic multi-room control must remain disabled.

## Web monitoring and room settings

The web application is a separate process and does **not** control equipment.
It reads the thermal database produced by the running controller and can save
room targets, limits and temporary overrides in SQL; it never sends equipment
commands or enables automatic room control. If thermal
learning is disabled, or the database is missing or stale, the dashboard reports
that condition. It does not call provider APIs. The observation time is the
collection time, not a guarantee of fresh provider data or physical heat output.
See [web architecture and roadmap](./docs/web-architecture.md) for the API,
safety boundaries, missing data and planned later stages.

Use Python 3.9 or newer and Node.js 20.19+ or 22.12+. From the repository root:

```powershell
python -m pip install -r requirements.txt -r requirements-web.txt
Set-Location frontend
npm install
npm run build
Set-Location ..
```

Add a strong, random password of at least 32 characters to the existing ignored
`heatautomation/.env` file:

```text
HEATAUTOMATION_WEB_PASSWORD=your-own-random-password-at-least-32-characters-long
```

Replace the example with your own secret and keep it private. An environment
variable of the same name takes precedence over `.env`. Restart the application
after editing it. Start the existing controller normally:

```powershell
python -m heatautomation.main
```

The controller automatically starts the web server in a separate process at
`http://127.0.0.1:8000`. Open that address and sign in with the password above.
The controller logs a warning if the password is missing or too short; if web
dependencies are missing or port 8000 is occupied, the web process logs the
error. Heating control continues if web startup fails or the web process exits.
Stop the controller normally to stop the server it started. Set
`HEATAUTOMATION_WEB_AUTOSTART=false` in `.env` or the environment to disable
automatic startup when running a separate web server. The web server is not
restarted automatically after a failure.

The Docker image also builds and installs the web component. Its default
loopback binding is private to the container. To publish it on the Docker host,
set `HEATAUTOMATION_WEB_HOST=0.0.0.0` inside the container and publish port 8000
only on the host's loopback interface (for example, `-p 127.0.0.1:8000:8000`).
Other containers on the same network may also reach it, so use a trusted network
and a strong password. Supply credentials at runtime, not in the image; the
local `.env` file is excluded from the build context.

Use the same working directory for the controller and web server or an absolute
thermal database path in configuration. `/docs` contains the HTTP API
documentation. The WebSocket at
`/api/live` emits `{type: "status", data: ...}` snapshots or
`{type: "error", message: ...}`, followed by separate `house` or `house_error`
messages; it requires the authenticated session and
configured origin. The dashboard observes database updates at five-second
intervals; the controller normally collects every five minutes.
The displayed target is Sensibo's reported target, not a confirmed scheduler
setting. Heat-pump cost is the observed Tibber total price divided by estimated
COP; additional grid fees are not included.

The room view requires the house migrations and a reconciled physical
configuration. It displays measured temperatures, configured heat paths and
thermal links, stored prediction comparisons, and saved simulation-only room
plans. Room schedules are not resolved in the overview, so their effective
target is shown as unknown unless an override is active. Room targets and
limits, per-room maximum preheating (0–2 °C) and weekly schedules are saved in
SQL, not dispatched to devices. Preheating affects new simulation plans only.
Overrides start at the
next 15-minute boundary and durations round up to 15-minute intervals; an
overlapping future override is rejected. The configuration editor validates
`config/house.json`, saves it atomically, and applies it to SQL as a separate
operation. Set `HEATAUTOMATION_HOUSE_CONFIG` for a different file; the file
must already exist. A revision prevents stale saves within this web process.
Do not edit the file concurrently from another process. If applying fails or
the process stops after saving, retry the explicit apply; no automatic recovery
is implied. Reconciliation preserves learned values and historical data.
Automatic control and complete historical series are
not yet managed by the GUI. Missing measurements and actual heat delivery remain unknown.
The base-target history starts with web changes after migration; it is not an
effective-target history and does not reconstruct past schedule or override values.
Activity and expenditure charts read only good-quality, measured
`room_measurements` with quantities `heating_activity` (unit `percent`) and
`heating_expenditure` (unit `SEK`, interval expenditure), respectively.
The existing integrations do not yet collect these series, so they remain
empty until a verified data source records them.

For frontend development, disable automatic web startup, run the web server
separately with `python -m uvicorn heatautomation.web.app:create_app --factory
--host 127.0.0.1 --port 8000`, run `npm run dev` in `frontend`, and set
`HEATAUTOMATION_WEB_ORIGIN=http://127.0.0.1:5173` in the backend process before
starting it. Vite proxies `/api` and WebSocket traffic to port 8000. The origin
must match the browser URL exactly. Restart the backend after building the
frontend. The password is backend-only and must not be committed; use a random
secret, not the example placeholder. Sessions last up to eight hours and are
lost when the web process restarts. Login attempts are limited in-process.

The default is **local access only**, not a production Internet deployment.
For remote access, arrange HTTPS and authenticated network access through a
trusted reverse proxy, set `HEATAUTOMATION_WEB_ORIGIN` to the exact HTTPS
origin and `HEATAUTOMATION_WEB_SECURE_COOKIE=true`, and do not expose the
backend port directly. Protect the database and environment variables.
The web service has no command, configuration, or scheduler execution API.

After installing the web dependencies, run the focused tests with
`python -m unittest tests.web.test_web`.

## Multi-room heating foundation

The multi-room implementation is being introduced incrementally. The current
code adds validated physical configuration, additive Alembic migrations,
idempotent SQL reconciliation, a room-level thermal model, a Tado Classic
service, and a guarded command interface. Device polling, automatic room
control, and the multi-room optimizer are not wired into the controller. A
standalone Mill Gen 3 integration is also available.

Copy `config/house.example.json` to `config/house.json`, replace placeholder
thermal values with verified physical data, and keep the real file local; it is
git-ignored. Before using a database with the new schema, create a backup and
apply migrations:

```powershell
python -m heatautomation.house.migrate --database thermal_history.sqlite3
python -m heatautomation.house.cli reconcile --database thermal_history.sqlite3 --config config/house.json
```

The migration command creates a SQLite backup beside an existing database
before upgrading it. Reconciliation only adds missing records or updates
physical configuration fields; learned parameter values and runtime settings
remain in SQL. Restore the backup if a migration fails. The legacy
`thermal_sensors` row named `indoor_c` is retained; assigning it to a room and
backfilling room measurements requires an explicit room mapping first.

To explicitly reset a room's learned thermal values to its current
configuration priors (with an audit event):

```powershell
python -m heatautomation.house.cli reset-parameters --database thermal_history.sqlite3 --room <room-uuid>
```

### Prediction-only room model

The standalone `heatautomation.thermal.rooms.RoomThermalService` uses the SQL
room parameters, settings and symmetric thermal graph. The example graph
connects KMP's living room to the kitchen, nursery and bathroom, not to the
office or Floor 0. Changing an existing house file requires reconciliation;
removed links are soft-deleted. Priors in the example are placeholders.

After migrating and reconciling, `save_snapshot(observed_at, temperatures,
outdoor_c, heat_inputs)` records one synchronized observation and associated
room temperatures. Temperature entries are keyed by room UUID and contain
`value` in Celsius, `quality`, and a configured room sensor's
`source_device_id`. Heat entries are keyed by room UUID and generator UUID;
each contains delivered `watts` and `classification` (`measured` or
`estimated`). Supply a source entry even for known zero delivery. Unknown
delivery must be omitted, **not inferred from an on command, a Tado demand
percentage, a Mill mode, or a generator's rated capacity**. Inputs must
represent the same instant; the caller is responsible for matching timestamps,
sensor identity and data quality. The existing provider adapters do not yet
automatically supply a synchronized, verified room heat-delivery history.

For example, after resolving trustworthy readings, callers can persist and
forecast them as follows (the IDs and readings must come from the actual
configuration and measurements):

```python
from datetime import datetime, timezone
from sqlalchemy import create_engine
from heatautomation.thermal.rooms import RoomThermalService

engine = create_engine("sqlite:///thermal_history.sqlite3")
thermal = RoomThermalService(engine)
observed_at = datetime.now(timezone.utc)
# Populate these with timestamp-aligned real observations, not commands.
temperatures = {}  # room UUID -> {value, source_device_id, quality}
heat_inputs = {}   # room UUID -> generator UUID -> {watts, classification}
snapshot = thermal.save_snapshot(observed_at, temperatures, None, heat_inputs)
forecasts = thermal.predict(observed_at, snapshot)
print(forecasts)  # Unknown until sufficient inputs are supplied.
engine.dispose()
```

`predict(observed_at, snapshot)` returns each room's 15-, 30-, 60-, 120-,
180- and 360-minute forecast, heat-up/cool-down rates, source inputs, heat
transfer, heat-loss coefficient, capacity, demand and time to target. The
model assumes constant outdoor temperature and delivered heat for the
forecast; capacity and graph transfer provide thermal inertia. Demand is a
shadow estimate, not a command. Missing inputs or a missing room in a coupled
component make its forecast unknown. Time-to-target is unknown when the target
is missing or not reached within six hours. Runtime targets come from SQL
`room_settings`; schedules and overrides are not yet resolved by this service.

Forecasts are stored for later comparison. `evaluate_predictions()` matches
them with good measured room temperatures within five minutes of each target
time and returns MAE and bias separately for each room and horizon. It waits
until the matching window has closed; forecasts generated after their target
time are not recorded as prospective predictions. `learn()` fits a constrained
physics-based model only when sufficient good temperatures and **measured
delivered heat** exist, the inputs are identifiable, and a chronological
holdout improves on the current parameters. Coupled conductances are updated
only when both room fits agree; SQL retains learned values across reloads.
The result reports why each room was or was not updated. Run these methods
explicitly; they do not train or control devices automatically.

`import_legacy_history(database, room_id, sensibo_device_id)` imports legacy
Sensibo/weather rows only after an explicit Sensibo-to-room assignment.
Legacy timestamps are collection times, not verified sensor timestamps, so
temperatures are marked suspect and legacy source capacities are not treated
as delivered room heat. Tado, KMP and Mill observations require explicit,
timestamp-aligned ingestion before they can support learning. With the
currently unverified integrations, many forecasts and all learned parameters
may therefore remain unavailable. Real-device accuracy validation is still
required before using this model for decisions.

The initial approach uses physical room parameters shared with the existing
heat balance, rather than a separate complex estimator per room. Ridge or
incremental SGD could be evaluated once representative history exists;
random forests and gradient boosting need more data and chronological
validation before they could replace or supplement the physical model.
No such model has been validated against this house yet.

### Multi-room MPC simulation

The existing single-temperature MPC is unchanged. The separate
`heatautomation.control.room_mpc` planner produces **simulation-only** plans;
it never issues device commands or starts automatic control. It solves 144
consecutive 15-minute intervals using a coupled linear heat balance and the
SQL thermal graph. KMP's configured direct room receives its heat; neighboring
rooms receive only modeled physical transfer. Storage's direct heater is
separate from the radiator circuit. Radiator heat is a hypothetical room
delivery allocation, not proof that Tado valves or the uncontrollable SPRSUN
heat pump can achieve it.

Use the existing `mpc_monitor.build_intervals` to prepare a full 36-hour
price/weather forecast, then `room_mpc.forecasts_from_legacy` to obtain
`Forecast` objects and the heat-pump capacity forecast. That helper refuses
partial coverage; current providers may not offer 36 hours of prices.
Construct a `Source` for each configured generator using verified ratings,
per-room emitter capacities, operating costs and, for KMP/Mill, known initial
on/off state, elapsed duration and dwell/max-runtime limits in quarter-hour
steps. Set the radiator source's `capacity_forecast_kw` from the returned
capacity list. `room_capacity_kw` maps room UUIDs to maximum **delivered**
heat; pellet/electric on/off sources have one direct room and fixed output.
The model does not infer a valve's delivered output from its commanded state.
The example's unknown ratings must be measured or otherwise validated first.

Call `rooms_from_database(engine, forecasts, readings, sources,
timezone_name=...)`, then `optimize_rooms(rooms, sources, forecasts)`.
Readings are keyed by room UUID and contain `value`, timezone-aware
`observed_at`, `quality="good"`, `classification="measured"`, a configured
`source_device_id`, and a defensible `uncertainty_c`. The SQL room settings
must include minimum, maximum and target temperatures. Room schedules and
manual overrides are resolved in the supplied house timezone; schedule and
override boundaries must align with 15-minute intervals. Missing or stale
inputs, missing source assignments, incomplete forecasts, overlapping
schedules/overrides or infeasible comfort constraints fail explicitly.

The plan contains room-level temperatures, targets, estimated maintenance
demand, source heat, transfer, activity, explanations and estimated costs.
It limits planned temperatures to the room maximum and at most 2°C above
the current target, with an explicit uncertainty margin. It minimizes
estimated energy cost, not a guarantee of actual expenditure; the radiator
electric cost assumes the forecast COP applies to delivered heat. Fixed
binary KMP/Mill output and constant uncertainty margins are simplifying
assumptions, not verified equipment behavior. Time-limited solutions are
accepted only when all constraints verify and their solver gap is within the
configured tolerance; solver status and gap are included. Optional
`save_simulation(engine, plan)` persists the plan to the existing optimizer
tables. Validate model accuracy, thermal capacities, equipment response and
simulation outcomes against real measurements before considering control.

See [the multi-room architecture](./docs/multi-room-architecture.md) for the
physical model, schema, safety boundaries, integration research, and rollout
plan.

## Tado Classic integration

The standalone synchronous `TadoService` supports Classic/V3 cloud homes.
Tado X support is **not verified**. It uses Tado's device authorization and
the unofficial Classic API, which can change without notice. It is not started
by the controller and does not automatically control heating. Tado may impose
subscription-dependent daily API quotas. Reads use a five-minute per-endpoint
cache by default; 429 responses respect `Retry-After` when supplied and
otherwise pause requests for 30 minutes. Errors are raised rather than
returning expired cached data as fresh.

Copy the example house configuration as described above. For initial
authorization and discovery, run the following in a Python shell from the
repository root. Choose a token path outside the repository in a directory
accessible only to the application user:

```python
import time
from pathlib import Path
from heatautomation.house.config import load_house_config
from heatautomation.integrations.tado import TadoService, TokenFile

house = load_house_config("config/house.json")
token_file = TokenFile(Path.home() / ".heatautomation" / "tado-token.json")
tado = TadoService(house, token_file)
if token_file.load() is None:
    auth = tado.begin_authentication()
    print("Open", auth["verification_uri"], "and enter", auth["user_code"])
    while not tado.poll_authentication():
        time.sleep(auth["interval"])
print(tado.get_homes())
print(tado.get_rooms())
tado.close()
```

In `config/house.json`, fill in each `tado_rooms` entry's numeric `home_id`
and `zone_id` from discovery. The `room_id` is the stable house UUID, not
the Tado room name or device serial. An unmapped entry has both IDs `null`;
zone IDs are unique only within a home. Reload the configuration and create
a new service after editing it. The example contains all ten requested Tado
rooms; the storage room is intentionally not mapped.

`get_rooms()` discovers all zones, including unmapped ones. `get_status()`,
`get_room_status(room_id)`, `get_room_temperature(room_id)`, and
`get_heating_activity()` read mapped room data. The reported target is the
provider's current setting, not proof of a successful command. The reported
heating-power percentage is a demand/activity signal, not measured radiator
heat delivery: `radiator_heating_activity` remains unknown. Readings expose
the provider timestamp when present, receipt time, classification, quality,
and zone identity; the individual sensor identity is unknown. An offline
device can still have an old reported value, marked stale.

`set_room_temperature` and `set_heating_enabled` require an attached
`CommandGate`. With TEST_MODE active they are audited as simulated and do
not make API requests. Outside TEST_MODE, the gate must also explicitly enable
automatic control and have confirmed device connectivity; the adapter checks
the HEATING capability and temperature limits before sending a temporary
overlay until the next schedule block. Enabling heating requires an explicit
target. A sent command does not prove that the room warmed or heat was
delivered. Live control is not wired to the application by default.

Refresh tokens rotate and are saved atomically by `TokenFile`; protect the token
file and its directory and never commit or share them. On Unix the temporary
file is restricted to owner access; on Windows, set appropriate directory ACLs
because `chmod` does not enforce equivalent privacy. Avoid HTTP debug logging,
which can expose OAuth request parameters. Reauthorize if token persistence
fails or access is revoked. Do not copy tokens or authorization codes into
logs, issues, or configuration examples.

## Mill storage heater

The standalone Mill integration uses the [manufacturer's Gen 3 local REST
API](https://github.com/Mill-International-AS/Generation_3_REST_API); verify
that the outlet is Gen 3 and enable its local API. The configured device is
the storage-room outlet on Floor 0, and the separately configured electric
heater is its generator. Set that generator's `rated_kw` in the local
`config/house.json` to the verified heater rating; `null` means unknown.

Mill documents an API key in the `Authentication` header and HTTPS with a
self-signed certificate after provisioning. Provision the key using Mill's
instructions, keep it in the ignored environment or a secret store, and
supply a trusted certificate or CA bundle whose certificate identity matches
the device hostname. The client refuses HTTP, missing trust bundles,
redirects, and certificate verification bypasses. It does not configure the
device's key or discover LAN devices: `get_devices()` returns the configured
device after contacting it.

Create `MillLocalDevice`, `MillPolicy` and `MillHeatingSource` from
`heatautomation.integrations.mill`. Pass the already migrated/reconciled house,
the generator and outlet UUIDs from the house file, a `CommandGate` using the
same database, and explicit minimum-on, minimum-off, maximum-on and polling
durations in seconds. Policy values are application-supplied runtime settings;
do not use arbitrary examples as equipment safety limits. Call `start_monitor()`
before requesting live on; `set_power(True/False)` uses the command gate, and
`set_manual_override(enabled, duration_seconds)` requests a temporary override.
Overrides do not bypass switching times, fault latches or the maximum runtime;
expiry clears the override but does not itself issue a command.

For example, after the migration and reconciliation above, create the service
in your own process. Replace the policy durations only after validating them
for the equipment; these examples are placeholders, not safety recommendations:

```python
import os
from sqlalchemy import create_engine
from heatautomation import config
from heatautomation.devices.command_gate import CommandGate
from heatautomation.house.config import load_house_config
from heatautomation.integrations.mill import MillHeatingSource, MillLocalDevice, MillPolicy

house = load_house_config("config/house.json")
engine = create_engine("sqlite:///thermal_history.sqlite3")
device = MillLocalDevice(
    os.environ["MILL_DEVICE_URL"], os.environ["MILL_API_KEY"],
    os.environ["MILL_CA_BUNDLE"],
)
gate = CommandGate(engine, test_mode=config.TEST_MODE, automatic_control_enabled=False)
service = MillHeatingSource(
    house, device,
    "09963c36-c69b-4472-be21-4d6095292759",
    "c1565fc6-f3e9-40db-a83d-a4dc4cd195c1",
    MillPolicy(min_on_seconds=300, min_off_seconds=300,
               max_on_seconds=3600, poll_seconds=30),
    gate,
)
print(service.get_devices())
print(service.get_status())
device.close()
engine.dispose()
```

The example is read-only and does not start the monitor or enable control. If
live control is deliberately configured later, use a single owning process
for the outlet and keep its monitor running. API keys must never be placed in
the house file or committed.

The service is **not wired into the existing controller**. TEST_MODE commands
are audited as simulated. Outside TEST_MODE, the gate must explicitly enable
control, and the service confirms connectivity before dispatch. It uses
`POST /operation-mode` (`Control individually` or `OFF`): individual mode
follows the Mill device's own temperature setting, so enabling it is not a
guarantee that the socket relay is energized. Mill's `switched_on` is exposed
as a device-reported field; the documented API does not establish a separate
socket relay readback or a measured electricity meter for this outlet. Actual
outlet state, heat delivery, electrical consumption and operating hours remain
unknown. The API's `current_power` is exposed separately without treating it
as measured outlet electricity.

The status optionally estimates energy and cost from sampled enabled-mode
hours multiplied by configured rated capacity and a caller-supplied price per
kWh. This assumes full rated consumption during sampled intervals, excludes
gaps and is not metered energy or proof of heat output. Statistics and manual
overrides are process-local and reset at restart.

**Safety limitation:** the monitor is a best-effort software watchdog, not a
hard runtime cutoff. It requires a running process, database, network and
device; shutdown can be delayed by polling and request timeouts or fail
entirely when communication is lost. At startup, or after loss of
communication, an observed enabled mode triggers an off request before
re-enabling is permitted. A device fault also latches the safety state.
`stop_monitor()` does not switch the outlet off. Use an independently verified
hardware thermostat/thermal cutoff and a device-side timer for any required
hard maximum runtime; do not rely on this integration as the sole safety
mechanism. Monitor failures are surfaced through `get_health()` and logs.

## Configuration

Edit [heatautomation/heatautomation.conf](./heatautomation/heatautomation.conf) for location,
heating model, logging, polling, and operating settings. The file centralizes
the application settings in INI format; restart the application after changing them.
Inline comments begin with `#` or `;` when preceded by whitespace. For values
containing those characters after whitespace, use an environment override instead.
The loader requires this file alongside the Python modules and reports invalid
or missing settings at startup. Heat-pump capacity rows are `temperature, capacity`
pairs ordered from highest to lowest temperature.
Keep credentials in the ignored `heatautomation/.env` file or in environment
variables, never in the tracked configuration file. Credentials and the
documented environment variables override their defaults in the configuration.
The `.env` file is loaded regardless of which module is run.
Existing variables are `TIBBER_API_KEY`, `SENSIBO_DEVICE_ID`, `SENSIBO_API_KEY`,
`KMP_USERNAME`, `KMP_PASSWORD`, `SOLIS_KEY_ID`, `SOLIS_KEY_SECRET`,
`SOLIS_STATION_ID`, `BASE_URL` (Solis), `CHROME_BINARY_PATH`,
`CHROMEDRIVER_PATH`, `HEATAUTOMATION_FAN_ASSIST`,
`SENSIBO_MAX_FAN_LEVEL`, and `SPOT_PRICE_LIMIT`.

Set `spotPriceLimit` to a positive price in SEK/kWh to select KMP when the
Tibber total spot price is above the limit, and Sensibo when it is at or below
the limit. `SPOT_PRICE_LIMIT` in the environment or `.env` overrides this
setting. The default `0` disables the override and keeps the normal
cost-and-capacity calculation. The override also bypasses the normal minimum
heat-pump capacity rule, so choose a limit appropriate for your system.
Existing KMP mode and switching safeguards still apply.

## Optional cost-based heating control

The `[cost_control]` section is disabled by default because this repository
does not have measured COP, heating demand, or pellet cost for your home.
Before setting `enabled = true`, supply:

* `pellet_bag_size_kg` (default 16), `pellet_energy_kwh_per_kg`,
  `pellet_price_per_bag` (SEK), and `stove_efficiency_percent` (default 94).
  The pellet fuel cost per delivered kWh is calculated as
  `price per bag / (bag size × energy per kg × efficiency / 100)`.
  This estimate does not include electricity used by the stove.
* `cop_by_temperature`: measured or manufacturer COP values for your heat pump
  at representative outdoor temperatures and operating conditions.
* `demand_by_temperature`: estimated heat demand in kW at those temperatures.
  The illustrative default assumes a 100 m² house needing 5 kW at -20°C and
  no space heating at 18°C. It does not include hot water. As a rough starting
  point, scale the kW values by your heated area divided by 100 m², then
  calibrate them to your building and climate; demand is not measured by this
  application. A zero value requests both sources off, so adjust the warm
  endpoint if heating is still needed at that temperature.

Enter each curve as `temperature, value` rows ordered from warmest to coldest.
Values are linearly interpolated and clamped at the endpoints. The existing
capacity table remains a stepwise estimate, and `pellet_stove_capacity` is
the estimated stove capacity. The model compares Tibber total price / COP with
the pellet cost, selects a single source when it can meet demand economically,
and selects `combined` when needed or cheaper. Neither device is modulated by
this application: mixed costs assume an ideal allocation and are estimates.
If estimated demand exceeds combined capacity, both sources are requested and
a warning is logged. A zero demand requests both sources off.

`cost_hysteresis` is a fraction (0.10 means 10% savings required); it applies
only when the current source can satisfy demand. The configurable minimum runtime
and minimum switching interval apply independently to each device. On startup or
after status loss, their timers restart conservatively; settings are checked on
the normal quarter-hour cycle. Starting a replacement is followed by a later
status check before stopping the previous source. If a command or status check
fails, or KMP is in a protected running mode, the transition is deferred.
The existing `spotPriceLimit` overrides economic selection, but not demand
capacity, timers or KMP safeguards. If the preferred source cannot meet demand,
combined operation is selected.

Fan assist is suppressed whenever cost control needs heat from Sensibo or cannot
determine device status, so fan mode cannot replace heat-pump operation. The
controller uses provider-reported status and command acknowledgements, not
independent physical-output confirmation; safety interlocks and thermostat
controls remain necessary.

## Thermal learning (monitoring only)

`[thermal_learning]` enables a separate read-only worker by default. It attempts
one observation every five minutes, independently of the quarter-hour control
loop, and retrains at most once per hour. Set `enabled = false` to disable it.
`database_path` and `model_path` are relative to the application working
directory unless absolute paths are provided. Install the updated dependencies
from `requirements.txt`; in Docker, persist both paths in a mounted volume if
the history and model should survive container replacement. Back up the SQLite
database regularly. The database and model may contain information about the
home and should be kept private. Only load trusted model files: joblib files
can execute code during loading.

The database stores UTC observation times, Sensibo temperature, humidity,
target and mode, Open-Meteo's current-hour temperature, Tibber total price,
and individual source states and nominal capacities. Tables for sensors and
sources allow new names without changing the observation schema. Missing
readings are stored as NULL, not as zero. KMP startup and shutdown modes, and
Sensibo modes whose thermal output is unknown, have unknown capacities and are
excluded from training. The capacities are configured estimates, **not**
measured delivered heat. An enabled heat pump may also be cycling internally.
The source readings are sequential rather than atomic, and the providers do
not establish that every sensor measurement is fresh. A slow or failed poll
can leave a gap; collection errors are logged and retried.

The heating and cooling models use the indoor/outdoor temperatures, a
30-minute indoor-temperature lag and source capacities to estimate net
temperature change in °C/hour. Training excludes missing and implausible
readings, intervals outside 3.5–6.5 minutes, and samples without 30 minutes
of continuous, unchanged source configuration both before and after the
interval. Heating samples additionally require an observed start and end of
the same uninterrupted provider-reported source power configuration. Unknown
startup capacities can occur within that cycle but are not used as samples;
ongoing or interrupted cycles are excluded. It keeps all stored history to
allow seasonal learning. Each regime
needs at least 120 qualifying intervals; models may therefore be unavailable
for some time. A chronological holdout with a 30-minute separation reports
one-step rate MAE and RMSE in `thermal_evaluations` on each successful
retraining. These are not measurements of six-hour forecast accuracy.

The interface takes an explicit weather and heating scenario; it never issues
commands or participates in source selection. For example:

```python
from heatautomation.learning.prediction import ThermalPredictor, PredictionUnavailable

recent = [20.0, 20.0, 19.9, 19.9, 19.9, 19.8, 19.8]  # 5-minute readings, oldest first
outdoor = [2.0] * 72  # replace with forecast temperatures for each 5-minute step
plan = [{"heatpump": 4.0, "pelletstove": 0.0}] * 72  # nominal kW for each step
try:
    predictor = ThermalPredictor("thermal_model.joblib")
    result = predictor.forecast(recent, outdoor, plan, target_c=21.0)
    print(result["predictions"])  # keys: 1, 2, 3 and 6 hours
    print(result["heat_up_hours"])  # None if target not reached within six hours
    print(predictor.cooling_rate(recent, 2.0))
    print(predictor.heating_rate(recent, 2.0, 4.0, 0.0))
    print(predictor.heating_demand(recent, 2.0))
    print(predictor.source_response("heatpump"))
except PredictionUnavailable as error:
    print("Insufficient data:", error)
```

Convert the hourly weather forecast into 72 five-minute values before calling
the interface, using an explicit timezone-aware mapping appropriate for the
installation. Plans must cover the entire six-hour horizon. A source
combination requires at least 20 qualifying observations; missing models and
unsupported combinations raise `PredictionUnavailable`. The forecast's
`error_scale_c` accumulates one-step holdout RMSE as a **heuristic**, not a
statistical confidence interval. Source response and demand are approximate
correlations expressed relative to nominal capacity, not measured output or
causal effects. They can be misleading when sources, weather and thermostat
behavior are correlated. Validate against real multi-hour outcomes, across
seasons and operating modes, before considering any future control integration.
The legacy CSV contains decisions rather than the necessary indoor and
device-state observations and is not imported as training data.

## Experimental shadow MPC scheduler

The `[mpc]` section is **disabled by default**. When enabled with thermal
learning and cost control, the existing five-minute thermal monitor also
attempts to generate and evaluate shadow plans. It never commands a device and
does not replace or influence the existing controller. It polls for updated
observations, prices and weather every five minutes; it is **not event-driven**
and therefore cannot react immediately between polls. A slow solver or provider
can delay the next attempt.

Planning uses Tibber's first subscribed home's quarter-hourly `total` prices
for today and tomorrow and the configured Open-Meteo forecast. It stops at the
first missing quarter-hour or weather hour, up to 144 intervals (36 hours).
Weather times are interpreted in the configured timezone; ambiguous daylight
saving timestamps are rejected rather than guessed. Plans begin at the next
UTC quarter hour and assume the latest temperature and device state persist
until then. Seven consecutive, recent indoor readings and known stable device
statuses are required. Provider failure, missing data, infeasible constraints,
or insufficient model quality are logged; no substitute plan or control
action is generated.

Before enabling, configure the comfort limits, nominal target, uncertainty
margin, electricity extra cost (SEK/kWh, in addition to Tibber `total`),
temperature-deviation penalty (SEK per °C per interval), switch penalty (SEK
per transition), and minimum on/off times. Configure and verify the cost-control
COP curve, capacities and pellet cost for the installation. The existing
`test_mode` controls the old controller; it does not change the shadow-only
behavior of MPC. The MPC requires Python 3.8+ and SciPy 1.9+.

Architecture:

* `control/mpc.py` formulates a mixed-integer linear program for SciPy/HiGHS;
  SciPy was selected instead of adding OR-Tools because the installed
  scientific stack already depends on it. It models none, pump, stove and
  combined operation as binary states. Capacities are full nominal outputs,
  not modulated equipment commands. Runtime and off-time constraints are
  rounded up to 15-minute intervals; unknown initial elapsed time is treated
  conservatively, and switches near the horizon end must complete their dwell.
* The existing separate linear thermal regressions supply the 15-minute Euler
  temperature dynamics, using indoor and outdoor temperature, source capacity,
  and a roughly 30-minute indoor lag. Only source combinations with sufficient
  support and acceptable one-step holdout RMSE are allowed. A configurable
  fixed comfort margin is **not** a calibrated forecast confidence interval.
  Temperatures are constrained inside the comfort range and no higher than
  normal target + 2°C. Objective terms are modeled energy cost, deviation
  from normal, and switching cost. A solver result is accepted only after
  checking all constraints and, for a time-limited incumbent, a maximum 10%
  reported optimality gap. Solver status and gap are saved with each interval.
* `control/mpc_monitor.py` adapts forecasts and observations and saves plans
  in `mpc_plans` and `mpc_plan_intervals` in the thermal SQLite database.
  The former records time, model version timestamp, initial temperature,
  settings and changes in overlapping source choices; the latter records
  forecast inputs, temperatures, source choices and nominal energy/cost
  estimates. The monitor also attempts a same-model comparison with preheating
  disabled. Its `modeled_savings` is a scenario comparison, **not actual
  savings**; the no-preheat case can be infeasible.
* `control/mpc_simulation.py` provides `simulate(intervals, recent_indoor_c,
  initial_state, predictor, settings, observations)`. Pass historic inputs
  using the `Interval` and `Settings` classes and a trusted model artifact
  created before the horizon. Keep forecasts as issued at the time: using
  observed future prices/weather instead introduces hindsight. `evaluate`
  compares plan-end temperatures with nearby historical readings.
  `mpc_plan_evaluations` stores these temperature errors and whether sources
  matched **at the endpoint only**; it does not establish that the full
  interval followed the plan.

Important limitations: this prototype does not model startup/shutdown ramps,
thermostat cycling, terminal stored-heat value, or forecast uncertainty
accurately. Its one-step model-quality gate does not validate multi-hour
accuracy. Nominal capacity/COP are not measurements of heat pump power,
pellet consumption, or delivered heat; actual device energy, cost and savings
are unavailable and are not inferred from whole-home grid import. Plan
evaluation does not automatically adjust the optimizer or train the thermal
model. Validate forecasts, equipment behavior, and genuinely measured costs
over seasons before considering any future control integration.

## Weather forecast

SMHI's current public forecast API does not provide a general-weather forecast
with all of these fields, so the `weather` module uses Open-Meteo as an
alternative. It fetches hourly temperature, wind speed, relative humidity,
surface pressure, and rain forecasts for the next 36 hours by default.
The default location is Hagge, Sweden. Change `latitude` and
`longitude` in [heatautomation/heatautomation.conf](./heatautomation/heatautomation.conf)
to use another location. `get_forecast()` returns a list of hourly readings;
`get_outdoor_temp()` remains available for the heating controller.

The application logs Tibber Pulse grid import and export at startup and every
five minutes when real-time consumption is enabled for the selected Tibber
home. If it is unavailable, check the Pulse installation and the home's
real-time access in Tibber; the remaining heating control can continue.

When switching from the pellet stove to the heat pump, the controller waits
while KMP reports `LADDAR`, `TÄNDNING`, `UPPVÄRMNING`, or `HÖGEFFEKT`.
It rechecks the heating decision and stove mode on the next quarter-hour cycle.
The switch proceeds once KMP is in `VILOLÄGE, SLÄCKER NED`, `VILAR...`, or
already off. An unavailable or unknown mode also defers the switch.
Switching to the pellet stove likewise requires a recognized KMP mode; otherwise
the heat pump is left unchanged and the switch is retried on the next cycle.

## Pellet stove fan assist

Set `fan_assist_enabled = true` in the configuration, or
`HEATAUTOMATION_FAN_ASSIST=true` in the environment or `.env`, to enable the optional fan assist (disabled
by default). It checks KMP once per minute and, while its mode is `HÖGEFFEKT`,
sets Sensibo to fan mode at its highest supported fixed fan level. When KMP
leaves that mode, the assist turns the fan off if it still has the setting
the assist applied; it does not turn off a different Sensibo setting. Unknown
or unavailable KMP or Sensibo status defers action until a later check.

The fan level is determined from Sensibo's fan-mode capabilities. If the device
uses an unrecognized level name, set `SENSIBO_MAX_FAN_LEVEL` to the exact
highest fixed level reported for that device. The setting is validated against
its capabilities. Test mode only logs the actions it would take. Assist state
is held in memory; after restarting, it does not turn off a pre-existing fan
unless it observes KMP in `HÖGEFFEKT` first.

## Test mode

Run the application without sending commands to KMP or Sensibo by setting
`test_mode = true` in the `[operation]` section of the configuration. Test mode
is enabled by default. In this mode, the application
logs the device actions it would take, attempts read-only status checks for
Sensibo and KMP, and does not change device settings. Set the variable to
`false` to enable normal operation, then restart the application.
The former `HEATAUTOMATION_TEST_MODE` environment or `.env` variable is ignored;
remove it to avoid a startup warning. **Only the config file controls test mode.**

## Running on Windows

Use the **Heat Automation** launch configuration in VS Code to run or debug
the app with the project virtual environment. To run without VS Code's debugger,
use `.venv\Scripts\python.exe -m heatautomation.main` from the repository root.
PowerShell script activation is optional.

The root `Dockerfile` uses the same module entry point. Build it with the
repository root as the Docker build context. Individual integration command-line
modules must likewise be run with `python -m heatautomation.integrations.kmp`
or `python -m heatautomation.integrations.weather`, rather than by file path.
Imports have moved into `heatautomation.integrations`, `heatautomation.control`,
`heatautomation.learning`, and `heatautomation.storage`; update any external
scripts using the old top-level module paths. In particular,
`heatautomation.thermal_prediction` is now `heatautomation.learning.prediction`,
and the other `thermal_*` modules have similarly moved into `learning` without
the `thermal_` prefix. Existing SQLite databases and joblib model paths do not
need to move.

Keep service credentials in the ignored `heatautomation/.env` file and never
paste them into chats or commit them. If credentials have been exposed, revoke
or rotate them with their respective providers.
