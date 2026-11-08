"""Validated physical house configuration."""

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import List, Literal, Optional, Set
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..discovery.nmap import trusted_networks

SupportedCapability = Literal[
    "read_temperature",
    "read_setpoint",
    "write_setpoint",
    "read_status",
    "set_power",
    "read_open_state",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class House(StrictModel):
    id: UUID
    name: str


class Floor(StrictModel):
    id: UUID
    name: str
    level: Optional[int] = None


class Room(StrictModel):
    id: UUID
    floor_id: UUID
    name: str


class Device(StrictModel):
    id: UUID
    kind: Literal[
        "heat_generator", "emitter", "thermostat", "actuator_sensor", "sensor"
    ]
    name: str
    room_id: Optional[UUID] = None
    adapter: Optional[str] = None
    external_id: Optional[str] = None
    capabilities: List[SupportedCapability] = Field(default_factory=list)
    rated_kw: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    rated_w_at_dt50: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    provenance: str
    controlled_by: Optional[UUID] = None


class HeatPath(StrictModel):
    id: str
    generator_id: UUID
    emitter_ids: Optional[List[UUID]] = None
    target_room_id: Optional[UUID] = None
    distribution: Optional[str] = None
    provenance: Optional[str] = None


class RoomPrior(StrictModel):
    room_id: UUID
    capacity_j_per_k: float = Field(gt=0, allow_inf_nan=False)
    ua_w_per_k: float = Field(gt=0, allow_inf_nan=False)
    provenance: str


class ThermalLink(StrictModel):
    room_a_id: UUID
    room_b_id: UUID
    conductance_w_per_k: float = Field(gt=0, allow_inf_nan=False)
    provenance: str


class TadoRoomMapping(StrictModel):
    room_id: UUID
    home_id: Optional[int] = Field(default=None, gt=0, strict=True)
    zone_id: Optional[int] = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_pair(self):
        if (self.home_id is None) != (self.zone_id is None):
            raise ValueError("Tado home_id and zone_id must both be provided or both null")
        return self


class ProtectedMill(StrictModel):
    ip_address: str
    mac_address: str

    @model_validator(mode="after")
    def validate_identity(self):
        try:
            address = ipaddress.ip_address(self.ip_address)
        except ValueError as error:
            raise ValueError("Mill address must be an IPv4 address") from error
        if address.version != 4:
            raise ValueError("Mill address must be an IPv4 address")
        mac = self.mac_address.upper()
        if (not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac)
                or int(mac[:2], 16) & 1 or mac == "00:00:00:00:00:00"):
            raise ValueError("Mill MAC address must be a unicast MAC address")
        self.ip_address = str(address)
        self.mac_address = mac
        return self


class LanDiscoveryConfig(StrictModel):
    enabled: bool = False
    allowed_cidrs: List[str] = Field(default_factory=list)
    interval_seconds: int = Field(default=3600, ge=300, le=604800, strict=True)
    poll_seconds: int = Field(default=300, ge=30, le=86400, strict=True)
    freshness_seconds: int = Field(default=900, ge=30, le=604800, strict=True)
    nmap_path: str = Field(default="nmap", min_length=1, max_length=1024)
    mdns_enabled: bool = False
    allow_unauthenticated_mill_http: bool = False
    protected_mill: List[ProtectedMill] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_networks(self):
        if self.enabled or self.allowed_cidrs:
            networks = trusted_networks(self.allowed_cidrs)
            for device in self.protected_mill:
                if not any(ipaddress.ip_address(device.ip_address) in network for network in networks):
                    raise ValueError("Protected Mill addresses must be in an allowed CIDR")
        macs = [device.mac_address for device in self.protected_mill]
        addresses = [device.ip_address for device in self.protected_mill]
        if len(set(macs)) != len(macs) or len(set(addresses)) != len(addresses):
            raise ValueError("Protected Mill MAC addresses and addresses must be unique")
        return self


class HouseConfig(StrictModel):
    schema_version: Literal[1]
    house: House
    floors: List[Floor]
    rooms: List[Room]
    devices: List[Device]
    heat_paths: List[HeatPath]
    room_priors: List[RoomPrior] = Field(default_factory=list)
    thermal_links: List[ThermalLink] = Field(default_factory=list)
    tado_rooms: List[TadoRoomMapping] = Field(default_factory=list)
    lan_discovery: LanDiscoveryConfig = Field(default_factory=LanDiscoveryConfig)

    @model_validator(mode="after")
    def validate_references(self):
        ids = [self.house.id]
        ids.extend(item.id for item in self.floors)
        ids.extend(item.id for item in self.rooms)
        ids.extend(item.id for item in self.devices)
        if len(ids) != len(set(ids)):
            raise ValueError("House, floor, room, and device IDs must be unique")

        floors = {item.id for item in self.floors}
        rooms = {item.id for item in self.rooms}
        devices = {item.id: item for item in self.devices}
        mill_ids = [device.external_id for device in self.devices
                    if device.adapter == "mill" and device.external_id
                    and re.fullmatch(r"mill:[0-9a-f]{12}", device.external_id)]
        if len(mill_ids) != len(set(mill_ids)):
            raise ValueError("A discovered Mill identity may only be assigned to one house device")
        path_ids = [item.id for item in self.heat_paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError("Heat path IDs must be unique")
        if any(room.floor_id not in floors for room in self.rooms):
            raise ValueError("Every room must reference a configured floor")
        if any(device.room_id is not None and device.room_id not in rooms
               for device in self.devices):
            raise ValueError("Every assigned device must reference a configured room")

        for device in self.devices:
            if device.kind != "emitter" and not device.adapter:
                raise ValueError("Non-emitter devices must declare an adapter")
            if device.kind == "emitter":
                controller = devices.get(device.controlled_by)
                if controller is None or controller.kind != "thermostat":
                    raise ValueError("Each emitter must reference a configured thermostat")

        for path in self.heat_paths:
            generator = devices.get(path.generator_id)
            if generator is None or generator.kind != "heat_generator":
                raise ValueError("Every heat path must reference a heat generator")
            is_circuit = bool(path.emitter_ids)
            is_direct = path.target_room_id is not None
            if is_circuit == is_direct:
                raise ValueError(
                    "A heat path must have either emitter_ids or target_room_id"
                )
            if is_circuit and any(
                    emitter_id not in devices or devices[emitter_id].kind != "emitter"
                    for emitter_id in path.emitter_ids or []):
                raise ValueError("Circuit paths must reference configured emitters")
            if is_direct and path.target_room_id not in rooms:
                raise ValueError("Direct heat paths must reference a configured room")

        prior_rooms = [prior.room_id for prior in self.room_priors]
        if len(prior_rooms) != len(set(prior_rooms)):
            raise ValueError("Room priors must be unique by room")
        if any(room_id not in rooms for room_id in prior_rooms):
            raise ValueError("Every room prior must reference a configured room")

        mapped_rooms = [item.room_id for item in self.tado_rooms]
        mapped_zones = [(item.home_id, item.zone_id) for item in self.tado_rooms
                        if item.home_id is not None]
        if any(room_id not in rooms for room_id in mapped_rooms):
            raise ValueError("Tado mappings must reference configured rooms")
        if len(mapped_rooms) != len(set(mapped_rooms)):
            raise ValueError("A room may only have one Tado zone mapping")
        if len(mapped_zones) != len(set(mapped_zones)):
            raise ValueError("A Tado zone may only be assigned to one room")

        link_pairs: Set[frozenset] = set()
        for link in self.thermal_links:
            if link.room_a_id == link.room_b_id:
                raise ValueError("A room cannot have a thermal link to itself")
            if link.room_a_id not in rooms or link.room_b_id not in rooms:
                raise ValueError("Thermal links must reference configured rooms")
            pair = frozenset((link.room_a_id, link.room_b_id))
            if pair in link_pairs:
                raise ValueError("Thermal links must be unique and symmetric")
            link_pairs.add(pair)
        return self


def load_house_config(path):
    """Load and validate a house configuration file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return HouseConfig.model_validate(data)


def config_hash(config):
    """Return a stable hash for a validated house configuration."""
    payload = config.model_dump(mode="json", exclude_none=False)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
