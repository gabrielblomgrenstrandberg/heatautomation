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
from datetime import datetime, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Retry helper function with dynamic delays
def retry_function(func, retries=3, delay=5):
    """Tries a function with retries in case of failure."""
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                next_delay = delay * (2 ** attempt)  # Exponential backoff
                logging.info(f"Retrying in {next_delay} seconds...")
                time.sleep(next_delay)
            else:
                logging.error("Max retries reached. Function failed.")
                return None

def get_spot_price_safe():
    """Safely fetches the spot price from Tibber with retry logic."""
    return retry_function(tibber.get_spot_price)

def wait_until_next_quarter():
    """Waits until the next full quarter hour (00, 15, 30, 45)."""
    now = datetime.now()
    
    # Calculate the next quarter hour
    next_quarter_minute = (now.minute // 15 + 1) * 15
    if next_quarter_minute == 60:
        # If next quarter is 60, reset to next hour and set minute to 00
        next_quarter_minute = 0
        if now.hour == 23:
            # If it's 23:59, set to 00:00 of the next day
            next_time = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            # Otherwise, just increment the hour
            next_time = now.replace(hour=now.hour + 1, minute=next_quarter_minute, second=0, microsecond=0)
    else:
        # Otherwise, just update the minute to the next quarter
        next_time = now.replace(minute=next_quarter_minute, second=0, microsecond=0)
    
    # If next time exceeds 23:59 (midnight), we need to move to the next day
    if next_time.hour == 24:
        next_time = next_time.replace(hour=0, minute=0)
    
    # Calculate the sleep time
    wait_time = (next_time - now).total_seconds()
    logging.info(f"Waiting for {wait_time:.2f} seconds until the next quarter hour.")
    
    # Sleep until next quarter hour
    time.sleep(wait_time)

def evaluate_heater(spot_price):
    adjusted_price = 2.99 - 0.94875  # Fixed cost adjustment
    logging.info(f"Spot price: {spot_price}, adjusted price: {adjusted_price}")
    if spot_price <= adjusted_price:
        logging.info("Electricity is cheaper than pellets.")
        return "heatpump"
    else:
        logging.info("The pellet stove is cheaper than the heatpump.")
        return "pelletstove"

def check_systems():
    """Check if systems are available and functioning."""
    try:
        sensibo_status = sensibo.check_connection()
        kmp_status = kmp.check_connection()

        if not sensibo_status and not kmp_status:
            logging.error("Both systems are down.")
        elif not sensibo_status:
            logging.warning("Sensibo is down. Using pellet stove.")
            kmp.on()
        elif not kmp_status:
            logging.warning("KMP is down. Using heat pump.")
            sensibo.on()

        return sensibo_status, kmp_status
    except Exception as e:
        logging.error(f"Error checking system connections: {e}")
        return False, False

def main_loop():
    heatType = "none"

    while True:
        # Get the spot price and evaluate the best heating system
        spot_price = get_spot_price_safe()
        outdoor_temp = smhi.get_outdoor_temp()
        if spot_price is None:
            logging.warning("No spot price available. Skipping this cycle.")
            time.sleep(30)  # Retry sooner, adjust based on needs
            continue
        else:
            heater_type = evaluate_heater_with_temperature(outdoor_temp, spot_price, max_price_threshold=3.0)
            if heater_type == "heatpump":
                if heatType != "heatpump":
                    logging.info("Starting the heat pump...")
                    try:
                        sensibo.on()
                        kmp.off()
                        heatType = "heatpump"
                    except Exception as e:
                        logging.error(f"Error starting the heat pump: {e}")
                else:
                    logging.info("Heat pump is already running.")
            else:
                if heatType != "pelletstove":
                    logging.info("Starting the pellet stove...")
                    try:
                        kmp.on()
                        sensibo.off()
                        heatType = "pelletstove"
                    except Exception as e:
                        logging.error(f"Error starting the pellet stove: {e}")
                else:
                    logging.info("Pellet stove is already running.")

        # Check system status periodically (could be adjusted for more frequent checks)
        sensibo_status, kmp_status = check_systems() # TODO
        if not sensibo_status or not kmp_status:
            logging.warning("One or more systems are unavailable. Taking necessary action.")

        # Wait until the next full quarter hour (00, 15, 30, 45)
        wait_until_next_quarter()

def get_effective_heating_capacity(outdoor_temp):
    """Calculates the heating effect based on outdoor temperature according to specification."""
    if outdoor_temp >= 0:
        return 6.5  # Max heating effect
    elif outdoor_temp >= -12.5:
        return 4.0  # Heating effect at -10°C
    elif outdoor_temp >= -17.5:
        return 3.6  # Heating effect at -15°C
    else:
        return 2.6  # Heating effect at -20°C

def evaluate_heater_with_temperature(outdoor_temp, spot_price, max_price_threshold=3.0):
    """Evaluates whether the heat pump or the pellet stove is more effective based on outdoor temperature and spot price."""
    effective_heating_capacity = get_effective_heating_capacity(outdoor_temp)
    adjusted_price = max_price_threshold - 0.94875  # Dynamic price adjustment

    if spot_price <= adjusted_price and effective_heating_capacity >= 3.0:
        logging.info(f"The heat pump is the most effective at {outdoor_temp}°C with {effective_heating_capacity} kW heating effect.")
        return "heatpump"
    else:
        logging.info(f"The pellet stove is more effective than the heat pump at {outdoor_temp}°C with 5 kW heating effect.")
        return "pelletstove"
    
def calculate_energy_cost_with_scop(spot_price, heating_capacity, scop=3.8):
    """Calculates energy use based on SCOP (Seasonal Coefficient of Performance)."""
    # Effective energy use per kWh
    energy_used = heating_capacity / scop
    # Calculating the cost for generating the necessary amount of heat
    cost = energy_used * spot_price
    logging.info(f"To generate {heating_capacity} kWh heat, {energy_used:.2f} kWh electricity is used. Cost: {cost:.2f} SEK.")
    return cost

def optimize_heating_system(outdoor_temp, spot_price):
    heater_type = evaluate_heater_with_temperature(outdoor_temp, spot_price)
    if heater_type == "heatpump":
        heating_capacity = get_effective_heating_capacity(outdoor_temp)
        cost = calculate_energy_cost_with_scop(spot_price, heating_capacity)
        threshold_cost = (2.99 - 0.94875)  # Adjust this threshold as needed
        if cost > threshold_cost:
            logging.info("The heat pump is too expensive, starting the pellet stove instead.")
            return "pelletstove"
        else:
            return "heatpump"
    else:
        return "pelletstove"

if __name__ == "__main__":
    main_loop()
