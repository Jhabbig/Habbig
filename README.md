# Polymarket Dashboard Suite — narve.ai

A monorepo of prediction-market dashboards and trading bots, all unified
behind a single auth/billing gateway. Each dashboard runs as its own service
on its own port. The gateway proxies subdomains, handles SSO, and gates
access by per-dashboard subscription.

See `CONTRIBUTING.md` for the contributor quick-start (Docker + manual).

## Layout

| Directory | Port | What it is |
|---|---|---|
| `gateway/` | 7000 | Central auth + reverse proxy. The single entry point. |
| `crypto-dashboard/` | 8000 | BTC/crypto signals + ML ensemble (CryptoEdge). |
| `stock-dashboard/` | 8050 | Stock market signal dashboard (StockSignal). |
| `midterm-dashboard/` | 8051 | US midterm election predictions (FastAPI + React). |
| `top-traders-dashboard/` | 8052 | Polymarket whale tracking + insider detection. |
| `polymarket_weather_dashboard/` | 5050 | Weather-market dashboard UI (Flask + PWA). |
| `sports-dashboard/` | 8888 | Sports arbitrage signals (The Odds API vs Polymarket). |
| `world-state-dashboard/` | 7050 | Geopolitical feed + infrastructure map. |
| `Dashboard-x-truth-research-prediction/` | 18789 | X / TruthSocial prediction-mining dashboard. |
| `voter-pulse-dashboard/` | 7062 | Voter Pulse — mood gauge, life indicators, polling aggregates, per-administration table, election backtest, state tile-map, political markets. |
| `polymarket_weather_bot/` | — | Headless weather-market trading bot (no UI). |
| `polymarket-bot/` | — | 5-minute up/down trading bot (single file, tightly coupled to crypto-dashboard). |
| `deploy/` | — | Systemd unit files for the Ubuntu production box. |
| `workdir/` | — | Scratch directory (mostly duplicate copies of crypto-dashboard files). |

Each directory has its own `README.md` with a per-file breakdown.

## Files in this directory

**Build / orchestration**
| File | Purpose |
|---|---|
| `docker-compose.yml` | Multi-service stack (Redis, gateway, all 8 dashboards). One command brings the whole suite up. |
| `start_dashboards.sh` | Manual launcher (no Docker). Boots each dashboard with PID files and `/tmp/dashboard_*.log` logs. Subcommands: `start`, `stop`, `restart`, `status`. |
| `deploy.sh` | Rsync deploy from this Mac to the Ubuntu production box. Supports per-site selection and automatic snapshot before sync. |
| `snapshot.sh` | Local backup/restore — `tar.gz` per site with safe `sqlite3 .backup` for live DBs. Index lives in `.snapshots/index.txt`. |
| `.dockerignore` | Top-level Docker build exclusions (also overridden per-dashboard). |
| `.gitignore` | Project-wide ignores: secrets, DBs, logs, Python/Node artifacts, OS cruft. The Python `lib/` rule is unignored for `midterm-dashboard/frontend/src/lib/` — without that explicit unignore, the entire frontend `src/lib/` directory (api.js, settings.jsx, currency.js) is silently swallowed. If you add a frontend `src/lib/` to another dashboard, add it to the unignore list too. |
| `.env.example` | Reference of every env key across every service. Each service also has its own `.env.example`. |
| `ruff.toml` | Linting config — 200-char lines, F821 (undefined names) only. |

**Documentation**
| File | Purpose |
|---|---|
| `README.md` | This file. |
| `CONTRIBUTING.md` | Setup, port table, branch workflow, code style, env-var guidance. |

## Common workflows

```bash
# Bring everything up via Docker
docker compose up --build

# Start one dashboard manually
cd crypto-dashboard && python3 server.py

# Snapshot before deploy
./snapshot.sh save all "before-cleanup"
./deploy.sh

# Lint
ruff check gateway/ crypto-dashboard/ stock-dashboard/
```
