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

# module: tibber.py – A module for interacting with the Tibber API to fetch spot prices.

import os
import requests
import json
import logging
import sys
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
from dotenv import load_dotenv
load_dotenv()

TIBBER_API_KEY = os.getenv("TIBBER_API_KEY")
URL = "https://api.tibber.com/v1-beta/gql"


def get_spot_price():
    if not TIBBER_API_KEY:
      logging.critical("TIBBER_API_KEY is not set. Please check your .env file.")
      sys.exit(1)

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

    payload = {
        "query": query
    }

    try:
        response = requests.post(URL, headers=headers, data=json.dumps(payload), timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error making request to Tibber API: {e}")
        return None

    data = response.json()

    homes = data.get("data", {}).get("viewer", {}).get("homes", [])

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

# For testing only
if __name__ == "__main__":
    print(get_spot_price())