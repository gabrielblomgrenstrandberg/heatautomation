"""SQLAlchemy Core metadata for multi-room physical and runtime data."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)


metadata = MetaData()

lan_devices = Table(
    "lan_devices", metadata,
    Column("id", String(80), primary_key=True),
    Column("device_type", String(32), nullable=False),
    Column("name", String(256), nullable=False),
    Column("ip_address", String(45)),
    Column("hostname", String(256)),
    Column("mac_address", String(32), nullable=False),
    Column("connection_method", String(32), nullable=False),
    Column("connection_state", String(32), nullable=False),
    Column("last_connection", String(40)),
    Column("last_measurement", String(40)),
    Column("last_discovery", String(40)),
    Column("last_error", String(256)),
    Column("capabilities_json", Text, nullable=False),
    Column("measurements_json", Text, nullable=False),
)
lan_scan_runs = Table(
    "lan_scan_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40)),
    Column("state", String(32), nullable=False),
    Column("error", String(256)),
    Column("candidate_count", Integer),
    Column("verified_count", Integer),
)
Index("ix_lan_scan_one_running", lan_scan_runs.c.state, unique=True,
      sqlite_where=lan_scan_runs.c.state == "RUNNING")
lan_scan_requests = Table(
    "lan_scan_requests", metadata,
    Column("id", Integer, primary_key=True),
    Column("requested_at", String(40), nullable=False),
    Column("handled_at", String(40)),
)
lan_probe_errors = Table(
    "lan_probe_errors", metadata,
    Column("device_id", String(80), primary_key=True),
    Column("checked_at", String(40), nullable=False),
    Column("error", String(256), nullable=False),
)

houses = Table(
    "houses",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("config_hash", String(64)),
    Column("config_loaded_at", DateTime(timezone=True)),
)
floors = Table(
    "floors",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("house_id", String(36), ForeignKey("houses.id", ondelete="RESTRICT"),
           nullable=False),
    Column("name", String(128), nullable=False),
    Column("level", Integer),
    Column("removed_at", DateTime(timezone=True)),
)
rooms = Table(
    "rooms",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("floor_id", String(36), ForeignKey("floors.id", ondelete="RESTRICT"),
           nullable=False),
    Column("name", String(128), nullable=False),
    Column("removed_at", DateTime(timezone=True)),
)
devices = Table(
    "devices",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("name", String(128), nullable=False),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT")),
    Column("adapter", String(64)),
    Column("external_id", String(256)),
    Column("rated_kw", Float),
    Column("rated_w_at_dt50", Float),
    Column("provenance", String(32), nullable=False),
    Column("controlled_by", String(36), ForeignKey("devices.id", ondelete="RESTRICT")),
    Column("removed_at", DateTime(timezone=True)),
)
device_capabilities = Table(
    "device_capabilities",
    metadata,
    Column("device_id", String(36), ForeignKey("devices.id", ondelete="RESTRICT"),
           nullable=False),
    Column("capability", String(64), nullable=False),
    PrimaryKeyConstraint("device_id", "capability"),
)
heat_paths = Table(
    "heat_paths",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("generator_id", String(36), ForeignKey("devices.id", ondelete="RESTRICT"),
           nullable=False),
    Column("path_type", String(16), nullable=False),
    Column("target_room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT")),
    Column("distribution", String(64)),
    Column("provenance", String(32)),
    Column("removed_at", DateTime(timezone=True)),
    CheckConstraint("path_type IN ('circuit', 'direct')", name="ck_heat_paths_type"),
    CheckConstraint(
        "(path_type = 'direct' AND target_room_id IS NOT NULL) OR "
        "(path_type = 'circuit' AND target_room_id IS NULL)",
        name="ck_heat_paths_target",
    ),
)
heat_path_emitters = Table(
    "heat_path_emitters",
    metadata,
    Column("heat_path_id", String(128), ForeignKey("heat_paths.id", ondelete="RESTRICT"),
           nullable=False),
    Column("emitter_id", String(36), ForeignKey("devices.id", ondelete="RESTRICT"),
           nullable=False),
    PrimaryKeyConstraint("heat_path_id", "emitter_id"),
)
thermal_links = Table(
    "thermal_links",
    metadata,
    Column("room_a_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("room_b_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("conductance_w_per_k", Float, nullable=False),
    Column("prior_conductance_w_per_k", Float, nullable=False),
    Column("provenance", String(32), nullable=False),
    Column("updated_at", DateTime(timezone=True)),
    Column("removed_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("room_a_id", "room_b_id"),
    CheckConstraint("room_a_id < room_b_id", name="ck_thermal_links_order"),
    CheckConstraint("conductance_w_per_k > 0", name="ck_thermal_links_value"),
)
thermal_parameters = Table(
    "thermal_parameters",
    metadata,
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("name", String(64), nullable=False),
    Column("value", Float, nullable=False),
    Column("prior_value", Float, nullable=False),
    Column("sample_count", Integer, nullable=False, default=0),
    Column("covariance", Float),
    Column("updated_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("room_id", "name"),
)
thermal_parameter_events = Table(
    "thermal_parameter_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("name", String(64), nullable=False),
    Column("old_value", Float),
    Column("new_value", Float),
    Column("reason", String(32), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

room_settings = Table(
    "room_settings",
    metadata,
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("comfort_profile", String(64), nullable=False, default="default"),
    Column("min_c", Float),
    Column("max_c", Float),
    Column("target_c", Float),
    Column("max_preheat_c", Float, nullable=False, server_default="2"),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("max_preheat_c >= 0 AND max_preheat_c <= 2",
                    name="ck_room_settings_preheat"),
)
room_target_events = Table(
    "room_target_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"), nullable=False),
    Column("target_c", Float, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("source", String(32), nullable=False),
)
Index("ix_room_target_events_room_time", room_target_events.c.room_id, room_target_events.c.recorded_at)
room_overrides = Table(
    "room_overrides",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("target_c", Float, nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("source", String(32), nullable=False),
    CheckConstraint("expires_at > starts_at", name="ck_room_overrides_expiry"),
)
room_schedules = Table(
    "room_schedules",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("weekday", Integer, nullable=False),
    Column("start_min", Integer, nullable=False),
    Column("end_min", Integer, nullable=False),
    Column("target_c", Float, nullable=False),
    CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_room_schedules_weekday"),
    CheckConstraint(
        "start_min BETWEEN 0 AND 1439 AND end_min BETWEEN 1 AND 1440",
        name="ck_room_schedules_time",
    ),
)
room_measurements = Table(
    "room_measurements",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("quantity", String(32), nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String(24), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("source_device_id", String(36),
           ForeignKey("devices.id", ondelete="RESTRICT")),
    Column("source_integration", String(64)),
    Column("connection_method", String(32)),
    Column("source_ref", String(128)),
    Column("quality", String(16), nullable=False),
    Column("classification", String(16), nullable=False),
    CheckConstraint(
        "quality IN ('good', 'suspect', 'stale', 'bad')",
        name="ck_room_measurements_quality",
    ),
    CheckConstraint(
        "classification IN ('measured', 'estimated', 'predicted')",
        name="ck_room_measurements_classification",
    ),
)
Index("ix_room_measurements_room_time",
      room_measurements.c.room_id, room_measurements.c.quantity,
      room_measurements.c.observed_at)
room_thermal_snapshots = Table(
    "room_thermal_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("observed_at", DateTime(timezone=True), nullable=False, unique=True),
    Column("data_json", Text, nullable=False),
)
room_thermal_predictions = Table(
    "room_thermal_predictions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("target_at", DateTime(timezone=True), nullable=False),
    Column("horizon_seconds", Integer, nullable=False),
    Column("predicted_c", Float, nullable=False),
    Column("measured_c", Float),
    Column("measured_at", DateTime(timezone=True)),
    Column("measurement_id", Integer, ForeignKey("room_measurements.id", ondelete="RESTRICT")),
)
Index("ix_room_thermal_predictions_room_target",
      room_thermal_predictions.c.room_id, room_thermal_predictions.c.target_at)
device_runtime_state = Table(
    "device_runtime_state",
    metadata,
    Column("device_id", String(36), ForeignKey("devices.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("reported_power", String(16)),
    Column("reported_at", DateTime(timezone=True)),
    Column("commanded_power", String(16)),
    Column("commanded_at", DateTime(timezone=True)),
    Column("connectivity", String(16), nullable=False, default="unknown"),
)
command_log = Table(
    "command_log",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("device_id", String(36), ForeignKey("devices.id", ondelete="RESTRICT"),
           nullable=False),
    Column("command", String(64), nullable=False),
    Column("payload", Text),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("detail", Text),
    CheckConstraint(
        "outcome IN ('simulated', 'sent', 'rejected', 'failed', 'timeout')",
        name="ck_command_log_outcome",
    ),
)
optimizer_plans = Table(
    "optimizer_plans",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("details_json", Text, nullable=False),
)
optimizer_plan_intervals = Table(
    "optimizer_plan_intervals",
    metadata,
    Column("plan_id", Integer,
           ForeignKey("optimizer_plans.id", ondelete="RESTRICT"), nullable=False),
    Column("room_id", String(36), ForeignKey("rooms.id", ondelete="RESTRICT"),
           nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("data_json", Text, nullable=False),
    PrimaryKeyConstraint("plan_id", "room_id", "starts_at"),
)
