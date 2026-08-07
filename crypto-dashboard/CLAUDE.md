# crypto-dashboard/ — CryptoEdge

BTC/ETH/SOL/DOGE/XRP signal dashboard. Port **8000**, subdomain `crypto.narve.ai`. Pulls 1-second klines from Binance, runs an ensemble ML predictor (LSTM + PyTorch FFN + Gradient-Boosted Trees), surfaces edges against Polymarket and Kalshi, optionally trades via the Polymarket CLOB. See [README.md](README.md) for full file breakdown.

## Don't touch unless you mean to

- **`ml_predictor.py`** is imported by `polymarket-bot/polymarket_bot.py` — moving it or renaming the API breaks that bot. Update both if you change the import surface.
- **`PICKLE_HMAC_SECRET`** must stay consistent across deploys, or saved models in `cache/` get rejected on load (HMAC mismatch).
- **`.secret_key`** is a Fernet key for trading credentials at rest. **Never commit.** Already in `.gitignore`. Losing it = re-enter all CLOB creds.
- **`cryptoedge.db`** is the source of truth for predictions/watchlists/alerts. WAL mode + threading lock — don't open it from another process while server is running.

## Stack

- FastAPI + uvicorn (REST + WebSocket — powers iOS app)
- SQLite (`cryptoedge.db` for app data, `data.db` for tick storage)
- PyTorch + LSTM, plus gradient-boosted trees, plus py-clob-client for trading
- Rate limiting + CORS + security middleware via `server.py`

## Files that matter most

| File | Purpose |
|---|---|
| `server.py` | FastAPI app, REST + WebSocket, schedules `news_trade_scanner` every 20 min |
| `btc_analyzer.py` | Multi-asset 5-min window analyzer; trains ensembles, generates dashboard HTML |
| `ml_predictor.py` | Multi-coin ensemble (LSTM + FFN + GBT). Imported by polymarket-bot |
| `database.py` | SQLite layer for `cryptoedge.db` |
| `clob_trading.py` | Polymarket CLOB integration. Read-only via REST; signed orders via py-clob-client |
| `kalshi_scanner.py` | Kalshi event-markets fetcher; cached in `cache/` |
| `suspicious_trades.py` | Polymarket large-trade scanner — ranks by profit (size × inverse odds), not raw size |
| `news_trade_scanner.py` | Trades-first / news-second correlation scanner |
| `trading_bot.py` | Standalone reactive paper-trading bot (5-min window cross events + RSI/momentum) |
| `email_alerts.py` | SMTP alerts; needs `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` |

## Conventions

- All Polymarket Gamma/CLOB calls **should go through `gateway/polymarket_client.py`** — but legacy code in this dashboard still rolls its own. New code: use the gateway client.
- ML model artifacts live in `cache/`, signed with HMAC. Don't bypass signing.
- The dashboard HTML (`crypto_dashboard.html`) is **generated** by `btc_analyzer.generate_dashboard()` and served by `server.py`. Don't hand-edit the generated file — edit the generator.
- Long warm-up: ML ensembles take ~90s to load on startup. Docker healthcheck `start_period: 90s` reflects this.

## Common gotchas

- **Cold start is slow.** ~90s before `/healthz` returns 200. Don't kill the process during boot.
- **`bot_output.log` and `server_output.log` grow unbounded.** Rotate manually if running long-term locally.
- **`sharpe.db` is shared with `sports-dashboard/`** — both write to the same file. If you change schema in one place, change both.
- **WebSocket route** — listed in `gateway/config.json` with `supports_websocket: true`. Don't break it without updating the gateway proxy logic.
- **Trading is real money** when CLOB creds are wired in. The default is read-only (no creds = no orders). Don't hardcode credentials anywhere.
