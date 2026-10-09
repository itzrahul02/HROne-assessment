import { useCallback, useEffect, useState } from "react";
import { formatIst, get, istToEpoch, query, send } from "./api";
import Pager from "./Pager";

function History({ history }) {
  if (!history || history.length === 0) {
    return <p className="hint">No corrections on this record.</p>;
  }
  const shown = (field, value) => {
    if (value == null) return "—";
    if (field === "punch_in" || field === "punch_out") return formatIst(value);
    return String(value);
  };
  return (
    <div className="history">
      {history.map((entry, index) => (
        <article key={`${entry.at}-${index}`}>
          <strong>{entry.by} · {formatIst(entry.at)} · {entry.reason}</strong>
          {Object.entries(entry.changes || {}).map(([field, diff]) => (
            <div key={field}>{field}: {shown(field, diff.from)} → {shown(field, diff.to)}</div>
          ))}
        </article>
      ))}
    </div>
  );
}

export default function Attendance({ notify }) {
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);
  const [filters, setFilters] = useState({});
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    const body = await get(`/attendance${query({ ...filters, page, page_size: 20 })}`);
    setTotal(body.total);
    setItems(body.items);
  }, [filters, page]);

  useEffect(() => {
    load().catch((error) => notify(error.message));
  }, [load, notify]);

  function punchPayload(form) {
    const payload = Object.fromEntries(new FormData(form));
    const punched = istToEpoch(payload.punched_at);
    if (punched == null) delete payload.punched_at;
    else payload.punched_at = punched;
    return payload;
  }

  async function punchIn(event) {
    event.preventDefault();
    try {
      const record = await send("POST", "/attendance/punch-in", punchPayload(event.target));
      notify(`Punched in ${record.emp_code} on ${record.date}. Late minutes: ${record.late_minutes}.`, "ok");
      setSelected(record);
      if (page !== 1) setPage(1);
      else await load();
    } catch (error) {
      notify(error.message);
    }
  }

  async function punchOut(event) {
    event.preventDefault();
    try {
      const record = await send("POST", "/attendance/punch-out", punchPayload(event.target));
      notify(`Punched out ${record.emp_code} on ${record.date}. Hours ${record.work_hours}, overtime ${record.overtime_minutes}.`, "ok");
      setSelected(record);
      await load();
    } catch (error) {
      notify(error.message);
    }
  }

  function search(event) {
    event.preventDefault();
    setPage(1);
    setFilters(Object.fromEntries(new FormData(event.target)));
  }

  async function regularize(event) {
    event.preventDefault();
    try {
      const payload = Object.fromEntries(new FormData(event.target));
      const code = payload.emp_code;
      const day = payload.date;
      delete payload.emp_code;
      delete payload.date;
      if (!payload.status) delete payload.status;
      if (payload.punch_in) payload.punch_in = istToEpoch(payload.punch_in);
      else delete payload.punch_in;
      if (payload.punch_out) payload.punch_out = istToEpoch(payload.punch_out);
      else delete payload.punch_out;
      const record = await send("PATCH", `/attendance/${encodeURIComponent(code)}/${day}`, payload);
      notify(`Corrected ${record.emp_code} on ${record.date}.`, "ok");
      setSelected(record);
      await load();
    } catch (error) {
      notify(error.message);
    }
  }

  return (
    <section>
      <div className="split">
        <form className="card grid" onSubmit={punchIn}>
          <h2>Punch in</h2>
          <label>Employee code<input name="emp_code" required placeholder="EMP0001" /></label>
          <label>Time (IST, optional)<input name="punched_at" type="datetime-local" step="1" /></label>
          <label>Status
            <select name="status" defaultValue="PRESENT">
              <option>PRESENT</option>
              <option>WFH</option>
              <option>ON_DUTY</option>
            </select>
          </label>
          <div className="actions"><button type="submit">Punch in</button></div>
        </form>
        <form className="card grid" onSubmit={punchOut}>
          <h2>Punch out</h2>
          <label>Employee code<input name="emp_code" required placeholder="EMP0001" /></label>
          <label>Time (IST, optional)<input name="punched_at" type="datetime-local" step="1" /></label>
          <p className="hint">Closes the latest open punch at or before this time, including an overnight shift.</p>
          <div className="actions"><button type="submit">Punch out</button></div>
        </form>
      </div>
      <div className="card">
        <form className="toolbar" onSubmit={search}>
          <label>Code<input name="emp_code" /></label>
          <label>From<input name="date_from" type="date" /></label>
          <label>To<input name="date_to" type="date" /></label>
          <label>Status
            <select name="status" defaultValue="">
              <option value="">Any</option>
              <option>PRESENT</option>
              <option>WFH</option>
              <option>ON_DUTY</option>
              <option>ABSENT</option>
              <option>LEAVE</option>
            </select>
          </label>
          <button type="submit">Search</button>
        </form>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th><th>Code</th><th>Status</th><th>In</th><th>Out</th><th>Hours</th><th>Late</th><th>OT</th><th>Half</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && <tr><td colSpan={9}>No attendance rows for this filter.</td></tr>}
              {items.map((record) => (
                <tr key={`${record.emp_code}-${record.date}`} onClick={() => setSelected(record)}>
                  <td>{record.date}</td>
                  <td className="mono">{record.emp_code}</td>
                  <td><span className={`tag ${record.status}`}>{record.status}</span></td>
                  <td>{formatIst(record.punch_in)}</td>
                  <td>{formatIst(record.punch_out)}</td>
                  <td>{record.work_hours ?? "—"}</td>
                  <td>{record.late_minutes}</td>
                  <td>{record.overtime_minutes}</td>
                  <td>{record.half_day ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pager page={page} pageSize={20} total={total} onPage={setPage} />
      </div>
      <form className="card grid" onSubmit={regularize}>
        <h2>Correct a day</h2>
        <p className="hint">
          {selected
            ? `${selected.emp_code} on ${selected.date}: ${selected.status}, in ${formatIst(selected.punch_in)}, out ${formatIst(selected.punch_out)}, ${selected.work_hours ?? "—"} h, late ${selected.late_minutes}, overtime ${selected.overtime_minutes}, half day ${selected.half_day}.`
            : "Click a row to fill the code and date. Leave a punch blank if it should stay as it is."}
        </p>
        <label>Employee code<input name="emp_code" required defaultValue={selected?.emp_code || ""} key={selected ? `${selected.emp_code}-code` : "code"} /></label>
        <label>Attendance date<input name="date" type="date" required defaultValue={selected?.date || ""} key={selected ? `${selected.emp_code}-${selected.date}` : "date"} /></label>
        <label>Status
          <select name="status" defaultValue="">
            <option value="">Unchanged</option>
            <option>PRESENT</option>
            <option>WFH</option>
            <option>ON_DUTY</option>
            <option>ABSENT</option>
            <option>LEAVE</option>
          </select>
        </label>
        <label>New punch in (IST)<input name="punch_in" type="datetime-local" step="1" /></label>
        <label>New punch out (IST)<input name="punch_out" type="datetime-local" step="1" /></label>
        <label>Reason<input name="reason" required minLength={5} maxLength={200} placeholder="At least 5 characters" /></label>
        <label>Corrected by<input name="regularized_by" required maxLength={50} placeholder="hr.admin" /></label>
        <div className="actions"><button type="submit">Save correction</button></div>
        <History history={selected?.history} />
      </form>
    </section>
  );
}
