import React, { useEffect, useState } from "react";
import HouseConfiguration from "./HouseConfiguration";

type Device = { id: string; name: string; adapter: string | null; connectivity: string };
type Room = {
  id: string; floor_id: string; name: string; temperature_c: number | null;
  temperature_stale: boolean; target_c: number | null; base_target_c: number | null;
  min_c: number | null; max_c: number | null; predicted_c: number | null;
  prediction_at: string | null;
  comfort: "cold" | "warm" | "comfortable" | "unknown";
  override_expires_at: string | null; devices: Device[];
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
};
type Plan = { created_at: string; intervals: { room_id: string; start: string; data: {
  predicted_temperature_c: number; target_c: number; heating_demand_kw?: number;
  selected_sources: string[]; expected_cost: number; explanation: string;
  planned_heat_kw: Record<string, number>; inter_room_transfer_kw: Record<string, number>;
} }[] };

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
  const points = rows.map((item, index) =>
    `${rows.length === 1 ? 50 : index * 100 / (rows.length - 1)},${95 - 90 * (item.value - min) / (max - min)}`
  ).join(" ");
  return <div><svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-40 w-full" role="img" aria-label="Recorded room temperature history">
    <polyline fill="none" stroke="currentColor" strokeWidth="1.5" className="text-teal-500" points={points}/>
  </svg><div className="flex justify-between text-xs text-stone-500"><span>{new Date(rows[0].at).toLocaleString()}</span><span>{new Date(rows[rows.length - 1].at).toLocaleString()}</span></div></div>;
}

export default function Rooms({ house, error }: { house: HouseData | null; error: string }) {
  const [floor, setFloor] = useState("");
  const [selected, setSelected] = useState("");
  const [history, setHistory] = useState<History | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [message, setMessage] = useState("");
  const [target, setTarget] = useState("");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
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
    setOverride(room.target_c == null ? "" : String(room.target_c));
    setMessage("");
    let active = true;
    setHistory(null);
    api(`/api/rooms/${encodeURIComponent(room.id)}/history`).then(data => {
      if (active) setHistory(data);
    }).catch(reason => { if (active) setMessage(reason.message); });
    return () => { active = false; };
  }, [selected]);
  useEffect(() => {
    api("/api/room-plan").then(setPlan).catch(reason => setMessage(reason.message));
  }, []);
  async function save(event: React.FormEvent, temporary: boolean) {
    event.preventDefault();
    if (!room) return;
    const input = temporary
      ? { target_c: Number(override), minutes: Number(minutes) }
      : { target_c: Number(target), min_c: Number(minimum), max_c: Number(maximum) };
    try {
      await api(`/api/rooms/${encodeURIComponent(room.id)}/${temporary ? "override" : "settings"}`, {
        method: temporary ? "POST" : "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input)
      });
      setMessage("Saved to runtime settings. No equipment command was sent.");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "Could not save settings"); }
  }
  const names = new Map(house?.rooms.map(item => [item.id, item.name]));
  return <section className="mt-8 space-y-5">
    <div><div className="eyebrow">MULTI-ROOM / SIMULATION</div><h2 className="mt-2 text-2xl font-semibold">House overview</h2>
      <p className="mt-2 text-sm text-stone-500">Room values and plans are observations or estimates, not proof of delivered heat.</p></div>
    {error && <div className="alert" role="alert">{error}</div>}
    {!house ? <div className="panel p-6">Room data unavailable. Reconcile the house configuration and apply migrations.</div> : <>
      <div className="flex flex-wrap gap-2" aria-label="Floor selector">{house.floors.map(item =>
        <button key={item.id} className={floor === item.id ? "primary-button" : "status-pill"} onClick={() => { setFloor(item.id); setSelected(""); }}>{item.name}</button>)}</div>
      {!house.floors.length && <div className="panel p-6">No floors configured.</div>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {house.rooms.filter(item => item.floor_id === floor).map(item =>
          <button key={item.id} onClick={() => setSelected(item.id)} className="panel p-5 text-left hover:border-teal-500">
            <div className="flex items-center justify-between"><strong>{item.name}</strong><span className={`rounded-full px-2 py-1 text-xs ${item.comfort === "cold" ? "bg-blue-100 text-blue-800" : item.comfort === "warm" ? "bg-orange-100 text-orange-800" : item.comfort === "comfortable" ? "bg-emerald-100 text-emerald-800" : "bg-stone-200 text-stone-700"}`}>{item.comfort}</span></div>
            <div className="mt-4 text-2xl font-semibold">{show(item.temperature_c)}</div>
            <div className="mt-2 text-sm text-stone-500">Effective target {show(item.target_c)} · Prediction {show(item.predicted_c)}</div>
            {item.prediction_at && <div className="mt-1 text-xs text-stone-500">Prediction for {new Date(item.prediction_at).toLocaleString()}</div>}
            <div className="mt-2 text-xs text-stone-500">Demand unknown · Activity unknown · Active source unknown</div>
            <div className="mt-2 text-xs text-stone-500">Connection: {item.devices.map(d => `${d.name}: ${d.connectivity}`).join(", ") || "unknown"}{item.temperature_stale ? " · Temperature stale" : ""}</div>
          </button>)}
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
          <form onSubmit={event => save(event, false)} className="mt-4 grid gap-3 sm:grid-cols-3">
            <label className="text-sm">Target °C<input className="input mt-1" type="number" step="0.1" required value={target} onChange={e => setTarget(e.target.value)}/></label>
            <label className="text-sm">Minimum °C<input className="input mt-1" type="number" step="0.1" required value={minimum} onChange={e => setMinimum(e.target.value)}/></label>
            <label className="text-sm">Maximum °C<input className="input mt-1" type="number" step="0.1" required value={maximum} onChange={e => setMaximum(e.target.value)}/></label>
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
          <p className="mt-4 text-xs text-stone-500">Target, heating activity, source contribution and expenditure history are not recorded yet.</p>
          <h4 className="mt-5 font-semibold">Predicted versus actual</h4>
          {history?.predictions.length ? <div className="mt-2 max-h-40 overflow-auto text-sm">{history.predictions.slice(-20).reverse().map((p, index) => <div key={index} className="py-1">{new Date(p.at).toLocaleString()} · predicted {show(p.predicted_c)} · actual {show(p.actual_c)}</div>)}</div> : <p className="mt-2 text-sm text-stone-500">No prediction comparisons available.</p>}
        </section>
      </div>}
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
