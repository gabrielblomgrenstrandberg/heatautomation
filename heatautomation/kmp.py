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

# module: kmp.py – A module for interacting with the KMP pellet stove portal using Selenium.

import os
import platform
import logging
import sys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import ElementClickInterceptedException

if __package__:
    from . import config
else:
    import config

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

USERNAME = config.KMP_USERNAME
PASSWORD = config.KMP_PASSWORD
PORTAL_URL = "http://portal.kmp-ab.se"
RUNNING_MODES = frozenset({"LADDAR", "TÄNDNING", "UPPVÄRMNING", "HÖGEFFEKT"})
IDLE_MODES = frozenset({"VILOLÄGE, SLÄCKER NED", "VILAR..."})
OFF_MODES = frozenset({"AV", "AVSTÄNGD, SLÄCKER NED"})

def start_chrome():
    try:
        system = platform.system()
        arch = platform.machine()
        logging.info(f"Detected system: {system}, architecture: {arch}")

        options = Options()
        options.add_argument("--headless")  # Works well on Raspberry Pi
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        chrome_binary_path = config.CHROME_BINARY_PATH
        if chrome_binary_path:
            options.binary_location = chrome_binary_path

        chromedriver_path = config.CHROMEDRIVER_PATH
        if chromedriver_path and os.path.basename(chromedriver_path).lower() in {
            "chrome", "chrome.exe"
        }:
            raise ValueError(
                "CHROMEDRIVER_PATH must point to chromedriver.exe, not the Chrome browser."
            )

        if chromedriver_path:
            driver = webdriver.Chrome(
                service=Service(executable_path=chromedriver_path),
                options=options
            )
        else:
            driver = webdriver.Chrome(options=options)
        logging.info("ChromeDriver started successfully.")
        return driver

    except Exception as e:
        logging.error(f"Failed to start ChromeDriver: {e}", exc_info=True)
        return None

def login(driver):
    try:
        if driver is None:
            raise RuntimeError("Failed to initialize the WebDriver. Please check the ChromeDriver setup.")
        driver.get(PORTAL_URL)
        wait = WebDriverWait(driver, 15)
        wait.until(EC.presence_of_element_located((By.ID, "kmac"))).send_keys(USERNAME)
        driver.find_element(By.ID, "kpwd").send_keys(PASSWORD)
        driver.find_element(By.NAME, "stove").click()
        wait.until(EC.presence_of_element_located((By.ID, "mode")))
    except Exception as e:
        logging.error(f"Login failed: {e}", exc_info=True)
        raise

def get_mode(driver):
    try:
        return WebDriverWait(driver, 15).until(
            lambda current_driver: current_driver.find_element(By.ID, "mode").text.strip()
        )
    except Exception as e:
        logging.error(f"Could not read the pellet stove mode: {e}", exc_info=True)
        return None

def click_start(driver):
    try:
        button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "startbild"))
        )
        button.click()
        logging.info("Power button clicked.")
        return True
    except ElementClickInterceptedException:
        logging.error("Could not click the power button, something is blocking it.")
    except Exception as e:
        logging.error(f"Error while clicking the power button: {e}")
    return False


def can_turn_off(mode):
    if mode in RUNNING_MODES:
        logging.info("Pellet stove is running in mode %s; deferring shutdown.", mode)
        return False
    if mode in IDLE_MODES or mode in OFF_MODES:
        return True
    logging.warning("Cannot turn off pellet stove: mode %s is unavailable or unknown.", mode)
    return False


def can_turn_on(mode):
    if mode in OFF_MODES or mode in RUNNING_MODES or mode in IDLE_MODES:
        return True
    logging.warning("Cannot turn on pellet stove: mode %s is unavailable or unknown.", mode)
    return False


def off():
    driver = start_chrome()
    try:
        login(driver)
        mode = get_mode(driver)

        if not can_turn_off(mode):
            return False
        if mode in OFF_MODES:
            logging.info("Pellet stove is already off.")
            return True
        logging.info("Pellet stove is idle, turning it off.")
        return click_start(driver)
    finally:
        if driver:
            driver.quit()

def on():
    driver = start_chrome()
    try:
        login(driver)
        mode = get_mode(driver)

        if not can_turn_on(mode):
            return False
        if mode in OFF_MODES:
            return click_start(driver)
        logging.info("Pellet stove is already on in mode: %s", mode)
        return True
    finally:
        if driver:
            driver.quit()

def pelletstove_error():
    driver = start_chrome()
    try:
        login(driver)
        mode = get_mode(driver)
        return "error" in mode.lower() if mode else False
    finally:
        driver.quit()

def check_connection():
    return get_status() is not None


def get_status():
    """Log in to the KMP portal and read the stove mode without changing it."""
    driver = start_chrome()
    if driver is None:
        return None

    try:
        login(driver)
        mode = get_mode(driver)
        if mode is None:
            return None
        return {"mode": mode}
    except Exception as e:
        logging.error("Could not read pellet stove status: %s", e)
        return None
    finally:
        driver.quit()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pelletstove.py [on|off|status|error]")
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "on":
        on()
    elif command == "off":
        off()
    elif command == "status":
        driver = start_chrome()
        try:
            login(driver)
            print("Läge:", get_mode(driver))
        finally:
            driver.quit()
    elif command == "error":
        print("Felstatus:", pelletstove_error())
    else:
        print(f"Unknown command: {command}")
