# Sports Dashboard — Polymarket vs Bookmaker Odds

Compares bookmaker odds (via The Odds API) against Polymarket market prices to
spot mispriced markets. Signals only — no auto-execution.

Port: **8888**. Lives behind the gateway at `sports.narve.ai` in production.

## Run locally

```bash
cd sports-dashboard
cp .env.example .env       # ODDS_API_KEY required
pip install -r requirements.txt
python3 sports_dashboard.py
# http://localhost:8888
```

Or via Docker from the repo root:

```bash
docker compose up --build sports
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `sports_dashboard.py` | Main server — serves the dashboard, polls The Odds API + Polymarket, computes divergences, runs the gateway SSO middleware, encrypts Telegram tokens via Fernet. |
| `arbitrage_bot.py` | Standalone arbitrage scanner (logs only, no execution). Uses the same env config as the dashboard. |
| `sharpe_pitch.py` | Sharpe-ratio analysis on historical signals — drives the numbers in `Sharpe_Pitch.pptx`. |

**Data / assets**
| File | Purpose |
|---|---|
| `data.db` | SQLite store for signals, divergences, and Telegram alert subscriptions. |
| `sharpe.db` | Historical performance data for the Sharpe-ratio computation. Shared with `crypto-dashboard/sharpe.db` (same schema). |
| `Sharpe_Pitch.pptx` | Investor/partner pitch deck. |
| `.secret_key` | Fernet key for encrypting Telegram tokens. **Auto-generated on first run, never commit.** |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `sports` service. |
| `.dockerignore` | Excludes `*.db`, `*.pptx`, `.secret_key` from the Docker build context. |
| `requirements.txt` | Python deps. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ODDS_API_KEY` | empty | The Odds API key — required for signals. Free tier at https://the-odds-api.com |
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API base URL |
| `SPORT_KEY` | `soccer_epl` | Sport key for The Odds API |
| `DIVERGENCE_THRESHOLD` | `5` | Percentage points before a signal is flagged |
| `POLL_INTERVAL` | `300` | Seconds between scans |
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth for local dev |
| `CLOUDFLARE_ORIGIN` | unset | Allowed Cloudflare Access origin (only if fronting with cf-access) |
