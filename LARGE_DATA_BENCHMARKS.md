# Large-data benchmarks

Last updated: 2026-04-23

Companion to [`EDGE_CASES.md`](EDGE_CASES.md) Phase 8. Records the
latency budget for every "power user" scenario — the cases the
dashboard must still render under 2 s even when a long-time
subscriber has accumulated thousands of rows.

Benchmark script: [`gateway/scripts/bench_large_data.py`](gateway/scripts/bench_large_data.py).
Runs against a throwaway SQLite file seeded with synthetic data; safe
to run locally or in CI.

```bash
cd gateway
python3 scripts/bench_large_data.py
```

Output format is one-line-per-scenario:

```
  scenario                      rows   p50(ms)  p95(ms)  budget(ms)  status
  user_saved_predictions_list   5000    312.1    344.5   2000        OK
  source_profile_detail        50000    680.2    812.4   2000        OK
  market_detail_with_signals     500    162.9    179.1   2000        OK
  admin_users_list              3000    511.0    623.7   2000        OK
```

Fail-fast: non-zero exit when any scenario busts its budget. Wire into
CI's post-deploy smoke step if the test DB is pre-seeded; skip
otherwise.

---

## Budgets

| Scenario | Rows | Budget p95 | Notes |
|---|---|---|---|
| Saved-predictions list for a heavy user | 5 000 | 2 000 ms | Pagination: 50/page default, cap 200 |
| Source profile page | 50 000 | 2 000 ms | Index: `idx_predictions_source_resolved` |
| Market detail (signals tab) | 500 | 2 000 ms | Index: `idx_predictions_market_resolved` |
| Admin users list | 3 000 | 1 500 ms | After N+1 fix in commit `f1c095c` |
| Takes list on a hot market | 200 | 1 000 ms | Cache: 60s TTL per market |
| Portfolio summary aggregate | 200 positions | 500 ms | Single aggregate SELECT |
| Insider signals for a topic | 1 000 | 2 000 ms | Cache: 5 min |
| Full credibility recompute (cron) | all users | 60 000 ms | Non-interactive; cron only |
| Best-bets tier page | top-N | 500 ms | TTL cache warm, DB otherwise |

Budgets are deliberately loose (2 s instead of the stricter 500 ms we'd
want for an SPA page) because SQLite on WAL single-writer doesn't
benefit from query-level tuning past a point; any regression past 2 s
is a signal to add a composite index or push into the TTL cache, not
to tune the query.

---

## When a scenario regresses

1. Run `python3 scripts/bench_large_data.py --scenario X` to confirm.
2. `EXPLAIN QUERY PLAN` the SQL for that scenario. Scans on large
   tables are the usual suspect.
3. Check `idx_*` coverage via
   `sqlite3 auth.db ".indexes <table>"`.
4. If the plan is already index-backed, the regression is likely
   row-count growth — paginate the endpoint if it isn't already.
5. If the endpoint is paginated and still slow, move it behind the
   TTL cache (see `cache/ttl.py`).

---

## Adding a new scenario

The bench script has a `_SCENARIOS` registry. Add an entry with:

* `name` — snake_case identifier
* `seed(conn, count)` — populates the DB
* `run(conn)` — the thing we're timing (one call per iteration)
* `budget_ms_p95` — hard ceiling; build fails above

Keep the seed functions deterministic (fixed seed, no clock reads)
so two runs on the same DB version produce comparable numbers.
