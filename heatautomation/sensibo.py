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

# module: sensibo.py – A module for interacting with the Sensibo API to control air conditioning units (heat pump).

import os
import requests
import logging

# Static global variables
DEVICE_ID = os.getenv("SENSIBO_DEVICE_ID")
POD_URL = f"https://home.sensibo.com/api/v2/pods/{DEVICE_ID}"
URL = f"{POD_URL}/acStates"
SENSIBO_API_KEY = os.getenv("SENSIBO_API_KEY")

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def handle_error(response):
    logging.error(f"Error {response.status_code}: {response.text}")


def status():
    # Define the parameters to include in the request
    params = {
        'fields': 'acState,mainMeasurementsSensor',
        'apiKey': SENSIBO_API_KEY
    }

    try:
        response = requests.get(POD_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logging.error("Could not retrieve Sensibo status: %s", e)
    except ValueError as e:
        logging.error("Sensibo returned invalid status data: %s", e)
    return None


def _first_result(data, description):
    if not isinstance(data, dict):
        logging.warning("Sensibo %s data is unavailable.", description)
        return None

    results = data.get('result')
    if isinstance(results, dict):
        return results
    if not isinstance(results, list) or not results:
        logging.warning("Sensibo returned no %s data.", description)
        return None

    result = results[0]
    if not isinstance(result, dict):
        logging.error("Sensibo returned an invalid %s entry.", description)
        return None
    return result


def send_post_request(data):
    headers = {'Content-Type': 'application/json'}
    response = requests.post(URL, headers=headers, json=data, params={'apiKey': SENSIBO_API_KEY}, timeout=10)

    if response.status_code == 200:
        logging.info("AC state updated successfully!")
        return response.json()
    else:
        handle_error(response)
        return None

def on(mode="heat"):
    data = {"acState": {"on": True, "mode": mode}}
    return send_post_request(data)

def off():
    data = {"acState": {"on": False}}
    return send_post_request(data)

def setTemp(temp):
    data = {"acState": {"targetTemperature": temp}}
    return send_post_request(data)        

def getTemp():
    result = _first_result(status(), "temperature")
    if result is None:
        return None

    sensor = result.get('mainMeasurementsSensor')
    measurements = sensor.get('measurements') if isinstance(sensor, dict) else None
    if not isinstance(measurements, dict) or 'temperature' not in measurements:
        logging.warning("Sensibo temperature data is unavailable.")
        return None
    return measurements['temperature']


def getSystemStatus():
    data = status()  # Using the status function
    result = _first_result(data, "AC state")
    if result is None:
        return None

    ac_state = result.get('acState')
    if not isinstance(ac_state, dict) or 'on' not in ac_state:
        logging.warning("Sensibo AC state is missing its power status.")
        return None

    return {
        "is_on": ac_state['on'],
        "mode": ac_state.get('mode'),
        "target_temperature": ac_state.get('targetTemperature', 'N/A')
    }

def check_connection():
    """
    Check if the Sensibo API is reachable.
    Returns True if reachable, False otherwise.
    """
    try:
        response = requests.get(POD_URL, params={'apiKey': SENSIBO_API_KEY}, timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error checking connection: {e}")
        return False