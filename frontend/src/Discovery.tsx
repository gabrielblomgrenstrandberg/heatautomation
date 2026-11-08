import React, { useEffect, useState } from "react";

type Measurement = {
  value: number | boolean; unit: string; timestamp: string; stale: boolean; classification: string;
};
type Device = {
  id: string; name: string; device_type: string; ip_address: string | null;
  connection_method: string; connection_state: string; last_connection: string | null;
  last_error: string | null; measurements: Record<string, Measurement>;
  house_device: { id: string; name: string } | null; mapping_error: string | null;
  monitoring_stale: boolean;
};
type DiscoveryData = {
  enabled: boolean; scan_requested: boolean; devices: Device[];
  probe_errors: { device_id: string; checked_at: string; error: string }[];
  last_discovery: { state: string; started_at: string; verified_count: number | null; error: string | null } | null;
};

export default function Discovery() {
  const [data, setData] = useState<DiscoveryData | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const response = await fetch("/api/discovery");
      if (!response.ok) throw new Error("Discovery data unavailable. Check configuration and database migration.");
      setData(await response.json() as DiscoveryData);
      setError("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Discovery unavailable."); }
  }
  useEffect(() => {
    void load();
    const timer = setInterval(() => { void load(); }, 10000);
    return () => clearInterval(timer);
  }, []);

  async function requestScan() {
    setBusy(true);
    try {
      const response = await fetch("/api/discovery", { method: "POST" });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(typeof body.detail === "string" ? body.detail : "Could not request discovery.");
      }
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not request discovery."); }
    finally { setBusy(false); }
  }

  return <section className="panel mt-5 p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="eyebrow">LOCAL NETWORK</div><h2 className="mt-2 text-xl font-semibold">Device discovery</h2>
        <p className="mt-2 text-sm text-stone-500">Read-only Mill observations. The controller performs scans; the browser never connects to devices.</p></div>
      <button type="button" className="status-pill" disabled={!data?.enabled || busy || data.scan_requested} onClick={requestScan}>
        {data?.scan_requested ? "Scan requested" : "Discover now"}
      </button>
    </div>
    {error && <p className="mt-3 text-sm text-red-500" role="alert">{error}</p>}
    {data && <div className="mt-4 text-sm text-stone-500">
      {!data.enabled ? "LAN discovery disabled." : data.scan_requested ? "Waiting for the controller and scan rate limit." :
        data.last_discovery ? `Last scan: ${data.last_discovery.state} · ${new Date(data.last_discovery.started_at).toLocaleString()} · ${data.last_discovery.verified_count ?? 0} verified` : "No scans recorded."}
      {data.last_discovery?.error && <p className="mt-1 text-red-500">{data.last_discovery.error}</p>}
    </div>}
    {data?.probe_errors.map(issue => <p key={issue.device_id} className="mt-3 text-sm text-red-500">
      {issue.device_id}: {issue.error} · {new Date(issue.checked_at).toLocaleString()}
    </p>)}
    {data?.devices.map(device => <div key={device.id} className="mt-4 rounded-xl border border-stone-200 p-4 dark:border-white/10">
      <div className="flex flex-wrap justify-between gap-2"><strong>{device.name}</strong><span>{device.connection_state} · {device.connection_method}</span></div>
      <p className="mt-2 text-sm text-stone-500">IP: {device.ip_address ?? "Unavailable"} · Last connection: {device.last_connection ? new Date(device.last_connection).toLocaleString() : "Unavailable"}</p>
      {device.monitoring_stale && <p className="mt-2 text-sm text-amber-600">Connection monitoring is stale or disabled.</p>}
      <p className="mt-2 text-sm text-stone-500">House device: {device.house_device ? device.house_device.name : "Not assigned"}</p>
      {device.mapping_error && <p className="mt-2 text-sm text-red-500">{device.mapping_error}</p>}
      {device.last_error && <p className="mt-2 text-sm text-red-500">{device.last_error}</p>}
      <div className="mt-3 flex flex-wrap gap-3 text-sm">
        {Object.entries(device.measurements).map(([name, item]) =>
          <span key={name}>{name.replace(/_/g, " ")}: {String(item.value)} {item.unit} {item.stale ? "(stale)" : ""} · {item.classification}</span>)}
      </div>
    </div>)}
  </section>;
}
