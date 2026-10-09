import { useState } from "react";
import { get, query } from "./api";

function Stat({ label, value }) {
  return (
    <div>
      <strong>{label}</strong>
      {value == null ? "—" : String(value)}
    </div>
  );
}

export default function Analytics({ notify }) {
  const [month, setMonth] = useState("2026-07");
  const [monthly, setMonthly] = useState(null);
  const [summary, setSummary] = useState([]);
  const [board, setBoard] = useState([]);
  const [trend, setTrend] = useState([]);

  async function loadMonthly(event) {
    event.preventDefault();
    try {
      const code = new FormData(event.target).get("emp_code").trim();
      setMonthly(await get(`/analytics/employees/${encodeURIComponent(code)}/monthly${query({ month })}`));
    } catch (error) {
      notify(error.message);
    }
  }

  async function loadSummary(event) {
    event.preventDefault();
    try {
      const department = new FormData(event.target).get("department");
      const body = await get(`/analytics/departments/summary${query({ month, department })}`);
      setSummary(body.items);
    } catch (error) {
      notify(error.message);
    }
  }

  async function loadBoard(event) {
    event.preventDefault();
    try {
      const values = Object.fromEntries(new FormData(event.target));
      const body = await get(`/analytics/leaderboard/late${query({ month, ...values })}`);
      setBoard(body.items);
    } catch (error) {
      notify(error.message);
    }
  }

  async function loadTrend(event) {
    event.preventDefault();
    try {
      const values = Object.fromEntries(new FormData(event.target));
      const body = await get(`/analytics/departments/${encodeURIComponent(values.department)}/trend${query({
        from: values.from,
        to: values.to,
      })}`);
      setTrend(body.items);
    } catch (error) {
      notify(error.message);
    }
  }

  return (
    <section>
      <form className="card toolbar" onSubmit={(event) => event.preventDefault()}>
        <label>Month<input type="month" value={month} required onChange={(event) => setMonth(event.target.value)} /></label>
        <p className="hint">The nine sample rows are in July 2026. A half day counts as 0.5, and only Monday–Friday counts toward present days.</p>
      </form>
      <div className="split">
        <form className="card grid" onSubmit={loadMonthly}>
          <h2>One employee</h2>
          <label>Employee code<input name="emp_code" required placeholder="EMP0001" /></label>
          <div className="actions"><button type="submit">Monthly summary</button></div>
          {monthly && (
            <div className="result">
              <Stat label="Working days" value={monthly.working_days} />
              <Stat label="Present days" value={monthly.present_days} />
              <Stat label="Leave days" value={monthly.leave_days} />
              <Stat label="Late count" value={monthly.late_count} />
              <Stat label="Late minutes" value={monthly.total_late_minutes} />
              <Stat label="Overtime minutes" value={monthly.total_overtime_minutes} />
              <Stat label="Attendance %" value={monthly.attendance_pct} />
            </div>
          )}
        </form>
        <form className="card grid" onSubmit={loadSummary}>
          <h2>Departments</h2>
          <label>Department (optional)<input name="department" placeholder="Exact name" /></label>
          <div className="actions"><button type="submit">Department summary</button></div>
        </form>
      </div>
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Department</th><th>Headcount</th><th>Present days</th><th>Avg hours</th><th>Late rows</th><th>Late minutes</th><th>Leave</th><th>On duty</th>
              </tr>
            </thead>
            <tbody>
              {summary.length === 0 && <tr><td colSpan={8}>Run a department summary to fill this table.</td></tr>}
              {summary.map((item) => (
                <tr key={item.department}>
                  <td>{item.department}</td>
                  <td>{item.headcount}</td>
                  <td>{item.present_days}</td>
                  <td>{item.avg_work_hours ?? "—"}</td>
                  <td>{item.late_count}</td>
                  <td>{item.total_late_minutes}</td>
                  <td>{item.leave_count}</td>
                  <td>{item.on_duty_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <form className="card toolbar" onSubmit={loadBoard}>
        <h2>Late leaderboard</h2>
        <label>Limit<input name="limit" type="number" min="1" max="50" defaultValue="10" /></label>
        <label>Department<input name="department" placeholder="Optional" /></label>
        <button type="submit">Rank</button>
      </form>
      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Rank</th><th>Code</th><th>Name</th><th>Department</th><th>Late minutes</th><th>Late days</th></tr>
            </thead>
            <tbody>
              {board.length === 0 && <tr><td colSpan={6}>Nobody was late, or the board has not been loaded.</td></tr>}
              {board.map((item) => (
                <tr key={item.emp_code}>
                  <td>{item.rank}</td>
                  <td className="mono">{item.emp_code}</td>
                  <td>{item.name}</td>
                  <td>{item.department}</td>
                  <td>{item.total_late_minutes}</td>
                  <td>{item.late_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <form className="card toolbar" onSubmit={loadTrend}>
        <h2>Daily trend</h2>
        <label>Department<input name="department" required placeholder="Engineering" /></label>
        <label>From<input name="from" type="date" defaultValue="2026-07-06" required /></label>
        <label>To<input name="to" type="date" defaultValue="2026-07-14" required /></label>
        <button type="submit">Draw</button>
      </form>
      <div className="card">
        <div className="chart">
          {trend.map((item) => {
            const rate = item.attendance_rate == null ? 0 : Math.min(item.attendance_rate, 1);
            return (
              <div className="bar-row" key={item.date}>
                <span>{item.date.slice(5)}</span>
                <div className="bar"><span style={{ width: `${rate * 100}%` }} /></div>
                <span>{item.attendance_rate == null ? "—" : item.attendance_rate}</span>
              </div>
            );
          })}
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th><th>Working day</th><th>Headcount</th><th>Present</th><th>Late</th><th>Rate</th><th>7-day avg</th>
              </tr>
            </thead>
            <tbody>
              {trend.map((item) => (
                <tr key={item.date}>
                  <td>{item.date}</td>
                  <td>{item.is_working_day ? "yes" : "no"}</td>
                  <td>{item.headcount}</td>
                  <td>{item.present_count}</td>
                  <td>{item.late_count}</td>
                  <td>{item.attendance_rate ?? "—"}</td>
                  <td>{item.moving_avg_7d ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
