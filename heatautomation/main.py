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

import time
import threading
import os
from datetime import datetime, timedelta
import logging

from . import config
from .control import heating_model
from .integrations import kmp, sensibo, tibber, weather
from .storage.datalogger import log_data

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

GRID_POLL_INTERVAL_SECONDS = config.GRID_POLL_INTERVAL_SECONDS
FAN_POLL_INTERVAL_SECONDS = config.FAN_POLL_INTERVAL_SECONDS
FAN_ASSIST_ENABLED = config.FAN_ASSIST_ENABLED
CONTROL_LOCK = threading.RLock()

TEST_MODE = config.TEST_MODE
advanced_heat_required = config.COST_CONTROL_ENABLED

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
    if not config.TIBBER_ENABLED:
        logging.warning("Tibber integration is disabled; spot price unavailable.")
        return None
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
    if not config.SENSIBO_ENABLED or not config.KMP_ENABLED:
        logging.warning("Heat-pump switching disabled: both Sensibo and KMP must be enabled.")
        return False
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
    if not config.SENSIBO_ENABLED or not config.KMP_ENABLED:
        logging.warning("Pellet-stove switching disabled: both Sensibo and KMP must be enabled.")
        return False
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
        if not config.SENSIBO_ENABLED or not config.KMP_ENABLED:
            logging.warning("Fan assist disabled: both Sensibo and KMP must be enabled.")
            return
        with CONTROL_LOCK:
            status = kmp.get_status()
            mode = status.get("mode") if status else None
            known_modes = kmp.RUNNING_MODES | kmp.IDLE_MODES | kmp.OFF_MODES
            if mode not in known_modes:
                logging.warning("Fan assist deferred: KMP mode is unavailable or unknown.")
                return

            if mode == "HÖGEFFEKT":
                if config.COST_CONTROL_ENABLED and advanced_heat_required:
                    logging.info("Fan assist suppressed while heat-pump heating is required.")
                else:
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


class CostController:
    def __init__(self):
        self.state = None
        self.changed_at = {}
        self.uncertain = False

    def observe(self):
        if not config.SENSIBO_ENABLED or not config.KMP_ENABLED:
            logging.warning("Cost control disabled: both Sensibo and KMP must be enabled.")
            self.uncertain = True
            return None
        stove = kmp.get_status()
        pump = sensibo.getSystemStatus()
        if not stove or not pump:
            logging.warning("Cost control deferred: device status is unavailable.")
            self.uncertain = True
            return None
        mode = stove.get("mode")
        power = pump.get("is_on")
        if mode not in kmp.RUNNING_MODES | kmp.IDLE_MODES | kmp.OFF_MODES:
            logging.warning("Cost control deferred: unknown KMP mode %s.", mode)
            self.uncertain = True
            return None
        if not isinstance(power, bool) or (power and pump.get("mode") not in ("heat", "fan")):
            logging.warning("Cost control deferred: Sensibo power or mode is unknown.")
            self.uncertain = True
            return None
        return {
            "pelletstove": mode not in kmp.OFF_MODES,
            "heatpump": power and pump.get("mode") == "heat",
        }

    def synchronize(self, observed, now):
        global advanced_heat_required
        if self.state is None:
            self.state = observed.copy()
            self.changed_at = {source: now for source in observed}
        elif not TEST_MODE:
            for source in observed:
                if observed[source] != self.state[source]:
                    logging.warning("Detected external %s state change.", source)
                    self.state[source] = observed[source]
                    self.changed_at[source] = now
        if self.uncertain:
            self.changed_at = {source: now for source in observed}
            self.uncertain = False
        advanced_heat_required = self.state["heatpump"]

    def current(self):
        pump = self.state["heatpump"]
        stove = self.state["pelletstove"]
        if pump and stove:
            return "combined"
        if pump:
            return "heatpump"
        if stove:
            return "pelletstove"
        return "none"

    def transition(self, desired, now):
        if not config.SENSIBO_ENABLED or not config.KMP_ENABLED:
            logging.warning("Cost control transition disabled: both heating integrations must be enabled.")
            return False
        global advanced_heat_required
        wanted = {
            "heatpump": desired in ("heatpump", "combined"),
            "pelletstove": desired in ("pelletstove", "combined"),
        }
        # Start replacement sources before stopping any currently running source.
        for turning_on in (True, False):
            for source in ("pelletstove", "heatpump"):
                if wanted[source] != turning_on or self.state[source] == turning_on:
                    continue
                elapsed = now - self.changed_at[source]
                minimum = config.MINIMUM_SWITCH_INTERVAL_SECONDS
                if not turning_on:
                    minimum = max(minimum, config.MINIMUM_RUNTIME_SECONDS)
                if elapsed < minimum:
                    logging.info("Deferring %s change for %.0f seconds.", source, minimum - elapsed)
                    return False
                if (source == "heatpump" and turning_on and desired == "heatpump"
                        and self.state["pelletstove"]):
                    status = kmp.get_status()
                    if not kmp.can_turn_off(status.get("mode") if status else None):
                        logging.warning("Heat-pump switch deferred until the stove is idle.")
                        return False
                if TEST_MODE:
                    if source == "pelletstove":
                        status = kmp.get_status()
                        mode = status.get("mode") if status else None
                        safe = kmp.can_turn_on(mode) if turning_on else kmp.can_turn_off(mode)
                        if not safe:
                            return False
                    logging.info("TEST MODE: would turn %s %s.", "on" if turning_on else "off", source)
                elif source == "pelletstove":
                    if not retry_function(kmp.on if turning_on else kmp.off):
                        logging.warning("Cost control: pellet stove command failed or was deferred.")
                        return False
                else:
                    command = sensibo.on if turning_on else sensibo.off
                    if retry_function(command) is None:
                        logging.warning("Cost control: heat pump command failed.")
                        return False
                self.state[source] = turning_on
                self.changed_at[source] = now
                advanced_heat_required = self.state["heatpump"]
                if turning_on and any(
                    self.state[name] and not wanted[name] for name in wanted
                ):
                    logging.info("Waiting until the next cycle before stopping the previous source.")
                    return False
        return True


def cost_control_cycle(controller, temperature, price):
    global advanced_heat_required
    with CONTROL_LOCK:
        observed = controller.observe()
        if observed is None:
            advanced_heat_required = True
            return None
        now = time.monotonic()
        controller.synchronize(observed, now)
        capacity = get_effective_heating_capacity(temperature)
        desired, delivered_capacity = heating_model.select(
            temperature, price, capacity, config.PELLET_STOVE_CAPACITY,
            config.PELLET_COST_PER_KWH, config.COP_BY_TEMPERATURE,
            config.DEMAND_BY_TEMPERATURE, config.COST_HYSTERESIS,
            controller.current(), config.spotPriceLimit,
        )
        logging.info("Cost-based selection: %s.", desired)
        if not controller.transition(desired, now):
            logging.warning("Cost-based transition deferred; current state is %s.", controller.current())
            return None
        return desired, delivered_capacity


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
    if config.SENSIBO_ENABLED:
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
    if config.KMP_ENABLED:
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
    if not config.TIBBER_ENABLED:
        logging.info("Tibber grid monitoring is disabled.")
        return
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


def room_advisory_loop():
    """Run optional multi-room observations independently of legacy thermal learning."""
    from pathlib import Path
    from .control.room_advisory_monitor import run_cycle
    from .house.config import config_hash, load_house_config
    from .integrations.tado import TadoService, TokenFile
    from .learning.database import ThermalDatabase
    from .thermal.advisory_inputs import load_equipment_inputs

    database = None
    service = None
    service_hash = None
    while True:
        started = time.monotonic()
        try:
            if database is None:
                database = ThermalDatabase(config.THERMAL_DATABASE_PATH)
            house_path = os.environ.get("HEATAUTOMATION_HOUSE_CONFIG", "config/house.json")
            house = load_house_config(house_path)
            digest = config_hash(house)
            if service is not None and (digest != service_hash or not config.TADO_ENABLED):
                service.close()
                service = None
            if service is None and config.TADO_ENABLED:
                service = TadoService(
                    house, TokenFile(Path.home() / ".heatautomation" / "tado-token.json"))
                service_hash = digest
            forecast = weather.get_forecast() if config.WEATHER_ENABLED else None
            outdoor = forecast[0].get("temperature_c") if forecast else None
            equipment_path = config._settings.get(
                "room_advisory", "equipment_path", fallback="config/room_advisory.json")
            try:
                equipment = load_equipment_inputs(equipment_path, datetime.now().astimezone())
            except OSError as error:
                equipment = None
                logging.warning("Room advisory equipment inputs unavailable: %s", error)
            except ValueError:
                equipment = None
                logging.warning("Room advisory equipment inputs are invalid, stale or future-dated.")
            result = run_cycle(
                database.engine, house, service, outdoor, equipment=equipment,
                suspect_uncertainty_c=config._number(
                    "room_advisory", "suspect_uncertainty_c", positive=True),
            )
            if not result["available"]:
                logging.warning("Room advisory unavailable: %s", "; ".join(result["reasons"]))
        except Exception:
            logging.exception("Room advisory collection failed; will retry on the next poll.")
        time.sleep(max(1, 300 - (time.monotonic() - started)))


def thermal_monitor_loop():
    # Imports are deferred so a monitoring dependency cannot prevent heat control.
    try:
        from .learning import collection as thermal_collection
        from .learning import database as thermal_database
        from .learning import training as thermal_training
        from .learning import profile as thermal_profile
    except ImportError:
        logging.exception("Thermal monitoring unavailable: dependency import failed.")
        return
    database = None
    tado_service = None
    tado_service_profile = None
    next_poll = time.monotonic()
    next_training = next_poll
    while True:
        sleep_seconds = next_poll - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        try:
            if database is None:
                database = thermal_database.ThermalDatabase(config.THERMAL_DATABASE_PATH)
            selected_profile = thermal_profile.current()
            if tado_service is not None and selected_profile != tado_service_profile:
                tado_service.close()
                tado_service = None
            if (selected_profile["indoor_source"] == "tado" and config.TADO_ENABLED
                    and tado_service is None):
                from pathlib import Path
                from .house.config import load_house_config
                from .integrations.tado import TadoService, TokenFile
                house_path = os.environ.get("HEATAUTOMATION_HOUSE_CONFIG", "config/house.json")
                try:
                    house = load_house_config(house_path)
                    tado_service = TadoService(
                        house, TokenFile(Path.home() / ".heatautomation" / "tado-token.json")
                    )
                    tado_service_profile = selected_profile
                except (OSError, ValueError):
                    logging.warning("Tado thermal monitoring requires a valid house configuration.")
            thermal_collection.collect(database, get_effective_heating_capacity, tado_service,
                                       selected_profile)
            if time.monotonic() >= next_training:
                thermal_training.train(database, config.THERMAL_MODEL_PATH, selected_profile)
                next_training = time.monotonic() + 3600
            if config.MPC_ENABLED:
                from .control.mpc_monitor import run_shadow_cycle
                from .control.mpc import PlanningUnavailable
                from .control.mpc_simulation import evaluate
                from .learning.prediction import PredictionUnavailable
                try:
                    database.evaluate_plans(evaluate)
                    run_shadow_cycle(database, get_effective_heating_capacity)
                except (PlanningUnavailable, PredictionUnavailable) as error:
                    logging.warning("Shadow MPC planning unavailable: %s", error)
        except Exception:
            logging.exception("Thermal monitoring failed; will retry on the next poll.")
        next_poll += 300
        if next_poll <= time.monotonic():
            next_poll = time.monotonic() + 300


# Main program loop
def main_loop():
    current_heat_type = "none"
    cost_controller = CostController() if config.COST_CONTROL_ENABLED else None
    if config.TIBBER_ENABLED:
        threading.Thread(
            target=grid_monitor_loop,
            name="tibber-grid-monitor",
            daemon=True
        ).start()
    if config.THERMAL_LEARNING_ENABLED:
        threading.Thread(
            target=thermal_monitor_loop,
            name="thermal-monitor",
            daemon=True
        ).start()
    if config._settings.getboolean("room_advisory", "enabled", fallback=False):
        threading.Thread(
            target=room_advisory_loop,
            name="room-advisory-monitor",
            daemon=True
        ).start()
    if FAN_ASSIST_ENABLED and config.SENSIBO_ENABLED and config.KMP_ENABLED:
        threading.Thread(
            target=fan_monitor_loop,
            name="pellet-fan-monitor",
            daemon=True
        ).start()
    elif FAN_ASSIST_ENABLED:
        logging.warning("Fan assist paused: Sensibo and KMP must both be enabled.")

    if TEST_MODE:
        logging.info(
            "TEST MODE is enabled. Device settings will not be changed; "
            "read-only status checks will be attempted."
        )

    automatic_control = all((config.SENSIBO_ENABLED, config.KMP_ENABLED,
                             config.TIBBER_ENABLED, config.WEATHER_ENABLED))
    if not automatic_control:
        logging.warning(
            "Automatic source switching paused: Sensibo, KMP, Tibber and weather "
            "must all be enabled. Enabled devices remain in monitoring-only mode."
        )
    while True:
        if not automatic_control:
            check_systems()
            wait_until_next_quarter()
            continue
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

        if cost_controller is not None:
            selection = cost_control_cycle(cost_controller, outdoor_temp, spot_price)
            if selection is not None:
                heating_type, heating_capacity = selection
                log_data(datetime.now(), spot_price, outdoor_temp, heating_type, heating_capacity)
            wait_until_next_quarter()
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

def run_application():
    from .web.startup import start_web_server, stop_web_server

    try:
        from .discovery.service import start_worker
        start_worker(config.THERMAL_DATABASE_PATH)
    except (ImportError, OSError, RuntimeError):
        logging.exception("Could not start LAN discovery; heating control will continue.")
    server = start_web_server()
    try:
        main_loop()
    finally:
        stop_web_server(server)


if __name__ == "__main__":
    run_application()