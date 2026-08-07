# forecast-dashboard

Cross-venue (Polymarket + Kalshi) short-horizon forecast board. Ingests
markets closing within a configurable horizon, computes a market-implied
baseline probability per market, layers model probabilities on top
(weather, Fed), links equivalent markets across venues, and tracks
calibration (Brier scores) against resolved outcomes.

Port **7062**, subdomain `forecast.narve.ai`. No trading — read-only market
data and forecasting only.

## Run locally

```bash
cd forecast-dashboard
DEV_MODE=1 ../venv/bin/python3 -m uvicorn server:app --port 7062
```

`DEV_MODE=1` bypasses the gateway SSO check. In production the gateway
injects `X-Gateway-Secret` (checked against `GATEWAY_SSO_SECRET`).

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: SSO middleware, API endpoints, three background loops (ingest 600s, models 1800s, resolve 1800s). |
| `db.py` | All SQLite access (WAL, `data/forecast.sqlite`). Tables: markets, snapshots, model_probs, venue_links, calibration. Also `compute_baseline()` (bid/ask/last → baseline prob + confidence tier) and `get_brier_score()`. |
| `ingest_polymarket.py` | Fetches active Polymarket markets within the horizon → normalized market dicts. (Stub — see docstring for the dict contract.) |
| `ingest_kalshi.py` | Same for Kalshi. (Stub.) |
| `models_weather.py` | Weather model probabilities for matching markets → model_probs rows. (Stub.) |
| `models_fed.py` | Fed/rates model probabilities, optionally via FRED. (Stub.) |
| `matching.py` | Cross-venue market matching → venue_links rows; disagreement listing. (Stub.) |
| `resolver.py` | Resolves closed markets, backfills calibration outcomes. (Stub.) |
| `static/` | `index.html` (board), `accuracy.html` (Brier/calibration), `style.css`, vendored `narve-shell.js`. |
| `tests/` | Pytest suite for `db.py` (DDL, upserts, baseline tiers, calibration dedup, Brier math). |

## Endpoints

- `GET /healthz` — liveness (auth-exempt)
- `GET /` — board page; `GET /accuracy` — accuracy page
- `GET /api/board?days=7&category=&venue=&sort=end_date` — board rows
  (display prob, baseline, model prob, 24h move, confidence tier,
  cross-venue disagreement)
- `GET /api/event/{uid}` — market detail + snapshot & model-prob history
- `GET /api/disagreements` — linked cross-venue pairs with prob deltas
- `GET /api/accuracy` — Brier scores + calibration buckets
- `GET /api/status` — loop last-run state + table row counts
- `POST /api/links` — `{poly_uid, kalshi_uid, action: confirm|reject}`

## Tests

```bash
../venv/bin/python3 -m pytest tests -q
```

## Data

SQLite at `data/forecast.sqlite` (WAL mode, git-ignored). Market uids are
`<venue>:<venue_id>`. Calibration logs one row per displayed market per
source per UTC day (`UNIQUE(market_uid, source, displayed_at)`), with
outcomes backfilled on resolution.
