# Polymarket Dashboard Suite — narve.ai

A monorepo of prediction-market dashboards and trading bots, all unified
behind a single auth/billing gateway. Each dashboard runs as its own service
on its own port. The gateway proxies subdomains, handles SSO, and gates
access by per-dashboard subscription.

See `CONTRIBUTING.md` for the contributor quick-start (Docker + manual).

## Storefront

The **whole fleet is live**: every dashboard is listed on the storefront and
deployed — docker-compose, `start_dashboards.sh`, `deploy.sh`, and the
systemd units run all of them. Only the merged dashboards (Disasters and
Climate, absorbed into Weather) stay retired; their subdomains 301-redirect.
The admin **Fleet dashboard** (`/admin/fleet`) shows live health for every
service. Parking a dashboard again = add `"hidden": true` + `"parked": true`
in `gateway/config.json` and drop its service from deploy.

| Product | Services | What it is |
|---|---|---|
| **Truth Research** ★ flagship | `Dashboard-x-truth-research-prediction/` (18789) | The LLM prediction-extraction engine: regex + Claude two-stage extractor over X/TruthSocial/Reddit/RSS, EV-priced against Polymarket & Kalshi, credibility-scored, paper-trade-settled. Pitch: `Dashboard-x-truth-research-prediction/PITCH.md` and `/preview/truth`. |
| **Weather, Climate & Disasters** | `polymarket_weather_dashboard/` (5050) | Weather-market edges plus the merged-in Disasters and Climate tabs (`/api/disasters/*`, `/api/climate/*`). |
| **Midterm Predictor** | `midterm-dashboard/` (8051) | US election predictions aggregating 6 sources (FastAPI + React). |
| **Market Edge — Stocks & Crypto** | `crypto-dashboard/` (8000) + `stock-dashboard/` (8050) | One subscription, two apps: CryptoEdge ML signals and the StockSignal stock desk, cross-linked. |
| **Sharpe Sports** | `sports-dashboard/` (8888) | Multi-venue +EV finder: bookmaker consensus vs Polymarket/Kalshi. |
| **World State** | `world-state-dashboard/` (7050) | Geopolitical feed + infrastructure map (cables, pipelines, rare earths). |
| **Top Traders** | `top-traders-dashboard/` (8052) | Polymarket whale tracking + insider-pattern detection. |
| **Central Bank Tracker** | `centralbank-dashboard/` (7060) | Fed/ECB/BoE rates, implied path, Polymarket FOMC edge. |
| **AI Race** | `ai-race-dashboard/` (7070) | Frontier-model leaderboard + live AI prediction markets. |
| **Crypto Trackers** | `crypto-trackers-dashboard/` (7054) | Every-coin, multi-exchange, data-fidelity-first trackers. |
| **Religion & Cults Tracker** | `religion-dashboard/` (7062) | World religions, NRM watchlist, USCIRF designations, religion markets. |
| **Whale Watch** | `whale-dashboard/` (8053) | Institutional flow from 13F, Form 4, 13D filings. |
| **Voters** | `voters-dashboard/` (7051) | The state of voters worldwide — newly registered in the gateway. |

**Narve One** (`/one` on the gateway) consolidates the entire fleet into a
single tabbed dashboard: every live product renders fully inside same-origin
iframes served by the gateway's `/d/<key>/` path proxy, while merged
dashboards show status cards. No per-dashboard code changes were needed —
the tab rail is derived from `gateway/config.json`, and auth, subscriptions,
caching, SSE, and WebSockets all flow through the same gateway machinery as
the per-subdomain views. See `gateway/README.md`.

## Layout

| Directory | Port | What it is |
|---|---|---|
| `gateway/` | 7000 | Central auth + reverse proxy. The single entry point. |
| `crypto-dashboard/` | 8000 | Market Edge: BTC/crypto signals + ML ensemble (CryptoEdge). |
| `stock-dashboard/` | 8050 | Market Edge: StockSignal stock desk (one sub with crypto via `access_alias`). |
| `midterm-dashboard/` | 8051 | US midterm election predictions (FastAPI + React). |
| `top-traders-dashboard/` | 8052 | Polymarket whale tracking + insider detection. |
| `polymarket_weather_dashboard/` | 5050 | Weather, Climate & Disasters (Flask + PWA; disasters + climate merged in). |
| `sports-dashboard/` | 8888 | Sports arbitrage signals (The Odds API vs Polymarket). |
| `world-state-dashboard/` | 7050 | Geopolitical feed + infrastructure map. |
| `voters-dashboard/` | 7051 | The state of voters worldwide (newly registered in the gateway). |
| `centralbank-dashboard/` | 7060 | Fed/ECB/BoE rates, implied path, Polymarket FOMC edge. |
| `ai-race-dashboard/` | 7070 | Frontier-model leaderboard + live AI prediction markets. |
| `whale-dashboard/` | 8053 | Institutional whale flow — 13F, Form 4, 13D filings. |
| `disasters-dashboard/` | — | **Legacy** — merged into the weather service; subdomain redirects. |
| `climate-dashboard/` | — | **Legacy** — merged into the weather service; subdomain redirects. |
| `crypto-trackers-dashboard/` | 7054 | Every-coin trackers: multi-exchange spot+perps, cross-exchange arb, funding rates, DeFi TVL, F&G. |
| `religion-dashboard/` | 7062 | World religions, NRM/cult watchlist, USCIRF designations, Polymarket religion markets. |
| `Dashboard-x-truth-research-prediction/` | 18789 | **★ Flagship** — the LLM prediction-extraction engine (X/TruthSocial/Reddit/RSS → priced, scored signals). See its `PITCH.md`. |
| `polymarket_weather_bot/` | — | Headless weather-market trading bot (no UI). |
| `polymarket-bot/` | — | 5-minute up/down trading bot (single file, tightly coupled to crypto-dashboard). |
| `deploy/` | — | Systemd unit files for the Ubuntu production box. |
| `workdir/` | — | Scratch directory (mostly duplicate copies of crypto-dashboard files). |

Each directory has its own `README.md` with a per-file breakdown.

## Files in this directory

**Build / orchestration**
| File | Purpose |
|---|---|
| `docker-compose.yml` | Multi-service stack (Redis, gateway, the whole 14-dashboard fleet). One command brings the whole suite up. |
| `start_dashboards.sh` | Manual launcher (no Docker). Boots each dashboard with PID files and `/tmp/dashboard_*.log` logs. Subcommands: `start`, `stop`, `restart`, `status`. |
| `deploy.sh` | Rsync deploy from this Mac to the Ubuntu production box. Supports per-site selection and automatic snapshot before sync. |
| `snapshot.sh` | Local backup/restore — `tar.gz` per site with safe `sqlite3 .backup` for live DBs. Index lives in `.snapshots/index.txt`. |
| `.dockerignore` | Top-level Docker build exclusions (also overridden per-dashboard). |
| `.gitignore` | Project-wide ignores: secrets, DBs, logs, Python/Node artifacts, OS cruft. The Python `lib/` rule is unignored for `midterm-dashboard/frontend/src/lib/` — without that explicit unignore, the entire frontend `src/lib/` directory (api.js, settings.jsx, currency.js) is silently swallowed. If you add a frontend `src/lib/` to another dashboard, add it to the unignore list too. |
| `.env.example` | Reference of every env key across every service. Each service also has its own `.env.example`. |
| `bootstrap_data.py` | Data-readiness bootstrap (stdlib only) — probes all 28 upstream feeds, audits credentials in `gateway/.env.production`, checks every service port, and (with `--refresh`) pulls real prices into the matrix toolkit. Run after deploy. |
| `DATA_SOURCES.md` | The complete data map: every feed each dashboard consumes, which need credentials (only 4 signup keys gate real functionality), and how refresh works. |
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
