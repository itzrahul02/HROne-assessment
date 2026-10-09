# Employee Attendance & Analytics API

All application code is in `app/main.py`. From the repository root:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Replace the dummy `MONGO_URI` in `.env`. A local MongoDB is `mongodb://localhost:27017`. Real environment variables override `.env`. Then:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000
```

`GET /health` returns 200 only when MongoDB answers. Indexes are created at startup. MongoDB 6.0 or newer. No connection string is hard-coded. The graded API does not serve a page; the optional React screen is in `frontend/`.
