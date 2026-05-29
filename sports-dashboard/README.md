# Sports Dashboard — Sharpe

Multi-venue +EV finder for sports markets. Joins bookmaker consensus
(via The Odds API) against Polymarket and Kalshi prices on the same
markets, applies de-vigged + sharp-consensus + liquidity + staleness
gates, and surfaces the surviving signals through a live web
dashboard, alerting fan-out (Telegram, webhook, browser push), and a
public proof-of-edge page.

Port: **8888**. Lives behind the gateway at `sports.narve.ai` in
production.

> **Start here when exploring**: open **`/features`** — the
> command-center index of every page and API in the product.

## Run locally

```bash
cd sports-dashboard
cp .env.example .env       # at minimum ODDS_API_KEY + GATEWAY_SSO_SECRET (or DEV_MODE=1)
pip install -r requirements.txt
python3 sports_dashboard.py
# http://localhost:8888
```

Or via Docker from the repo root:

```bash
docker compose up --build sports
```

## Tests

```bash
pip install -r requirements-dev.txt
DEV_MODE=1 python -m pytest tests/   # 308 tests, runs in <5s
```

CI runs the same command on every PR to `main` via
`.github/workflows/ci.yml` (job: `test-sports-dashboard`).

## Pages

| Page | Purpose | Auth |
|---|---|---|
| `/` | Live signal feed for the active sport | Required |
| `/features` | Command-center index of every surface | Public |
| `/track-record` | CLV / simulated P&L / calibration of all flagged signals | Public |
| `/leaderboard` | Opt-in roster of users ranked by mean closing line value | Public |
| `/player-props` | Cross-venue player-prop EV (book × Kalshi × Polymarket) | Required |
| `/cross-book-arbitrage` | Book-vs-book low-hold + middles | Required |
| `/smart-money` | Top-50 Polymarket whale positions on the active sport | Required |
| `/poly-fills` | Real-time tape of large Polymarket fills | Required |
| `/steam-moves` | Sharp-book line moves (Δ ≥ 2pp within 30 min by default) | Required |
| `/trades` | Bet tracker with per-trade CLV + per-sport/book stats + CSV export | Required |
| `/backtest` | Replay an arbitrary alert rule against resolved history | Required |
| `/settings`, `/admin`, `/users` | Existing dashboard settings / admin / user-directory pages | Required |

## API surface

### Public (anonymous-readable)
- `GET /api/track-record/{clv,pnl,calibration}` — aggregated edge metrics
- `GET /api/leaderboard/clv` — opt-in user CLV leaderboard

### Live data
- `GET /api/data` — current comparison feed (live prices via PM WS overlay)
- `WebSocket /ws` — real-time updates pushed when a signal flips
- `GET /api/sports`, `POST /api/sport/{sport_key}` — list / switch active sport
- `GET /api/orderbook/{token_id}` — Polymarket order-book depth
- `GET /api/h2h`, `/api/h2h-stats` — historical head-to-head ESPN data
- `GET /api/scores` — score feed for the active sport

### Cross-venue + props
- `GET /api/player-props/cross-venue?sport=` — book × Kalshi × Polymarket prop join
- `GET /api/kalshi/player-props?sport=` — Kalshi-only fallback
- `GET /api/cross-book-arbitrage?sport=` — low-hold + middles
- `GET /api/smart-money?sport=` — top-trader positions overlay
- `GET /api/poly-fills?min_usd=&side=&limit=` — recent large Polymarket fills (live)
- `GET /api/steam-moves?sport=&hours=&min_delta_pp=&window_min=` — sharp-book line moves
- `GET /api/closing-lines?sport=&days=` — sharp closing line per event/outcome

### Bet tracking + bankroll (T4.3 + T4.4 + T4.6)
- `GET/POST /api/trades`, `POST /api/trades/{id}/resolve`, `DELETE /api/trades/{id}` — bet CRUD
- `GET /api/trades/stats` — aggregate + per-sport/book/market_type breakdown
- `GET /api/trades/csv` — full bet history export
- `GET/PUT /api/bankroll` — bankroll config (starting, Kelly fraction, max-per-bet, drawdown alert)
- `POST /api/bankroll/suggest-stake` — Kelly-adjusted stake suggestion
- `POST /api/backtest/replay` — replay a rule against resolved history

### Alert routing
- `GET/POST /api/alert-rules`, `PATCH/DELETE /api/alert-rules/{id}` — structured alert rules
- `GET/POST/DELETE /api/watchlist`, `PATCH /api/watchlist/{id}` — per-market watchlist with thresholds
- `GET/POST/DELETE /api/webhooks/signing-key` — HMAC signing key rotate/revoke
- `POST /api/webhooks/test` — fire a signed test payload

### Push + automation
- `GET /api/push/vapid-public-key`, `POST/DELETE /api/push/subscribe`, `POST /api/push/test` — Web Push
- `GET/POST/DELETE /api/auth/tokens`, `DELETE /api/auth/tokens/{id}` — Bearer tokens
- Authenticate programmatic requests via `Authorization: Bearer <token>`

### AI
- `POST /api/signals/explain` — Claude-generated plain-English explanation
  (prompt-cached system + 30-min DB cache per signal)

### Public leaderboard
- `GET /api/leaderboard/clv` — anonymous-readable CLV ranking
- `GET/PUT/DELETE /api/leaderboard/optin` — join / leave (auth required)

### Operational
- `GET /metrics` — Prometheus scrape (25+ metrics, no auth — bind to private network)
- `GET /api/diagnostics/match-rejects` — admin: near-reject log
- `GET /api/diagnostics/odds-quota` — admin: Odds API quota state
- `GET /healthz` — liveness probe

## Environment variables

### Core
| Variable | Default | Effect |
|---|---|---|
| `ODDS_API_KEY` | empty | The Odds API key. Free tier 500 req/mo — **player props need a paid tier** (per-event endpoint). |
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API base URL |
| `DIVERGENCE_THRESHOLD` | `5` | Default % point threshold before a signal fires |
| `POLL_INTERVAL` | `300` | Base seconds between scans (adaptive: shorter for live games, longer when Odds API quota is low) |
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway |
| `DEV_MODE` | unset | `1` bypasses gateway auth for local dev |
| `CLOUDFLARE_ORIGIN` | unset | Allowed Cloudflare Access origin |
| `HOST` | `0.0.0.0` | uvicorn bind address |
| `PORT` | `8888` | uvicorn bind port |

### Polymarket WebSocket + fills tape
| Variable | Default | Effect |
|---|---|---|
| `PM_FILL_MIN_USD` | `1000` | Minimum fill USD to capture into the tape buffer |

### AI explanations (Claude)
| Variable | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Set to enable `/api/signals/explain`. Without it the endpoint 503s. |
| `EXPLAIN_MODEL` | `claude-opus-4-7` | Model used for explanations |
| `EXPLAIN_CACHE_TTL_SECONDS` | `1800` | DB cache TTL per signal identity |

### Web Push (VAPID)
| Variable | Default | Effect |
|---|---|---|
| `VAPID_PUBLIC_KEY` | empty | Required for push delivery; subscription storage works without it |
| `VAPID_PRIVATE_KEY` | empty | Required for signing pushes |
| `VAPID_SUBJECT` | `mailto:admin@narve.ai` | Contact URL in VAPID claims |

### Leaderboard
| Variable | Default | Effect |
|---|---|---|
| `LEADERBOARD_MIN_TRADES` | `10` | Minimum resolved trades to qualify for the public CLV leaderboard |

## Architecture overview

**Polling + WebSocket hybrid.** Adaptive per-sport polling: 15s when a
game is <30 min from kickoff, 60s within 4h, 5 min today, 30 min idle.
Quota-aware floor stretches the loop to 30 min when Odds API budget
runs low. A persistent Polymarket WebSocket subscription overlays
live prices on top of poll snapshots so the dashboard never lags by
more than ~2s.

**Signal quality gates.** Every flagged signal must pass:
1. De-vigged sharp consensus exceeds threshold
2. At least one sharp book (Pinnacle / Circa / BetCRIS / Betfair Ex)
3. Polymarket volume ≥ $1000 and spread ≤ 5pp
4. Market has traded recently (not stale)

**Cache strategy.**
- Prompt caching on Claude system prompt → ~10% the input cost on repeat
- DB cache on AI explanations (30-min TTL per signal identity)
- 10-min cache on per-event Odds API prop fetches
- 5-min cache on Polymarket Gamma fetches
- 6-h cache on FRED-style historical data (none currently used here)

**Storage.** SQLite (`data.db`) for everything; WAL mode. Schemas
created idempotently on startup. Migrations run for column additions.
~15 tables: profiles, signals, edge history, snapshots, trades,
watchlists, alert rules + config, scores, team history/info, player
info, top-trader positions, push subs, signal explanations, bankroll,
leaderboard opt-ins, API tokens.

## Files in this directory

| File | Purpose |
|---|---|
| `sports_dashboard.py` | Main server — ~7000 lines of polling/matching/serving |
| `sharpe_pitch.py` | Offline Sharpe analysis (drives the investor pitch deck) |
| `templates/*.html` | All page templates loaded via `_load_template()` |
| `static/` | PWA manifest, service worker, icons |
| `tests/` | Pytest suite, ~280 tests, <5s wall |
| `data.db` | SQLite store (gitignored) |
| `sharpe.db` | Sharpe-analysis history (gitignored) |
| `.secret_key` | Fernet key for encrypted Telegram tokens (gitignored, auto-generated) |
| `Dockerfile`, `requirements*.txt`, `.env.example` | Build + config |

## Operational notes

- **First production deploy** should smoke-test outbound to Anthropic,
  Polymarket WS, Kalshi, and The Odds API — sandbox builds couldn't
  validate those endpoints.
- **`/metrics` is unauthenticated** — bind to a private network or front
  with Cloudflare Access.
- **PWA icons** in `static/` are placeholders (favicon reused as 192/512).
  Replace with proper icons before public launch.
- **Older `sports_edge_history` rows** without `commence_time` will be
  rejected by backtest rules that filter on time-to-event. Backfill
  via `sports_scores` if you want to replay long history.
- **Player props need a paid Odds API tier** — the per-event endpoint
  burns through the free 500 req/month quota fast.
