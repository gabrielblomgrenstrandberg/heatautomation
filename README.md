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
