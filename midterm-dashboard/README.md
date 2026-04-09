# Midterm Dashboard — Election Predictions

US midterm election prediction dashboard. Aggregates Polymarket, Kalshi,
PredictIt, and polling data into a single view of every house, senate, and
gubernatorial race.

Port: **8051**. Lives behind the gateway at `midterm.narve.ai` in production.

Split architecture: FastAPI **backend** + React/Vite **frontend** (pre-built
into `frontend/dist` and served by FastAPI in production).

## Run locally

```bash
cd midterm-dashboard
cp .env.example .env       # fill in keys

# Backend
pip install -r backend/requirements.txt
python3 backend/main.py
# http://localhost:8051

# Frontend (separate terminal — only needed for frontend dev)
cd frontend
npm install
npm run dev
# http://localhost:3000 (talks to backend on :8051)
```

Or via Docker from the repo root:

```bash
docker compose up --build midterm
```

## Files in this directory

This is a wrapper directory — the real code lives in `backend/` and `frontend/`.
See their own READMEs for the per-file breakdowns.

| File / dir | Purpose |
|---|---|
| `backend/` | FastAPI Python backend (port 8051). See `backend/README.md`. |
| `frontend/` | React + Vite + Tailwind UI. See `frontend/README.md`. |
| `Dockerfile` | Multi-stage container — builds the React frontend, then runs the FastAPI backend serving the built `dist/`. |
| `.dockerignore` | Excludes `node_modules`, `dist`, `*.db`, `venv` from the Docker build context. |
| `.gitignore` | Local-only ignores (Vite `dist/`, the 108MB SQLite DB, etc.). |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `deploy.sh` | Standalone deploy script (separate from the root `deploy.sh`). Builds the frontend, sets up a venv, seeds an admin user from `ADMIN_EMAIL`/`ADMIN_PASSWORD`. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `PORT` | `8051` | FastAPI listen port |
| `FRONTEND_ORIGIN` | `http://localhost:3000` | CORS origin for the Vite dev server |
| `DEV` | unset | Any non-empty value enables uvicorn auto-reload |
| `ADMIN_EMAIL` | unset | First-deploy admin user (used by `deploy.sh` only) |
| `ADMIN_PASSWORD` | unset | First-deploy admin password (used by `deploy.sh` only) |

## Notes

- The DB (`midterm_dashboard.db`, ~108MB) is gitignored; the symlink `data.db` points to it.
- Frontend builds are not committed — run `npm run build` before deploying or rely on the Dockerfile to do it.
