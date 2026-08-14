# Whale Watch — Institutional Flow Dashboard

Tracks institutional "whale" money through SEC filings: quarterly 13F holdings
for ~17 hand-curated mega-funds (with sub-CIKs collapsed per parent), Form 4
insider trades with a cluster-buy detector, and Schedule 13D/13G activist
stakes with intent classification — plus smart-money consensus, crowdedness
percentiles, CFTC Commitment of Traders, and Polymarket cross-links.

Port: **8053**. Lives behind the gateway at `whale.narve.ai` in production,
and as the Whale Watch tab of the unified view at `/one`.

## Run locally

```bash
cd whale-dashboard
cp .env.example .env       # set SEC_USER_AGENT (EDGAR requires it) + GATEWAY_SSO_SECRET
pip install -r backend/requirements.txt
python3 backend/main.py
# http://localhost:8053
```

Or via Docker from the repo root:

```bash
docker compose up --build whale
```

## Files in this directory

**Backend** (`backend/`)
| File | Purpose |
|---|---|
| `main.py` | FastAPI app — REST API, serves the frontend, daemon-thread ingest workers (no APScheduler). Mirrors `midterm-dashboard/backend/main.py` conventions. |
| `database.py` | SQLite layer — whales, holdings, deltas, insider transactions, 13D/13G filings, watchlists, alert rules. |
| `alerts.py` | Alert engine — watchlist rules fire on 13D filings, cluster buys, whale moves, consensus crosses; webhook + SMTP delivery. |
| `auth.py` | Gateway SSO verification via the `x-gateway-secret` header; `GET /` and `/health` stay open (health probes), `/api/*` is gated. |
| `ws_feed.py` | WebSocket fanout for real-time filing/alert updates (`supports_websocket: true` in the gateway config). |
| `requirements.txt` | Python deps for the backend. |

**Frontend** (`frontend/`) — static vanilla JS, served by the backend.
| File | Purpose |
|---|---|
| `index.html` | Single-page dashboard shell. |
| `app.js` | All frontend logic — tabs, tables, charts, WebSocket feed. |
| `style.css` | Dashboard styling. |

**Other**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build (python:3.12-slim, `EXPOSE 8053`). |
| `.env.example` | Reference env vars — `SEC_USER_AGENT` (required by EDGAR), `GATEWAY_SSO_SECRET`, optional `OPENFIGI_API_KEY` (faster CUSIP→ticker resolution) and SMTP creds. |
| `scripts/whale-watchdog.sh` | Watchdog script for the production box. |
| `tests/smoke.py` | Smoke tests. |

## Env vars

- `SEC_USER_AGENT` — **required for ingestion**: EDGAR rate-limits or blocks
  requests without a descriptive User-Agent + contact email. The server still
  boots without it (workers log-and-retry; nothing ingests).
- `GATEWAY_SSO_SECRET` — gates `/api/*`; without it the API returns 401 while
  `GET /` keeps serving the frontend (so health probes pass either way).
- `PORT` — bind port, default 8053 (matches `gateway/config.json`).
- `OPENFIGI_API_KEY`, `SMTP_*` — optional; features degrade gracefully.
