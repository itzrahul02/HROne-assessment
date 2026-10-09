# Employee Attendance & Analytics API

From a clean clone, at the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` is fine: copy `.env.example` and replace the dummy URI (`mongodb://localhost:27017` for a local MongoDB). Real environment variables override `.env`. No connection string is hard-coded.

```bash
uvicorn app.main:app --port 8000
```

All application code is in `app/main.py`. Indexes are created at startup, with no manual step. `GET /health` returns 200 only when MongoDB answers, otherwise 503. MongoDB 6.0 or newer. `python sample_seed.py` loads the nine sample documents. Nothing from the contract is missing.
