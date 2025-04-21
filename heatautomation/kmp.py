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
import time
import logging
import sys
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import ElementClickInterceptedException
#from webdriver_manager.chrome import ChromeDriverManager

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

USERNAME = os.getenv("KMP_USERNAME")
PASSWORD = os.getenv("KMP_PASSWORD")
PORTAL_URL = "http://portal.kmp-ab.se"

def start_chrome():
    try:
        system = platform.system()
        arch = platform.machine()
        logging.info(f"Detected system: {system}, architecture: {arch}")

        options = Options()
        options.add_argument("--headless")  # Works well on Raspberry Pi
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        # Find local chromedriver
        chromedriver_path = shutil.which("chromedriver")
        if not chromedriver_path:
            logging.error("Could not find chromedriver in PATH.")
            return None

        service = Service(executable_path=chromedriver_path)
        chromedriver_path = shutil.which("chromedriver")
        if not chromedriver_path:
            raise RuntimeError("chromedriver not found in PATH")
        driver = webdriver.Chrome(service=Service(chromedriver_path), options=options)
        logging.info("ChromeDriver started successfully.")
        return driver

    except Exception as e:
        logging.error(f"Failed to start ChromeDriver: {e}", exc_info=True)
        return None

def login(driver):
    try:
        driver.get(PORTAL_URL)
        wait = WebDriverWait(driver, 15)
        wait.until(EC.presence_of_element_located((By.ID, "kmac"))).send_keys(USERNAME)
        driver.find_element(By.ID, "kpwd").send_keys(PASSWORD)
        driver.find_element(By.NAME, "stove").click()
        wait.until(EC.presence_of_element_located((By.ID, "mode")))
        time.sleep(10)
    except Exception as e:
        logging.error(f"Login failed: {e}", exc_info=True)
        raise

def get_mode(driver):
    try:
        time.sleep(10)  # Wait for full load
        return driver.find_element(By.ID, "mode").text
    except Exception as e:
        logging.error(f"Could not find the mode element: {e}", exc_info=True)
        return None

def click_start(driver):
    try:
        button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "startbild"))
        )
        button.click()
        logging.info("Power button clicked.")
    except ElementClickInterceptedException:
        logging.error("Could not click the power button, something is blocking it.")
    except Exception as e:
        logging.error(f"Error while clicking the power button: {e}")

def off():
    driver = start_chrome()
    try:
        login(driver)
        mode = get_mode(driver)

        if mode in ["AV", "AVSTÄNGD, SLÄCKER NED"]:
            logging.info(f"Pellet stove is already off.")
        else:
            logging.info(f"Pellet stove is on, turning it off.")
            click_start(driver)
    finally:
        driver.quit()

def on():
    driver = start_chrome()
    try:
        login(driver)
        mode = get_mode(driver)

        if mode in ["AV", "AVSTÄNGD, SLÄCKER NED"]:
            click_start(driver)
        elif mode in ["LADDAR", "TÄNDNING", "UPPVÄRMNING", "HÖGEFFEKT", "VILOLÄGE, SLÄCKER NED", "VILAR..."]:
            logging.info(f"Pellet stove is already on in mode: {mode}")
        else:
            logging.warning(f"Warning, unknown mode: {mode}")
    finally:
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
    # TODO: Implement a check to see if the pellet stove is reachable
    return True

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

