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

# main.py - Main script for the Heat Automation program.

import tibber
import sensibo
import kmp
import smhi
import time
from datalogger import log_data
from datetime import datetime, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Retry helper function with exponential backoff
def retry_function(func, retries=3, delay=5):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))
            else:
                logging.error("Max retries reached. Function failed.")
                return None

# Fetch spot price with retry logic
def get_spot_price_safe():
    return retry_function(tibber.get_spot_price)

# Wait until the next full quarter hour
def wait_until_next_quarter():
    now = datetime.now()
    next_quarter_minute = (now.minute // 15 + 1) * 15
    if next_quarter_minute == 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_quarter_minute, second=0, microsecond=0)
    time.sleep((next_time - now).total_seconds())

# Calculate heating capacity based on outdoor temperature
def get_effective_heating_capacity(outdoor_temp):
    if outdoor_temp >= 0:
        return 6.5
    elif outdoor_temp >= -12.5:
        return 4.0
    elif outdoor_temp >= -17.5:
        return 3.6
    else:
        return 2.6

# Calculate cost based on SCOP
def calculate_energy_cost_with_scop(spot_price, heating_capacity, scop=3.8):
    energy_used = heating_capacity / scop
    return energy_used * spot_price

# Turn on heat pump with retry logic
def turn_on_heat_pump():
    retry_function(sensibo.on)
    retry_function(kmp.off)

# Turn on pellet stove with retry logic
def turn_on_pellet_stove():
    retry_function(kmp.on)
    retry_function(sensibo.off)

# Unified decision logic for heating source
def decide_heating_source(outdoor_temp, spot_price, max_price_threshold=3.0, scop=3.8):
    heating_capacity = get_effective_heating_capacity(outdoor_temp)
    adjusted_threshold = max_price_threshold - 0.94875
    electricity_cost = calculate_energy_cost_with_scop(spot_price, heating_capacity, scop)

    if electricity_cost <= adjusted_threshold and heating_capacity >= 3.0:
        logging.info(f"Heat pump selected at {outdoor_temp}°C, {heating_capacity} kW, cost: {electricity_cost:.2f} SEK.")
        return "heatpump", heating_capacity
    else:
        logging.info(f"Pellet stove selected at {outdoor_temp}°C. Electricity cost too high or capacity too low.")
        return "pelletstove", 5.0

# System availability check
def check_systems():
    try:
        sensibo_status = sensibo.check_connection()
        kmp_status = kmp.check_connection()
        return sensibo_status, kmp_status
    except Exception as e:
        logging.error(f"Error checking systems: {e}")
        return False, False

# Main program loop
def main_loop():
    current_heat_type = "none"

    while True:
        spot_price = get_spot_price_safe()
        if spot_price is None:
            logging.warning("No spot price available. Skipping cycle.")
            time.sleep(30)
            continue

        outdoor_temp = smhi.get_outdoor_temp()
        heating_type, heating_capacity = decide_heating_source(outdoor_temp, spot_price)

        if heating_type == "heatpump" and current_heat_type != "heatpump":
            logging.info("Activating heat pump...")
            turn_on_heat_pump()
            current_heat_type = "heatpump"
        elif heating_type == "pelletstove" and current_heat_type != "pelletstove":
            logging.info("Activating pellet stove...")
            turn_on_pellet_stove()
            current_heat_type = "pelletstove"
        else:
            logging.info(f"{heating_type.capitalize()} is already active.")

        # System availability monitoring
        sensibo_status, kmp_status = check_systems()
        if not sensibo_status or not kmp_status:
            logging.warning("One or more systems unavailable. Action taken.")

        # Log data for ML training
        log_data(datetime.now(), spot_price, outdoor_temp, heating_type, heating_capacity)

        wait_until_next_quarter()

if __name__ == "__main__":
    main_loop()
    logging.info("Starting main loop...")