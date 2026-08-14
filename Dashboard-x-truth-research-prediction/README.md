# Polymarket Prediction Intelligence Dashboard

Scrapes X (Twitter) and TruthSocial for prediction-related posts, cross-references against live Polymarket and Kalshi odds, scores each prediction by best-side EV and source credibility, opens a $1 paper-trade for every tradeable signal, settles the ledger when markets resolve, and (optionally) DMs you on Telegram.

Subdomain when running behind the narve.ai gateway: `truth.narve.ai` (port 18789).

## What it does

- **Scrapes four sources** every 5 min: X (Twitter), TruthSocial, **Reddit** (configurable subreddits via `config.yaml`), and **RSS / Substack** (any feeds you list). Per-user API keys (Profile → API Keys) are used in addition to env-var keys.
- **Two-stage prediction extraction**: precise regex first (free, fast), then Claude (LLM fallback) for natural-language predictions the regex misses. Results are cached by content hash so repeat posts cost nothing. Defaults to `claude-opus-4-7`; swap to `claude-haiku-4-5` via `LLM_EXTRACTOR_MODEL` for ~5× lower cost.
- **Matches predictions to markets** on Polymarket *and* Kalshi via Jaccard token-overlap (≥3 shared tokens, strict category gating). For **multi-outcome events** (Trump vs Harris vs RFK in "2028 Election Winner"), the matcher additionally requires the candidate name to appear in the prediction text — so a "Trump will win" post can't be misrouted to the Harris market even though the question stems are 90% identical.
- **Scores credibility** per source — Bayesian-smoothed accuracy, decay-weighted by half-life, category-spread + dominance penalties, manual trust override, **plus Brier-score calibration** for probability-bearing predictions (lower is better; surfaced as a colored column on the Leaderboard).
- **Picks the better side**: for every prediction with a matched market, computes the EV of buying YES vs buying NO at the live price, surfaces the higher-EV side as a `BUY YES` / `BUY NO` signal.
- **Opens a paper-trade** ($1 stake) every time the system fires a signal that clears the EV + credibility filter. Settles when the market resolves. Running P&L visible in the Performance tab.
- **Backtest harness** with tunable thresholds (min EV / min credibility / stake) — replays every resolved prediction, returns total P&L, ROI, **annualised Sharpe**, **max drawdown**, per-category breakdown, and a **cumulative daily P&L curve** chart. The "would I trust this with real money?" interface.
- **Liquidity-aware EV** — `/markets/<slug>/liquidity?side=YES` walks the Polymarket CLOB order book and returns volume-weighted execution prices + slippage in bps at $100 / $1k / $10k stake sizes. Reveals the "this edge dies past $X" reality that midpoint EV hides.
- **Cross-venue arbitrage** — Arbitrage tab matches Polymarket vs Kalshi markets (≥0.6 Jaccard on de-templated tokens, same category, close times within 14 days) and surfaces YES-price spreads ≥3pp. Deep-link buttons take you to both venues to execute the legs.
- **Fast price stream** — every 60s the scheduler polls Polymarket for markets with open paper trades, updates snapshots, and fires a Telegram drift alert if a market moves ≥10pp against an open bet.
- **Public JSON API** — `/api/v1/signals`, `/api/v1/sources`, `/api/v1/backtest`, `/api/v1/arbitrage`. Authenticate with `X-API-Key: narve_...`; generate/revoke keys from `/profile`.
- **Telegram query bot** — once you've connected a bot in Profile, `/edge @handle`, `/source @handle`, and `/stats` return live data in chat. Polled every 15s.
- **User calibration mode** — record your own predictions at `/me/calibration`, build a Brier-scored track record over time. Same scoring methodology as the source leaderboard.
- **Telegram alerts** on each new signal (per-user opt-in, bot token Fernet-encrypted at rest).
- **DB-backed sessions** so a process restart no longer logs everyone out.

## Quick Start

```bash
cd Dashboard-x-truth-research-prediction
cp .env.example .env   # edit with your credentials
docker-compose up --build
# Open http://127.0.0.1:18789
# Login: admin / changeme
```

## Without Docker

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 18789
```

## macOS Desktop App

```bash
pip install -r requirements.txt
bash scripts/build_dmg.sh
# Output: PolymarketDashboard-1.0.0-arm64.dmg
```

Requires: macOS 13+, Apple Silicon, `brew install create-dmg`

## Tests

```bash
pytest app/tests/ -v
```

## Configuration

Edit `app/config.yaml` for keywords, credibility weights, risk thresholds, quota limits.
Edit `.env` for API credentials and auth.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Auth required" JSON | Clear cookies, go to /login |
| No predictions | Add API creds to .env, click Refresh |
| Markets not matching | Lower market_match_threshold in config.yaml |
| X quota exhausted | Resets 1st of month |
| "App is damaged" (macOS) | Right-click -> Open |

## Files in this directory

**Application code**
| File / dir | Purpose |
|---|---|
| `app/` | Python application package — FastAPI app, scraping pipeline, scoring, desktop wrapper. See `app/README.md`. |
| `scripts/` | Build/release helpers — DMG builder, icon generator. See `scripts/README.md`. |

**Config / data**
| File | Purpose |
|---|---|
| `polymarket.spec` | PyInstaller spec file. Used by `scripts/build_dmg.sh` to bundle the desktop app. |
| `predictions.db` (+ `-shm`, `-wal`) | Main SQLite DB. Stores raw posts, predictions, sources, market snapshots, credibility history. |
| `.encryption_key` | App-managed encryption key for sensitive fields. Auto-generated, never commit. |

**Docker / build**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `truth-research` service. |
| `docker-compose.yml` | **Standalone** compose file (separate from the root one). Brings up just this dashboard. |
| `.dockerignore` | Excludes `*.db`, `.env`, `dist/`, etc. from the Docker build context. |
| `requirements.txt` | Python deps (FastAPI, SQLModel, APScheduler, httpx, tweepy, etc.). |
| `.env` / `.env.example` | Per-service env vars. Copy `.env.example` to `.env` to use. |
| `README.md` | This file. |
