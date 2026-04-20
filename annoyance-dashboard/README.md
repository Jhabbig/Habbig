# Annoyance Dashboard (MVP)

7th narve.ai dashboard. Detects spikes in public frustration about specific
entities (companies, people, products) *before* mainstream news catches up,
so users can bet the related prediction markets ahead of the repricing.

**This is a local-only MVP.** Runs on `localhost:8053`. No gateway
integration, no deploy. Reddit is the only live source; Bluesky, HN, GDELT,
YouTube are planned but not built.

## Why this complements the happiness index

- **Happiness** = slow, structural baseline mood → a trend line
- **Annoyance** = fast, event-driven spikes → red dots on the trend with
  attribution ("+47% at 14:23, top entity: United Airlines, cause: EWR ground
  stop")

Happiness answers "how are we doing?" Annoyance answers "what just broke?"
Together they form the full vibes picture. Users of narve.ai's other
dashboards click spikes → get routed to related markets (CEO turnover, stock
drops, product recalls) → bet before news cycle catches up.

## Layout

```
annoyance-dashboard/
├── server.py           FastAPI app, lifespan-managed background loops
├── config.py           env vars, port, sub list, alias dict
├── db.py               sync sqlite3 helpers
├── classifier.py       Claude batch classifier + spike summarizer
├── aggregator.py       hourly index + entity counts + alias canonicalization
├── spike_detector.py   MAD-based composite-signal anomaly detector
├── sources/
│   ├── base.py         SourceBase ABC + RawPost contract
│   └── reddit.py       /new.json polling across config.REDDIT_SUBS
├── static/
│   ├── index.html
│   ├── annoyance.css
│   └── annoyance.js    Chart.js, polls /api/* every 60s
├── seed_test_data.py   CLI to populate fake data for UI dev
└── annoyance.db        SQLite (created on first boot)
```

## Running locally

```bash
cd /Users/shocakarel/Habbig/annoyance-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env → set ANTHROPIC_API_KEY if you want real classification
python server.py
```

Open `http://localhost:8053/`. First boot shows "Collecting data…". Wait
~10 minutes for the Reddit loop to fetch + classifier to process, or run:

```bash
# skip the wait for UI dev
python seed_test_data.py --posts 200 --hours 48
```

Then reload the dashboard.

## Manual triggers (dev)

```bash
curl -X POST "localhost:8053/admin/trigger?loop=reddit"
curl -X POST "localhost:8053/admin/trigger?loop=classifier"
curl -X POST "localhost:8053/admin/trigger?loop=aggregator"
curl -X POST "localhost:8053/admin/trigger?loop=spike_detector"

# reset N most recent posts back to classified=0 for prompt iteration:
curl -X POST "localhost:8053/admin/reclassify?limit=100"
```

All admin routes are localhost-gated (IP-check on `127.0.0.1`).

## API

| Route | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/index?hours=24` | Time series of hourly annoyance index |
| `GET /api/spikes?limit=20` | Recent detected entity spikes (hydrated with sample posts) |
| `GET /api/entities/top?limit=20` | Top entities for the current hour |
| `GET /api/entity/{name}` | Per-entity history (last 168 hours) |
| `GET /api/sources` | Source health (backs the pill in the topbar) |
| `GET /healthz` | Liveness |
| `POST /admin/trigger?loop=…` | Manually run one background loop |
| `POST /admin/reclassify?limit=…` | Reset N posts to classified=0 |

## Background loops

Started in FastAPI lifespan via `asyncio.create_task`:

| Task | Interval | What it does |
|---|---|---|
| `reddit_loop` | 600s | Poll `/r/{sub}/new.json` across config.REDDIT_SUBS, dedup via PK |
| `classifier_loop` | 300s | Drain up to 20 unclassified posts via Claude, persist scores |
| `aggregator_loop` | 900s | Rebuild current + previous hour's index + entity counts |
| `spike_detector_loop` | 900s (offset 30s) | MAD + composite signal → fire spikes |

All loops `try/except` wrap their work. Transient errors log and continue;
loops never die.

## Spike detector logic

- Composite signal = `count * (avg_annoyance / 50)` — rewards high-anger even
  at low volume, penalizes lukewarm spam
- MAD (median absolute deviation), not stddev — robust to viral outliers
- Baseline = same hour-of-week, not flat 7×24 — respects weekly seasonality
- Three gates to fire: `z >= 3` AND `multiple >= 3` AND `count >= 5`
- Cold start: during first 48 hours of history for an entity, fall back to
  absolute threshold `count >= 10 AND avg_annoyance >= 70`
- Dedup on `UNIQUE(entity, detected_hour)` — the 15-min loop can't re-emit

## Classifier hardening

- Fail-soft on no `ANTHROPIC_API_KEY` — dashboard still boots, classifier
  no-ops and logs
- Invalid JSON from Claude → one retry at temperature=0 with "previous output
  was invalid" suffix → still invalid → whole batch marked `classified=2`
  (poisoned) to prevent infinite loops
- Length mismatch (N posts in, M < N responses) → match by `id`, not index.
  Missing posts stay `classified=0` for next batch.
- **Hallucination gate**: drop any entity whose name isn't literally a
  substring of the post content (case-insensitive). Catches ~80% of
  made-up entities cheaply.
- Cost cap via `MAX_POSTS_PER_HOUR` in config.py
- Model id stored per classification row so you can re-run after model upgrades

## To push to the server later

1. Add a 7th entry to `/Users/shocakarel/Habbig/gateway/config.json`:
   ```json
   "annoyance": {
     "subdomain": "annoyance",
     "target": 8053,
     "display_name": "Annoyance Index",
     "description": "Leading-indicator vibes: detect frustration spikes before the news",
     "accent": "#ff4d4f",
     "monthly_cents": 1499,
     "annual_cents": 14900,
     "supports_websocket": false
   }
   ```
2. `scp` the directory (minus `.venv`, `annoyance.db`) to the server
3. Add `ANTHROPIC_API_KEY` to server env
4. `nohup python3 server.py > /tmp/annoyance.log 2>&1 &`
5. **Commit on server** (required per gateway deploy process)
6. DNS: `annoyance.narve.ai` → Cloudflare Tunnel → port 8053

## Launch checklist — email notifications

Email notifications (decision #6) ship behind a master kill switch plus an
optional allowlist. **Three-stage rollout** prevents a bad first deploy
from spamming every Pro subscriber:

| Stage          | `EMAIL_NOTIFICATIONS_ENABLED` | `EMAIL_NOTIFICATIONS_ALLOWLIST`     | What happens                                                                                         |
|----------------|-------------------------------|-------------------------------------|------------------------------------------------------------------------------------------------------|
| **Pre-release**| `false`                       | *(ignored)*                         | Notifier exits immediately. No gateway-DB read, no SMTP call, no ledger rows. Default for staging.   |
| **Soak test**  | `true`                        | `shocakarel@gmail.com`              | Full code path runs; recipients filtered down to the allowlist. Run ~48h before opening up.          |
| **Launch day** | `true`                        | *(empty)*                           | Fires to every matching Pro subscriber. Per-user 5/day cap still applies as defence-in-depth.        |

Related env vars (all fail-soft — missing values degrade to zero
recipients or log-and-skip, never crash the detector loop):

- `GATEWAY_AUTH_DB` — absolute path to `~/Habbig/gateway/auth.db` (or the
  staging equivalent). Notifier opens it read-only.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` — outgoing SMTP
  credentials. Set `EMAIL_DRY_RUN=1` during dev to log sends without
  actually connecting.
- `EMAIL_FROM`, `EMAIL_FROM_NAME` — sender address / display name.
- `EMAIL_UNSUBSCRIBE_URL` — defaults to `https://narve.ai/profile#email-preferences`.

Verify before promoting:

```bash
# Pre-release → staging soak: confirm the flag gates all sends.
EMAIL_NOTIFICATIONS_ENABLED=false python -c "
import asyncio, notifications
asyncio.run(notifications.send_spike_email(
    spike_id=1, entity='Test', summary='x',
    confidence=60.0, entity_url='https://x/'))
"
# Expect: "notifications: disabled by flag; skipping spike_id=1"

# Soak test: allowlist limits delivery to your own inbox.
EMAIL_NOTIFICATIONS_ENABLED=true \
EMAIL_NOTIFICATIONS_ALLOWLIST=shocakarel@gmail.com \
EMAIL_DRY_RUN=1 \
python -c "import asyncio, notifications; asyncio.run(notifications.send_spike_email(
    spike_id=1, entity='Test', summary='x',
    confidence=60.0, entity_url='https://x/'))"
# Expect: exactly one recipient (yours) in the log.
```

## Roadmap after MVP proves the loop

- Add Bluesky, HackerNews, GDELT, YouTube source modules (HTTP APIs, stub
  them into `sources/`)
- Multi-source corroboration gate on spikes (2-of-N required to fire)
- Backtest framework: score historical events (United dragging, CrowdStrike,
  SVB, Bud Light) → publish hit rate + lead time table
- Link entities to existing narve.ai markets (one click from spike card to
  related market)
- User-level P&L tracker: show "you've made $X on annoyance-flagged trades"
- Happiness index integration (share the fetcher pipeline)
