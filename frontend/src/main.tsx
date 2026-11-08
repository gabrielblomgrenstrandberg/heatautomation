import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowUpRight, CircleAlert, Flame, House, LogOut, Moon, Sun, Thermometer, Wind, Zap } from "lucide-react";
import "./style.css";
import Rooms, { type HouseData } from "./Rooms";

type Device = { on: boolean | null; mode: string | null; capacity_kw: number | null };
type Status = {
  state: "current" | "stale" | "unavailable";
  message: string;
  observed_at?: string | null;
  indoor_c?: number | null;
  outdoor_c?: number | null;
  target_c?: number | null;
  price_sek_kwh?: number | null;
  heatpump?: Device | null;
  pelletstove?: Device | null;
  estimated_cop?: number | null;
  estimated_demand_kw?: number | null;
  estimated_pump_cost_sek_kwh?: number | null;
};
type LiveMessage = { type: "status"; data: Status } | { type: "error"; message: string }
  | { type: "house"; data: HouseData } | { type: "house_error"; message: string };

function value(number: number | null | undefined, unit: string, digits = 1) {
  return number == null ? "—" : `${number.toFixed(digits)} ${unit}`;
}

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [house, setHouse] = useState<HouseData | null>(null);
  const [houseError, setHouseError] = useState("");
  const [dark, setDark] = useState(() => localStorage.getItem("theme") !== "light");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    fetch("/api/status").then(response => {
      if (response.status === 401) setAuthenticated(false);
      else if (response.ok || response.status === 503) setAuthenticated(true);
      else throw new Error(`Status request failed (${response.status}).`);
    }).catch(() => { setError("Cannot connect to the monitoring service."); setAuthenticated(false); });
  }, []);

  useEffect(() => {
    if (!authenticated) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let socket: WebSocket;
    const connect = () => {
      socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/live`);
      socket.onopen = () => { setConnected(true); setError(""); };
      socket.onmessage = event => {
        const message = JSON.parse(event.data) as LiveMessage;
        if (message.type === "status") { setStatus(message.data); setError(""); }
        else if (message.type === "house") { setHouse(message.data); setHouseError(""); }
        else if (message.type === "house_error") { setHouse(null); setHouseError(message.message); }
        else { setStatus(null); setError(message.message); }
      };
      socket.onclose = event => {
        setConnected(false);
        setHouse(null);
        if (stopped) return;
        if (event.code === 1008) { setAuthenticated(false); setStatus(null); return; }
        setError("Live connection lost. Reconnecting…");
        timer = setTimeout(connect, 5000);
      };
    };
    connect();
    return () => { stopped = true; clearTimeout(timer); socket.close(); };
  }, [authenticated]);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const response = await fetch("/api/session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password })
      });
      if (!response.ok) throw new Error(response.status === 429 ? "Too many attempts. Try again in a minute." : "Sign-in failed.");
      setPassword("");
      setAuthenticated(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Sign-in failed."); }
  }

  async function logout() {
    try {
      const response = await fetch("/api/session", { method: "DELETE" });
      if (!response.ok) throw new Error("Sign-out failed.");
      setAuthenticated(false); setStatus(null); setHouse(null);
    } catch { setError("Sign-out failed. Check the connection and try again."); }
  }

  const stale = status?.state !== "current" || !connected;
  const active = !stale && status?.heatpump?.capacity_kw != null && status?.pelletstove?.capacity_kw != null
    ? [status.heatpump.capacity_kw > 0 && "Sensibo", status.pelletstove.capacity_kw > 0 && "KMP"].filter(Boolean).join(" + ") || "None observed"
    : "Unavailable";

  return <div className="min-h-screen">
    <header className="border-b border-stone-200 dark:border-white/10">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-5 lg:px-10">
        <div className="flex items-center gap-3"><div className="brand"><Flame size={20}/></div><div><div className="font-semibold tracking-tight">HEAT<span className="text-teal-500">.</span> AUTOMATION</div><div className="text-xs text-stone-500 dark:text-stone-400">HOME ENERGY MANAGEMENT</div></div></div>
        <div className="flex items-center gap-3">
          <button className="icon-button" onClick={() => setDark(!dark)} aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}>{dark ? <Sun size={18}/> : <Moon size={18}/>}</button>
          {authenticated && <button className="icon-button" onClick={logout} aria-label="Sign out"><LogOut size={18}/></button>}
        </div>
      </div>
    </header>
    {authenticated === null ? <main className="mx-auto max-w-lg p-10 text-stone-500">Connecting…</main> : !authenticated ?
      <main className="mx-auto max-w-md px-5 py-24"><div className="panel p-8">
        <div className="eyebrow">PRIVATE MONITORING</div><h1 className="mt-3 text-3xl font-semibold">Welcome home.</h1>
        <p className="mt-3 text-sm text-stone-500 dark:text-stone-400">Sign in to view your heating system.</p>
        <form className="mt-8 space-y-4" onSubmit={login}><label className="block text-sm" htmlFor="password">Web password</label>
          <input id="password" className="input" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required />
          {error && <p className="text-sm text-red-500" role="alert">{error}</p>}
          <button className="primary-button w-full" type="submit">Sign in <ArrowUpRight size={17}/></button>
        </form></div></main> :
      <main className="mx-auto max-w-7xl px-5 py-10 lg:px-10">
        <div className="flex flex-wrap items-end justify-between gap-5">
          <div><div className="eyebrow">YOUR HOME / OVERVIEW</div><h1 className="mt-3 text-4xl font-semibold tracking-tight">Heating overview</h1><p className="mt-2 text-sm text-stone-500 dark:text-stone-400">A clearer picture of your home comfort.</p></div>
          <div className="status-pill"><span className={`dot ${!connected || status?.state === "unavailable" ? "bg-red-400" : stale ? "bg-amber-400" : "bg-emerald-400"}`}/>{!connected ? "Disconnected" : status?.state === "current" ? "Observation current" : status?.state === "stale" ? "Observation stale" : "Data unavailable"}</div>
        </div>
        {error && <div className="alert mt-7" role="alert"><CircleAlert size={18}/>{error}</div>}
        {status?.state === "stale" && <div className="alert mt-7" role="alert"><CircleAlert size={18}/>This observation is over 15 minutes old or has a future timestamp. Do not rely on it as current equipment status.</div>}
        {status?.state === "unavailable" && <div className="alert mt-7" role="alert"><CircleAlert size={18}/>{status.message}</div>}
        <div className="mt-8 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Metric icon={<House size={18}/>} label="INDOOR TEMPERATURE" main={value(status?.indoor_c, "°C")} detail={`Sensibo target ${value(status?.target_c, "°C")}`} accent />
          <Metric icon={<Wind size={18}/>} label="OUTDOOR TEMPERATURE" main={value(status?.outdoor_c, "°C")} detail="Weather observation" />
          <Metric icon={<Zap size={18}/>} label="ELECTRICITY PRICE" main={value(status?.price_sek_kwh, "SEK/kWh", 2)} detail="Tibber total spot price" />
          <Metric icon={<Activity size={18}/>} label="HEATING SOURCE" main={active} detail="Based on estimated source capacity" small />
        </div>
        <div className="mt-5 grid gap-5 lg:grid-cols-3">
          <section className="panel p-6 lg:col-span-2"><div className="flex justify-between"><div><div className="eyebrow">EQUIPMENT</div><h2 className="mt-2 text-xl font-semibold">Heating sources</h2></div><Thermometer className="text-teal-500" size={22}/></div>
            <p className="mt-2 text-sm text-stone-500 dark:text-stone-400">Provider-reported states, not proof of physical output.</p>
            <div className="mt-6 grid gap-3 sm:grid-cols-2"><DeviceCard name="Sensibo" description="Heat pump" device={status?.heatpump} stale={stale}/><DeviceCard name="KMP" description="Pellet stove" device={status?.pelletstove} stale={stale}/></div>
          </section>
          <section className="panel p-6"><div className="eyebrow">SYSTEM ESTIMATES</div><h2 className="mt-2 text-xl font-semibold">At a glance</h2>
            <div className="mt-5 divide-y divide-stone-200 dark:divide-white/10">
              <Row label="Heat pump COP" text={value(status?.estimated_cop, "", 2)} />
              <Row label="Heating demand" text={value(status?.estimated_demand_kw, "kW")} />
              <Row label="Heat pump cost" text={value(status?.estimated_pump_cost_sek_kwh, "SEK/kWh heat", 2)} />
              <Row label="ML prediction" text="Not available yet" />
            </div><p className="mt-4 text-xs leading-5 text-stone-500 dark:text-stone-400">Estimates use configured curves when cost control is enabled. They are not measured heat or actual operating costs.</p>
          </section>
        </div>
        <Rooms house={house} error={houseError}/>
        <footer className="mt-7 flex flex-wrap justify-between gap-2 text-xs text-stone-500 dark:text-stone-400"><span>Room settings do not dispatch commands · Control runs independently</span><span>Last observation: {status?.observed_at ? new Date(status.observed_at).toLocaleString() : "Unavailable"}</span></footer>
      </main>}
  </div>;
}

function Metric({ icon, label, main, detail, accent = false, small = false }: { icon: React.ReactNode; label: string; main: string; detail: string; accent?: boolean; small?: boolean }) {
  return <div className={`panel p-6 ${accent ? "metric-accent" : ""}`}><div className="flex items-center justify-between"><span className="eyebrow">{label}</span><span className="text-teal-500">{icon}</span></div><div className={`mt-7 font-semibold tracking-tight ${small ? "text-2xl" : "text-3xl"}`}>{main}</div><div className="mt-2 text-xs text-stone-500 dark:text-stone-400">{detail}</div></div>;
}

function DeviceCard({ name, description, device, stale }: { name: string; description: string; device?: Device | null; stale: boolean }) {
  const state = stale || device?.on == null ? "Unavailable" : device.on ? "On" : "Off";
  return <div className="rounded-xl border border-stone-200 p-4 dark:border-white/10"><div className="flex justify-between gap-3"><div><div className="font-medium">{name}</div><div className="mt-1 text-xs text-stone-500 dark:text-stone-400">{description}</div></div><div className="flex items-center gap-2 text-xs"><span className={`dot ${state === "On" ? "bg-emerald-400" : state === "Off" ? "bg-stone-400" : "bg-amber-400"}`}/>{state}</div></div><div className="mt-6 flex justify-between text-xs text-stone-500 dark:text-stone-400"><span>Mode: {device?.mode || "—"}</span><span>Est. {value(device?.capacity_kw, "kW")}</span></div></div>;
}

function Row({ label, text }: { label: string; text: string }) {
  return <div className="flex items-center justify-between gap-3 py-3 text-sm"><span className="text-stone-500 dark:text-stone-400">{label}</span><span className="text-right font-medium">{text}</span></div>;
}

createRoot(document.getElementById("root")!).render(<App />);
