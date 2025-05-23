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

# module: smhi.py – A module for fetching outdoor temperature from SMHI using Selenium.

import os
import shutil
import logging
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# Load environment variables from .env file if present
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_outdoor_temp():
    url = "https://www.smhi.se/vader/prognoser-och-varningar/vaderprognos/q/hagge/2709191"

    # Configure Chrome to run headless
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/chromium"


    # Get chromedriver path from environment or PATH
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH") or shutil.which("chromedriver")
    if not chromedriver_path:
        raise RuntimeError(
            "chromedriver not found. Set CHROMEDRIVER_PATH environment variable or add chromedriver to PATH."
        )

    driver = webdriver.Chrome(service=Service(chromedriver_path), options=options)

    try:
        driver.get(url)

        # Wait until the temperature element is present
        wait = WebDriverWait(driver, 10)
        temperature_element = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'span._asH1_1bwlw_168'))
        )
        
        temperature = temperature_element.text.strip()

        if not temperature:
            logging.warning("Temperature value is empty!")
            return None

        # Clean up temperature value
        temperature = temperature.replace("°", "").replace("−", "-")
        temperature_int = int(temperature)

        logging.info(f"Current temperature: {temperature_int}°")
        return temperature_int

    except Exception as e:
        logging.error(f"Error occurred while fetching the temperature: {e}")
        return None

    finally:
        driver.quit()

# Example usage
if __name__ == "__main__":
    temperature = get_outdoor_temp()
    if temperature is not None:
        logging.info(f"Fetched temperature: {temperature}°")
    else:
        logging.error("Failed to fetch the temperature.")
