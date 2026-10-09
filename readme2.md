# How this attendance API actually works

This note is for you, Rahul. `README.md` stays short because that is the file the assessment asks for. This one is the walkthrough for a live review.

## What you are handing in

The graded program is one FastAPI app in `app/main.py`. Uvicorn must start with `uvicorn app.main:app --port 8000`. MongoDB 6 or 7. Two collections: `employees` and `attendance_logs`. The business id is `emp_code` (`EMP` plus 4–6 digits), not Mongo `_id`.

The connection comes only from the environment. `MONGO_URI` and `MONGO_DB` are required. `.env` is loaded with `override=False`, so a real variable already set in the shell wins over the file. The dummy string in `.env.example` is a placeholder. For the local Docker container use `mongodb://127.0.0.1:27017` and any database name except `attendance_test` (the tests own that name and wipe it).

Instants in the API are epoch milliseconds. In Mongo they are timezone-aware UTC datetimes. Dates (`YYYY-MM-DD`) and shift times (`HH:MM`) are IST strings. Punches are truncated to whole seconds before they are stored.

## The ten rules, with the numbers that get asked

**R1, attendance date.** Take the IST calendar date of punch-in. Exception: an overnight shift (`shift_end` minutes ≤ `shift_start` minutes — compare minutes, never the `HH:MM` strings, because `"09:30" > "18:30"` as text) and a punch earlier than `shift_end` belong to the previous day. A 22:00–06:00 shift punched at 05:30 IST on 7 July is the 6 July record. Shift end for that record is the next calendar day.

**R2, late.** Late only if the punch is strictly more than 10:00 after shift start. `late_minutes` is the floor of minutes since shift start. Shift 09:30: punch 09:40:00 is 0, punch 09:40:01 is 10, punch 10:15:59 is 45.

**R3, overtime.** Floor minutes after shift end, and only if that is at least 30. A 21:55 IST punch-in and 06:40 next-day punch-out on a 22:00–06:00 shift is 8.75 hours, 40 overtime minutes, and not late.

**R4, work hours.** Seconds divided by 3600, then half-up to 2 decimals. Python `round` is banker’s rounding, so `round(3618/3600, 2)` is `1.0`, not `1.01`. The code uses `Decimal`.

**R5, half day.** After that rounding, hours under 4.50 are a half day.

**R6, who counts as present.** `PRESENT`, `WFH`, and `ON_DUTY` are present. `ABSENT` and `LEAVE` are not. `ABSENT` or `LEAVE` cannot be saved with punches.

**R7, working days.** Monday–Friday from `joined_on` through the period end. Present days count only on those weekdays. A half day is 0.5. Weekend logs still add to late and overtime totals. July 2026 has 23 weekdays. Someone who joined on 13 July has 15. July 6 2026 is a Monday.

**R8, rounding of reports.** Rates use 4 decimals, half-up. Other reported numbers use 2.

**R9, headcount.** Employees with `joined_on` on or before the period end, including people with zero logs. A person who joins in August is not in the July headcount.

**R10, pages.** Page starts at 1. Default size 20, max 100. `total` is the filtered count, not the page size.

A range longer than 92 days is 422. Missing `history` is `[]`, missing `half_day` is false, missing late or overtime is 0.

## Sample July 2026, so you can quote it

`EMP0001`: 23 working days, 2 present, attendance 8.70%. `EMP0002` joined 13 July: 15 working, 1 present, 6.67%. `EMP0004`: 0.5 present, 2.17%. `EMP0003`: 35 late minutes, 40 overtime, 1 leave, 4.35%. Sales average hours 6.29 (the 9.08 and the 3.5). Support present days 2, average 8.75; the open `EMP0006` row is excluded from the average because it has no hours yet. Engineering with the extra weekend and half-day fixtures: headcount 3, present days 3.5, late count 2, total late 114, average hours 8.63. June Engineering headcount is 2 (`EMP0001` and `EMP0099`). Ops is the average of the stored hour rows (two 10h and one 4h → 8.00), not an average of averages.

## What the starter got wrong

`REVIEW.md` lists the starter defects with the original line numbers. The ones a reviewer usually picks on: late used a raw millisecond gap and treated the 10-minute boundary wrong; overnight detection compared time strings; work hours used banker’s `round`; overtime subtracted a naive datetime from an aware one; punch-out did not find the open overnight record; regularize did not recompute derived fields or append history; analytics recomputed from punches instead of reading stored fields; explain could fall back to a collection scan. `load_dotenv` without `override` was already correct. Punch-in defaults and inclusive date filters were already correct.

## Writes, races, and regularize

Create employee and punch-in do not read-then-insert. The unique indexes reject the loser, and `DuplicateKeyError` becomes 409.

Punch-out closes the latest record whose punch-in is at or before `punched_at`, including overnight. Already out, or a lost race, is 409. `punched_at` at or before punch-in, or more than 24 hours after, is 422. Exactly 24 hours is allowed.

`PATCH /attendance/{emp_code}/{date}` recomputes late, overtime, hours, and half day from the employee’s shift. It appends one history entry, and only for fields that actually changed. Nothing changed is 422. The new punch-in must stay on that attendance date. Concurrent edits use the history length plus the old field values as the update condition, so the second writer gets 409.

## Analytics and explain

Every analytics route is an aggregation. They read the stored derived fields. They do not recompute from punches.

The leaderboard uses `$setWindowFields` and `$rank` (competition ranking: 1, 2, 2, 4). `limit` keeps `rank <= limit`, so a tie on the cutoff returns more than `limit` rows, then those rows are sorted by `emp_code`. August 2026 Engineering with limit 2: `EMP0101` rank 1 (105), `EMP0102` and `EMP0103` rank 2 (80 each). Unknown codes are ignored. Unfiltered limit 1 returns only the person with 200 late minutes.

The department trend emits one row per calendar day, weekends and empty days included, with `$facet`, `$range`, and `$map`. `$densify` is the wrong tool: it needs a date or numeric field, and an empty input yields nothing. Attendance rate is null on a non-working day or when headcount is 0. A working day with no records is 0.0. The 7-day moving average is the mean of non-null rates in the current row plus up to six earlier rows inside the requested range. Mongo `$round` is half-even, so half-up is integer division inside the pipeline.

`GET /admin/explain/{endpoint}` runs with `executionStats`. The grader searches the whole JSON for `IXSCAN` and rejects any `COLLSCAN` string, so the queries are hinted. A rejected collection-scan plan would still fail that string search.

Indexes, created at startup: `emp_code_unique`; `dept_joined` on `(department, joined_on)`; `joined_on`; `emp_date_unique` on `(emp_code, date)`; `date_emp`; `status_date_emp`; `emp_status_date`; `date_late`; `emp_punch_in`. A department-only index was skipped because the compound prefix already covers that lookup. `/health` returns 200 only if `ping` succeeds, otherwise 503. Index creation errors are swallowed at startup so a down database can still answer `/health`.

## The React screen

`frontend/` is a Vite + React app. It is not mounted by FastAPI and the grader does not need it. `npm install` then `npm run dev` serves http://localhost:5173 and proxies `/health`, `/employees`, `/attendance`, `/analytics`, and `/admin` to the API on port 8000.

The three views match the API: employees (create and filter), attendance (punch in, punch out, search, correct one day), analytics (monthly person, department summary, late leaderboard, daily trend). Datetime inputs are read as IST (`+05:30`), not as the laptop zone, and epoch values are shown back in IST. The health pill polls `GET /health` and does not overwrite the last response panel. User data is rendered as text, not as HTML.

## What to say if they ask “why”

Indexes exist so the explain endpoint cannot fall into a collection scan, and so create and punch-in are safe under two concurrent requests. Derived fields are stored because the aggregations are required to trust them. Half-up is explicit because the language default would mark 1.01 hours as 1.00. Overnight shifts use minute numbers because string order lies. The React app is a desk for the same contract, not a second backend.

## Deploy later

Keep the files in this section out of the assessment repository until after you submit. The problem forbids a Dockerfile in the submission. Copy them into a `deploy/` folder only when you are ready to ship.

The screen calls `/health`, `/employees`, `/attendance`, `/analytics`, and `/admin` on the same origin. `npm run dev` proxies those paths to port 8000. A production build has no proxy, so one web server must serve `frontend/dist` and forward those five paths to uvicorn. That keeps the browser and the API on one origin, so the existing `fetch` calls work with no frontend change.

Use MongoDB Atlas for the database. Create a database user, allow the host’s IP (or `0.0.0.0/0` if the host IP changes), and create an empty database named `attendance_db`. Leave `attendance_test` alone; the tests wipe that name. The URI looks like `mongodb+srv://USER:PASSWORD@your-cluster.example.mongodb.net/?retryWrites=true&w=majority`. Put it in the host’s environment as `MONGO_URI`. Set `MONGO_DB=attendance_db`. A real environment variable wins over `.env`.

Inside a container, uvicorn must listen on every interface. The graded command stays `uvicorn app.main:app --port 8000` because the grader calls localhost. The container command adds `--host 0.0.0.0`.

Create `deploy/Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY sample_seed.py .
COPY sample_data ./sample_data

ENV MONGO_DB=attendance_db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Create `deploy/nginx.conf`:

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;

    location /health     { proxy_pass http://api:8000; }
    location /employees  { proxy_pass http://api:8000; }
    location /attendance { proxy_pass http://api:8000; }
    location /analytics  { proxy_pass http://api:8000; }
    location /admin      { proxy_pass http://api:8000; }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

Create `deploy/Dockerfile.web`:

```dockerfile
FROM nginx:1.27-alpine
COPY frontend/dist /usr/share/nginx/html
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
```

Create `deploy/docker-compose.yml`:

```yaml
services:
  api:
    build:
      context: .
      dockerfile: deploy/Dockerfile
    environment:
      MONGO_URI: ${MONGO_URI}
      MONGO_DB: ${MONGO_DB}
    restart: unless-stopped

  web:
    build:
      context: .
      dockerfile: deploy/Dockerfile.web
    ports:
      - "8080:80"
    depends_on:
      - api
    restart: unless-stopped
```

From the repository root, with `MONGO_URI` and `MONGO_DB` exported in the shell (or in a `.env` next to the compose file, and that `.env` stays uncommitted):

```bash
cd frontend
npm install
npm run build
cd ..
docker compose -f deploy/docker-compose.yml up --build -d
docker compose -f deploy/docker-compose.yml exec api python sample_seed.py
curl http://localhost:8080/health
```

`/health` should return `{"status":"ok"}`. Open http://localhost:8080 and create an employee. The seed command is optional; Atlas can start empty.

On a public machine, store `MONGO_URI` as a secret, open Atlas to that machine, and put HTTPS in front of port 8080. The API process still reads only `MONGO_URI` and `MONGO_DB`. Indexes are created when the process starts.
