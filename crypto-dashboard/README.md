# CryptoEdge — Crypto Signals & ML

BTC/ETH/SOL/DOGE/XRP signal dashboard. Pulls 5-minute kline data from Binance,
runs an ensemble ML predictor, surfaces divergences against Polymarket and
Kalshi, and (optionally) executes trades via the Polymarket CLOB.

Port: **8000**. Lives behind the gateway at `crypto.narve.ai` in production.

## Run locally

```bash
cd crypto-dashboard
cp .env.example .env       # fill in keys
pip install -r requirements.txt
python3 server.py
# http://localhost:8000
```

Or via Docker from the repo root:

```bash
docker compose up --build crypto
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `server.py` | FastAPI app — REST + WebSocket, serves the dashboard and powers the iOS app. Adds rate limiting, CORS, security middleware. |
| `btc_analyzer.py` | Multi-asset 5-min window analyzer. Fetches 1-second klines from Binance for BTC/ETH/SOL/DOGE/XRP, splits into windows, trains ensembles, generates the dashboard HTML. |
| `ml_predictor.py` | Multi-coin ensemble ML predictor (LSTM + PyTorch FFN + Gradient-Boosted Trees) trained on raw 1-second tick data. Imported by `server.py` and by `polymarket-bot/polymarket_bot.py`. |
| `database.py` | SQLite layer (`cryptoedge.db`) for predictions, watchlists, alerts, accuracy, Kalshi markets. WAL-mode, threading lock. |
| `clob_trading.py` | Polymarket CLOB integration. Read-only via REST, signed orders via `py-clob-client`. Credentials encrypted at rest with Fernet. |
| `kalshi_scanner.py` | Fetches Kalshi event markets via the public trade-api/v2 endpoint and caches them in `cache/` for the dashboard to read. |
| `suspicious_trades.py` | Polymarket suspicious-trades scanner — ranks bets by potential profit (size × inverse odds), not raw size. |
| `news_trade_scanner.py` | Trades-first / news-second correlation scanner. Looks at flagged suspicious trades, scans breaking news for matching topics, scores correlations. Runs every 20 min from `server.py`. |
| `trading_bot.py` | Standalone reactive paper-trading bot. Monitors 5-minute windows for cross events, confirms with velocity/momentum/RSI/choppiness, runs entry/exit logic with daily loss limits. |
| `email_alerts.py` | SMTP alert sender for high-confidence signals. SMTP creds come from `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` env vars. |

**HTML / data**
| File | Purpose |
|---|---|
| `crypto_dashboard.html` | Generated dashboard HTML — written by `btc_analyzer.generate_dashboard()`, served by `server.py`. |
| `btc_dashboard.html` | Legacy/standalone single-asset BTC dashboard. |
| `progress.html` | Static progress page shown while the analyzer is warming up. |
| `mining_regulations_data.json` | Hand-curated dataset of crypto mining regulations by country, served as a static feed. |
| `cache/` | Subdirectory of cached klines, ensemble model JSONs, and rolling suspicious-trades snapshots (see `cache/README.md`). |

**Data stores (gitignored, auto-created)**
| File | Purpose |
|---|---|
| `cryptoedge.db` | Main SQLite DB. Created on first run by `database.py`. |
| `sharpe.db` | Sharpe-ratio tracking DB shared with `sports-dashboard/sharpe.db`. |
| `bot_output.log` / `server_output.log` | Log output from `trading_bot.py` and `server.py`. |
| `.secret_key` | Auto-generated Fernet key for encrypting trading credentials. Never commit. |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `crypto` service (referenced by `docker-compose.yml`). |
| `.dockerignore` | Excludes `cache/`, `*.db`, logs, etc. from the Docker build context. |
| `requirements.txt` | Python deps for the dashboard process. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API base URL |
| `DIVERGENCE_THRESHOLD` | `10` | Percentage points before a signal is flagged |
| `SPORT_KEY` | `soccer_epl` | Used by the cross-market scanner |
| `POLL_INTERVAL` | `300` | Seconds between scans |

See `.env.example` for the full list.

## Notes

- `polymarket-bot/polymarket_bot.py` imports `ml_predictor` from this package — don't move it without updating that import path.
- Models in `cache/` are HMAC-signed. Set `PICKLE_HMAC_SECRET` consistently across deploys or saved models will be rejected on load.
