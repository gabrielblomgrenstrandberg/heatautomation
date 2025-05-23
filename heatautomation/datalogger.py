import csv
from datetime import datetime
import os

LOG_FILE = "heating_data_log.csv"

def log_data(timestamp, spot_price, outdoor_temp, heat_source, heating_capacity):
    file_exists = os.path.isfile(LOG_FILE)
    
    with open(LOG_FILE, mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["timestamp", "spot_price", "outdoor_temp", "heat_source", "heating_capacity_kW"])
        
        writer.writerow([timestamp, spot_price, outdoor_temp, heat_source, heating_capacity])
