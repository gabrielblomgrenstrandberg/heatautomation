# Multi-room heating architecture

## Read-only measurement and advisory foundation

The codebase contains a separate, opt-in read-only advisory path. It does not
replace the legacy MPC or call device commands. Physical inputs still require
independently verified equipment observations and model calibration.

`thermal.measurements.Measurement` records the room, optional physical device,
integration, connection method, observation/receipt times, quality and
classification. `MeasurementStore` persists good or suspect values in the
existing `room_measurements` table; migration `0008` adds source metadata.
Invalid and unknown values are not stored as replacements for valid values.
Read the latest value with its source integration and source reference to avoid
mixing different zones after a remapping. Freshness is evaluated at read time:
stale values remain historical values, not current input. Existing legacy
rows have unknown source metadata and are preserved.

`collect_tado_room` can query a mapped zone through the existing Tado service.
It preserves the adapter's suspect temperature quality and uses the reported
observation timestamp. It records demand as a reported percentage, **not
delivered heat**. Since the adapter cannot identify the physical sensor,
the device ID is unknown and the Tado home/zone is stored as the source
reference. A target with no provider timestamp is returned as unknown and is
not stored as a current measurement. Errors propagate and do not erase prior
measurements. The optional worker polls configured mapped Tado rooms every
five minutes and checks that the current house configuration is reconciled.

`thermal.radiators.estimate_delivery` uses explicitly supplied radiator ratings,
water temperatures, demand, generator availability, capacity, exponent and
three configured delivery factors. It scales competing radiator estimates
proportionally to shared capacity. These factors and the sharing model need
installation-specific calibration; their low/nominal/high values are assumed
scenarios, **not measured heat or confidence intervals**. Missing inputs
produce an unknown estimate rather than zero. A generator explicitly reported
off can only yield zero after the remaining scenario inputs are supplied.

`control.room_advisory.predict_scenarios` consumes fresh measured room
temperatures and matching demand measurements plus these estimates and
explicit other-source heat scenarios. Suspect temperatures get extra
user-supplied uncertainty; stale, invalid and missing temperatures or demand
block prediction. It uses the existing coupled heat-balance model in five
minute steps: a direct KMP input enters only its assigned room, and transfer
to other rooms follows configured conductances. Other direct sources, including
Sensibo and the heater behind a Mill outlet, require their own explicit
estimated or measured scenarios; an empty mapping is an explicit declaration
that a room has no other sources. Unknown sources must not be represented by
empty mappings. The result is a one-step conditional prediction, not a
thermostat command or an optimized achievable allocation. Cost remains
unknown without a verified generator energy model.

The worker resolves rooms, radiator identities and required direct heat paths
from the reconciled house, then reads optional independently supplied equipment
inputs. `save_advisory` persists available outputs and assumptions in the
existing optimizer tables under a separate `advisory` mode; unavailable results
are recorded too. Authenticated `GET /api/room-advisory` returns the latest
result. The worker does not infer a live SPRSUN output from its name or Tado
demand, or infer direct heat from a device being enabled.

To opt in, migrate the database, create and reconcile a real house
configuration, authorize Tado, configure mapped rooms and targets, and set
`[room_advisory] enabled = true` in `heatautomation.conf`. Restart the
controller and web server. The worker reads
`config/room_advisory.json` (or `[room_advisory] equipment_path`), an
independently maintained JSON snapshot in the format of
`config/room_advisory.example.json`. The example's timestamp and physical
values are deliberately fictitious. A snapshot expires after ten minutes and
must carry a timezone-aware observation time, `measured` or `estimated`
classification and provenance. Supply **every configured direct heat source**
and one shared radiator generator with independently supported state, capacity,
water temperatures, radiator ratings and delivery-factor assumptions. Cost
rates are optional costs per kWh *delivered heat*, in the same currency;
missing rates produce an unknown cost. The outdoor value is the first weather
forecast entry, not a measured outdoor temperature. Missing inputs or any
unknown room temperature prevent a house prediction.

The worker currently supports one water circuit and mapped Tado room sensors;
it has no direct SPRSUN telemetry or automatic source-delivery estimator.
Its equipment snapshot must be supplied by a trusted external measurement or
estimation process, and individual inputs share one timestamp and provenance.
It does not automatically estimate confidence from water conditions, vary
weather over the horizon, calibrate radiator delivery, feed its estimates to
the learner or produce a full multi-interval plan. The existing room optimizer
still uses idealized heat allocation and must not be treated as a direct Tado
command. All outputs are conditional one-step scenarios, not validated
comfort guarantees.

To upgrade an existing SQLite database, run
`python -m heatautomation.house.migrate --database <path>`; it backs up the
database before applying migrations. Do not copy example ratings or thermal
parameters into a real installation as measured values.

The remainder describes the target architecture and phased plan; portions
have since been implemented. Automatic multi-room heating control remains
disabled. The example configuration contains placeholder physical values.

The example physical configuration is [config/house.example.json](../config/house.example.json). It uses placeholder physical values and is not loaded by the runtime. The real file will be `config/house.json`.

---

## 1. System architecture

### 1.1 Current state

The existing application is a single-zone controller:

- `main.py` controls the heat pump and the KMP pellet stove inline, gated by `TEST_MODE` from `[operation] test_mode`.
- `control/mpc.py` optimises one house-wide plan. Its `_validate` hard-codes the sources `{"heatpump", "pelletstove"}`.
- `control/heating_model.py` uses one house-wide demand curve.
- `learning/database.py` stores sensor readings in `thermal_sensors`, where `indoor_c` is a sensor name associated with an observation ID. It previously created tables with SQLAlchemy Core and `metadata.create_all`; it now requires a migrated legacy schema and does not create tables.
- `web/` is an optional FastAPI subprocess over the same SQLite file. It can write room targets, limits and overrides but does not dispatch device commands.

### 1.2 Target layering

```
              config/house.json  (physical: floors, rooms, devices, paths, priors)
                       │  validated by Pydantic, reconciled idempotently
                       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ SQLite (SQLAlchemy Core + Alembic)  — authoritative runtime store       │
│ runtime settings, measurements, learned parameters, schedules, plans    │
└────────────────────────────────────────────────────────────────────────┘
   ▲               ▲                       ▲                    ▲
   │ writes        │ reads/writes          │ reads              │ reads
┌──┴─────────┐ ┌───┴───────────────┐ ┌─────┴─────────────┐ ┌────┴──────────┐
│ collectors │ │ thermal/          │ │ control/          │ │ web/ (read    │
│ (devices/  │ │ state estimator,  │ │ room optimiser,   │ │ only, FastAPI)│
│  adapters) │ │ parameter learner │ │ MPC (later)       │ │               │
└──┬─────────┘ └───────────────────┘ └────┬──────────────┘ └───────────────┘
   │ read-only in phase 3                 │ requests
   │                                ┌─────┴──────────────┐
   │                                │ CommandGate        │
   │                                │ TEST_MODE → logs   │
   │                                │ live → adapter     │
   │                                └─────┬──────────────┘
   ▼                                      ▼
 Tado / Mill / KMP / heat pump adapters (capability-based)
```

Rules:

- The optimiser never imports a vendor adapter. It sees only capability protocols and the `CommandGate`.
- The web process never imports command paths. It reads the database only.
- Core optimisation code takes rooms, devices and paths as data. Adding a floor, room, thermostat or generator means editing configuration, not core logic.

### 1.3 Process model

- **Collector** (phase 3): polls read-only sources on an interval and writes `room_measurements` and `device_runtime_state`. It does not command devices.
- **Estimator/learner** (phase 4): runs after each collection cycle. It writes estimated room temperatures and learned parameters.
- **Optimiser** (phases 6–7): reads state, writes `optimizer_plans` and `optimizer_plan_intervals`. Its commands go through `CommandGate`.
- **Web**: unchanged process model.

### 1.4 Package layout (proposed)

```
config/
  house.example.json            # committed example, placeholder physics
  house.json                    # real physical config (see §11 for the commit decision)
alembic.ini
migrations/
  env.py
  versions/
    0001_legacy_baseline.py     # verifies old tables, creates only missing tables
    0002_house_physical_model.py # additive: house, devices, paths, thermal parameters
    0003_room_runtime.py        # additive: settings, schedules, measurements, runtime
heatautomation/
  house/
    config.py                   # Pydantic models, loader, canonical config hash
    models.py                   # SQLAlchemy Core tables
    reconcile.py                # idempotent reconciliation and explicit reset
    migrate.py, cli.py          # backup/migration and reconciliation commands
  devices/
    capabilities.py             # Capability enum and Protocol definitions
    command_gate.py             # TEST_MODE, audit, and dispatch
    tado.py, mill.py, kmp.py, sprsun.py, manual.py   # adapters (phases 3 and 5)
  thermal/
    model.py                    # RC room model: heat balance, discrete step
    observer.py                 # fixed-gain observer
  control/
    room_optimizer.py           # room-level optimiser (phase 6)
    mpc.py                      # existing; generalised in phase 7
```

---

## 2. Physical house configuration

`config/house.json` contains physical facts only. Runtime settings are not stored here (§3.1).

### 2.1 Sections

| Section | Contents | Notes |
|---|---|---|
| `schema_version` | integer | Pinned to `1`. Used for loader validation. |
| `house` | `id`, `name` | Single house for now; the `house` concept keeps the model general. |
| `floors` | `id`, `name`, `level` | Level is informational only. |
| `rooms` | `id`, `floor_id`, `name` | Stable UUID. |
| `devices` | `id`, `kind`, `name`, `room_id`, `adapter`, `external_id`, `capabilities`, `provenance` | `kind` is one of `heat_generator`, `emitter`, `thermostat`, `actuator_sensor`, `sensor`. |
| `heat_paths` | `id`, `generator_id`, and either `emitter_ids` (circuit) or `target_room_id` (direct) | Describes heat distribution, not physics. |
| `room_priors` | `room_id`, `capacity_j_per_k`, `ua_w_per_k`, `provenance` | Initial values only. |
| `thermal_links` | `room_a_id`, `room_b_id`, `conductance_w_per_k`, `provenance` | Symmetric. Stored once per unordered pair. |

### 2.2 Modelling rules

- **Generators** (`heat_generator`) produce heat: the SPRSUN heat pump, the KMP stove, and the electric heater.
- **Emitters** (`emitter`) deliver heat to a room: the radiators.
- **Thermostats** (`thermostat`) control and measure a room: Tado. They are not generators.
- **Actuators** (`actuator_sensor` with `set_power`) switch power: the Mill outlet. The electric heater is the generator behind the outlet.
- **Sensors** report temperature without control: the Mill outlet also reports temperature, so it is `actuator_sensor` with `read_temperature`.
- A **circuit** path (`emitter_ids`) means one generator feeds many emitters. Its delivered heat is distributed across open emitters by rating. Distribution is not a fixed percentage; see §5.3.
- A **direct** path (`target_room_id`) means the generator heats the room it sits in. Neighbour heating is never declared here. It emerges from `thermal_links`.

### 2.3 Provenance values

Every physical value carries `provenance`:

- `config`: entered deliberately by the user.
- `measured`: derived from a measurement campaign.
- `placeholder`: an example value, not calibrated.
- `unknown`: not known yet (for example, generator ratings).
- `unverified_api`: the device or capability is named in config but not yet confirmed against the live API (§8).

### 2.4 Example summary

- 2 floors, 11 rooms, 24 devices.
- 1 SPRSUN heat pump (generator, circuit), 10 radiators (emitters), 10 Tado thermostats.
- KMP in the living room (direct path).
- Electric heater in the storage room (direct path), behind the Mill outlet.
- 10 thermal links. Living room links only to kitchen, nursery and bathroom;
  seven links connect Floor 0 rooms. The office and Floor 0 have no initial
  thermal connection to KMP.

The living room is adjacent to the kitchen, nursery and bathroom. KMP reaches them only through `thermal_links`, not through a fixed share.

---

## 3. Database schema and migrations

### 3.1 Authority split

| Data | Authority | Written by |
|---|---|---|
| Floors, rooms, devices, paths, priors, initial links | `house.json` (reconciled into SQL) | reconciliation |
| Comfort profiles, min/max, targets | SQL | web/CLI |
| Overrides, schedules | SQL | web/CLI |
| Device runtime state | SQL | collectors, CommandGate |
| Measurements | SQL | collectors, estimator |
| Learned thermal parameters | SQL | learner |
| Optimiser plans and history | SQL | optimiser |

Database values take precedence. `house.json` only creates missing rows and updates the static fields of existing rows. It never overwrites learned values.

### 3.2 Table proposal

Static (reconciled from `house.json`). All primary keys are the UUID from config, stored as text.

```sql
CREATE TABLE houses (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  config_hash   TEXT,
  config_loaded_at TIMESTAMP
);

CREATE TABLE floors (
  id          TEXT PRIMARY KEY,
  house_id    TEXT NOT NULL REFERENCES houses(id) ON DELETE RESTRICT,
  name        TEXT NOT NULL,
  level       INTEGER,
  removed_at  TIMESTAMP            -- soft delete, §4.3
);

CREATE TABLE rooms (
  id          TEXT PRIMARY KEY,
  floor_id    TEXT NOT NULL REFERENCES floors(id) ON DELETE RESTRICT,
  name        TEXT NOT NULL,
  removed_at  TIMESTAMP
);

CREATE TABLE devices (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind IN ('heat_generator','emitter','thermostat','actuator_sensor','sensor')),
  name         TEXT NOT NULL,
  room_id      TEXT REFERENCES rooms(id) ON DELETE RESTRICT,
  adapter      TEXT,             -- null for passive emitters
  external_id  TEXT,
  controlled_by TEXT REFERENCES devices(id) ON DELETE RESTRICT,   -- emitter -> thermostat
  rated_kw     REAL,               -- generators
  rated_w_at_dt50 REAL,            -- emitters
  provenance   TEXT NOT NULL,
  removed_at   TIMESTAMP
);

CREATE TABLE device_capabilities (
  device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  capability  TEXT NOT NULL,       -- read_temperature, read_setpoint, write_setpoint, read_status, set_power, ...
  PRIMARY KEY (device_id, capability)
);

CREATE TABLE heat_paths (
  id              TEXT PRIMARY KEY,
  generator_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  path_type       TEXT NOT NULL CHECK (path_type IN ('circuit','direct')),
  target_room_id  TEXT REFERENCES rooms(id) ON DELETE RESTRICT,   -- direct only
  distribution    TEXT,                                           -- circuit only
  removed_at      TIMESTAMP
);

CREATE TABLE heat_path_emitters (
  heat_path_id TEXT NOT NULL REFERENCES heat_paths(id) ON DELETE RESTRICT,
  emitter_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  PRIMARY KEY (heat_path_id, emitter_id)
);

CREATE TABLE thermal_links (
  room_a_id          TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  room_b_id          TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  conductance_w_per_k REAL NOT NULL,               -- initial physical value
  prior_conductance_w_per_k REAL NOT NULL,         -- updated from config, learned parameter is separate
  provenance         TEXT NOT NULL,
  removed_at         TIMESTAMP,
  PRIMARY KEY (room_a_id, room_b_id),
  CHECK (room_a_id < room_b_id)                    -- symmetric, stored once
);
```

Runtime settings (SQL authority).

```sql
CREATE TABLE room_settings (
  room_id          TEXT PRIMARY KEY REFERENCES rooms(id) ON DELETE RESTRICT,
  comfort_profile  TEXT NOT NULL DEFAULT 'default',
  min_c            REAL,
  max_c            REAL,
  target_c         REAL,
  updated_at       TIMESTAMP NOT NULL
);

CREATE TABLE room_overrides (
  id          INTEGER PRIMARY KEY,
  room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  target_c    REAL NOT NULL,
  starts_at   TIMESTAMP NOT NULL,
  expires_at  TIMESTAMP NOT NULL,
  source      TEXT NOT NULL              -- web, cli
);

CREATE TABLE room_schedules (
  id          INTEGER PRIMARY KEY,
  room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  weekday     INTEGER NOT NULL,          -- 0..6
  start_min   INTEGER NOT NULL,          -- minutes after midnight
  end_min     INTEGER NOT NULL,
  target_c    REAL NOT NULL
);
```

Measurements and state.

```sql
CREATE TABLE room_measurements (
  id              INTEGER PRIMARY KEY,
  room_id         TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  quantity        TEXT NOT NULL,         -- 'temperature_c', 'setpoint_c', 'humidity_pct'
  value           REAL NOT NULL,
  unit            TEXT NOT NULL,
  observed_at    TIMESTAMP NOT NULL,     -- device timestamp, or received time if none
  received_at     TIMESTAMP NOT NULL,
  source_device_id TEXT REFERENCES devices(id) ON DELETE RESTRICT,
  quality         TEXT NOT NULL CHECK (quality IN ('good','suspect','stale','bad')),
  classification  TEXT NOT NULL CHECK (classification IN ('measured','estimated','predicted'))
);
CREATE INDEX ix_room_measurements_room_time ON room_measurements (room_id, quantity, observed_at);

CREATE TABLE device_runtime_state (
  device_id        TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE RESTRICT,
  reported_power   TEXT,                 -- 'on','off','unknown'
  reported_at      TIMESTAMP,
  commanded_power  TEXT,                 -- last command sent, not proof of delivery
  commanded_at     TIMESTAMP,
  connectivity     TEXT NOT NULL DEFAULT 'unknown'  -- 'ok','degraded','offline','unknown'
);

CREATE TABLE command_log (
  id           INTEGER PRIMARY KEY,
  device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  command      TEXT NOT NULL,
  payload      TEXT,
  requested_at TIMESTAMP NOT NULL,
  outcome      TEXT NOT NULL CHECK (outcome IN ('simulated','sent','rejected','failed','timeout')),
  detail       TEXT
);
```

Learned parameters and audit.

```sql
CREATE TABLE thermal_parameters (
  room_id         TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  name            TEXT NOT NULL,         -- 'inv_capacity', 'ua_over_c', 'g_over_c:<other_room>'
  value           REAL NOT NULL,         -- learned value, authoritative
  prior_value     REAL NOT NULL,         -- last value from config, for ridge target
  sample_count    INTEGER NOT NULL DEFAULT 0,
  covariance      REAL,                  -- diagonal term for RLS
  updated_at      TIMESTAMP NOT NULL,
  PRIMARY KEY (room_id, name)
);

CREATE TABLE thermal_parameter_events (
  id           INTEGER PRIMARY KEY,
  room_id      TEXT NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
  name         TEXT NOT NULL,
  old_value    REAL,
  new_value    REAL,
  reason       TEXT NOT NULL,            -- 'learned','reset','prior_updated','clamped'
  occurred_at  TIMESTAMP NOT NULL
);
```

Legacy tables are kept unchanged: `thermal_observations`, `thermal_sensors` (including the row named `indoor_c`), `thermal_sources`, `thermal_evaluations`, `mpc_plans`, `mpc_plan_intervals`, `mpc_plan_evaluations`. The `mpc_plans` tables serve as the optimiser history until phase 6 adds a room-level plan table.

### 3.3 Legacy mapping

- The legacy `thermal_sensors` row named `indoor_c` is kept. The current writer continues to write it.
- The `room_measurements` table is additive, but no legacy-to-room backfill runs until a `legacy_indoor_room_id` has been explicitly configured and verified.
- Earlier readings stay in the legacy table. The legacy writer is unchanged by the foundation implementation.

### 3.4 Migration strategy

Alembic is the only production schema manager. `create_all()` is removed from the production path, and new schema changes go through revisions.

1. **Revision 0001 (baseline)** verifies any existing legacy tables contain the expected columns and creates only missing legacy tables. This handles both an existing database and a new one; it does not replace existing tables.
2. **Revisions 0002–0003** add the physical house tables and room runtime tables. The current implementation does not yet backfill `room_measurements`.
3. **Backup first.** `python -m heatautomation.house.migrate --database <path>` uses SQLite's online backup API to create `<database>.<UTC timestamp>.bak` before `alembic upgrade`, and refuses to continue if the copy fails. The backup API accounts for SQLite WAL state.
4. **Downgrades** are not supported for data-bearing revisions. Recovery is a restore from the backup.
5. **Legacy compatibility**: the `thermal_sensors` row named `indoor_c` remains until every consumer reads `room_measurements`. Removing or migrating it is a separate later revision after the room mapping is explicit.

---

## 4. Configuration reconciliation

### 4.1 Algorithm

Reconciliation runs at startup and on an explicit `reconcile` command. It is idempotent.

1. Load `house.json`, validate it with Pydantic (`extra='forbid'`), and compute a canonical SHA-256 hash.
2. Open a single transaction.
3. For each static entity, upsert by stable ID:
   - Missing → insert.
   - Existing → update name, floor, room, adapter, external ID and provenance only.
   - Never touch `thermal_parameters.value`, `room_settings`, `room_overrides`, `room_schedules`, or measurements.
4. `thermal_parameters.prior_value` is refreshed from config. Learned `value` is not changed.
5. For each new `(room_id, name)` in `room_priors` or `thermal_links`, insert a parameter row with `value = prior_value`. Existing rows are untouched.
6. Entities that are absent from the file are soft-deleted (§4.3).
7. Record `houses.config_hash` and `config_loaded_at`.
8. Commit. Any validation error aborts before the first write.

Running reconciliation twice with the same file produces no changes.

### 4.2 Learned-value policy

- **Database wins.** A learned value is never overwritten by reload, restart, or a changed prior.
- **Initialise only.** A config value is used only when no row exists.
- **Explicit reset.** `reset-parameters --room <id> [--name <n>]` is a separate command. It first snapshots the current parameters into `thermal_parameter_events` and then sets `value = prior_value`. It does not delete history.
- **Prior change.** When a prior changes in config, `prior_value` is updated and an event is logged. The learned `value` does not move until the learner is told to re-anchor, which requires `reset-parameters`.

### 4.3 Removed rooms and devices

- A room or device removed from `house.json` gets `removed_at` set. The row is kept.
- Observations, commands, plans and parameter events referencing it are kept. Foreign keys use `ON DELETE RESTRICT`, so a hard delete is blocked by the database.
- Removed entities are excluded from active calculations (`WHERE removed_at IS NULL`).
- If the same ID reappears, `removed_at` is cleared. Its history is then active again, which is intended.
- Hard deletion is a manual, separate operation with a backup. It is not part of reconciliation.

---

## 5. Thermal model

### 5.1 Physics

For each room *i*:

```
C_i · dT_i/dt = Q_heating_i + Σ_j G_ij·(T_j − T_i) − UA_i·(T_i − T_out) + Q_gain_i
```

| Term | Meaning | Source |
|---|---|---|
| `C_i` | effective thermal capacity (J/K) | `thermal_parameters` (`inv_capacity`) |
| `Q_heating_i` | heat delivered into room *i* (W) | §5.3 |
| `G_ij` | symmetric inter-room conductance (W/K) | `thermal_links` / `thermal_parameters` |
| `UA_i` | external loss coefficient (W/K) | `thermal_parameters` (`ua_over_c`) |
| `T_i` | measured or estimated room temperature (°C) | measurements / estimator |
| `T_out` | outdoor temperature (°C) | outdoor source (§7 uncertainty) |
| `Q_gain_i` | internal gains: occupants, appliances, sun | estimated, default 0 |

Rules:

- The target temperature never appears in the heat balance. Targets are used only in the demand and optimiser logic.
- Heat generation, delivery, room-to-room transfer, external loss and internal gain are separate terms.
- `G_ij` is symmetric: `G_ij = G_ji`. It is stored once per unordered pair.

### 5.2 Discretisation

With time step Δt:

```
T_i(k+1) = T_i(k) + Δt/C_i · [Q_i(k) + Σ_j G_ij·(T_j(k) − T_i(k)) − UA_i·(T_i(k) − T_out(k)) + Q_gain_i(k)]
```

Stability requires Δt small against the fastest coupled time constant, conservatively bounded by `C_i / (2·Σ_j G_ij + UA_i)`. The simulation subdivides a requested step to keep its explicit-Euler update within that bound.

### 5.3 Heat generation and delivery

Heat is modelled in two stages.

**Generation**: the generator's output, in W.

- The KMP stove has `Q_KMP(k)` as an estimate. The stove has no measured output in this design, so the estimate is classified as `estimated`. It is calculated from the stove's reported state and rated power, if the adapter provides them.
- The SPRSUN heat pump is uncontrolled and unmonitored. Its output is `unknown`. The circuit-level heat is inferred from the residual of the room-balance equations, and the result is classified as `estimated`.
- The electric heater is treated as `Q = P_rated` only while the outlet reports power on. Power reported on is a status, not proof that heat is delivered, so the result is `estimated` with a lower confidence.

**Delivery**: heat from a generator is assigned to rooms.

- **Direct path** (KMP, storage heater): `Q_heating_target = Q_generator`. Nothing is assigned to other rooms directly.
- **Circuit path** (heat pump → radiators): circuit output is distributed to emitters by rating among emitters whose valve is open. The distribution is by *rating-weighted open valves*, not fixed shares. Valve-open is a Tado setpoint signal, not measured flow, so the emitter-level delivery is `estimated` until the radiator circuit is instrumented.

Neighbour heating from KMP never appears as a delivery term. It appears only through `G_ij(T_j − T_i)` in the balance.

### 5.4 Room demand

Heat demand for room *i* over the horizon is the power needed to reach the target from the current estimate, with the same model:

```
Q_demand_i = C_i · (T_target_i − T_i_est)/τ_i + UA_i·(T_i_est − T_out) − Σ_j G_ij·(T_j_est − T_i_est)
```

with `τ_i` a chosen response time. This is an inversion of the same physics, so it stays consistent with the balance. The target enters only here, not in the balance. The formula replaces the house-wide demand curve from `control/heating_model.py`.

### 5.5 Parameter learning

Parameters `θ_i = [1/C_i, UA_i/C_i, G_ij/C_i for neighbours]` are learned from

```
dT_i/dt ≈ (1/C_i)·Q_i + Σ_j (G_ij/C_i)·(T_j − T_i) − (UA_i/C_i)·(T_i − T_out)
```

- Regressors are built from good-quality measured temperatures only, plus estimated `Q` (§5.3). `Q` estimates are flagged, so the learner can exclude windows where the heat input is unknown.
- Estimation uses **recursive least squares with forgetting** (λ ≈ 0.995). It is incremental, so it fits a Raspberry-Pi-class host.
- A **ridge term toward the config prior** keeps the estimate stable when data are poor.
- Parameters are **clamped** to physical bounds (for example, `C_i ∈ [0.5, 50] MJ/K`, `UA_i ∈ [5, 500] W/K`, `G_ij ∈ [0, 200] W/K`). A clamp is logged in `thermal_parameter_events`.
- A **symmetry constraint** applies: `G_ij/C_i` and `G_ji/C_j` are tied through `G_ij`, so only one `G` per pair is learned. Identifiability depends on the difference in `C_i` and `C_j`, so this is an open risk (§7).

### 5.6 Measured, estimated, predicted, unknown

- **Measured**: directly reported by a sensor with `quality='good'`.
- **Estimated**: produced by the observer or by inverting the model from other measured values.
- **Predicted**: produced by forward simulation for future horizons.
- **Unknown**: no valid value. Stored as absence, not as a value. A room without a sensor and no valid estimate is reported as `unknown` with no fabricated temperature.

---

## 6. State estimation and parameter learning

### 6.1 Observer

A fixed-gain observer (Luenberger style) corrects the model prediction toward the measurement:

```
T̂_i(k+1) = T̂_i(k) + Δt/C_i · [Q_i + Σ_j G_ij(T̂_j − T̂_i) − UA_i(T̂_i − T_out)] + L_i·(y_i(k+1) − T̂_i(k+1)|model)
```

- Only rooms with a valid measurement receive a correction. Rooms with a stale or missing sensor use the prediction only, and their classification becomes `estimated`. After a configurable timeout they become `unknown`.
- `L_i` is fixed per room, a tunable value in `[0, 1]`, starting at 0.3 for a measured room.
- A measurement with `quality != 'good'` produces no correction.

### 6.2 Why not a Kalman filter yet

A Kalman filter is deferred until one of these holds:

- Multi-room MPC needs state covariance to propagate uncertainty over the horizon.
- Many rooms are unmeasured and the observer's fixed gains become unreliable.
- Sensor noise and model error are large enough that a fixed gain is measurably worse than a tuned Kalman gain on recorded data.

The observer is replaceable behind the same interface, so the switch does not change callers.

### 6.3 Learning schedule

- The learner runs after each collection cycle, at most once per configured interval.
- It uses only windows where all required measurements are `good`.
- It records `sample_count` and `covariance`, and logs every change in `thermal_parameter_events`.

---

## 7. Device interfaces

### 7.1 Capabilities

Devices do not share a universal interface. Each adapter declares a set of capabilities, and callers ask for what they need.

```python
from enum import Enum
from typing import Protocol, runtime_checkable

class Capability(str, Enum):
    READ_TEMPERATURE = "read_temperature"
    READ_SETPOINT = "read_setpoint"
    WRITE_SETPOINT = "write_setpoint"
    READ_STATUS = "read_status"
    SET_POWER = "set_power"
    READ_OPEN_STATE = "read_open_state"    # valve/open signal, not measured flow

@runtime_checkable
class TemperatureSensor(Protocol):
    def read_temperature(self) -> "Reading": ...

@runtime_checkable
class SetpointController(Protocol):
    def read_setpoint(self) -> "Reading": ...
    def write_setpoint(self, celsius: float) -> "CommandResult": ...

@runtime_checkable
class HeatActuator(Protocol):
    def read_status(self) -> "Reading": ...
    def set_power(self, on: bool) -> "CommandResult": ...

@runtime_checkable
class HeatGenerator(Protocol):
    def read_status(self) -> "Reading": ...
    def estimated_output_w(self) -> "Estimate | None": ...   # None when unknown

@runtime_checkable
class HeatEmitter(Protocol):
    def read_open_state(self) -> "Reading": ...
    def rated_w_at_dt50(self) -> float | None: ...
```

Common types:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

@dataclass(frozen=True)
class Reading:
    value: float | str | None           # None means unknown
    unit: str
    observed_at: datetime               # device timestamp, or receive time if absent
    received_at: datetime
    source_device_id: str
    quality: Literal["good", "suspect", "stale", "bad"]
    classification: Literal["measured", "estimated", "predicted", "unknown"]

@dataclass(frozen=True)
class CommandResult:
    outcome: Literal["simulated", "sent", "rejected", "failed", "timeout"]
    detail: str | None = None
    # A sent command is not proof of delivered heat. Only sensors prove effect.
```

Pydantic models mirror the config, and SQLAlchemy Core handles persistence. The two sets are kept separate, so the database layer does not depend on the config schema.

### 7.2 Mapping devices to capabilities

| Device | Kind | Capabilities | Notes |
|---|---|---|---|
| SPRSUN CGK/C-12(LIO) heat pump | `heat_generator` | none | Uncontrollable and unmonitored. Output is `unknown`. |
| KMP Lilla Frö | `heat_generator` | `read_status`, `set_power` | Adapter wraps the existing KMP functions. |
| Electric heater (storage) | `heat_generator` | via outlet | Output is an estimate from the outlet state. |
| Mill smart outlet | `actuator_sensor` | `read_temperature`, `read_status`, `set_power` | Built-in temperature sensor, per the user. |
| Tado thermostat | `thermostat` | `read_temperature`, `read_setpoint`, `write_setpoint` | Controller, not generator. Specifics in §8. |
| Radiator | `emitter` | `read_open_state` (via Tado) | Emitter only. Output is estimated. |

---

## 8. Tado and Mill integration research

Status legend: **verified** = confirmed against a primary source; **secondary** = from a community source; **unknown** = not verified. Nothing below is assumed to work until §11 phase 3 confirms it with a read-only test.

### 8.1 Tado

- **Authentication.** Tado's [official instructions](https://help.tado.com/en/articles/8565472-how-do-i-authenticate-to-access-the-rest-api) specify OAuth device authorization. The device authorization endpoint, public client ID, offline scope, token endpoint and refresh rotation are corroborated by the [python-tado implementation](https://github.com/wmalgadey/PyTado). Tokens are persisted outside physical configuration.
- **API.** The consumer API is not officially published. The [community OpenAPI specification](https://github.com/kritsel/tado-openapispec-v2) and python-tado corroborate the Classic endpoints used by the adapter: `/me`, home zones, bulk zone states, home devices, zone capabilities and zone overlay. These are best-effort and not endorsed by Tado.
- **Measurements.** Zone states can contain inside temperature, requested target, heating-power percentage, connectivity and provider timestamps. Device sensor identity and delivered radiator heat are not established by those fields. Preserve absent data as unknown and offline data as stale.
- **Controls.** The adapter sends HEATING overlays ending at the next schedule block, only via the audited command gate. Temperature ranges and step come from zone capabilities. TEST_MODE never dispatches, and automatic control remains unwired.
- **Generation.** The adapter targets Classic/V3. Tado X is not validated; the community spec describes only partial compatibility, while [Home Assistant's Classic integration](https://www.home-assistant.io/integrations/tado/) does not support Tado X.
- **Rate limits and errors.** Home Assistant documents subscription-dependent daily quotas. The adapter caches endpoint responses for five minutes by default, respects `Retry-After` on 429, and uses a 30-minute fallback when the header is absent. It does not retry writes and does not return expired cache entries on errors.
- **Loss of connectivity.** Connection, authentication and rate-limit failures are surfaced explicitly. A cached provider observation includes its receipt and provider timestamps; a setting or command is not evidence of physical heat delivery.

### 8.2 Mill

- **Local API (Gen 3).** Mill's [official Gen 3 REST API](https://github.com/Mill-International-AS/Generation_3_REST_API) lists the Wi-Fi Socket Gen 3. It documents `GET /status`, `GET /control-status` and `POST /operation-mode`. The [Home Assistant local client](https://github.com/Danielhiversen/pyMillLocal) uses operation modes `OFF` and `Control individually`. Individual mode follows the device setpoint and is not a direct confirmed socket relay command.
- **Authentication.** Verified: after `POST /set-api-key`, the device switches to HTTPS with a self-signed certificate. Requests carry an `Authentication` header. The adapter needs fingerprint pinning or an explicit trust store.
- **Socket on/off, power and temperature.** The API describes `switched_on`, `ambient_temperature` and `current_power` for devices, but does not establish socket-specific relay readback or electrical metering. The adapter exposes the fields without treating the power as measured socket consumption or the device mode as proof of heat. Socket-specific behavior and sensor accuracy still require hardware validation.
- **Rate limits.** Not published. The README recommends reasonable polling. Design: 5-minute minimum interval until measured.
- **Cloud API.** Unknown. A secondary source says access requires developer approval. The fetch of the official Mill cloud API document failed, so the authentication flow is unverified.
- **Loss of connectivity.** The standalone monitor latches on communication loss and attempts to turn off when an enabled device is reachable again. The gate audits mode commands. An independent cutoff is necessary: the monitor cannot guarantee off during outages, process failure, database failure or after stopping the monitor. Runtime counters and overrides are process-local.

### 8.3 SPRSUN heat pump

- No research done. Per the user, it cannot be controlled. Its output and status are `unknown`. No adapter in phases 3–5. The circuit output is estimated from the room balance only.

### 8.4 Adapter rule

Each vendor is one adapter module that implements only the protocols it supports. Adapters return `Reading` objects with `quality` set. An adapter that cannot reach its device returns `unknown`, never a cached value presented as current.

---

## 9. Failure handling and TEST_MODE

### 9.1 CommandGate

All commands pass through `CommandGate`:

```python
class CommandGate:
    def __init__(self, test_mode: bool, audit: "CommandLog"): ...
    def request(self, device_id: str, command: str, payload: dict) -> CommandResult:
        # TEST_MODE: log and return CommandResult(outcome="simulated")
        # live: dispatch to the adapter, record the result, return it
```

- In `TEST_MODE` the gate records `simulated` and never calls an adapter. Heat balance and plans still run.
- A sent command is recorded as `sent`. This does not update `Q` estimates as delivered.
- The gate refuses a command if the device's connectivity is `offline` or `unknown`.
- Automatic control stays disabled until the explicit enable flag is set. This design does not set it.

### 9.2 Failure cases

| Failure | Behaviour |
|---|---|
| Sensor missing | Room becomes `estimated`, then `unknown` after timeout. No fabricated temperature. |
| Stale reading | Treated as `stale`, not as measured. |
| Unknown generator output | Estimate from the balance, classification `estimated`. |
| Device offline | `connectivity='offline'`. Commands rejected. |
| API rate-limited (429) | Back off with `Retry-After`. Record and continue. |
| Config invalid | Reconciliation aborts before any write. Last valid state stays. |
| Database migration fails | Restore from the pre-migration backup. Never continue with a half-applied revision. |
| Learner diverges | Clamp to bounds, log an event, keep the previous value. |

### 9.3 TEST_MODE behaviour summary

- Collectors run in both modes (read-only).
- Estimator and learner run in both modes.
- Optimiser plans and writes plans in both modes.
- `CommandGate` simulates in `TEST_MODE`.
- Live commands require `TEST_MODE = false` and an explicit enable flag.

---

## 10. Phased implementation plan

Each phase has an exit criterion. No phase enables automatic heating control.

1. **Configuration and schema.** Implemented Pydantic models, validation, canonical config hash, SQLAlchemy Core tables, and idempotent reconciliation. Exit: config reconciles twice with no diff; invalid config is rejected before any write.
2. **Alembic and legacy compatibility.** Implemented baseline revision, backup wrapper, and additive revisions 0002–0003. `create_all` is removed from the runtime storage constructor. Verified against a legacy database fixture with the old rows and `indoor_c` sensor row preserved.
3. **Read-only room and device measurements.** Pending API verification. Mill local API, Tado read-only status, KMP status, and the SPRSUN marked `unknown`. Add measured room records only after device identity and supported read capabilities are verified.
4. **Thermal model and shadow demand.** Implemented a room-level RC step, demand calculation, and fixed-gain observer. Shadow runner, parameter learner, and measured-vs-predicted reporting remain to be wired after the measurement adapters.
5. **Device adapters and commands behind TEST_MODE.** Implemented capability Protocols and an audited command gate. No vendor adapter or live command is connected. Live command paths remain disabled until device verification and a separate explicit enable decision.
6. **Room-level optimiser.** Replaces the house-wide plan with per-room demand and per-source allocation. Exit: the room plan matches shadow demand on recorded data.
7. **Predictive multi-room MPC.** Generalise `control/mpc.py` away from the hard-coded sources. Exit: simulated scenarios beat the baseline on comfort and energy.
8. **Web GUI extensions.** Room monitoring, runtime targets, per-room simulation preheating limits, weekly schedules and overrides, simulation schedule viewing, and validated physical JSON editing with explicit reconciliation are available. Automatic room control, complete histories and learned-parameter management remain future work; device commands remain disabled.

Estimated effort is deliberately not given. Phase 3 depends on the device API verification in §8.

---

## 11. Remaining uncertainties

1. **Tado**: actual house generation (Classic or X), zone IDs, subscription quota, behavior against the real account and devices, and any use of overlay DELETE remain to be verified.
2. **Mill**: whether the outlet is Gen 3 (required for the local API). Socket power and temperature endpoints. Cloud API access and authentication.
3. **SPRSUN**: whether any data or control is exposed. Assumed none.
4. **Physical priors**: all `C`, `UA` and `G` values are placeholders. Generator ratings are `null`.
5. **Adjacency**: the `thermal_links` in the example are placeholders for the real floor plan.
6. **Identifiability**: whether symmetric `G_ij` can be learned separately from `C_i` when rooms have different capacities.
7. **Legacy room mapping**: which room `indoor_c` measures. Not stated in the existing code.
8. **Cadence**: the observation cadence (about 5 minutes in the shadow monitor) and the 15-minute MPC step need reconciling.
9. **Outdoor temperature**: the source is not identified. The current project's source needs confirmation.
10. **Real config commit**: whether `config/house.json` (with device external IDs) should be committed or kept local. Recommended: commit only the example; ignore the real file.
11. **Database baseline**: whether the existing `thermal_history.sqlite3` matches the legacy schema exactly. Verify before stamping.
12. **Pydantic v2 compatibility**: `web/app.py` uses only `BaseModel` and `Field`, so it should be compatible. Confirm in phase 2.

---

## 12. Decisions incorporated

- Config split: `config/house.json` for physical configuration; SQL is authoritative for runtime data.
- Stable UUIDs for floors, rooms and devices. Relationships use composite keys.
- DB values take precedence. Reload never overwrites learned parameters. Reset is explicit.
- Soft delete for removed rooms and devices. Observations are never deleted.
- Alembic for migrations. `create_all` retired from production. `indoor_c` preserved.
- Physics-based RC model, symmetric `G_ij`, target never in the balance.
- Generators, emitters, actuators, thermostats and sensors modelled separately.
- KMP heats the living room directly. Neighbours receive heat only through thermal links.
- Mill outlet is an actuator with a temperature sensor. The electric heater is the generator.
- Observer first. Kalman deferred with stated conditions.
- Measured, estimated, predicted and unknown classified explicitly.
- Tado and Mill integrations kept behind capability adapters. No endpoints assumed without verification.
- Dependencies: `alembic` and `pydantic` added to `requirements.txt` and `setup.py`. FastAPI remains in `requirements-web.txt`.

---

## 13. Implementation status and files

- `.gitignore`: ignores `frontend/node_modules/`, `thermal_history.sqlite3*`, and the local `config/house.json`.
- `docs/multi-room-architecture.md`: this document.
- `config/house.example.json`: example physical configuration with placeholder values.
- `requirements.txt` and `setup.py`: include Alembic and Pydantic v2. `setup.py` now declares Python 3.9+, matching the documented runtime.
- `alembic.ini`, `migrations/`: baseline plus additive physical and room-runtime revisions.
- `heatautomation/house/`: Pydantic config, migration/backup CLI, idempotent reconciliation, and explicit learned-parameter reset.
- `heatautomation/house/models.py`: SQLAlchemy Core definitions for the new tables.
- `heatautomation/thermal/`: room-level physics, demand calculation, and fixed-gain observer.
- `heatautomation/devices/`: capability protocols and a TEST_MODE-first command gate. No vendor adapter is connected.
- `heatautomation/learning/database.py`: no longer calls `metadata.create_all`; requires a migrated legacy schema.
- `tests/house/` and existing DB-backed test setup: migration, reconciliation, model and safety regression tests.
- `README.md`: migration/reconciliation/reset commands and the implementation boundary.

No vendor integration, room-level optimizer, automatic heating control, or GUI extension has been connected to the existing controller. RLS learning, room-measurement ingestion, legacy backfill, and shadow reporting are also pending.

---

## 14. Risks and assumptions requiring validation

- **Unverified device APIs.** Tado and Mill capabilities may differ from the assumptions in §8. Phase 3 must confirm each read path before anything depends on it.
- **Tado is undocumented.** The community spec may break without notice. Keep adapters thin and isolated.
- **Mill Gen 3 requirement.** If the outlet is not Gen 3, the local API is unavailable.
- **Physics without calibration.** Placeholder priors could produce misleading demand. Shadow mode (phase 4) must run before any output is used.
- **Identifiability.** Symmetric `G_ij` may be poorly determined. Monitor parameter variance and clamp events.
- **Estimated delivery.** Valve-open does not prove heat delivery. Demand decisions must allow for this uncertainty.
- **Schema migration.** SQLite `ALTER` limits require batch mode. The backup step must succeed before every upgrade.
- **Commit exposure.** Real device IDs must not be committed to a public repository without review.
