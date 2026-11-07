# Web application architecture and implementation plan

## Existing boundaries

`heatautomation.main` owns the control loop and the in-process `CONTROL_LOCK`.
The thermal monitor collects provider-reported readings every five minutes via
`learning.collection` and writes observations through `learning.database`.
The CSV logger records selected sources, not verified equipment output. MPC is
an experimental **shadow** planner: `control.mpc_monitor` stores plans but does
not execute them. The thermal model and `control.heating_model` already provide
prediction and cost calculations; they should not be reimplemented in the GUI.

The lock is not shared across processes. A web process must never import the
controller and call equipment clients as a substitute for a command interface.
Stopping or restarting the web process must not affect the controller.

## Proposed structure

```
heatautomation/web/       FastAPI app, schemas, authentication, read adapters
frontend/                 React/TypeScript Vite client
tests/web/                API, authorization, freshness and storage tests
docs/web-architecture.md  Architecture, API contract and staged roadmap
```

In the first stage the web app reads the existing thermal SQLite database using
SQLAlchemy and existing table definitions, with a read-only connection. It does
not create or migrate the controller database, contact providers, load joblib
models, or write device settings. Data is a cached observation, not a live
provider query or proof of actual heat output. The timestamp is for the whole
collection, whose provider reads are sequential. Missing values stay missing;
old observations are marked stale. WebSocket updates poll this store and are
therefore as fresh as the controller's collection cadence.

The client uses a same-origin API (Vite proxies during development). The initial
single-user login uses an environment-supplied password and an HttpOnly,
SameSite session cookie; the websocket also checks the configured origin.
Bind locally by default. Remote use requires HTTPS, a secure cookie, a trusted
reverse proxy and additional network/access controls. Do not put credentials in
the frontend or publish the backend port. The initial implementation is not an
Internet-facing identity provider.

## Data and API requirements

Stage one provides `POST /api/session`, `DELETE /api/session`, `GET /api/status`
and `WS /api/live`. FastAPI's `/docs` and `/openapi.json` describe HTTP schemas.
Each live message has the same shape as the status response; errors and missing
or stale data are explicit. Only authenticated sessions can read status or
subscribe. APIs never return integration secrets. The session endpoint accepts
only the web login password, not provider credentials.

Future historical endpoints should query bounded, paginated time ranges from
`thermal_observations`, `thermal_sensors`, `thermal_sources`, and evaluations;
do not use `ThermalDatabase.history()` for unbounded web requests. Scheduler
endpoints can read `mpc_plans`, `mpc_plan_intervals` and
`mpc_plan_evaluations`, preserving plan creation times, model version, changes,
units and uncertainty. Existing observations do not contain measured energy,
pellet consumption, physical heat output, complete provider health, or all
requested logs. Those require explicitly instrumented controller-side data,
with retention, privacy, migrations and provenance defined before displaying
them; never invent values or treat estimates as measurements.

Configuration changes need a validated, versioned controller-owned settings
interface and an explicit apply/restart policy; secrets remain backend-only.
Manual commands require a durable, authenticated, audited controller-owned
command channel with idempotency, expiry, authorization, acknowledgements,
manual/automatic arbitration, interlocks and conflict tests. A SQLite write or
calling an integration from the API is not that interface. The shadow MPC
cannot be presented as an active predictive scheduler without separate safety
design and validation.

## Incremental plan

1. **Foundation:** authenticated read-only FastAPI, responsive dashboard,
   WebSocket snapshot updates, stale/error handling, tests and run instructions.
2. **History and shadow schedules:** bounded queries, charts, interval details,
   plan comparison and honest data availability; retain shadow-only labeling.
3. **ML observability:** model metadata and stored evaluation/prediction data,
   carefully distinguish holdout error from confidence. Load trusted artifacts
   only in a controlled backend process.
4. **Monitoring:** structured, redacted, retained events and integration health
   recorded by the controller, then searchable read APIs.
5. **Settings and controls:** design and test the controller-owned command and
   configuration interfaces before exposing any write operations; add audited
   confirmations and explicit requested-versus-observed state.
6. **Production hardening:** deployment-specific identity, TLS, rate limits,
   observability, backups and migration procedures; assess safety before any
   automated scheduler execution.
