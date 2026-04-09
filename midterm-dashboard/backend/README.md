# midterm-dashboard/backend/ — FastAPI server

The Python half of the Midterm Dashboard. FastAPI app that pulls election
predictions from Polymarket / Kalshi / PredictIt / polling aggregators,
stores them in SQLite, and exposes JSON endpoints + the built React frontend
on port 8051.

Run standalone:

```bash
pip install -r requirements.txt
python3 main.py
```

Or rely on the multi-stage `Dockerfile` in the parent directory, which builds
the frontend then runs this backend.

## Files in this directory

| File | Purpose |
|---|---|
| `main.py` | The FastAPI app. Routes (`/data/*`, `/auth/*`, `/admin/*`), gateway SSO middleware, CORS for the Vite dev server, background data-refresh task (every 5 min), serves the built `frontend/dist/` as static files in production. |
| `database.py` | SQLite layer (WAL mode, threading lock, contextmanager). User auth was migrated out — the gateway handles users/sessions; this module only manages dashboard data and the shared `profiles` table. UUID string user IDs. |
| `district_profiles.py` | Hand-curated background context per state/district — demographics, economy, infrastructure, political history, geography. A background task in `main.py` keeps profiles fresh for every state with an active race. |
| `race_context.py` | Per-race context dictionary keyed by `{race_type}_{state}`: incumbents, likely candidates, state ballot measures, key issues, Cook/Sabato lean rating, narrative. Drives the "why is this market moving" view. |
| `historical_results.py` | Hand-curated dataset of recent federal/statewide election winners with vote totals and margins. Powers the `/data/historical` endpoint so users can compare current markets against historical baselines. |
| `requirements.txt` | Python deps (FastAPI, aiohttp, sqlmodel, etc.). Tiny — most logic is in stdlib + FastAPI. |
| `midterm_dashboard.db` | Main SQLite DB (~108MB, gitignored). Auto-created on first run. |

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `aggregators/` | Source connectors for prediction markets and polling | `aggregators/README.md` |
| `templates/` | Reserved for future server-rendered templates (currently empty). | — |
