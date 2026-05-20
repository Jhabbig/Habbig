# Midterm Dashboard — Claude notes

US midterm election predictions. **The only dashboard with a JS build step.**
FastAPI backend on **8051**, React/Vite frontend pre-built into
`frontend/dist/` and served by the backend in production.

## Layout

- `backend/` — FastAPI app. Real backend code lives here, not in this dir.
  See `backend/README.md` for the per-file breakdown.
- `frontend/` — React + Vite + Tailwind. **`src/lib/` is the one place in
  this repo where a frontend `lib/` directory is unignored in `.gitignore`.**
  If you mirror this structure to another dashboard, add it to the unignore
  list at the repo root or the directory will be silently swallowed.
- `frontend/dist/` — build output, gitignored. Backend serves from here in
  prod, so the Dockerfile builds it as the first stage.
- `deploy.sh` — **separate from the repo-root `deploy.sh`**. Builds the
  frontend, sets up a venv, seeds an admin user from `ADMIN_EMAIL` /
  `ADMIN_PASSWORD`. Don't conflate the two.

## Running

```bash
# Backend (production-mode, serves built dist/)
pip install -r backend/requirements.txt
python3 backend/main.py            # http://localhost:8051

# Frontend dev server (talks to backend on :8051)
cd frontend && npm install && npm run dev   # http://localhost:3000
```

`DEV=1` enables uvicorn auto-reload on the backend.

## Verifying changes

- **Frontend change**: hit the Vite dev server on :3000 — that's where
  HMR works. The backend on :8051 only sees changes after `npm run build`.
- **Backend change**: hit :8051 directly. The built UI under `dist/` is
  stale unless you rebuild.
- **Before deploy**: `npm run build` then verify :8051 serves the
  rebuilt assets, because production uses the backend-served path, not
  the dev server.

## Env vars

`GATEWAY_SSO_SECRET` (must match `gateway/.env`), `PORT` (default 8051),
`FRONTEND_ORIGIN` (CORS, default `http://localhost:3000`), `DEV`,
`ADMIN_EMAIL` / `ADMIN_PASSWORD` (deploy.sh only — first-deploy seeding).

## Don't

- Don't commit `frontend/dist/`, `node_modules/`, the SQLite DB (it's
  ~108MB), or `.env`. All gitignored.
- Don't add a second frontend build to another dashboard "for consistency."
  This is the only one with React on purpose — everything else stays
  static HTML.
- Don't edit `backend/main.py`'s static-mount path without also updating
  the Dockerfile build stage.
