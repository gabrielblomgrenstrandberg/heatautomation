# Heat Automation; A program that selects the best heat source based on spot price and outdoor temperature
# Copyright (C) 2025  Gabriel Blomgren Strandberg <gabriel.blomgren.strandberg@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# module: weather.py - Weather forecasts for the configured location.

import logging

import requests

if __package__:
    from .config import WEATHER_LATITUDE, WEATHER_LONGITUDE, WEATHER_TIMEZONE, FORECAST_HOURS
else:
    from config import WEATHER_LATITUDE, WEATHER_LONGITUDE, WEATHER_TIMEZONE, FORECAST_HOURS

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
def get_forecast():
    """Fetch the configured number of hourly weather forecasts.

    Returns a list of hourly readings with time, temperature (C), wind speed
    (km/h), relative humidity (%), surface pressure (hPa), and rain (mm).
    """
    params = {
        "latitude": WEATHER_LATITUDE,
        "longitude": WEATHER_LONGITUDE,
        "hourly": (
            "temperature_2m,wind_speed_10m,relative_humidity_2m,"
            "surface_pressure,rain"
        ),
        "forecast_hours": FORECAST_HOURS,
        "timezone": WEATHER_TIMEZONE,
    }

    try:
        response = requests.get(FORECAST_URL, params=params, timeout=15)
        response.raise_for_status()
        hourly = response.json()["hourly"]
        times = hourly["time"]
        forecast = []

        for index, forecast_time in enumerate(times):
            forecast.append({
                "time": forecast_time,
                "temperature_c": hourly["temperature_2m"][index],
                "wind_speed_kmh": hourly["wind_speed_10m"][index],
                "relative_humidity_percent": hourly["relative_humidity_2m"][index],
                "surface_pressure_hpa": hourly["surface_pressure"][index],
                "rain_mm": hourly["rain"][index],
            })

        logging.info("Fetched %s hourly weather forecasts.", len(forecast))
        return forecast
    except (requests.RequestException, KeyError, TypeError, ValueError, IndexError) as error:
        logging.error("Error fetching weather forecast: %s", error)
        return None


def get_outdoor_temp():
    """Return the current-hour forecast temperature for existing callers."""
    forecast = get_forecast()
    if not forecast:
        return None

    temperature = forecast[0]["temperature_c"]
    if temperature is None:
        logging.warning("Current-hour temperature is not available.")
        return None

    return int(temperature)


if __name__ == "__main__":
    weather_forecast = get_forecast()
    if weather_forecast:
        for forecast_hour in weather_forecast:
            logging.info("%s: %s", forecast_hour["time"], forecast_hour)
    else:
        logging.error("Failed to fetch the weather forecast.")
