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

# module: tibber.py – A module for interacting with the Tibber API to fetch spot prices and Pulse data.

import os
import requests
import json
import logging
import sys
from dotenv import load_dotenv

# Ladda miljövariabler
load_dotenv()
TIBBER_API_KEY = os.getenv("TIBBER_API_KEY")
URL = "https://api.tibber.com/v1-beta/gql"

# Kontrollera API-nyckel direkt
if not TIBBER_API_KEY:
    logging.critical("TIBBER_API_KEY is not set. Please check your .env file.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_spot_price():
    """
    Fetches the current spot price from the Tibber API.
    Returns:
        float: The current spot price, or None if not found.
    """
    headers = {
        "Authorization": f"Bearer {TIBBER_API_KEY}",
        "Content-Type": "application/json"
    }
    query = """
    {
      viewer {
        homes {
          currentSubscription {
            priceInfo {
              current {
                total
                energy
                tax
                startsAt
              }
            }
          }
        }
      }
    }
    """

    payload = {"query": query}

    try:
        response = requests.post(URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error making request to Tibber API: {e}")
        return None

    data = response.json()
    if "data" not in data or "viewer" not in data["data"]:
        logging.error("Malformed response: missing 'data.viewer'")
        return None

    homes = data["data"]["viewer"].get("homes", [])
    if not homes:
        logging.warning("No homes found in Tibber response.")
        return None

    for home in homes:
        current_subscription = home.get("currentSubscription", {})
        if current_subscription:
            total_price = current_subscription.get("priceInfo", {}).get("current", {}).get("total")
            if total_price is not None:
                logging.info(f"Total price: {total_price}")
                return total_price
            else:
                logging.warning("Total price not found for this home.")

    logging.warning("Could not determine total price from any home.")
    return None

def get_live_power():
    """
    Fetches current power usage (and solar production if net metering is used) via Tibber Pulse.
    Returns:
        float: Current power in watts, or None.
    """
    headers = {
        "Authorization": f"Bearer {TIBBER_API_KEY}",
        "Content-Type": "application/json"
    }
    query = """
    {
      viewer {
        homes {
          consumptionLive {
            power
            timestamp
          }
        }
      }
    }
    """

    payload = {"query": query}

    try:
        response = requests.post(URL, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        homes = data.get("data", {}).get("viewer", {}).get("homes", [])
        if not homes:
            logging.warning("No homes found in Tibber response.")
            return None

        power = homes[0].get("consumptionLive", {}).get("power")
        if power is not None:
            logging.info(f"Current live power: {power} W")
            return power
        else:
            logging.warning("Power data not available in consumptionLive.")
            return None

    except Exception as e:
        logging.error(f"Error fetching live power: {e}")
        return None

# For testing only
if __name__ == "__main__":
    price = get_spot_price()
    if price is not None:
        print(f"Current spot price: {price} kr/kWh")
    else:
        print("Failed to retrieve spot price.")

    power = get_live_power()
    if power is not None:
        print(f"Current power usage: {power} W")
    else:
        print("Failed to retrieve power usage.")
