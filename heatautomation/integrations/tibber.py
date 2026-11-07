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

import requests
import json
import logging
import sys
import websocket
from ..config import TIBBER_API_KEY
URL = "https://api.tibber.com/v1-beta/gql"

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
        websocketSubscriptionUrl
        homes {
          id
          features {
            realTimeConsumptionEnabled
          }
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
        viewer = response.json().get("data", {}).get("viewer", {})
        homes = viewer.get("homes", [])
        if not homes:
            logging.warning("No homes found in Tibber response.")
            return None

        home_id = homes[0].get("id")
        if not home_id:
            logging.warning("Home ID not found in Tibber response.")
            return None

        features = homes[0].get("features") or {}
        if features.get("realTimeConsumptionEnabled") is False:
            logging.warning(
                "Tibber real-time consumption is not enabled for this home; "
                "check the Pulse setup and Tibber account."
            )
            return None

        subscription_url = viewer.get("websocketSubscriptionUrl")
        if not subscription_url:
            logging.warning("WebSocket subscription URL not found in Tibber response.")
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
            subscription_url,
            header={"User-Agent": "heatautomation/1.0"},
            subprotocols=["graphql-transport-ws"],
            timeout=10
        )
        try:
            socket.send(json.dumps({
                "type": "connection_init",
                "payload": {"token": TIBBER_API_KEY}
            }))
            subscribed = False

            while True:
                raw_message = socket.recv()
                if not raw_message:
                    logging.error("Tibber closed the WebSocket without sending a response.")
                    return None

                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    logging.error("Tibber returned an invalid WebSocket message.")
                    return None

                if not isinstance(message, dict):
                    logging.error("Tibber returned an unexpected WebSocket message.")
                    return None

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
                    errors = message.get("payload", {}).get("errors")
                    if errors:
                        logging.error("Tibber live measurement error: %s", errors)
                        return None
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
