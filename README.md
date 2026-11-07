# heatautomation
A script for selecting the best heat source based on spot price and temperature

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

* `pellet_cost_per_kwh`: your actual cost in SEK per delivered kWh of heat,
  including fuel, efficiency losses, and any relevant stove electricity.
* `cop_by_temperature`: measured or manufacturer COP values for your heat pump
  at representative outdoor temperatures and operating conditions.
* `demand_by_temperature`: estimated heat demand in kW at those temperatures.
  Calibrate it to your building; it is not measured by this application.

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
use `.venv\Scripts\python.exe heatautomation\main.py` from the repository root.
PowerShell script activation is optional.

Keep service credentials in the ignored `heatautomation/.env` file and never
paste them into chats or commit them. If credentials have been exposed, revoke
or rotate them with their respective providers.
