# Weather Dashboard — Claude notes

Flask + PWA on port **5050**, behind the gateway at `weather.narve.ai`.
Multi-model NWP ensemble + intraday METAR tracker + cross-market correlations.

## Files that matter

- `server.py` — Flask app (~4000 lines). REST endpoints, consensus engine,
  bias correction, intraday METAR polling, ENSO/teleconnections, gzip
  middleware, gateway SSO, admin endpoints. **Runs 3 background threads on
  startup** (snapshot, bias-pairing, intraday poll). Any change near
  thread start/stop, signal handlers, or `app.run()` needs a full restart
  to verify, not just a reload.
- `backtest.py` — standalone replay; reads `weather_price_snapshots` from
  `data.db`, fetches Open-Meteo archive, computes PnL by edge threshold.
  Run with `python3 backtest.py` after meaningful forecast-engine changes.

## Data stores (gitignored, auto-created)

- `data.db` — live state: snapshots, edges, intraday max, forecast history,
  bias pairs. **Schema-changing edits need a migration path** — there are
  rows on the production server. If you add a column, default it or write
  an idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` block.
- `history.db` — historical signals + trade outcomes. Same migration rule.
- `backtest_results.json` — output of `backtest.py`. The one checked into
  git is a sample; don't overwrite from local runs unless explicitly asked.

## Gotchas

- **PWA = service worker + manifest.** If you change anything served from
  `static/` that the SW caches, bump the SW version string or installed
  clients will keep the stale cache forever. Grep `static/` for the version
  constant before touching cached assets.
- **Gzip middleware** is in `server.py`. If you add a new endpoint returning
  large JSON, confirm it actually gzips (response header `Content-Encoding`).
- **8 NWP ensembles + climatology + persistence/analog baselines** — each
  has its own fetch path with per-model bias correction and sigma inflation.
  Don't generalize "just use the new model X for everything" — the weighting
  is by ensemble member count and tuned per source.
- **METAR polling every 5 min** is the basis for the intraday running-max
  tracker. Don't shorten the interval without checking NOAA's rate limits;
  don't lengthen it without updating the BREACHED/AT_RISK thresholds that
  assume 5-min granularity.

## Verifying changes

```bash
cd polymarket_weather_dashboard && python3 server.py
# http://localhost:5050
```

After forecast-engine changes, also run `python3 backtest.py` and eyeball
the PnL/Sharpe shift vs the previous run. A large unexplained drop usually
means a sign error or a model-weight bug, not a real improvement.

## Don't

- Don't commit `data.db`, `history.db`, `.env`, or anything under `cache/`.
- Don't add a JS build tool — the PWA is hand-written static + service worker.
- Don't change the `weather` `key` in `gateway/config.json`. Subdomain /
  display name / price are fine; `key` is the foreign key in `subscriptions`.
