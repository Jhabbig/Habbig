# Election Night runbook

Steps to make sure `/live` survives the Nov 3, 2026 spike.

## 1. Environment

```bash
# Required for live race calls (else markets-only mode)
export LIVE_NIGHT_MODE=1
export LIVE_ELECTION_DATE=2026-11-03
export LIVE_POLL_INTERVAL_SEC=30

# Pick one provider (or both — they're surfaced as separate calls
# so disagreements between providers are themselves a feature)
export AP_API_KEY=...        # contract via https://developer.ap.org
export DDHQ_API_KEY=...      # contract via https://decisiondeskhq.com

# Already-required for full functionality:
export ANTHROPIC_API_KEY=...    # movement explanations
export SENTRY_DSN=...           # error tracking
export SMTP_HOST=... SMTP_FROM=... SMTP_USER=... SMTP_PASSWORD=...
export VAPID_PRIVATE_KEY=... VAPID_PUBLIC_KEY=... VAPID_SUBJECT=...
export PUBLIC_BASE_URL=https://midterm.narve.ai
```

## 2. Process model

The app is a single asyncio process. The in-process caches (`TTLCache`)
assume one worker — multiple uvicorn workers each have their own cache
and would multiply DB load by the worker count. Run single-worker and
scale horizontally via a load balancer if needed.

```bash
# Single worker, behind a reverse proxy
uvicorn main:app \
  --host 0.0.0.0 --port 8051 \
  --workers 1 \
  --proxy-headers --forwarded-allow-ips='*'
```

If a single worker isn't enough on election night: keep the worker count
low (2–4) and either (a) accept the cache duplication — 5s cache TTL means
worst case is each worker doing the same DB scan every 5s, or (b) move
the `TTLCache` behind Redis (the `get_or_compute` interface is unchanged).

## 3. Hot-path performance budget

Measured locally on `/data/live/dashboard` with `loadtest_live.py`:

| Scenario | p50 | p95 | p99 | Notes |
|---|---:|---:|---:|---|
| 50-client cold burst (post-warm) | 47ms | 48ms | 48ms | 5s in-process cache absorbs the spike |
| Single-client steady poll | 1.7ms | 2.4ms | 2.4ms | **87% of responses 304 Not Modified** via ETag |

SLO: p95 < 2s for the live dashboard. Current p95 is ~50× under budget.

## 4. Rate limiter

In-memory per-identity at 60 RPM (free), 120 RPM (premium), unlimited
(admin). For election night either:
- Trust the reverse proxy to handle DDoS upstream and raise the free tier
  to 600 RPM, OR
- Issue premium API keys to high-volume integrators (Slack/Discord
  webhook receivers, journalists' scrapers, etc.)

## 5. Pre-flight checklist (24h before)

- [ ] Stand up a staging environment with `LIVE_NIGHT_MODE=1` against the
  staging AP/DDHQ keys (if they have a test endpoint)
- [ ] Run `python3 loadtest_live.py --base $STAGING --clients 200 --interval 1 --duration 60` —
  expect zero errors, p95 < 500ms
- [ ] Manually call a few test races via `POST /admin/race-call` and
  confirm the `/live` page renders the disagreement badges correctly
- [ ] Make sure CDN (if any) doesn't cache `/data/live/*` — those endpoints
  must hit origin so the 5s TTL stays accurate
- [ ] Run `python3 test_live.py` against the staging deploy as a smoke test
- [ ] Confirm Sentry is receiving events
- [ ] Subscribe to the alert digest yourself so you'll see if the worker stops

## 6. During the night

- Watch logs for `Race-call poller: N calls upserted` — that's the heartbeat
- Watch `last_error` on outbound webhooks (in the admin UI) — broken Slack
  endpoints don't need to be fixed during the night, but you'll want to
  know which ones to retry tomorrow
- If a provider misfires, retract a call with
  `DELETE /admin/race-call/{race_key}/{provider}`
- If the in-process cache feels stale: it isn't, the TTL is 5s. If it
  REALLY feels stale, restart the worker — that flushes the cache.

## 7. After the night

- Convert the night's calls into accuracy-backtest rows by appending to
  `backend/accuracy_backfill.py`. Every cycle the dataset grows, the
  defensibility moat grows with it.
- Snapshot the database before retiring the 2026 markets so the historical
  prices remain queryable for future backtests.
