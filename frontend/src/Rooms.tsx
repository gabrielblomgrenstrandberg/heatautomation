import React, { useEffect, useState } from "react";
import HouseConfiguration from "./HouseConfiguration";
import RoomSchedules from "./RoomSchedules";

type Device = { id: string; name: string; adapter: string | null; connectivity: string };
type Room = {
  id: string; floor_id: string; name: string; temperature_c: number | null;
  temperature_stale: boolean; temperature_quality: string; target_c: number | null; base_target_c: number | null;
  min_c: number | null; max_c: number | null; predicted_c: number | null;
  max_preheat_c: number | null;
  prediction_at: string | null;
  comfort: "cold" | "warm" | "comfortable" | "unknown";
  override_expires_at: string | null; devices: Device[];
  heat_inputs: { observed_at: string; sources: { id: string; watts: number; classification: "measured" | "estimated" }[] } | null;
  sources: { id: string; name: string; type: string }[];
};
export type HouseData = {
  floors: { id: string; name: string; level: number | null }[];
  rooms: Room[];
  thermal_links: { room_a_id: string; room_b_id: string; conductance_w_per_k: number }[];
  message: string;
};
type History = {
  temperatures: { at: string; value: number; quality: string; classification: string }[];
  predictions: { at: string; predicted_c: number; actual_c: number | null }[];
  target_history: { at: string; value: number; source: string }[];
  source_contribution_history: { at: string; source_id: string; watts: number; classification: "measured" | "estimated" }[];
  heating_activity_history: { at: string; value: number; source_device_id: string | null }[];
  measured_expenditure: { at: string; value: number; source_device_id: string | null }[];
};
type Plan = { created_at: string; intervals: { room_id: string; start: string; data: {
  predicted_temperature_c: number; target_c: number; heating_demand_kw?: number;
  selected_sources: string[]; expected_cost: number; explanation: string;
  planned_heat_kw: Record<string, number>; inter_room_transfer_kw: Record<string, number>;
} }[] };
type Scenario = { low: number; nominal: number; high: number };
type Advisory = {
  available: boolean; created_at: string; model_version: string; reasons: string[];
  connection_warnings?: string[];
  rooms?: { room_id: string; current_temperature_c: number; temperature_quality: string;
    target_c: number; estimated_radiator_w: Scenario; estimated_other_heat_w: Scenario;
    predicted_temperature_c: Scenario }[];
  house?: { estimated_heating_cost: Scenario | null; estimated_heating_cost_currency: string | null };
};

function show(value: number | null | undefined, unit = "°C") {
  return value == null ? "Unknown" : `${value.toFixed(1)} ${unit}`;
}

async function api(url: string, options?: RequestInit) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail :
      Array.isArray(detail) ? detail.map(item => item.msg).join("; ") : `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function TemperatureChart({ history }: { history: History }) {
  const rows = history.temperatures.filter(item => item.classification === "measured" && item.quality === "good" && Number.isFinite(item.value));
  if (!rows.length) return <p className="text-sm text-stone-500">No room temperature history available.</p>;
  const min = Math.min(...rows.map(item => item.value)) - 1;
  const max = Math.max(...rows.map(item => item.value)) + 1;
  const firstTime = new Date(rows[0].at).getTime();
  const lastTime = new Date(rows[rows.length - 1].at).getTime();
  const points = rows.map(item =>
    `${lastTime === firstTime ? 50 : (new Date(item.at).getTime() - firstTime) * 100 / (lastTime - firstTime)},${95 - 90 * (item.value - min) / (max - min)}`
  ).join(" ");
  return <div><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-40 w-full" role="img" aria-label="Recorded room temperature history">
    <polyline fill="none" stroke="currentColor" strokeWidth="1.5" className="text-teal-500" points={points}/>
  </svg><div className="flex justify-between text-xs text-stone-500"><span>{new Date(rows[0].at).toLocaleString()}</span><span>{new Date(rows[rows.length - 1].at).toLocaleString()}</span></div></div>;
}

function PredictionChart({ history }: { history: History }) {
  const rows = history.predictions.filter(item => Number.isFinite(item.predicted_c) && Number.isFinite(Date.parse(item.at)));
  if (!rows.length) return <p className="mt-2 text-sm text-stone-500">No prediction comparisons available.</p>;
  const times = rows.map(row => Date.parse(row.at));
  const values = rows.flatMap(row => row.actual_c == null ? [row.predicted_c] : [row.predicted_c, row.actual_c]);
  const first = Math.min(...times), last = Math.max(...times);
  const minimum = Math.min(...values) - 1, maximum = Math.max(...values) + 1;
  const x = (time: number) => first === last ? 50 : 2 + 96 * (time - first) / (last - first);
  const y = (temperature: number) => 95 - 90 * (temperature - minimum) / (maximum - minimum);
  return <div className="mt-3"><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-40 w-full" role="img" aria-label="Predicted and measured temperatures at forecast target times">
    {rows.map((row, i) => <React.Fragment key={i}>
      <circle cx={x(times[i])} cy={y(row.predicted_c)} r="1.5" className="fill-teal-500"/>
      {row.actual_c != null && <circle cx={x(times[i])} cy={y(row.actual_c)} r="1.5" className="fill-orange-500"/>}
    </React.Fragment>)}
  </svg><div className="flex flex-wrap justify-between gap-2 text-xs text-stone-500"><span>Teal: predicted · Orange: actual</span><span>{new Date(first).toLocaleString()} – {new Date(last).toLocaleString()}</span></div></div>;
}

function HeatInputChart({ history, names }: { history: History; names: Map<string, string> }) {
  const rows = history.source_contribution_history.filter(row => Number.isFinite(row.watts) && Number.isFinite(Date.parse(row.at)));
  if (!rows.length) return <p className="mt-2 text-sm text-stone-500">No recorded heat-input snapshots for this room.</p>;
  const times = rows.map(row => Date.parse(row.at)), first = Math.min(...times), last = Math.max(...times);
  const max = Math.max(1, ...rows.map(row => row.watts));
  return <div className="mt-3"><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-40 w-full" role="img" aria-label="Recorded room heat inputs">
    {rows.map((row, i) => <circle key={i} cx={first === last ? 50 : 2 + 96 * (times[i] - first) / (last - first)}
      cy={95 - 90 * row.watts / max} r="1.5" className={row.classification === "measured" ? "fill-teal-500" : "fill-orange-500"}>
      <title>{names.get(row.source_id) || row.source_id}: {row.watts} W ({row.classification}) at {new Date(row.at).toLocaleString()}</title></circle>)}
  </svg><div className="text-xs text-stone-500">Teal: measured · Orange: estimated · Scale: 0–{max.toFixed(0)} W. Each point is a delivered-heat input, not a command.</div></div>;
}

function TargetChart({ history }: { history: History }) {
  const rows = history.target_history.filter(row => Number.isFinite(row.value) && Number.isFinite(Date.parse(row.at)));
  if (!rows.length) return <p className="mt-2 text-sm text-stone-500">No recorded base-target changes yet.</p>;
  const first = Date.parse(rows[0].at), last = Date.parse(rows[rows.length - 1].at);
  const low = Math.min(...rows.map(row => row.value)) - 1, high = Math.max(...rows.map(row => row.value)) + 1;
  return <div className="mt-3"><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-32 w-full" role="img" aria-label="Recorded room base-target changes">
    {rows.map((row, i) => <circle key={i} cx={first === last ? 50 : 2 + 96 * (Date.parse(row.at) - first) / (last - first)}
      cy={95 - 90 * (row.value - low) / (high - low)} r="1.5" className="fill-teal-500">
      <title>{show(row.value)} at {new Date(row.at).toLocaleString()}</title></circle>)}
  </svg><p className="text-xs text-stone-500">Recorded web changes to the base target only; this is not historical effective schedule or override data.</p></div>;
}

function RecordedChart({ rows, title, unit }: { rows: { at: string; value: number }[]; title: string; unit: string }) {
  const points = rows.filter(row => Number.isFinite(row.value) && Number.isFinite(Date.parse(row.at)));
  if (!points.length) return <p className="mt-2 text-sm text-stone-500">No verified {title.toLowerCase()} measurements recorded.</p>;
  const times = points.map(row => Date.parse(row.at));
  const first = Math.min(...times), last = Math.max(...times);
  const values = points.map(row => row.value), low = Math.min(0, ...values), high = Math.max(low + 1, ...values);
  return <div className="mt-3"><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-32 w-full" role="img" aria-label={title}>
    {points.map((row, i) => <circle key={i} cx={first === last ? 50 : 2 + 96 * (times[i] - first) / (last - first)}
      cy={95 - 90 * (row.value - low) / (high - low)} r="1.5" className="fill-teal-500">
      <title>{show(row.value, unit)} at {new Date(row.at).toLocaleString()}</title></circle>)}
  </svg><p className="text-xs text-stone-500">Measured samples · scale {show(low, unit)}–{show(high, unit)}</p></div>;
}

export default function Rooms({ house, error }: { house: HouseData | null; error: string }) {
  const [floor, setFloor] = useState("");
  const [selected, setSelected] = useState("");
  const [history, setHistory] = useState<History | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [advisory, setAdvisory] = useState<Advisory | null>(null);
  const [advisoryError, setAdvisoryError] = useState("");
  const [message, setMessage] = useState("");
  const [target, setTarget] = useState("");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
  const [preheat, setPreheat] = useState("2");
  const [override, setOverride] = useState("");
  const [minutes, setMinutes] = useState("60");
  useEffect(() => {
    if (house?.floors.length && !house.floors.some(item => item.id === floor)) setFloor(house.floors[0].id);
  }, [house, floor]);
  const room = house?.rooms.find(item => item.id === selected);
  useEffect(() => {
    if (!room) return;
    setTarget(room.base_target_c == null ? "" : String(room.base_target_c));
    setMinimum(room.min_c == null ? "" : String(room.min_c));
    setMaximum(room.max_c == null ? "" : String(room.max_c));
    setPreheat(room.max_preheat_c == null ? "2" : String(room.max_preheat_c));
    setOverride(room.target_c == null ? "" : String(room.target_c));
    setMessage("");
    let active = true;
    setHistory(null);
    const refresh = () => api(`/api/rooms/${encodeURIComponent(room.id)}/history`).then(data => {
      if (active) setHistory(data);
    }).catch(reason => { if (active) setMessage(reason.message); });
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => { active = false; clearInterval(timer); };
  }, [selected]);
  useEffect(() => {
    let active = true;
    const refresh = () => api("/api/room-plan").then(data => { if (active) setPlan(data); })
      .catch(reason => { if (active) setMessage(reason.message); });
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    let active = true;
    const refresh = () => api("/api/room-advisory").then(data => {
      if (active) { setAdvisory(data); setAdvisoryError(""); }
    }).catch(reason => {
      if (active) { setAdvisory(null); setAdvisoryError(reason.message); }
    });
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  async function save(event: React.FormEvent, temporary: boolean) {
    event.preventDefault();
    if (!room) return;
    const input = temporary
      ? { target_c: Number(override), minutes: Number(minutes) }
      : { target_c: Number(target), min_c: Number(minimum), max_c: Number(maximum), max_preheat_c: Number(preheat) };
    try {
      await api(`/api/rooms/${encodeURIComponent(room.id)}/${temporary ? "override" : "settings"}`, {
        method: temporary ? "POST" : "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input)
      });
      setMessage("Saved to runtime settings. No equipment command was sent.");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not save settings"); }
  }
  const names = new Map(house?.rooms.map(item => [item.id, item.name]));
  const now = Date.now();
  const currentPlan = plan && now >= Date.parse(plan.created_at) && now - Date.parse(plan.created_at) <= 36 * 3600000
    ? plan.intervals.filter(item => {
      const start = Date.parse(item.start);
      return start <= now && now < start + 15 * 60000;
    }) : [];
  return <section className="mt-8 space-y-5">
    <div><div className="eyebrow">MULTI-ROOM / SIMULATION</div><h2 className="mt-2 text-2xl font-semibold">House overview</h2>
      <p className="mt-2 text-sm text-stone-500">Room values and plans are observations or estimates, not proof of delivered heat.</p></div>
    {error && <div className="alert" role="alert">{error}</div>}
    {!house ? <div className="panel p-6">Room data unavailable. Reconcile the house configuration and apply migrations.</div> : <>
      <section className="panel p-5">
        <h3 className="font-semibold">Read-only room advisory</h3>
        <p className="mt-1 text-xs text-stone-500">Conditional scenarios, not commands or confidence bounds. Heat and weather assumptions remain fixed for one step.</p>
        {advisoryError ? <p className="mt-2 text-sm" role="alert">Advisory unavailable: {advisoryError}</p> :
          !advisory ? <p className="mt-2 text-sm text-stone-500">No advisory recorded. The worker is optional.</p> : <>
            <p className="mt-2 text-xs text-stone-500">Recorded {new Date(advisory.created_at).toLocaleString()} · {advisory.model_version}
              {(!Number.isFinite(Date.parse(advisory.created_at)) || now - Date.parse(advisory.created_at) > 600000 || now < Date.parse(advisory.created_at)) ? " · Historical or stale" : ""}</p>
            {!advisory.available && <div className="mt-2 text-sm">Prediction unavailable: {advisory.reasons.join("; ")}</div>}
            {(advisory.connection_warnings || []).map((warning, index) => <p key={index} className="mt-1 text-xs text-amber-700">{warning}</p>)}
            {advisory.available && advisory.rooms?.map(item => <div key={item.room_id} className="mt-3 border-t border-stone-200 pt-2 text-sm dark:border-white/10">
              <strong>{names.get(item.room_id) || item.room_id}</strong>: measured {show(item.current_temperature_c)} ({item.temperature_quality}), target {show(item.target_c)}.
              Predicted low / nominal / high: {show(item.predicted_temperature_c.low)} / {show(item.predicted_temperature_c.nominal)} / {show(item.predicted_temperature_c.high)}.
              <div className="text-xs text-stone-500">Nominal estimated radiator {show(item.estimated_radiator_w.nominal, "W")} · other heat {show(item.estimated_other_heat_w.nominal, "W")}</div>
            </div>)}
            {advisory.available && advisory.house?.estimated_heating_cost && <p className="mt-2 text-xs text-stone-500">Estimated nominal cost: {show(advisory.house.estimated_heating_cost.nominal, advisory.house.estimated_heating_cost_currency || "")}</p>}
          </>}
      </section>
      <div className="flex flex-wrap gap-2" aria-label="Floor selector">{house.floors.map(item =>
        <button key={item.id} className={floor === item.id ? "primary-button" : "status-pill"} onClick={() => { setFloor(item.id); setSelected(""); }}>{item.name}</button>)}</div>
      {!house.floors.length && <div className="panel p-6">No floors configured.</div>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {house.rooms.filter(item => item.floor_id === floor).map(item => {
          const planned = currentPlan.find(interval => interval.room_id === item.id);
          return <button key={item.id} onClick={() => setSelected(item.id)} className="panel p-5 text-left hover:border-teal-500">
            <div className="flex items-center justify-between"><strong>{item.name}</strong><span className={`rounded-full px-2 py-1 text-xs ${item.comfort === "cold" ? "bg-blue-100 text-blue-800" : item.comfort === "warm" ? "bg-orange-100 text-orange-800" : item.comfort === "comfortable" ? "bg-emerald-100 text-emerald-800" : "bg-stone-200 text-stone-700"}`}>{item.comfort}</span></div>
            <div className="mt-4 text-2xl font-semibold">{show(item.temperature_c)}</div>
            <div className="mt-2 text-sm text-stone-500">Effective target {show(item.target_c)} · Prediction {show(item.predicted_c)}</div>
            {item.prediction_at && <div className="mt-1 text-xs text-stone-500">Prediction for {new Date(item.prediction_at).toLocaleString()}</div>}
            <div className="mt-2 text-xs text-stone-500">Demand unknown · Activity unknown · Active source unknown</div>
            {planned && <div className="mt-2 text-xs text-stone-500">Simulation: demand {show(planned.data.heating_demand_kw, "kW")} · planned sources {planned.data.selected_sources.map(id => item.sources.find(s => s.id === id)?.name || id).join(", ") || "none"}</div>}
            {item.heat_inputs && <div className="mt-2 text-xs text-stone-500">Heat inputs at {new Date(item.heat_inputs.observed_at).toLocaleTimeString()}: {item.heat_inputs.sources.map(source => `${item.sources.find(s => s.id === source.id)?.name || source.id} ${show(source.watts, "W")} (${source.classification})`).join(", ")}</div>}
            <div className="mt-2 text-xs text-stone-500">Connection: {item.devices.map(d => `${d.name}: ${d.connectivity}`).join(", ") || "unknown"} · Temperature quality: {item.temperature_quality}{item.temperature_stale ? " (stale)" : ""}</div>
          </button>;
        })}
      </div>
      <section className="panel p-6"><div className="eyebrow">HEAT DISTRIBUTION</div><h3 className="mt-2 text-xl font-semibold">Configured heat paths and room links</h3>
        <p className="mt-2 text-sm text-stone-500">Generators heat assigned rooms directly. Room links represent possible transfer, not measured KMP output or fixed shares. Tado controls radiators supplied by the radiator generator; Mill controls the storage heater.</p>
        <div className="mt-4 grid gap-2 sm:grid-cols-2">{house.rooms.map(item => <div key={item.id} className="rounded-lg border border-stone-200 p-3 text-sm dark:border-white/10">
          <strong>{item.name}</strong> · {item.sources.map(source => source.name).join(", ") || "No direct generator"}
          <div className="mt-1 text-xs text-stone-500">Connected to: {house.thermal_links.filter(link => link.room_a_id === item.id || link.room_b_id === item.id).map(link => names.get(link.room_a_id === item.id ? link.room_b_id : link.room_a_id)).join(", ") || "none configured"}</div>
        </div>)}</div>
      </section>
      {room && <div className="grid gap-5 lg:grid-cols-2">
        <section className="panel p-6"><div className="eyebrow">ROOM SETTINGS</div><h3 className="mt-2 text-xl font-semibold">{room.name}</h3>
          <p className="mt-2 text-sm text-stone-500">Settings are stored in SQL; they do not control devices. Automatic room control is not available. Scheduled targets are not resolved in this view.</p>
          <label className="mt-4 flex items-center gap-2 text-sm text-stone-500"><input type="checkbox" disabled checked={false} readOnly/> Automatic room control unavailable</label>
          <form onSubmit={event => save(event, false)} className="mt-4 grid gap-3 sm:grid-cols-3">
            <label className="text-sm">Target °C<input className="input mt-1" type="number" step="0.1" required value={target} onChange={e => setTarget(e.target.value)}/></label>
            <label className="text-sm">Minimum °C<input className="input mt-1" type="number" step="0.1" required value={minimum} onChange={e => setMinimum(e.target.value)}/></label>
            <label className="text-sm">Maximum °C<input className="input mt-1" type="number" step="0.1" required value={maximum} onChange={e => setMaximum(e.target.value)}/></label>
            <label className="text-sm sm:col-span-3">Maximum preheating above target °C<input className="input mt-1" type="number" min="0" max="2" step="0.1" required value={preheat} onChange={e => setPreheat(e.target.value)}/></label>
            <button className="primary-button sm:col-span-3">Save room settings</button>
          </form>
          <form onSubmit={event => save(event, true)} className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="text-sm">Override °C<input className="input mt-1" type="number" step="0.1" required value={override} onChange={e => setOverride(e.target.value)}/></label>
            <label className="text-sm">Duration minutes<input className="input mt-1" type="number" min="1" max="10080" required value={minutes} onChange={e => setMinutes(e.target.value)}/></label>
            <button className="primary-button sm:col-span-2">Set temporary override</button>
          </form>
          <p className="mt-2 text-xs text-stone-500">Overrides start at the next 15-minute boundary; duration rounds up to 15 minutes. A pending overlapping override cannot be replaced.</p>
          {room.override_expires_at && <p className="mt-3 text-xs">Override expires {new Date(room.override_expires_at).toLocaleString()}</p>}
          {message && <p className="mt-3 text-sm" role="status">{message}</p>}
          <div className="mt-5 text-sm"><strong>Assigned generators:</strong> {room.sources.map(s => s.name).join(", ") || "None configured"}</div>
          <div className="mt-2 text-sm"><strong>Devices:</strong> {room.devices.map(d => `${d.name} (${d.adapter || "unknown"})`).join(", ") || "None configured"}</div>
          <div className="mt-2 text-sm"><strong>Thermal links:</strong> {house.thermal_links.filter(link => link.room_a_id === room.id || link.room_b_id === room.id).map(link => names.get(link.room_a_id === room.id ? link.room_b_id : link.room_a_id)).join(", ") || "None configured"}</div>
        </section>
        <section className="panel p-6"><div className="eyebrow">ROOM HISTORY</div><h3 className="mt-2 text-xl font-semibold">Temperature</h3>
          <div className="mt-5">{history ? <TemperatureChart history={history}/> : "Loading…"}</div>
          <p className="mt-4 text-xs text-stone-500">Activity and expenditure charts require independently recorded measurements; device commands and estimated costs are excluded.</p>
          <h4 className="mt-5 font-semibold">Base-target changes</h4>
          {history && <TargetChart history={history}/>}
          <h4 className="mt-5 font-semibold">Predicted versus actual</h4>
          {history && <PredictionChart history={history}/>}
          <h4 className="mt-5 font-semibold">Recorded heat contribution</h4>
          {history && <HeatInputChart history={history} names={new Map(house.rooms.flatMap(r => r.sources.map(s => [s.id, s.name] as [string, string])))}/>}
          <h4 className="mt-5 font-semibold">Measured heating activity</h4>
          {history && <RecordedChart rows={history.heating_activity_history} title="Heating activity" unit="%"/>}
          <h4 className="mt-5 font-semibold">Measured heating expenditure</h4>
          {history && <RecordedChart rows={history.measured_expenditure} title="Heating expenditure" unit="SEK"/>}
        </section>
      </div>}
      {room && <RoomSchedules roomId={room.id}/>}
      <section className="panel p-6"><div className="eyebrow">36-HOUR PLAN / SIMULATION ONLY</div><h3 className="mt-2 text-xl font-semibold">Room schedule</h3>
        {!plan ? <p className="mt-3 text-sm text-stone-500">No saved room simulation available.</p> : <div className="mt-4 max-h-96 overflow-auto">
          <table className="w-full text-left text-sm"><thead><tr><th>Time</th><th>Room</th><th>Temp / target</th><th>Demand</th><th>Sources</th><th>Cost</th></tr></thead>
            <tbody>{plan.intervals.filter(i => selected ? i.room_id === selected : house.rooms.some(r => r.id === i.room_id && r.floor_id === floor)).map((item, index) =>
              <tr key={`${item.room_id}-${index}`} className="border-t border-stone-200 align-top dark:border-white/10"><td className="py-2">{new Date(item.start).toLocaleString()}</td><td>{names.get(item.room_id) || item.room_id}</td><td>{show(item.data.predicted_temperature_c)} / {show(item.data.target_c)}</td><td>{show(item.data.heating_demand_kw, "kW")}</td><td><div>{item.data.selected_sources.map(id => house.rooms.flatMap(r => r.sources).find(s => s.id === id)?.name || id).join(", ") || "None"}</div><div className="text-xs text-stone-500">Planned heat: {Object.entries(item.data.planned_heat_kw).map(([id, power]) => `${house.rooms.flatMap(r => r.sources).find(s => s.id === id)?.name || id} ${show(power, "kW")}`).join(", ") || "none"}</div><div className="text-xs text-stone-500">{item.data.explanation}</div><div className="text-xs text-stone-500">Estimated transfers: {Object.entries(item.data.inter_room_transfer_kw).map(([id, power]) => `${names.get(id) || id} ${show(power, "kW")}`).join(", ") || "none"}</div></td><td>{show(item.data.expected_cost, "SEK")}</td></tr>)}</tbody></table>
        </div>}
      </section>
    </>}
    <HouseConfiguration/>
  </section>;
}
