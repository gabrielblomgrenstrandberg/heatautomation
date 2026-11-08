import React, { useEffect, useState } from "react";

type Entry = { weekday: number; start_min: number; end_min: number; target_c: number };
const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export default function RoomSchedules({ roomId }: { roomId: string }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [message, setMessage] = useState("");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let active = true;
    setLoaded(false);
    setMessage("");
    fetch(`/api/rooms/${encodeURIComponent(roomId)}/schedules`).then(async response => {
      if (!response.ok) throw new Error(`Could not load schedules (${response.status})`);
      return response.json();
    }).then(data => { if (active) { setEntries(data); setLoaded(true); } })
      .catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [roomId]);
  function change(index: number, field: keyof Entry, value: number) {
    setEntries(rows => rows.map((row, i) => i === index ? { ...row, [field]: value } : row));
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    try {
      const response = await fetch(`/api/rooms/${encodeURIComponent(roomId)}/schedules`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entries })
      });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(typeof body.detail === "string" ? body.detail : "Invalid schedule");
      }
      setMessage("Schedules saved for future simulations; no device commands sent.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not save schedules."); }
  }
  return <section className="panel p-6"><div className="eyebrow">WEEKLY ROOM SCHEDULE</div>
    <h3 className="mt-2 text-xl font-semibold">Temperature schedule</h3>
    <p className="mt-2 text-sm text-stone-500">Times are local to the planner's configured time zone. Use minutes after midnight, in 15-minute increments; the end is exclusive. No cross-midnight or overlapping intervals.</p>
    {loaded && <form onSubmit={save} className="mt-4 space-y-3">
      {entries.map((entry, index) => <div key={index} className="grid gap-2 sm:grid-cols-5">
        <label className="text-xs">Day<select className="input mt-1" value={entry.weekday} onChange={e => change(index, "weekday", Number(e.target.value))}>{days.map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>
        <label className="text-xs">Start minute<input className="input mt-1" type="number" min="0" max="1425" step="15" required value={entry.start_min} onChange={e => change(index, "start_min", Number(e.target.value))}/></label>
        <label className="text-xs">End minute<input className="input mt-1" type="number" min="15" max="1440" step="15" required value={entry.end_min} onChange={e => change(index, "end_min", Number(e.target.value))}/></label>
        <label className="text-xs">Target °C<input className="input mt-1" type="number" step="0.1" required value={entry.target_c} onChange={e => change(index, "target_c", Number(e.target.value))}/></label>
        <button type="button" className="status-pill self-end" onClick={() => setEntries(rows => rows.filter((_, i) => i !== index))}>Remove</button>
      </div>)}
      {!entries.length && <p className="text-sm text-stone-500">No scheduled intervals; the base target applies.</p>}
      <div className="flex flex-wrap gap-2"><button type="button" className="status-pill" onClick={() => setEntries(rows => [...rows, { weekday: 0, start_min: 0, end_min: 60, target_c: 20 }])}>Add interval</button>
        <button className="primary-button">Save schedule</button></div>
    </form>}
    {message && <p role="status" className="mt-3 text-sm">{message}</p>}
  </section>;
}
