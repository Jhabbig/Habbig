# Top Traders Dashboard — Claude notes

FastAPI on port **8052**, behind the gateway at `traders.narve.ai`.
Whale tracking + suspicious-trade scanners + Bayesian wallet skill model.

## Files that matter

- `server.py` — FastAPI app, leaderboard polling, trade streaming, 20-second
  in-memory cache, gateway SSO middleware. Single-file UI served from `/`
  (reads `index.html`).
- `suspicious_trades.py` — multi-signal scanner: potential profit,
  timing-before-close, volume spikes, first-trade wallets, coordinated
  wallets, statistical outliers, new-account + long-shot patterns. **Adding
  a new signal**: keep it as a separate function returning a score in
  `[0, 1]`, don't fold it into an existing one.
- `resolved_markets.py` — retroactive insider detection. Looks at closed
  markets and finds repeat winners on long-shot outcomes.
- `bayesian_wallets.py` — Beta(α, β) skill priors per wallet, persisted to
  `bayesian_wallets.db`. **Don't reset priors** unless explicitly asked;
  they accumulate over time and resetting throws away signal.
- `wallet_ml.py` — Isolation Forest + XGBoost ranker. **Imports of sklearn
  / xgboost are guarded** — the module degrades gracefully if missing.
  Keep that pattern; don't move imports to the top level.

## Data sources

Public, **unauthenticated** Polymarket endpoints:
- `https://lb-api.polymarket.com/volume?window=<all|1d|7d|30d>&limit=N`
- `https://data-api.polymarket.com/trades?user=<wallet>&limit=N`

No API keys here. If you add a new data source that needs auth, route it
through `kalshi_client.py` / `kalshi_creds.py` (those handle Kalshi auth)
rather than introducing a second creds pattern.

## Gotchas

- **20-second cache is in-memory.** A restart wipes it — that's fine, just
  don't add code that assumes cache survival across restarts.
- **`index.html` is the whole frontend.** Single-file, no build step.
  Don't introduce a bundler.
- **`bayesian_wallets.db` is read AND written.** If you take a long-running
  write lock in `bayesian_wallets.py`, the live leaderboard endpoints stall.
  Keep writes short or batched.
- **`DEV_MODE=1`** skips gateway SSO checks. Useful locally; **never** ship
  a code path that defaults it on.

## Verifying changes

```bash
cd top-traders-dashboard && python3 server.py
# http://localhost:8052
```

For scanner / ML changes, hit the leaderboard endpoint, then the suspicious
endpoints, then watch logs for graceful-degrade messages — the sklearn/xgboost
fallback paths only trigger if those libs are missing, but their absence
shouldn't break the page.

## Don't

- Don't commit `bayesian_wallets.db`, `cache/`, `.env`. All gitignored.
- Don't introduce authenticated Polymarket APIs without first checking
  whether the public endpoints already cover the use case — they usually do.
- Don't change the `top_traders` `key` in `gateway/config.json` (its
  subdomain is `traders`, the internal key is `top_traders` — the mismatch
  is intentional).
