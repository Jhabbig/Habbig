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
