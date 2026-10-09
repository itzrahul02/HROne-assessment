import { useCallback, useEffect, useState } from "react";
import { get, onRaw } from "./api";
import Analytics from "./Analytics";
import Attendance from "./Attendance";
import Employees from "./Employees";

const titles = {
  employees: ["Employees", "Create a person, then find them by department."],
  attendance: ["Attendance", "Punch in, punch out, and correct a single day."],
  analytics: ["Analytics", "Monthly totals, department averages, late ranks, and the daily trend."],
};

export default function App() {
  const [view, setView] = useState("employees");
  const [health, setHealth] = useState("checking");
  const [banner, setBanner] = useState(null);
  const [raw, setRaw] = useState("Nothing yet.");

  useEffect(() => {
    onRaw((path, body) => setRaw(`${path}\n\n${JSON.stringify(body, null, 2)}`));
  }, []);

  useEffect(() => {
    let stop = false;
    async function ping() {
      try {
        const body = await get("/health", true);
        if (!stop) setHealth(body.status === "ok" ? "ok" : "bad");
      } catch {
        if (!stop) setHealth("bad");
      }
    }
    ping();
    const timer = setInterval(ping, 15000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, []);

  const notify = useCallback((message, kind = "bad") => {
    setBanner(message ? { message, kind } : null);
  }, []);

  const [title, sub] = titles[view];

  return (
    <div className="app">
      <aside>
        <p className="mark">Attendance desk</p>
        <p className="aside-kicker">HR floor book</p>
        <nav>
          {Object.keys(titles).map((name) => (
            <button
              key={name}
              type="button"
              className={view === name ? "active" : ""}
              onClick={() => setView(name)}
            >
              {name[0].toUpperCase() + name.slice(1)}
            </button>
          ))}
        </nav>
        <p className="aside-note">
          Type clock times as IST (UTC+5:30). The screen does not use your laptop’s time zone.
        </p>
      </aside>
      <main>
        <header>
          <div>
            <h1>{title}</h1>
            <p>{sub}</p>
          </div>
          <p className={health === "ok" ? "health ok" : health === "bad" ? "health bad" : "health"}>
            {health === "ok" ? "MongoDB connected" : health === "bad" ? "MongoDB not reachable" : "Checking MongoDB…"}
          </p>
        </header>
        {banner && <p className={`banner ${banner.kind}`}>{banner.message}</p>}
        {view === "employees" && <Employees notify={notify} />}
        {view === "attendance" && <Attendance notify={notify} />}
        {view === "analytics" && <Analytics notify={notify} />}
        <details className="raw card">
          <summary>Last response</summary>
          <pre>{raw}</pre>
        </details>
      </main>
    </div>
  );
}
