# DECISIONS.md

1. **Indexes.** emp_code on employees is unique, so a duplicate create is 409 from the database. (emp_code, date) on attendance_logs is unique and serves punch-in. (department, joined_on) does headcount; joined_on alone covers the unfiltered summary. Lists use (date, emp_code), (status, date, emp_code), or (emp_code, status, date). (date, late_minutes) bounds the leaderboard, and (emp_code, punch_in) finds punch-out. I rejected a department-only index; the compound prefix already covers it.

2. **Punch-in race.** Both requests compute the same attendance date and insert. One insert commits and returns 201. The other hits the unique index and returns 409. Nothing reads-then-inserts.

3. **Ties.** $rank orders by total late minutes only, so a tie shares a rank and the next number is skipped. limit then keeps rows with rank <= limit. A tie on the cutoff is returned in full, so the list can be longer than limit. Tied rows are ordered by emp_code.

4. **Headcount.** The pipeline starts from employees with joined_on on or before month end, then looks up logs. The group counts people, so zero logs still count. Later joiners never match.

5. **100x.** Shard attendance_logs by emp_code, pre-aggregate the leaderboard by month, and replace skip/limit with a keyset on (date, emp_code).
