# polymarket_weather_dashboard/ — Weather Dashboard

Polymarket + Kalshi weather temperature markets UI. Flask backend + PWA frontend. Port **5050**, subdomain `weather.narve.ai`. See [README.md](README.md) for the multi-model ensemble details.

## Don't touch unless you mean to

- **`server.py` is ~4000 lines** and runs **3 background threads** (snapshot, bias-pairing, intraday METAR). Touching the threading model without understanding it = silent data corruption.
- **`data.db` and `history.db`** — both SQLite, both gitignored, both auto-created. `data.db` = current state; `history.db` = signals + outcomes. Don't merge them.
- **`GATEWAY_SSO_SECRET`** must match `gateway/.env`. Required when running behind the gateway.
- **`FLASK_SECRET`** — set in production for session persistence across restarts. Default is random-per-restart (fine for dev).
- **Pairs with `polymarket_weather_bot/`** — the bot writes data this dashboard reads. Don't change the schema in one without the other.

## Stack

- Flask (not FastAPI — historical reason, don't migrate without good cause)
- SciPy + requests for the consensus engine
- gzip middleware (cuts 3.5 MB market JSON to ~500 KB over the wire — keep it)
- PWA — manifest + service worker + installable on iOS

## The consensus engine (the core IP)

8 NWP ensembles (GFS, ECMWF IFS, ICON, GEM, UKMO, JMA, Meteofrance ARPEGE, KNMI) + NWS deterministic + climatology. Each member:
1. Bias-corrected per-model from `bias-pairing` thread (uses yesterday's observed temps from Open-Meteo archive)
2. Weighted by member count
3. Sigma inflated by lead time
4. Aggregated → Gaussian CDF → YES/NO probability per market

Plus context layers: ENSO + NAO + AO from NOAA, intraday METAR running max per ICAO station, 8 storm-track corridors for cross-market correlation, NWS narrative parsing for fronts/troughs, persistence + analog baselines for calibration.

## Background threads (don't break)

| Thread | Cadence | Job |
|---|---|---|
| Snapshot loop | 30 min | Polls Polymarket/Kalshi APIs, stores price snapshots, enriches with model probabilities |
| Bias-pairing loop | 6 h | Fetches yesterday's observed temps from Open-Meteo archive, pairs against prior forecasts to compute per-model bias |
| Intraday poll loop | 5 min | Fetches METAR for tracked stations, updates daily-high tracker (BREACHED / SAFE / AT_RISK) |

If a thread dies silently, the dashboard slowly goes stale. Add an `/api/health` check that surfaces last-tick-age per thread.

## Files

| File | Purpose |
|---|---|
| `server.py` | Flask app + consensus engine + 3 background threads + admin endpoints |
| `backtest.py` | Standalone — replays historical forecasts vs observed temps from Open-Meteo archive, computes PnL/Sharpe by edge threshold |
| `data.db` / `history.db` | SQLite stores (gitignored, auto-created) |
| `static/` | PWA assets — manifest, service worker, dashboard HTML/JS/CSS |

## Common gotchas

- **`DEV_MODE=1`** bypasses gateway auth for local dev. **Never set this on the production server.**
- **METAR fetching** depends on station ICAOs being current. Stations decommission occasionally — handle the 404.
- **Open-Meteo archive** has a ~1-day lag for "yesterday's observed temps." Don't pair against today's forecasts.
- **Multi-URL fallback for ENSO/teleconnections** — NOAA URLs change. The fallback list is non-trivial; don't simplify.
- **NWP ensemble member counts** vary by model. The weighting is sensitive — check the math before adjusting weights.
- **Service worker caching** — bumping the SW version invalidates installed PWA caches; users may see stale UI on first reload after a deploy.

## Conventions

- All HTTP via `requests` (Flask habit) — not httpx. Don't mix.
- Threading lock around DB writes (multiple threads write to `data.db`). Don't remove the lock.
- Gzip is mandatory for `/api/markets` — the payload is genuinely large.
