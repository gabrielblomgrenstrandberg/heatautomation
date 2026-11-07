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
from pathlib import Path
import websocket
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"))

TIBBER_API_KEY = os.getenv("TIBBER_API_KEY")
URL = "https://api.tibber.com/v1-beta/gql"
WEBSOCKET_URL = "wss://websocket-api.tibber.com/v1-beta/gql/subscriptions"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def get_spot_price():
    if not TIBBER_API_KEY:
        logging.critical("TIBBER_API_KEY is not set. Please check your .env file.")
        sys.exit(1)

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
        response = requests.post(URL, headers=headers, data=json.dumps(payload), timeout=10)
        response.raise_for_status()
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
                    logging.info(f"Spot price: {total_price}")
                    return total_price
                else:
                    logging.warning("Total price not found for this home.")

        logging.warning("Could not determine total price from any home.")
        return None

    except Exception as e:
        logging.error(f"Error making request to Tibber API: {e}")
        return None


def get_current_power_usage():
    if not TIBBER_API_KEY:
        logging.critical("TIBBER_API_KEY is not set. Please check your .env file.")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {TIBBER_API_KEY}",
        "Content-Type": "application/json"
    }

    query = """
    {
      viewer {
        homes {
          currentSubscription {
            liveMeasurement {
              power
              timestamp
            }
          }
        }
      }
    }
    """

    payload = {"query": query}

    try:
        response = requests.post("wss://websocket-api.tibber.com/v1-beta/gql/subscriptions", headers=headers, data=json.dumps(payload), timeout=10)
        response.raise_for_status()
        data = response.json()

        homes = data.get("data", {}).get("viewer", {}).get("homes", [])
        if not homes:
            logging.warning("No homes found in Tibber response.")
            return None

        for home in homes:
            current_subscription = home.get("currentSubscription", {})
            if current_subscription:
                live_measurement = current_subscription.get("liveMeasurement", {})
                power = live_measurement.get("power")
                if power is not None:
                    logging.info(f"Current power usage: {power} W")
                    return power
                else:
                    logging.warning("Live power data not found for this home.")

        logging.warning("Could not determine live power usage from any home.")
        return None

    except Exception as e:
        logging.error(f"Error making request to Tibber API: {e}")
        return None


def get_grid_import_export():
    """Return the latest Tibber Pulse grid import/export readings in watts."""
    if not TIBBER_API_KEY:
        logging.critical("TIBBER_API_KEY is not set. Please check your .env file.")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {TIBBER_API_KEY}",
        "Content-Type": "application/json"
    }
    homes_query = """
    query {
      viewer {
        homes {
          id
        }
      }
    }
    """

    try:
        response = requests.post(
            URL,
            headers=headers,
            json={"query": homes_query},
            timeout=10
        )
        response.raise_for_status()
        homes = response.json().get("data", {}).get("viewer", {}).get("homes", [])
        if not homes:
            logging.warning("No homes found in Tibber response.")
            return None

        home_id = homes[0].get("id")
        if not home_id:
            logging.warning("Home ID not found in Tibber response.")
            return None

        measurement_query = """
        subscription {
          liveMeasurement(homeId: %s) {
            power
            powerProduction
          }
        }
        """ % json.dumps(home_id)
        socket = websocket.create_connection(
            WEBSOCKET_URL,
            header=headers,
            subprotocols=["graphql-transport-ws"],
            timeout=10
        )
        try:
            socket.send(json.dumps({"type": "connection_init"}))
            subscribed = False

            while True:
                message = json.loads(socket.recv())
                message_type = message.get("type")

                if message_type == "ping":
                    socket.send(json.dumps({
                        "type": "pong",
                        "payload": message.get("payload")
                    }))
                elif message_type == "connection_ack" and not subscribed:
                    socket.send(json.dumps({
                        "id": "1",
                        "type": "subscribe",
                        "payload": {"query": measurement_query}
                    }))
                    subscribed = True
                elif message_type == "next":
                    measurement = (
                        message.get("payload", {})
                        .get("data", {})
                        .get("liveMeasurement")
                    )
                    if not measurement:
                        logging.warning("Live Pulse measurement not found in Tibber response.")
                        return None

                    grid_import = measurement.get("power")
                    grid_export = measurement.get("powerProduction")
                    logging.info(
                        "Tibber Pulse grid import: %s W, grid export: %s W",
                        grid_import,
                        grid_export
                    )
                    return {
                        "grid_import_w": grid_import,
                        "grid_export_w": grid_export
                    }
                elif message_type in ("error", "connection_error"):
                    logging.error("Tibber WebSocket error: %s", message.get("payload"))
                    return None
                elif message_type == "complete":
                    logging.warning("Tibber ended the live measurement subscription without data.")
                    return None
        finally:
            socket.close()

    except (requests.RequestException, websocket.WebSocketException, OSError, ValueError) as e:
        logging.error("Error retrieving Tibber Pulse measurements: %s", e)
        return None


if __name__ == "__main__":
    spot_price = get_spot_price()
    if spot_price is not None:
        print(f"Current spot price: {spot_price} kr/kWh")
    else:
        print("Failed to retrieve spot price.")

    power_usage = get_current_power_usage()
    if power_usage is not None:
        print(f"Current power usage: {power_usage} W")
    else:
        print("Failed to retrieve power usage.")
