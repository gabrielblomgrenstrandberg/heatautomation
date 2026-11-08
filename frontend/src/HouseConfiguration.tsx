import React, { useState } from "react";

type Configuration = { revision: string; config: object };

async function request(path: string, method = "GET", data?: object) {
  const response = await fetch(path, {
    method, headers: data ? { "Content-Type": "application/json" } : undefined,
    body: data ? JSON.stringify(data) : undefined
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail :
      Array.isArray(detail) ? detail.map(item => `${item.loc.join(".")}: ${item.msg}`).join("; ") :
      `Request failed (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

export default function HouseConfiguration() {
  const [revision, setRevision] = useState("");
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("");
  async function load() {
    try {
      const data: Configuration = await request("/api/house-configuration");
      setRevision(data.revision);
      setText(JSON.stringify(data.config, null, 2));
      setDirty(false);
      setMessage("Loaded the physical configuration.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not load configuration."); }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    try {
      const config = JSON.parse(text);
      const result = await request("/api/house-configuration", "PUT", { revision, config });
      setRevision(result.revision);
      setDirty(false);
      setMessage("File saved. Apply it separately to reconcile the database.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not save configuration."); }
  }
  async function apply() {
    try {
      await request("/api/house-configuration/apply", "POST", { revision });
      setMessage("Configuration reconciled. Learned values and historical data were preserved.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not apply configuration."); }
  }
  return <section className="panel p-6"><div className="eyebrow">PHYSICAL CONFIGURATION</div>
    <h3 className="mt-2 text-xl font-semibold">House configuration editor</h3>
    <p className="mt-2 text-sm text-stone-500">Edit floors, rooms, stable IDs, devices, Tado mappings, Mill device assignments, heat paths and thermal links as validated JSON. Save changes to the house file, then apply them to the database. Existing learned values are preserved. Do not edit the file concurrently outside this web process.</p>
    <div className="mt-4"><button type="button" className="status-pill" onClick={load}>Load / reload configuration</button></div>
    {revision && <form className="mt-4 space-y-3" onSubmit={save}>
      <label className="block text-sm" htmlFor="house-config">House JSON</label>
      <textarea id="house-config" className="input h-96 w-full font-mono text-xs" spellCheck={false}
        value={text} onChange={event => { setText(event.target.value); setDirty(true); }}/>
      <div className="flex flex-wrap gap-2"><button className="primary-button" disabled={!dirty}>Save file</button>
        <button type="button" className="status-pill" disabled={dirty} onClick={apply}>Apply saved configuration</button></div>
    </form>}
    {message && <p className="mt-3 text-sm" role="status">{message}</p>}
  </section>;
}
