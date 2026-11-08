"""Identify thermal observations that can safely share a learned model."""

import hashlib
import os

from .. import config


def current():
    source = config._settings.get("mpc", "indoor_source", fallback="sensibo").strip().lower()
    if source not in ("sensibo", "tado"):
        raise ValueError("[mpc] indoor_source must be sensibo or tado")
    room = config._settings.get("mpc", "tado_room_id", fallback="").strip()
    if source == "tado" and not room:
        raise ValueError("[mpc] tado_room_id is required for Tado indoor temperature")
    zone = ""
    if source == "tado":
        from ..house.config import load_house_config
        path = os.environ.get("HEATAUTOMATION_HOUSE_CONFIG", "config/house.json")
        try:
            house = load_house_config(path)
        except (OSError, ValueError) as error:
            raise ValueError("Tado indoor source requires a valid house configuration") from error
        mapping = next((item for item in house.tado_rooms if str(item.room_id) == room), None)
        if mapping is None or mapping.home_id is None:
            raise ValueError("Tado indoor room requires a mapped home and zone")
        zone = "{}:{}".format(mapping.home_id, mapping.zone_id)
    absent_pump = config._settings.getboolean("mpc", "heatpump_absent", fallback=False)
    absent_stove = config._settings.getboolean("mpc", "pelletstove_absent", fallback=False)
    other_absent = config._settings.getboolean("mpc", "other_heating_absent", fallback=False)
    if absent_pump and config.SENSIBO_ENABLED:
        raise ValueError("A Sensibo heat pump cannot be both enabled and declared absent")
    if absent_stove and config.KMP_ENABLED:
        raise ValueError("A KMP pellet stove cannot be both enabled and declared absent")
    return {
        "indoor_source": source,
        "tado_room_id": room if source == "tado" else "",
        "tado_zone": zone,
        "heatpump_absent": absent_pump,
        "pelletstove_absent": absent_stove,
        "other_heating_absent": other_absent if source == "tado" else False,
    }


def marker(profile):
    text = "|".join((profile["indoor_source"], profile["tado_room_id"], profile["tado_zone"],
                     str(profile["heatpump_absent"]), str(profile["pelletstove_absent"]),
                     str(profile["other_heating_absent"])))
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:6], "big")


def legacy(profile):
    return profile == {
        "indoor_source": "sensibo", "tado_room_id": "",
        "tado_zone": "",
        "heatpump_absent": False, "pelletstove_absent": False,
        "other_heating_absent": False,
    }


def compatible(record, profile):
    value = record.get("thermal_profile")
    return value == marker(profile) or (value is None and legacy(profile))
