# StockSignal — Stock Market Dashboard

Polymarket prediction dashboard for stocks. Notion/Wispr-style UI, ML-based
signals, optional bot that logs trades to `stock_trades.json`.

Port: **8050**. Lives behind the gateway at `markets.narve.ai` in production.

## Run locally

```bash
cd stock-dashboard
cp .env.example .env       # fill in keys (optional in DEV_MODE=1)
pip install -r requirements.txt
python3 stock_dashboard.py --port 8050
# http://localhost:8050
```

Or via Docker from the repo root:

```bash
docker compose up --build stock
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `stock_dashboard.py` | Main HTTP server (stdlib `http.server`). Serves the dashboard HTML and JSON endpoints, enforces gateway SSO via `GATEWAY_SSO_SECRET`, supports `--port` CLI arg. |
| `stock_predictor_bot.py` | Background bot — scans Polymarket stock markets and logs predictions to `stock_trades.json`. |
| `stock_ml_model.py` | Base ML model. Loads HMAC-signed pickles using `PICKLE_HMAC_SECRET` so tampered files are rejected. |
| `advanced_model.py` | Extended feature engineering layered on top of `stock_ml_model`. |
| `enhanced_data.py` | Data ingestion / normalization helpers used by the predictor bot. |
| `sentiment_signals.py` | News and social-sentiment feature extraction. |
| `smart_betting.py` | Bet sizing logic — translates a model edge into a position size. |

**HTML / data**
| File | Purpose |
|---|---|
| `stock_trades.json` | Trade log written by `stock_predictor_bot.py`, read by `stock_dashboard.py`. |
| `stock_trades_backup.json` | Atomic-rename backup of `stock_trades.json` from the previous write. |
| `stock_bot_activity.log` | Bot activity log. |
| `ml_models/` | Empty placeholder for cached ML model pickles. |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `stock` service. |
| `.dockerignore` | Excludes `*.json`, `*.log`, `*.db` from the Docker build context. |
| `requirements.txt` | Python deps. |
| `start.sh` | Helper script that launches the dashboard with sensible defaults. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth for local dev. |
| `PICKLE_HMAC_SECRET` | `stock-model-default` | HMAC key for pickled ML models. Override in production. |

## Notes

- When neither `GATEWAY_SSO_SECRET` nor `DEV_MODE=1` is set, the server rejects every request as a safety default.
- Bot activity goes to `stock_bot_activity.log`.
