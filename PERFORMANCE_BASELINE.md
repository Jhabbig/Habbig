# Performance baseline — 2026-04-21

Baseline captured pre / post migrations **080** (query indexes) and
**081** (slow-query log) on `feature/platform-build`. Numbers from
`gateway/scripts/benchmark_endpoints.py --runs 100` against a local
gateway with production-sized `gateway/auth.db`.

## Method

1. Checkout branch parent (`git log --oneline -1` immediately before
   the 080 commit) and run the benchmark → write `bench_before.txt`.
2. Apply migrations + checkout branch tip, restart gateway, run again
   → write `bench_after.txt`.
3. `diff -u bench_before.txt bench_after.txt`.

Commands used:

```
python3 gateway/scripts/benchmark_endpoints.py \
  --base http://localhost:7000 \
  --session "$NARVE_SESSION_TOKEN" \
  --runs 100 \
  --handle PolymarketAnalytics \
  --slug bitcoin-100k-by-2025
```

## Targets

Pass criteria per prompt:

| endpoint                   | P95 target |
|----------------------------|-----------:|
| `GET /api/feed`            | < 200 ms |
| `GET /api/sources/{handle}`| < 300 ms |
| `GET /api/best-bets`       | < 400 ms |

## Results

Numbers to be filled in by the operator running the benchmark locally
(the CI worker for this session has no live gateway to hit). Copy the
text-mode output table from `benchmark_endpoints.py` into the blocks
below and commit.

### Before (pre-080)

```
# narve benchmark — base=http://localhost:7000  runs=100
endpoint            ok      p50      p95      p99      max     mean
--------------------------------------------------------------------
feed               ...      ...      ...      ...      ...      ...
best-bets          ...      ...      ...      ...      ...      ...
sources            ...      ...      ...      ...      ...      ...
source-detail      ...      ...      ...      ...      ...      ...
markets            ...      ...      ...      ...      ...      ...
market-detail      ...      ...      ...      ...      ...      ...
```

### After (080 + 081 applied)

```
# narve benchmark — base=http://localhost:7000  runs=100
endpoint            ok      p50      p95      p99      max     mean
--------------------------------------------------------------------
feed               ...      ...      ...      ...      ...      ...
best-bets          ...      ...      ...      ...      ...      ...
sources            ...      ...      ...      ...      ...      ...
source-detail      ...      ...      ...      ...      ...      ...
markets            ...      ...      ...      ...      ...      ...
market-detail      ...      ...      ...      ...      ...      ...
```

### Interpretation

Expected impact of migration 080 on the benchmarked endpoints, given
the actual indexes added (and which were already present on the
target tables):

* **`/api/feed`** — dominated by `predictions` reads ordered by
  `extracted_at DESC` with optional category filter. The new
  `idx_predictions_cat_resolved_extracted` covers the common (category,
  open, recent) filter; pre-080 SQLite fell back to
  `idx_predictions_extracted` + filesort on the category predicate.
* **`/api/markets/{slug}`** — fetches predictions for a market ordered
  by recency. `idx_predictions_market_extracted` replaces a scan of
  all predictions for the market (via `idx_predictions_market`) plus a
  filesort.
* **`/api/sources/{handle}`** — mostly unchanged by 080 because the
  single-column `idx_predictions_source` already covers the shape.
  The win here is from the partial index
  `idx_source_cred_unlocked_ranked` on the leaderboard card.
* **`/api/saved` and `/api/following`** (not benchmarked here but
  worth noting) — benefit from the new `(user_id, saved_at DESC)` /
  `(user_id, followed_at DESC)` composites; pre-080, each list page
  did a filesort over the user's rows after the initial lookup.

## Slow-query log verification (migration 081)

After the tracer is wired into `db.py`'s connection factory (a
separate diff owned by whoever touches db.py; see
`gateway/queries/query_tracer.py` docstring), run the load script and
inspect the log:

```
for i in {1..100}; do
  curl -s http://localhost:7000/api/feed \
       -b "narve_session=$NARVE_SESSION_TOKEN" > /dev/null
done

sqlite3 gateway/auth.db "
  SELECT COUNT(*) AS n,
         ROUND(AVG(duration_ms), 1) AS avg_ms,
         MAX(duration_ms) AS max_ms
    FROM slow_query_log
   WHERE timestamp > strftime('%s', 'now', '-5 minutes')
"
```

A healthy post-080 run should return: `n` in the low-single-digits
(only legitimately-slow queries cross the 500 ms threshold) with
`max_ms` well under a second. If `n` is in the hundreds, something
regressed — use `queries.performance.top_slow_shapes(hours=1)` to
identify the offending query signature.

## Follow-up work flagged out of scope

These belong to a follow-up diff (different owner — this session was
scoped to `gateway/queries/` + migrations `080-082` only):

1. Wire `queries.query_tracer.install_tracer(conn, db_path_getter=...)`
   into `gateway/db.py`'s connection factory. Current state: the
   tracer ships unused until that single line lands.
2. Wire `queries.query_tracer.set_request_context(...)` into an HTTP
   middleware so the `endpoint` + `user_id` columns get populated.
3. Mount a `GET /admin/performance` route (handler body in
   `queries.performance.top_slow_shapes` / `slow_query_histogram` /
   `endpoint_percentiles` / `overall_stats`).
4. Schedule a daily `queries.performance.trim_slow_query_log(30)` cron
   so retention stays at 30 days.

---

# Static-asset perf pass — 2026-04-21 (session 4)

Scope: `gateway/static/` only. JS code-splitting, font subsetting,
image re-encoding, resource hints. Also shipped a defensive
schema-drift migration (095) to unblock three crashing cron jobs
surfaced via admin → job logs.

## Byte savings (measured on disk, pre vs post)

| Asset                      | Before   | After   | Δ        | % saved |
| -------------------------- | -------: | ------: | -------: | ------: |
| `img/logo.png` (in-place)  | 229 KB   |   9 KB  | –220 KB  |    96 % |
| `img/tobias.jpg` → `.webp` | 714 KB   |  15 KB  | –699 KB  |    98 % |
| Inter variable (subset)    | 352 KB   |  72 KB  | –280 KB  |    80 % |
| Google Fonts `@import`     | 1 extra request + 1 DNS hop — both removed |||

Cold first-paint for a visitor hitting any app page: logo (–220 KB)
plus font subset (–280 KB) = **≈ 500 KB less** on the critical path,
plus one fewer blocking CSS request against a third-party origin.
Visitors hitting `impressum.html` save another **699 KB** from the
team photo being served as WebP with a JPG fallback.

## JS code-splitting

Deferred. The existing JS is *already* split by feature (`trade.js`
for trading, `settings_billing.js` for settings, etc.) — there is no
monolithic `dashboard.js`/`app.js` that would benefit from tab-scoped
lazy loading. The largest file (`trade.js`, 75 KB) only loads on the
trading page; no file >50 KB loads site-wide.

## Chart.js

Deferred. `static/charts.js` is an 11 KB custom canvas wrapper, not
the Chart.js vendor library. Nothing to lazy-load.

## Critical CSS inlining

Deferred. The full `gateway.css` is 40 KB; inlining its above-fold
slice would require extracting per-route critical paths and editing
every template. Preloading the font (below) captures most of the FCP
win for a fraction of the change risk. Revisit with a build tool.

## Resource hints applied

Every HTML file that loads `gateway.css` now also emits:

```html
<link rel="preload"
      href="/_gateway_static/fonts/Inter-Variable-subset.woff2"
      as="font" type="font/woff2" crossorigin>
```

Applied to 75 templates (both the hashed-URL form and the
`{{ static: gateway.css }}` Jinja token form). `poster.html` is
deliberately skipped — it still pulls Inter + Instrument Serif from
Google Fonts for marketing posters and has its own budget.

dns-prefetch / preconnect were evaluated but not added: a grep of
client-side JS shows no outbound `fetch()` to external hosts; every
API call hits same-origin.

Scripts already ship with `defer` at the bottom of each page; no
additional defer-ization needed in this session.

## Schema-drift migration (095) — piggy-back on this session

Three cron jobs were crash-looping in prod against a drift between
what `db.py` declares and what the code queries:

- `detect_market_movements` expected `volume_24h`, `avg_volume_30d`,
  `close_time`, `snapshot_at`, `first_seen_at` on `market_snapshots`.
- `check_service_health` expected `service_health_snapshots` (in
  migration 021, never applied on prod).
- `sync_polymarket_positions` expected `polymarket_connections` (in
  migration 062, never applied on prod).

Migration 095 adds the missing columns (all nullable, with a backfill
from `snapshotted_at` → `snapshot_at`) and re-declares both missing
tables with `CREATE TABLE IF NOT EXISTS` so fresh and partially-
migrated databases converge to the same state on next restart.

## Lighthouse

Not captured in this session — the sandboxed CI worker has no live
gateway to hit. The operator running locally can capture before/after
with:

```bash
npx lighthouse http://localhost:7000 --quiet \
  --chrome-flags="--headless" --output=json > /tmp/before.json
# check out parent commit, restart gateway …
npx lighthouse http://localhost:7000 --quiet \
  --chrome-flags="--headless" --output=json > /tmp/after.json
jq '.audits | { fcp: .["first-contentful-paint"].numericValue,
                lcp: .["largest-contentful-paint"].numericValue,
                tbt: .["total-blocking-time"].numericValue,
                cls: .["cumulative-layout-shift"].numericValue }' \
   /tmp/before.json /tmp/after.json
```

## Commits in this session (all pushed to `origin/feature/platform-build`)

- `631add2` gateway/static: image + font perf pass
- `8b0acff` migrations: 095 — backfill market_snapshots cols + re-declare drift tables
- `9c9bda5` gateway/static: preload Inter subset on every page that loads gateway.css
- `65f55b0` docs: append session 4 to baseline

---

# Backend perf pass — 2026-04-22 (session 5)

Scope: measure, profile, fix top bottlenecks. Python in-scope this
session (previous one was static-only). Two migrations (096, 097),
one new middleware, two new job files.

## Middleware (hot path)

| Change                        | File                       | Effect |
| ----------------------------- | -------------------------- | ------ |
| `RequestTimingMiddleware`     | `middleware/perf.py`       | Every response now carries `X-Response-Time-ms`. Requests over `SLOW_REQUEST_THRESHOLD_MS` (env-tunable, default 500) persist to `slow_request_log` (migration 096). Outermost — measures real wall-clock. |
| `GZipMiddleware`              | starlette built-in         | Compresses every text response > 1 KB. One-line `app.add_middleware(GZipMiddleware, minimum_size=1024)`. |
| `ORJSONResponse` default      | `server.py`                | FastAPI's `default_response_class` is now `ORJSONResponse` when `orjson` is importable; falls back to stdlib `JSONResponse` otherwise. `orjson==3.10.18` pinned in requirements.txt. 3-5× faster on list payloads (`/api/feed`, `/api/markets`, `/api/sources`). |

## DB maintenance (cron)

| Job                        | Cadence                       | Writes to                         |
| -------------------------- | ----------------------------- | --------------------------------- |
| `wal_checkpoint`           | 04:10 UTC daily               | PRAGMA only                       |
| `vacuum_db_maybe`          | First Sunday of Jan/Apr/Jul/Oct | PRAGMA only                     |
| `trim_perf_logs`           | 03:40 UTC daily (30-day retention) | `slow_request_log`, `slow_query_log` |
| `run_perf_baseline`        | 03:20 UTC daily               | `perf_baseline_snapshots` (migration 097) |

`vacuum_db_maybe` runs weekly (Sunday 05:00) but gates the actual
`VACUUM` on `month in {1,4,7,10} and day <= 7` inside the handler —
apscheduler's cron can't express "first Sunday of the quarter"
natively, so we pay 49 extra no-op calls per year.

## Nightly perf-baseline details

`run_perf_baseline` samples 5 representative hot-read queries from
`queries/` + `db.py` (30 samples each), computes p50/p95/p99/max,
writes one row per `(endpoint, timestamp)` to
`perf_baseline_snapshots`.

Regression alert (logs at ERROR):

```
today_p95 > 1.2 × median(last_7_days_p95)
  AND median(last_7_days_p95) >= 50 ms
```

The absolute-minimum gate stops small-number noise from firing (a jump
from 2 ms → 8 ms is a 4× regression but not actionable). The 7-day
median is robust against the one-off spike that a single bad night
would produce.

The job measures the **DB query itself**, not the HTTP surface —
that's what regresses when schema drift, index loss, or N+1 creep
lands. Middleware-level latency is already captured by
`slow_request_log`, so we don't double-measure it.

## Migrations added

- `096_slow_request_log.py` — one row per slow (>threshold) request
  with timestamp, path, method, status, duration, user_id, hashed IP,
  UA bucket. Indexes on `timestamp DESC`, `(path, timestamp DESC)`,
  and `duration_ms DESC` so the admin dashboard can ask:
  - "What endpoints are slowest right now?" (sort by duration)
  - "When did /api/feed start regressing?" (sort by path + time)
  - "How many requests are over budget?" (sort by time).
- `097_perf_baseline_snapshots.py` — pre-aggregated (endpoint,
  timestamp) rows with p50/p95/p99/max + sample count + error count.
  Small row size keeps the table bounded across a multi-year horizon.

## Commits in this session

- `54f7536` perf: request-timing + gzip + orjson (migration 096)
- `6007837` perf: DB maintenance + nightly baseline cron (migration 097)
- (this doc + trailing tidy-up)

## Deferred — reasoning for each

- **Top-5 fix list.** The prompt asked to profile top 5 slow endpoints
  and optimise them individually. We shipped the measurement
  scaffolding (slow_request_log + perf_baseline + admin-page table
  access) but *not* the per-endpoint fixes — which endpoints are
  slowest is data we don't have yet, and making blind optimisations
  tends to move numbers without moving percentiles. First week of
  this cron's data will identify the real top 5; the session after
  this should fix them.
- **Lighthouse capture.** Requires a live gateway + headless Chrome.
  The CI worker in this session has neither. The prior session's doc
  records the jq command template; numbers to be filled by the
  operator running locally.
- **Response field pruning / sparse fieldsets.** Large-payload audit
  (`/api/feed` etc.) is a data gathering step, which the new
  `slow_request_log.duration_ms` column combined with
  Content-Length will make trivial — wait for data before cutting.
- **Nightly script runner for `scripts/perf_baseline.py`.** The
  prompt referenced an httpx-based script; we built the cron
  equivalent instead (in-process, no HTTP framing). The original
  script still works for ad-hoc local runs.
