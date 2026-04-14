# Weather Dashboard — Polymarket Weather Markets UI

Flask backend + PWA frontend for the Polymarket & Kalshi weather temperature
markets. Mobile-installable (manifest + service worker), gzipped JSON payloads,
and an admin panel.

Port: **5050**. Lives behind the gateway at `weather.narve.ai` in production.

## Key features

- **Multi-model ensemble consensus** — fetches 8 NWP ensembles (GFS, ECMWF IFS,
  ICON, GEM, UKMO, JMA, Meteofrance ARPEGE, KNMI) plus NWS deterministic and
  climatology, weights by member count, applies per-model bias correction and
  lead-time sigma inflation, and computes YES/NO probabilities via Gaussian CDF.
- **Intraday running-max tracker** — polls METAR every 5 min, tracks daily high
  per ICAO station, flags markets as BREACHED/SAFE/AT_RISK before resolution.
- **Cross-market correlations** — 8 defined storm-track corridors across US
  cities, detects upstream synoptic features and propagates alerts downstream.
- **ENSO + teleconnections** — fetches ONI, AO, NAO indices from NOAA with
  multi-URL fallback. Used for seasonal bias context.
- **Forecast drift sparkline** — snapshots consensus every 30 min, shows trend.
- **Coastal flow indicator** — classifies onshore/offshore wind for coastal
  cities.
- **NWS synoptic parsing** — extracts fronts, troughs, ridges from NWS narrative.
- **Persistence + analog baselines** — model-free forecasts for calibration.
- **Backtest** — `backtest.py` replays historical forecasts against observed
  outcomes, computes PnL/Sharpe by edge threshold.

## Run locally

```bash
cd polymarket_weather_dashboard
cp .env.example .env
pip install -r requirements.txt
python3 server.py
# http://localhost:5050
```

Or via Docker from the repo root:

```bash
docker compose up --build weather
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `server.py` | Flask app (~4000 lines) — REST endpoints, multi-model consensus engine, bias correction, intraday METAR polling, cross-market correlations, ENSO/teleconnections, gzip middleware, gateway SSO, admin endpoints. Runs 3 background threads (snapshot, bias-pairing, intraday poll). |
| `backtest.py` | Standalone backtest — reads `weather_price_snapshots` from `data.db`, fetches observed temps from Open-Meteo archive, resolves each market, computes PnL at various edge thresholds. |

**Data stores (gitignored, auto-created)**
| File | Purpose |
|---|---|
| `data.db` | Current weather-market state (live snapshots, edge calculations, intraday max, forecast history, bias pairs). |
| `history.db` | Historical signals and trade outcomes. |
| `backtest_results.json` | Generated output from `backtest.py`. |

**Static / PWA**
| File | Purpose |
|---|---|
| `static/` | PWA assets — see `static/README.md` for full breakdown. |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `weather` service. |
| `.dockerignore` | Excludes `*.db`, `*.log` from the Docker build context. |
| `.gitignore` | Local-only ignores on top of the root `.gitignore`. |
| `requirements.txt` | Python deps (Flask, scipy, requests). |
| `deploy.sh` | Custom deploy hook for this dashboard. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `FLASK_SECRET` | random per-restart | Session signing key. Set in production so sessions persist. |
| `FLASK_DEBUG` | `0` | Set to `1` for Flask debug mode (auto-reload, debugger). Never in prod. |
| `PRODUCTION` | `0` | Set to `1` in production. Used by the WSGI runner branch. |
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth for local dev. |

## Architecture

The server runs 3 background threads:
1. **Snapshot loop** — polls Polymarket/Kalshi APIs every 30 min, stores price
   snapshots and enriches with model probabilities.
2. **Bias-pairing loop** — every 6 h, fetches yesterday's observed temps from
   Open-Meteo archive, pairs against prior forecasts to compute per-model bias.
3. **Intraday poll loop** — every 5 min, fetches METAR for all tracked stations,
   updates the running daily high.

## Notes

- Gzip middleware cuts the 3.5 MB market JSON to ~500 KB over the wire.
- Pairs with the standalone `polymarket_weather_bot/` which writes the data this
  dashboard reads.
