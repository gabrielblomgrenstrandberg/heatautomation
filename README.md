# heatautomation
A script for selecting the best heat source based on spot price and temperature

## Weather forecast

SMHI's current public forecast API does not provide a general-weather forecast
with all of these fields, so the `weather` module uses Open-Meteo as an
alternative. It fetches hourly temperature, wind speed, relative humidity,
surface pressure, and rain forecasts for the next 36 hours.
The default location is Hagge, Sweden. Change `WEATHER_LATITUDE` and
`WEATHER_LONGITUDE` in [heatautomation/config.py](./heatautomation/config.py)
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

Set `HEATAUTOMATION_FAN_ASSIST=true` to enable the optional fan assist (disabled
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
`HEATAUTOMATION_TEST_MODE=true` in the environment (or in the `.env` file) before
starting it. Test mode is enabled by default. In this mode, the application
logs the device actions it would take, attempts read-only status checks for
Sensibo and KMP, and does not change device settings. Set the variable to
`false` to enable normal operation.

## Running on Windows

Use the **Heat Automation** launch configuration in VS Code to run or debug
the app with the project virtual environment. To run without VS Code's debugger,
use `.venv\Scripts\python.exe heatautomation\main.py` from the repository root.
PowerShell script activation is optional.

Keep service credentials in the ignored `heatautomation/.env` file and never
paste them into chats or commit them. If credentials have been exposed, revoke
or rotate them with their respective providers.
