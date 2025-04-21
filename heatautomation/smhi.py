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

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import logging
import shutil

# Setup logging for better error tracking
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def get_outdoor_temp():
    url = "https://www.smhi.se/vader/prognoser-och-varningar/vaderprognos/q/hagge/2709191"

    # Configure Chrome to run in headless mode
    options = Options()
    options.add_argument("--headless")  # Run without opening a window
    options.add_argument("--disable-gpu")  # Disable GPU acceleration
    options.add_argument("--no-sandbox")  # Needed for running as root in some environments
    options.add_argument("--disable-dev-shm-usage")  # Prevent issues with shared memory

    chromedriver_path = shutil.which("chromedriver")
    if not chromedriver_path:
        raise RuntimeError("chromedriver not found in PATH")
    driver = webdriver.Chrome(service=Service(chromedriver_path), options=options)

    try:
        # Open the target URL
        driver.get(url)

        # Wait until the temperature element is present
        wait = WebDriverWait(driver, 10)
        temperature_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'span._asH1_1bwlw_168')))
        
        # Get the temperature text
        temperature = temperature_element.text.strip()
        
        if not temperature:
            logging.warning("Temperature value is empty!")
            return None

        # Remove the degree symbol and handle special minus characters
        temperature = temperature.replace("°", "").replace("−", "-")
        temperature_int = int(temperature)

        logging.info(f"Current temperature: {temperature_int}°")
        return temperature_int

    except Exception as e:
        logging.error(f"Error occurred while fetching the temperature: {e}")
        return None

    finally:
        # Always close the browser session
        driver.quit()

# Example usage
if __name__ == "__main__":
    temperature = get_outdoor_temp()
    if temperature is not None:
        logging.info(f"Fetched temperature: {temperature}°")
    else:
        logging.error("Failed to fetch the temperature.")
