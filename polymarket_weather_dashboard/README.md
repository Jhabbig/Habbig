# Weather Dashboard — Polymarket Weather Markets UI

Flask backend + PWA frontend for the Polymarket weather markets bot. Mobile-
installable (manifest + service worker), gzipped JSON payloads, and an admin
panel.

Port: **5050**. Lives behind the gateway at `weather.narve.ai` in production.

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
| `server.py` | Flask app — REST endpoints, gzip middleware (cuts 3.5MB JSON to ~500KB), gateway SSO check, admin endpoints. |

**Data stores (gitignored, auto-created)**
| File | Purpose |
|---|---|
| `data.db` | Current weather-market state (live snapshots, edge calculations). |
| `history.db` | Historical signals and trade outcomes. |

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

## Notes

- gzip middleware cuts the 3.5MB market JSON to ~500KB over the wire.
- Pairs with the standalone `polymarket_weather_bot/` which writes the data this dashboard reads.
