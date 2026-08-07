# forecast-dashboard

Cross-venue short-horizon forecast board: Polymarket, Kalshi, Manifold,
Metaculus, options-implied equity markets, and free-form "ask" questions.
Ingests markets closing within a configurable horizon, computes a
market-implied baseline probability per market, layers model probabilities
on top (weather, Fed, scored Claude forecaster), links equivalent markets
across venues, and tracks calibration (Brier scores) against resolved
outcomes.

Port **7062**, subdomain `predict.narve.ai`. No trading — probabilities and
factors only.

## Run locally

```bash
cd forecast-dashboard
DEV_MODE=1 ../venv/bin/python3 -m uvicorn server:app --port 7062
```

`DEV_MODE=1` bypasses the gateway SSO check. In production the gateway
injects `X-Gateway-Secret` (checked against `GATEWAY_SSO_SECRET`). See
`.env.example` for the optional knobs (LLM forecasting, Metaculus token,
FRED key, horizon).

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: SSO middleware, API endpoints, three background loops (ingest 600s, models 1800s, resolve 1800s). |
| `db.py` | All SQLite access (WAL, `data/forecast.sqlite`). Tables: markets, snapshots, model_probs, venue_links, calibration. Also `compute_baseline()` (bid/ask/last → baseline prob + confidence tier) and `get_brier_score()`. |
| `ingest_polymarket.py` | Active Polymarket markets within the horizon → normalized market dicts. |
| `ingest_kalshi.py` | Same for Kalshi (server-side series filter keeps the liquid/model-relevant slice). |
| `ingest_manifold.py` | Same for Manifold (public v0 API, no auth). |
| `ingest_metaculus.py` | Same for Metaculus binary questions — needs `METACULUS_API_TOKEN` (anonymous reads 403 as of 2026-08). |
| `ingest_options.py` | Self-issued binary markets from equity options chains (close ≥ strike by expiry). |
| `models_weather.py` | Weather-ensemble probabilities for matching markets → model_probs rows. |
| `models_fed.py` | Fed/rates model probabilities, optionally via FRED. |
| `models_llm.py` | Scored Claude forecaster (`FORECAST_LLM_ENABLED=1`, daily cap) — independent probs, never shown the market price. |
| `ask.py` | Ask-anything engine behind `/api/ask`: search matches + on-demand Claude forecast logged under a `venue='custom'` market. |
| `matching.py` | Cross-venue market matching → venue_links rows; disagreement listing. |
| `resolver.py` | Settles closed markets against venue settlement APIs, adjudicates custom questions, backfills calibration outcomes (voided/cancelled markets never poison the ledger). |
| `static/` | `index.html` (board + ask UI), `accuracy.html` (Brier/calibration), `style.css`, vendored `narve-shell.js`. |
| `tests/` | Offline pytest suite — DB, every ingest venue, models, matching, resolver, ask. |

## Endpoints

- `GET /healthz` — liveness (auth-exempt)
- `GET /` — board page; `GET /accuracy` — accuracy page
- `GET /api/board?days=7&category=&venue=&sort=end_date&q=` — board rows
  (display prob, baseline, model prob, 24h move, confidence tier,
  cross-venue disagreement)
- `GET /api/ask?q=` — search matches + optional on-demand forecast
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
`<venue>:<venue_id>` (venues: polymarket, kalshi, manifold, metaculus,
options, custom). Categories share one vocabulary across venue maps —
politics / science / tech / sports / finance / economics / weather / fed /
other. Calibration logs one row per displayed market per source per UTC day
(`UNIQUE(market_uid, source, displayed_at)`), with outcomes backfilled on
resolution. Custom (ask-created) markets have no snapshots, so they never
appear on the board; they resolve via LLM adjudication.
