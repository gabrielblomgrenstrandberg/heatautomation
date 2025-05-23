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

# module: solis.py – A module for interacting with the Solis Cloud API for solar production data.

# module: solis.py – A module for interacting with the Solis Cloud API for solar production data.

import requests
import hashlib
import hmac
import time
import os
import base64
from dotenv import load_dotenv
import logging

# Ladda miljövariabler
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")



def generate_signature(secret, timestamp, path):
    raw = f"{KEY_ID}{timestamp}{path}"
    hashed = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(hashed.digest()).decode("utf-8")

def get_current_production():
    timestamp = str(int(time.time() * 1000))
    request_path = "/v1/api/stationRealKpi"

    sign = generate_signature(KEY_SECRET, timestamp, request_path)

    headers = {
        "Content-Type": "application/json",
        "keyId": KEY_ID,
        "sign": sign,
        "timestamp": timestamp,
        "version": "1.0"
    }

    payload = {
        "stationId": STATION_ID
    }

    try:
        response = requests.post(f"{BASE_URL}{request_path}", json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            power = data.get("data", {}).get("pac")
            if power is not None:
                logging.info(f"Aktuell solelsproduktion: {power} W")
                return power
            else:
                logging.warning("Fältet 'pac' saknas i svaret.")
                return None
        else:
            logging.error(f"API-svar indikerar fel: {data.get('msg')}")
            return None

    except Exception as e:
        logging.error(f"Fel vid anrop till SolisCloud API: {e}")
        return None
    
def list_stations():
    request_path = "/v1/api/stationList"
    timestamp = str(int(time.time() * 1000))

    string_to_sign = f"{KEY_ID}{timestamp}{request_path}"
    signature = hmac.new(KEY_SECRET.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1)
    sign = base64.b64encode(signature.digest()).decode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "keyId": KEY_ID,
        "sign": sign,
        "timestamp": timestamp,
        "version": "1.0"
    }

    try:
        res = requests.post(f"{BASE_URL}{request_path}", json={}, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        logging.info("Stationslista hämtad:")
        logging.info(data)
        return data
    except Exception as e:
        logging.error(f"Fel vid anrop till stationList: {e}")
        return None



# Testkörning
if __name__ == "__main__":
    KEY_ID = ""         # Din keyId från SolisCloud
    KEY_SECRET = "" # Din keySecret från SolisCloud
    STATION_ID = "" # Station ID
    BASE_URL = "https://www.soliscloud.com"
    if not KEY_ID or not KEY_SECRET:
        logging.error("KEY_ID eller KEY_SECRET saknas! Kontrollera .env-filen.")
    print(KEY_ID)
    print(KEY_SECRET)
    list_stations() 
    current_power = get_current_production()
    if current_power is not None:
        print(f"Aktuell solelsproduktion: {current_power} W")
    else:
        print("Kunde inte hämta produktion.")
