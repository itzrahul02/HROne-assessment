import { useCallback, useEffect, useState } from "react";
import { formatIst, get, query, send } from "./api";
import Pager from "./Pager";

export default function Employees({ notify }) {
  const [department, setDepartment] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState([]);

  const load = useCallback(async () => {
    const body = await get(`/employees${query({ department, page, page_size: 20 })}`);
    setTotal(body.total);
    setItems(body.items);
  }, [department, page]);

  useEffect(() => {
    load().catch((error) => notify(error.message));
  }, [load, notify]);

  async function createEmployee(event) {
    event.preventDefault();
    try {
      const created = await send("POST", "/employees", Object.fromEntries(new FormData(event.target)));
      notify(`Created ${created.emp_code}.`, "ok");
      event.target.reset();
      event.target.elements.shift_start.value = "09:30";
      event.target.elements.shift_end.value = "18:30";
      if (page !== 1) setPage(1);
      else await load();
    } catch (error) {
      notify(error.message);
    }
  }

  function applyFilter(event) {
    event.preventDefault();
    const next = new FormData(event.target).get("department").trim();
    setPage(1);
    setDepartment(next);
  }

  return (
    <section>
      <form className="card grid" onSubmit={createEmployee}>
        <h2>New employee</h2>
        <label>Employee code<input name="emp_code" required pattern="EMP\d{4,6}" placeholder="EMP0007" /></label>
        <label>Name<input name="name" required maxLength={100} /></label>
        <label>Email<input name="email" type="email" required maxLength={120} /></label>
        <label>Department<input name="department" required maxLength={50} placeholder="Engineering" /></label>
        <label>Shift start<input name="shift_start" required defaultValue="09:30" pattern="([01]\d|2[0-3]):[0-5]\d" /></label>
        <label>Shift end<input name="shift_end" required defaultValue="18:30" pattern="([01]\d|2[0-3]):[0-5]\d" /></label>
        <label>Joined on<input name="joined_on" type="date" required /></label>
        <div className="actions"><button type="submit">Create employee</button></div>
      </form>
      <div className="card">
        <form className="toolbar" onSubmit={applyFilter}>
          <label>Department<input name="department" placeholder="Exact name" defaultValue={department} /></label>
          <button type="submit">Filter</button>
        </form>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Code</th><th>Name</th><th>Department</th><th>Shift</th><th>Joined</th><th>Created</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr><td colSpan={6}>No employees yet. Create one, or run python sample_seed.py.</td></tr>
              )}
              {items.map((employee) => (
                <tr key={employee.emp_code}>
                  <td className="mono">{employee.emp_code}</td>
                  <td>{employee.name}</td>
                  <td>{employee.department}</td>
                  <td>{employee.shift_start}–{employee.shift_end}</td>
                  <td>{employee.joined_on}</td>
                  <td>{formatIst(employee.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Pager page={page} pageSize={20} total={total} onPage={setPage} />
      </div>
    </section>
  );
}
