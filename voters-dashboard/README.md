# Voters Dashboard

Public-facing-but-gated dashboard showing the state of voters around the world:
who they are, what they want, what their next elections look like, and
(in later slices) the downstream chains from voter concerns to policy to
market impact.

## Status

**Slice 1 — Atlas.** Region grid, country drawers with demographics +
issue salience + democracy quality + election calendar. Threaded comments
and emoji reactions for any authenticated subscriber.

**Slice 2 — Polling + markets.** Curated polling time-series chart per
country (`data/polls.yaml`), and live cross-links to Polymarket and Kalshi
markets that match the country's election keywords (`data/election_keywords.yaml`).

**Slice 3 — Impact chains.** Curated chains in `data/impact_chains.yaml`
seed the DB on first boot. Subscribers can author new chains
(concern → actor → policy → market) as drafts; reviewers approve/reject/
request-changes from a `/api/reviewer/queue` panel.

**Slice 4 — Counter-chains.** Any approved chain can be countered via a
`refute` / `fork` / `extend` typed link. Counter-chains nest under their
parent in the country drawer.

## Architecture

- `server.py` — FastAPI app, SSO-gated via `GATEWAY_SSO_SECRET`. Mirrors
  the pattern used by `world-state-dashboard/` and `centralbank-dashboard/`.
- `data/countries.yaml` — single source of truth for the 25 curated
  countries. Hand-curated metadata + ETL-fillable fields.
- `data/sources/*.py` — ETL pulls (V-Dem, World Bank, IFES elections
  calendar, Wikipedia polling pages, Pew Global Attitudes). Each is
  idempotent and writes into `data/cache/`.
- `schema.sql` — SQLite schema for `thoughts`, `thought_flags`, and
  `audit_log` (user comments, reactions, moderation).
- `cross_dashboard.py` — fetches sibling-dashboard data (midterm,
  polymarket markets) for cross-linking on country pages.
- `index.html` + `static/` — MapLibre world map, country drawer UI,
  comment thread component.

## Auth model

- Gateway forwards `X-Gateway-User-Id`, `X-Gateway-User-Email`, and
  `X-Gateway-Secret` (HMAC-verified).
- **Subscriber**: any authenticated user. Can view, comment, react, flag,
  vote, draft impact chains, post counter-chains.
- **Reviewer**: email listed in `VOTERS_REVIEWER_EMAILS` (comma-sep).
  Can approve/reject/request-changes on chains; sees the review queue;
  can hide/unhide thoughts; sees the audit log.
- **Admin**: email listed in `VOTERS_ADMIN_EMAILS`. Reviewer + future
  policy controls.
- In `DEV_MODE` without an SSO secret, `dev@local` is auto-promoted to
  admin so the reviewer UI is visible during local work.
- 3 distinct flags on a thought auto-hide it pending reviewer action.
- Soft rate limits: 10 comments/hour/user, 30 flags/hour/user, 5 chain
  drafts/hour/user.

## Running locally (dev mode, no gateway)

```bash
cd voters-dashboard
DEV_MODE=1 python3 -m uvicorn server:app --port 7051 --reload
# open http://localhost:7051/
```

## Running in production

The dashboard expects to sit behind the gateway at `voters.narve.ai`,
which forwards requests with the SSO secret. See `start_dashboards.sh`
in the repo root and `docker-compose.yml`.

## ETL refresh

ETL scripts are designed to run as cron jobs on the host. Each writes
to `data/cache/*.json` and the server reads from there. They have safe
fallbacks: if the upstream source is down, the dashboard keeps serving
the last successful pull.

```bash
python3 data/sources/vdem_pull.py            # nightly
python3 data/sources/worldbank_pull.py       # nightly
python3 data/sources/elections_calendar.py   # daily
python3 data/sources/polling_aggregator.py   # 6-hourly
python3 data/sources/pew_loader.py           # manual on Pew release
```

Recommended host crontab (`crontab -e`):

```cron
# Voters dashboard ETL — adjust paths to match your install
ROOT=/srv/polymarket/voters-dashboard
PY=/srv/polymarket/venv/bin/python3
0 3 * * *     cd $ROOT && $PY data/sources/vdem_pull.py        >> /var/log/voters_etl.log 2>&1
30 3 * * *    cd $ROOT && $PY data/sources/worldbank_pull.py   >> /var/log/voters_etl.log 2>&1
0 4 * * *     cd $ROOT && $PY data/sources/elections_calendar.py >> /var/log/voters_etl.log 2>&1
0 */6 * * *   cd $ROOT && $PY data/sources/polling_aggregator.py >> /var/log/voters_etl.log 2>&1
```

Live data (polymarket + kalshi prediction markets) is fetched in-process
on demand with a 5-min TTL — no cron needed for those.

## Provider notes

- **Polymarket** (gamma API) — public reads include question, slug, YES
  outcome price, 24h volume, end date. Used as the primary price source.
- **Kalshi** (`/events` + `/markets`) — public reads return question,
  ticker, category, but **not** bid/ask/last/volume; those require an
  authenticated session. We surface the question + click-through to the
  Kalshi market page; the UI hides empty price/volume slots.
- Both providers cached at 5-min TTL with 6-second per-request timeout.
  Negative cache 60s on failure so transient outages recover quickly.

## Outstanding setup items

- **Stripe price IDs** in `gateway/config.json` are still placeholders
  (`TODO_VOTERS_STRIPE_MONTHLY` / `_ANNUAL`). Create the recurring prices
  in the Stripe dashboard at $5.99/mo and $59/yr, then drop the IDs in.
  Until then the gateway will refuse paid signups for this dashboard.
- **Reviewer emails**: set `VOTERS_REVIEWER_EMAILS` and
  `VOTERS_ADMIN_EMAILS` in the dashboard's environment (or the
  docker-compose service block) so the right people can approve chains.
- **Impact-chain seed**: 10 chains seed automatically on first boot from
  `data/impact_chains.yaml` (idempotent — re-runs do nothing). To add
  more curated seeds later, append to the YAML and run
  `DELETE FROM impact_chains WHERE source_kind='seed'` then restart.
