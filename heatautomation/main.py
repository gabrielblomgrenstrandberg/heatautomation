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
import weather
import time
import config
import threading
from datalogger import log_data
from datetime import datetime, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

GRID_POLL_INTERVAL_SECONDS = config.GRID_POLL_INTERVAL_SECONDS
FAN_POLL_INTERVAL_SECONDS = config.FAN_POLL_INTERVAL_SECONDS
FAN_ASSIST_ENABLED = config.FAN_ASSIST_ENABLED
CONTROL_LOCK = threading.RLock()

TEST_MODE = config.TEST_MODE

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
    for minimum_temperature, capacity in config.HEAT_PUMP_CAPACITY_BY_TEMPERATURE:
        if outdoor_temp >= minimum_temperature:
            return capacity
    return config.HEAT_PUMP_CAPACITY_BELOW_MINIMUM

# Calculate cost based on SCOP
def calculate_energy_cost_with_scop(spot_price, heating_capacity, scop=config.HEAT_PUMP_SCOP):
    energy_used = heating_capacity / scop
    return energy_used * spot_price

# Turn on heat pump with retry logic
def turn_on_heat_pump():
    with CONTROL_LOCK:
        return _turn_on_heat_pump()


def _turn_on_heat_pump():
    if TEST_MODE:
        status = kmp.get_status()
        if not status or not kmp.can_turn_off(status.get("mode")):
            logging.warning("TEST MODE: heat pump switch deferred; KMP mode is not safe.")
            return False
        logging.info("TEST MODE: would turn off KMP and turn on Sensibo.")
        return True
    if not retry_function(kmp.off):
        logging.warning("Heat pump switch deferred; KMP was not turned off.")
        return False
    if retry_function(sensibo.on) is None:
        logging.error("Heat pump switch failed: Sensibo could not be turned on.")
        return False
    return True

# Turn on pellet stove with retry logic
def turn_on_pellet_stove():
    with CONTROL_LOCK:
        return _turn_on_pellet_stove()


def _turn_on_pellet_stove():
    if TEST_MODE:
        status = kmp.get_status()
        if not status or not kmp.can_turn_on(status.get("mode")):
            logging.warning("TEST MODE: pellet stove switch deferred; KMP mode is unknown.")
            return False
        logging.info("TEST MODE: would turn on KMP and turn off Sensibo.")
        return True
    if not retry_function(kmp.on):
        logging.warning("Pellet stove switch deferred; KMP could not be turned on.")
        return False
    if FAN_ASSIST_ENABLED and fan_assist.active:
        state = sensibo.get_fan_state()
        if state is None:
            logging.warning("Pellet stove switch deferred; Sensibo state is unavailable.")
            return False
        if (state.get("on") is True and state.get("mode") == "fan"
                and state.get("fanLevel") == fan_assist.fan_level):
            logging.info("Leaving Sensibo fan assist running while the pellet stove is active.")
            return True
    if retry_function(sensibo.off) is None:
        logging.error("Pellet stove switch failed: Sensibo could not be turned off.")
        return False
    return True


class FanAssist:
    def __init__(self):
        self.active = False
        self.fan_level = None

    def poll(self):
        with CONTROL_LOCK:
            status = kmp.get_status()
            mode = status.get("mode") if status else None
            known_modes = kmp.RUNNING_MODES | kmp.IDLE_MODES | kmp.OFF_MODES
            if mode not in known_modes:
                logging.warning("Fan assist deferred: KMP mode is unavailable or unknown.")
                return

            if mode == "HÖGEFFEKT":
                self._start_or_maintain()
            elif self.active:
                self._stop()

    def _start_or_maintain(self):
        level = sensibo.get_fan_level()
        if level is None:
            return
        state = sensibo.get_fan_state()
        if state is None:
            return
        if state.get("on") is True and state.get("mode") == "fan" and state.get("fanLevel") == level:
            self.active = True
            self.fan_level = level
            return
        if TEST_MODE:
            logging.info("TEST MODE: would turn on Sensibo fan at level %s.", level)
        elif retry_function(lambda: sensibo.fan_on(level)) is None:
            logging.error("Fan assist could not turn on the Sensibo fan.")
            return
        self.active = True
        self.fan_level = level

    def _stop(self):
        state = sensibo.get_fan_state()
        if state is None:
            return
        if (state.get("on") is True and state.get("mode") == "fan"
                and state.get("fanLevel") == self.fan_level):
            if TEST_MODE:
                logging.info("TEST MODE: would turn off the Sensibo fan.")
            elif retry_function(sensibo.off) is None:
                logging.error("Fan assist could not turn off the Sensibo fan.")
                return
        elif TEST_MODE:
            logging.info("TEST MODE: would stop Sensibo fan assist.")
        self.active = False
        self.fan_level = None


fan_assist = FanAssist()


def fan_monitor_loop():
    next_poll = time.monotonic()
    while True:
        sleep_seconds = next_poll - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        fan_assist.poll()
        next_poll += FAN_POLL_INTERVAL_SECONDS
        if next_poll <= time.monotonic():
            next_poll = time.monotonic() + FAN_POLL_INTERVAL_SECONDS


# Unified decision logic for heating source
def decide_heating_source(
    outdoor_temp, spot_price, max_price_threshold=config.MAX_PRICE_THRESHOLD,
    scop=config.HEAT_PUMP_SCOP
):
    heating_capacity = get_effective_heating_capacity(outdoor_temp)
    if config.spotPriceLimit > 0:
        if spot_price > config.spotPriceLimit:
            logging.info(
                "Pellet stove selected: spot price %.4f SEK/kWh exceeds "
                "the configured limit %.4f SEK/kWh.", spot_price, config.spotPriceLimit
            )
            return "pelletstove", config.PELLET_STOVE_CAPACITY
        logging.info(
            "Heat pump selected: spot price %.4f SEK/kWh is at or below "
            "the configured limit %.4f SEK/kWh.", spot_price, config.spotPriceLimit
        )
        return "heatpump", heating_capacity

    adjusted_threshold = max_price_threshold - config.PELLET_PRICE_ADJUSTMENT
    electricity_cost = calculate_energy_cost_with_scop(spot_price, heating_capacity, scop)

    cost_is_too_high = electricity_cost > adjusted_threshold
    capacity_is_too_low = heating_capacity < config.MIN_HEAT_PUMP_CAPACITY
    if not cost_is_too_high and not capacity_is_too_low:
        logging.info(
            "Heat pump selected: estimated electricity cost %.2f SEK is within "
            "the %.2f SEK threshold and capacity %.1f kW meets the %.1f kW minimum "
            "at %s°C.",
            electricity_cost,
            adjusted_threshold,
            heating_capacity,
            config.MIN_HEAT_PUMP_CAPACITY,
            outdoor_temp
        )
        return "heatpump", heating_capacity

    reasons = []
    if cost_is_too_high:
        reasons.append(
            "estimated electricity cost %.2f SEK exceeds the %.2f SEK threshold"
            % (electricity_cost, adjusted_threshold)
        )
    if capacity_is_too_low:
        reasons.append(
            "heating capacity %.1f kW is below the %.1f kW minimum"
            % (heating_capacity, config.MIN_HEAT_PUMP_CAPACITY)
        )
    logging.info(
        "Pellet stove selected at %s°C because %s.",
        outdoor_temp,
        " and ".join(reasons)
    )
    return "pelletstove", config.PELLET_STOVE_CAPACITY

# System availability check
def check_systems():
    sensibo_available = False
    try:
        sensibo_status = sensibo.getSystemStatus()
        if sensibo_status is None:
            logging.warning("Sensibo status unavailable.")
        else:
            sensibo_available = True
            logging.info(
                "Sensibo status: on=%s, mode=%s, target temperature=%s°C.",
                sensibo_status["is_on"],
                sensibo_status["mode"],
                sensibo_status["target_temperature"]
            )
            current_temperature = sensibo.getTemp()
            if current_temperature is not None:
                logging.info("Sensibo measured temperature: %s°C.", current_temperature)
    except Exception as e:
        logging.error("Could not read Sensibo status: %s", e)

    kmp_available = False
    try:
        kmp_status = kmp.get_status()
        if kmp_status is None:
            logging.warning("KMP status unavailable.")
        else:
            kmp_available = True
            logging.info("KMP pellet stove mode: %s.", kmp_status["mode"])
    except Exception as e:
        logging.error("Could not read KMP status: %s", e)

    return sensibo_available, kmp_available


def grid_monitor_loop():
    next_poll = time.monotonic()
    while True:
        sleep_seconds = next_poll - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        readings = tibber.get_grid_import_export()
        if readings is None:
            logging.warning("Could not retrieve Tibber grid import/export readings.")
        else:
            logging.info(
                "Grid electricity: import=%s W, export=%s W.",
                readings.get("grid_import_w"),
                readings.get("grid_export_w")
            )

        next_poll += GRID_POLL_INTERVAL_SECONDS
        if next_poll <= time.monotonic():
            next_poll = time.monotonic() + GRID_POLL_INTERVAL_SECONDS


# Main program loop
def main_loop():
    current_heat_type = "none"
    threading.Thread(
        target=grid_monitor_loop,
        name="tibber-grid-monitor",
        daemon=True
    ).start()
    if FAN_ASSIST_ENABLED:
        threading.Thread(
            target=fan_monitor_loop,
            name="pellet-fan-monitor",
            daemon=True
        ).start()

    if TEST_MODE:
        logging.info(
            "TEST MODE is enabled. Device settings will not be changed; "
            "read-only status checks will be attempted."
        )

    while True:
        spot_price = get_spot_price_safe()
        if spot_price is None:
            logging.warning("No spot price available. Skipping cycle.")
            time.sleep(30)
            continue

        outdoor_temp = weather.get_outdoor_temp()
        if outdoor_temp is None:
            logging.warning("No outdoor temperature available. Skipping cycle.")
            time.sleep(30)
            continue

        heating_type, heating_capacity = decide_heating_source(outdoor_temp, spot_price)

        switch_failed = False
        if heating_type == "heatpump" and current_heat_type != "heatpump":
            logging.info("Activating heat pump...")
            if turn_on_heat_pump():
                current_heat_type = "heatpump"
            else:
                switch_failed = True
        elif heating_type == "pelletstove" and current_heat_type != "pelletstove":
            logging.info("Activating pellet stove...")
            if turn_on_pellet_stove():
                current_heat_type = "pelletstove"
            else:
                current_heat_type = "none"
                switch_failed = True
        else:
            logging.info(f"{heating_type.capitalize()} is already active.")

        sensibo_status, kmp_status = check_systems()
        if not sensibo_status or not kmp_status:
            logging.warning("One or more systems unavailable or could not be read.")

        # Log data for ML training
        if not switch_failed:
            log_data(datetime.now(), spot_price, outdoor_temp, heating_type, heating_capacity)

        wait_until_next_quarter()

if __name__ == "__main__":
    main_loop()
    logging.info("Starting main loop...")