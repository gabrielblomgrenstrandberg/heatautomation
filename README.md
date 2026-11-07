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

## Test mode

Run the application without sending commands to KMP or Sensibo by setting
`HEATAUTOMATION_TEST_MODE=true` in the environment (or in the `.env` file) before
starting it. In this mode, the application logs the device actions it would
take and skips device connection checks. Set the variable to `false` or remove
it to enable normal operation.
