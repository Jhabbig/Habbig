# Polymarket Weather Bot

Standalone trading bot for Polymarket weather markets. Runs on a 15-minute
loop: fetches active weather markets, parses each title for city/date/temp
threshold, pulls a forecast from Open-Meteo (GFS ensemble), computes
probability vs market price using a Gaussian model, and trades when edge
exceeds the configured threshold.

Has **no Dockerfile** — deployed via systemd in production
(`deploy/narve-weather.service`). Pairs with `polymarket_weather_dashboard/`
for the UI.

## Run locally

```bash
cd polymarket_weather_bot
cp .env.example .env       # PRIVATE_KEY required for live mode
pip install -r requirements.txt
python3 main.py
```

Stays in paper mode by default (`PAPER_MODE=true`). Set `PAPER_MODE=false`
only after you've verified signals on a few real markets.

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `main.py` | Entry point. Sets up rotating-file logging, instantiates `Config` / `RiskManager` / `TradingClient` / `DataStore`, runs the 15-minute scan loop. |
| `config.py` | Loads environment into a typed `Config` dataclass with validation. |
| `gamma_client.py` | Polymarket Gamma API — fetches active weather markets and parses titles into city/date/threshold. |
| `weather_client.py` | Open-Meteo GFS-ensemble forecast fetcher. |
| `city_stations.py` | City → weather station mapping (used to pick the right Open-Meteo grid point). |
| `edge_calculator.py` | Gaussian model: P(temp > threshold) given forecast distribution. Returns a `Signal` with edge vs market price. |
| `risk_manager.py` | Bankroll-aware position sizing — daily loss limit, Kelly fraction, max position pct, min liquidity. |
| `clob_client.py` | Polymarket CLOB trading client. Paper mode logs to stdout; live mode signs orders with `PRIVATE_KEY`. |
| `datastore.py` | SQLite trade log (`DB_PATH`, default `trades.db`). |
| `dashboard.py` | Per-run summary + daily report printers — stdout only, used by `main.py` after each scan. |

**Build / config**
| File | Purpose |
|---|---|
| `requirements.txt` | Python deps (`aiohttp`, `web3`, etc.). |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

> **No Dockerfile by design.** The bot is deployed via systemd in production (`deploy/narve-weather.service`). For local dev, run `python3 main.py` directly inside a venv.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PRIVATE_KEY` | empty | Wallet private key. **Required for live trading. Treat as a secret.** |
| `POLYMARKET_API_KEY` | empty | Polymarket API key |
| `PAPER_MODE` | `true` | Set to `false` to enable live execution |
| `EDGE_THRESHOLD` | `0.08` | Minimum edge (probability - price) before trading |
| `BANKROLL` | `1000.0` | Starting bankroll for sizing |
| `MAX_POSITION_PCT` | `0.05` | Max fraction of bankroll per position |
| `DAILY_LOSS_LIMIT_PCT` | `0.10` | Stop trading for the day after this drawdown |
| `KELLY_FRACTION` | `0.15` | Fractional Kelly multiplier |
| `MIN_LIQUIDITY` | `500.0` | Skip markets thinner than this |
| `MAX_FORECAST_HOURS` | `48` | Don't trade markets resolving beyond this horizon |
| `RUN_INTERVAL_MINUTES` | `15` | Scan loop interval |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DB_PATH` | `trades.db` | SQLite trade store path |

## Logs

Rotating file handler at `weather_bot.log` (10MB × 5 backups) plus stdout.
