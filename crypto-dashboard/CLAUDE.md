# Crypto Dashboard — Claude notes

BTC/ETH/SOL/DOGE/XRP signal dashboard + ML ensemble + CLOB trader.
FastAPI on port **8000**, behind the gateway at `crypto.narve.ai`.

## Files that matter

- `server.py` — FastAPI app, REST + WebSocket, also serves the iOS app
  endpoints. Adds rate limiting, CORS, security middleware.
- `btc_analyzer.py` — multi-asset 5-min window analyzer; pulls 1-second
  klines from Binance and generates the dashboard HTML.
- `ml_predictor.py` — LSTM + PyTorch FFN + GBT ensemble. **Imported by
  `polymarket-bot/polymarket_bot.py`** — see coupling rule below.
- `clob_trading.py` — Polymarket CLOB orders. Credentials are Fernet-encrypted
  at rest. Don't log decrypted creds; don't add a "read raw key" helper.
- `database.py` — `cryptoedge.db`, WAL mode, threading lock. Never disable
  the lock for "speed" — concurrent writers will corrupt it.
- `news_trade_scanner.py` — runs every 20 min from `server.py`. If you
  change the cadence or the scanner shape, check `server.py`'s scheduler.

## The coupling rule

`polymarket-bot/polymarket_bot.py` imports `ml_predictor` from this
directory. **Any change to `ml_predictor.py`'s public surface
(class names, method signatures, return shapes) can break the bot.**
After such a change, also run the bot:

```bash
cd ../polymarket-bot && python3 polymarket_bot.py --dry-run
```

If you change the DB schema in `database.py`, the bot may also touch
`cryptoedge.db` — grep `polymarket-bot/` for the table name first.

## Verifying changes

```bash
cd crypto-dashboard && python3 server.py
# http://localhost:8000
```

For ML / scanner changes, **don't trust import-time success** — run a full
request cycle: hit `/`, hit the prediction endpoints, watch logs for
exceptions in the background scanners (news, suspicious-trades).

## Don't

- Don't add a frontend build tool. The dashboard is static HTML
  (`crypto_dashboard.html`, `btc_dashboard.html`, `progress.html`).
- Don't commit `cryptoedge.db`, `cache/`, `.env`, or anything under
  `data/`. They're gitignored — keep them that way.
- Don't edit copies under repo-root `workdir/`. Those are stale scratch
  files; the live code is here.
- Don't disable rate limiting / CORS / security middleware in `server.py`
  even for "quick testing." Run with the middleware on.
