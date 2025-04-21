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
URL = f"https://home.sensibo.com/api/v2/pods/{DEVICE_ID}/acStates" # Sensibo API endpoint for fetching user pod information
SENSIBO_API_KEY = os.getenv("SENSIBO_API_KEY")

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def handle_error(response):
    logging.error(f"Error {response.status_code}: {response.text}")


def status():
    # Define the parameters to include in the request
    params = {
        'fields': '*',  # Fetch all fields
        'apiKey': SENSIBO_API_KEY
    }
    
    # Send a GET request to the Sensibo API
    response = requests.get(URL, params=params, timeout=10)

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        data = response.json()
        return data
    else:
        # If the request failed, print the status code and error message
        print(f"Failed to retrieve data. Status code: {response.status_code}")
        return None

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
    params = {'fields': '*', 'apiKey': SENSIBO_API_KEY}
    response = requests.get(URL, params=params, timeout=10)

    if response.status_code == 200:
        data = response.json()
        try:
            target_temperature = data['result'][0]['mainMeasurementsSensor']['measurements']['temperature']
            return target_temperature
        except KeyError:
            logging.error("Temperature data not found in the response")
            return None
    else:
        handle_error(response)
        return None
    

def getSystemStatus():
    data = status()  # Using the status function
    if data:
        ac_state = data['result'][0]['acState']
        return {
            "is_on": ac_state['on'],
            "mode": ac_state['mode'],
            "target_temperature": ac_state.get('targetTemperature', 'N/A')
        }
    return None

def check_connection():
    """
    Check if the Sensibo API is reachable.
    Returns True if reachable, False otherwise.
    """
    try:
        response = requests.get(URL, params={'apiKey': SENSIBO_API_KEY}, timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Error checking connection: {e}")
        return False