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
| `long_term.py` | Long-horizon lens: daily bars + CoinMetrics on-chain metrics, cycle-phase classifier (Mayer / 200WMA), Sharpe/Sortino/drawdown/vol-regime, MVRV/NVT proxies, cycle-aware DCA recommender, drift-band rebalance plan, risk-off composite. Served at `/long-term`. Refreshed every 6 h by a background task in `server.py`. |
| `indicators.py` | Cycle-indicator math: Pi Cycle Top, 200WMA distance, Stock-to-Flow, NUPL, SOPR proxy, Puell Multiple, Hash Ribbons, Exchange Net-Flow, RHODL proxy, BTC dominance, ETH/BTC. Each returns a uniform `{value, signal, threshold, description, source}` shape. |
| `derivatives.py` | Binance USDT-M Futures fetcher — funding rate, open interest, perp basis. Persists time-series in `crypto_derivatives_series`. Computes funding composite (BTC+ETH weighted), OI trend signal. Refreshed hourly. |
| `macro.py` | Macro overlay — DXY, US10Y, VIX, M2, gold. Pulls FRED (needs free API key) + Stooq (no key for DXY/gold). Computes BTC correlation (90d), per-series crypto-tailwind/-headwind signal, and a composite macro regime. Refreshed every 12 h. |
| `backtest.py` | Walk-forward backtester for cycle indicators. At every historical date, recomputes each indicator with only data available then, measures 30/90/365d forward returns vs unconditional baseline. Persists to `crypto_indicator_backtests`. Re-run on every long-term refresh. |
| `exchanges.py` | Spot exchange adapters — Coinbase Advanced Trade (JWT/ES256) and Kraken (HMAC-SHA512). Common interface: `get_balances`, `get_price`, `place_limit_buy`, `place_market_buy`, `cancel_order`. Credentials stored Fernet-encrypted per user in `crypto_exchange_credentials`. |
| `execution.py` | Auto-execution engine. Reads `crypto_dca_schedule` and runs each due leg through a 7-step safety gauntlet (dry-run gate, per-order cap, daily cap, portfolio circuit breaker, asset whitelist, adapter check, price discovery). Places limit orders 0.5% below mid with 1h TTL + optional market fallback. Append-only log in `crypto_executions`. Dry-run is the default. |
| `tax.py` | Tax-optimal selling. Immutable lot ledger + disposition ledger with a join table tracking which acquisition lots funded which sale. Lot methods: FIFO / LIFO / HIFO (default) / LOFO / TAX_OPTIMAL. `preview_sell()` shows the hypothetical LT/ST split without persisting. `find_harvest_opportunities()` scans open lots for losses ≥ user threshold and ≥ N days old, flagging wash-sale risk. `export_form_8949()` emits Part-I/II CSV. Realised-P&L summary computes annual ST + LT + estimated tax + loss-carryforward. |
| `push.py` | Web Push notifications (VAPID, payload-less). VAPID P-256 keypair is generated once into `.vapid_key`; subsequent pushes are POSTed to the user's browser push endpoint with an ES256-signed JWT. Service worker (`/service-worker.js`) fetches the actual notification content from `/api/notifications/pending` and renders it locally — avoids the AES-GCM payload-encryption complexity. Hooked into long-term alerts and the executor so any threshold-cross or DCA placed/blocked event fires a push. |
| `strategy.py` | Strategy library + backtester + marketplace. A `Strategy` is a dataclass composing DCA cadence + cycle-aware multipliers + optional harvest + optional rebalance. `backtest()` walks daily bars forward, evaluates rules per day, tracks a virtual portfolio with HIFO sell allocation, and reports equity curve + Sharpe + Sortino + max-DD + trade count. `evaluate_today()` translates a strategy into a single tick's worth of buy decisions for the live-subscription ticker. Marketplace: public visibility puts a strategy on a Sharpe-vs-drawdown leaderboard; users can fork or subscribe. |
| Onboarding | 6-step wizard rendered as a full-screen overlay on first visit to `/long-term`: welcome → jurisdiction + lot method → exchange (or skip) → target weights → starter strategy (or skip) → push opt-in → done. State persists in `crypto_user_onboarding`; per-step side-effects upsert tax settings, target weights, and (optionally) a strategy subscription. |
| Live subscriptions | A user can subscribe to a public strategy. The `strategy_subscription_ticker` (5-min cadence in `server.py`) evaluates each due subscription's rules against today's market state and routes the resulting actions through `execution._evaluate_leg` — same safety gauntlet as manual DCA. Subscriptions are isolated from the user's manual DCA schedule (separate table) so they can't accidentally overwrite each other. Pause/resume/unsubscribe + "Run now" all exposed in the Strategies tab. |
| `billing.py` | Stripe-backed subscription tiers (free / pro / wealth). Direct REST calls (no `stripe` SDK dep). `create_checkout_session()` returns a Checkout URL; `create_billing_portal_session()` lets users self-serve upgrades/downgrades. Webhook handler at `/api/billing/webhook` verifies HMAC-SHA256 signatures, maps `checkout.session.completed` → upsert billing row, handles subscription updates + payment failures (drops user back to free on lapsed status). Feature gating via `feature_allowed(user_tier, feature)` consulted by gated endpoints (exchange-connect, live-execution, tax-harvest-execute, tax-form-8949, strategy-publish, extra-subscriptions, multi-exchange). Billing is optional — if `STRIPE_SECRET_KEY` is unset every user stays free and the pricing page shows disabled buttons. |
| `digest.py` | Weekly email digest. Hourly cron checks for users whose preferred day-of-week is today and who haven't received in the last 6 days. Content: portfolio + 7-day change, execution count, cycle-indicator composite per asset, harvest opportunity total, active strategy subscriptions. Inline-styled dark-theme HTML. Reuses `email_alerts.send_email()`; no-ops gracefully if SMTP not configured. User opt-in + day-of-week in `crypto_user_preferences`. |
| `news.py` | Real-time news aggregator across 10 RSS sources (CoinDesk / The Block / Decrypt / CoinTelegraph / SEC / CFTC / Fed / Treasury / ECB / BoE). Per item: regex-based entity extraction (5 tickers + 11 regulators + 12 entities), lexicon-based sentiment scoring with negation, keyword-bucket topic classifier (regulation / ETF / macro / market / tech / hack / adoption / stablecoin). Persists deduped items in `crypto_news_items`. Refreshes every 7 min. User alert rules (JSON-filter form) live in `crypto_news_alert_rules`; matches fire push notifications via `push.notify_user`. UNIQUE(rule_id, news_id) prevents duplicate fires. RSS parsing uses `defusedxml` to avoid XXE on potentially-compromised feeds. |

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
| `LONG_TERM_RF_RATE` | `0.04` | Risk-free rate used in Sharpe/Sortino on the `/long-term` page |
| `GLASSNODE_API_KEY` | _(unset)_ | Optional. If set, the long-term module will pull richer on-chain metrics from Glassnode in addition to the free CoinMetrics Community tier |

See `.env.example` for the full list.

## Notes

- `polymarket-bot/polymarket_bot.py` imports `ml_predictor` from this package — don't move it without updating that import path.
- Models in `cache/` are HMAC-signed. Set `PICKLE_HMAC_SECRET` consistently across deploys or saved models will be rejected on load.
