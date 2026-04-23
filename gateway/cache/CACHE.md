# Cache layer — canonical reference

narve.ai ships two cache layers that live side by side. Use the right one for
the call site; don't reach across.

```
cache/
├── ttl.py       # Sync, in-process, single-worker. Bounded TTLCache + stats.
│                # Import: from cache import ttl_cache, ttl_invalidate, DEFAULT_TTLS
├── service.py   # Async, Redis-backed (falls back to in-process dict if REDIS_URL unset).
│                # Import: from cache import cache, invalidate
└── __init__.py  # Re-exports both, disjoint namespaces.
```

This document is the source of truth for the **sync TTL cache** (`ttl.py`). For
the async Redis-backed cache, see docstrings in `service.py`.

---

## Key schema

Every cached key starts with a lowercase prefix followed by `:`. The prefix
determines both the default TTL (via `DEFAULT_TTLS` in `ttl.py`) and the
invalidation rule that applies when a write happens.

| Prefix                    | TTL  | Example                                           | Reader                   | Invalidated on                             |
|---------------------------|------|---------------------------------------------------|--------------------------|--------------------------------------------|
| `feed`                    | 60s  | `feed:user_123:cat_all:sort_new:page_1`          | (reserved — no reader)   | new prediction, market resolved, sub change|
| `best_bets`               | 120s | `best_bets:tier_pro:page_1`                       | market_routes.py:196     | new prediction, sub change                 |
| `markets`                 | 30s  | `markets:cat_crypto:sort_vol:page_1`              | market_routes.py:112     | (rolls over on TTL)                        |
| `market`                  | 30s  | `market:will-fed-hold-jan-2027`                   | market_routes.py:237     | market resolved                            |
| `source`                  | 300s | `source:fedwatcher`                               | api_v1.py:222            | new prediction, credibility recompute      |
| `sources`                 | 120s | `sources:sort_default:filter_none:page_0_size_100`| api_v1.py:186            | credibility recompute                      |
| `source_history`          | 300s | `source_history:fedwatcher`                       | (reserved — no reader)   | new prediction, credibility recompute      |
| `source_network`          | 600s | `source_network`                                  | network_routes.py:111    | credibility recompute                      |
| `market_chart`            | 120s | `market_chart:will-fed-hold`                      | (market_routes.py inline)| market resolved                            |
| `insider_signals`         | 120s | `insider_signals:type_senate:days_7:page_1`       | insider_routes.py:128    | (rolls over on TTL)                        |
| `insider_leaderboard`     | 600s | `insider_leaderboard`                             | insider_routes.py:176    | (rolls over on TTL)                        |
| `og_card`                 | 3600s| `og_card:source:fedwatcher`                       | og_cards.py:299          | manual (og_cards.py:305)                   |
| `credibility_consensus`   | 60s  | `credibility_consensus:will-fed-hold`             | api_v1.py (§consensus)   | new prediction, market resolved            |
| `scenarios_correlation`   | 86400s | scenario-specific                                | scenarios/correlation.py | (rolls over on TTL)                        |
| `signal_search`           | 30s  | query-hashed                                      | search_routes.py:278     | (rolls over on TTL)                        |

TTLs live in `DEFAULT_TTLS` in `ttl.py`. If you cache at a prefix not in that
map you must pass `ttl_seconds=` explicitly — don't invent a new prefix without
also adding it here.

---

## Reader call sites

All reader wrappers use the same shape:

```python
from cache import ttl_cache, DEFAULT_TTLS

def _compute() -> dict:
    # Your DB query / external call goes here.
    return ...

payload = ttl_cache.get_or_compute(
    f"source:{handle}",           # canonical key
    _compute,                     # factory, runs only on miss
    DEFAULT_TTLS["source"],       # TTL from the table above
)
```

Current reader sites:

| File                             | Line | Key                                      |
|----------------------------------|------|------------------------------------------|
| `api_v1.py`                      | 186  | `sources:sort_default:filter_none:page_*`|
| `api_v1.py`                      | 222  | `source:{handle}`                        |
| `market_routes.py`               | 112  | `markets:cat_{cat}:sort_{sort}:page_{p}` |
| `market_routes.py`               | 196  | `best_bets:tier_{tier}:page_{p}`         |
| `market_routes.py`               | 237  | `market:{slug}`                          |
| `insider_routes.py`              | 128  | `insider_signals:type_*:days_*:page_*`   |
| `insider_routes.py`              | 176  | `insider_leaderboard`                    |
| `network_routes.py`              | 111  | `source_network`                         |
| `search_routes.py`               | 278  | signal search (30s override)             |
| `og_cards.py`                    | 299  | `og_card:*`                              |
| `scenarios_routes.py`            | 266  | scenarios pre-computation                |
| `scenarios/correlation.py`       | 256  | correlation lattice                      |

### Factory rules

* Must be **synchronous**. The handler awaits nothing inside `get_or_compute`.
* Factory exceptions **propagate** — they do not get cached. A second call
  after a failure re-runs the factory.
* Parallel first-hit racers may both run the factory once (no single-flight
  guarantee). Last-writer-wins on `set()`. If your factory is expensive, that
  first race is the only cost; steady state hits the cache.
* Don't return mutable references you plan to mutate downstream — other
  readers will see your mutation. Clone before edit.

---

## Invalidation

Writers **never** call `ttl_cache.delete*` directly. Always go through
`ttl_invalidate` so the schema stays in one place and new keys are covered
without touching writers.

```python
from cache import ttl_invalidate
ttl_invalidate.on_new_prediction(source_handle, market_slug)
```

### Helpers and current wiring

| Helper                            | Flushes                                                                                                                      | Call sites                                                                                                                              |
|-----------------------------------|------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| `on_new_prediction(handle, slug)` | `feed:*`, `best_bets:*`, `source:{handle}`, `source_history:{handle}`, `credibility_consensus:{slug}`                        | `pipeline/extract_step.py:119`                                                                                                          |
| `on_market_resolved(slug)`        | `market:{slug}`, `market_chart:{slug}`, `feed:*`, `credibility_consensus:{slug}`                                             | `jobs/resolution_jobs.py:107,108,148,149`                                                                                               |
| `on_credibility_recompute()`      | `source:*`, `source_history:*`, `sources:*`, `source_network`                                                                | `jobs/ai_maintenance.py:155`                                                                                                            |
| `on_subscription_change(user_id)` | `feed:user_{uid}:*`, `best_bets:*`                                                                                           | `billing_routes.py:890,955,982,1001,1028,1052`, `queries/subscriptions.py:232`, `stripe_webhook_hardening.py:310`, `jobs/reconcile_subscriptions.py:131` |
| `on_feature_flag_change()`        | `feed:*`                                                                                                                     | `admin_routes.py:451`                                                                                                                   |
| `everything()`                    | all keys                                                                                                                     | `admin_routes.py:808` (clear button)                                                                                                    |

### Rules for adding a new invalidation site

1. Identify the write: which DB rows changed?
2. Match that to a helper in the table above. If none fits, add a new
   `on_*` classmethod in `ttl.py` — don't scatter prefix deletes across the
   codebase.
3. Wire the helper call at the write site, **after** the DB commit, **never**
   inside the transaction (otherwise a rollback leaves stale cache).
4. In a long-lived job, wrap the call in `try/except` so a cache hiccup
   doesn't break the job.

---

## Stats and admin

`ttl_cache.stats()` returns per-prefix hit/miss/set counters plus totals:

```json
{
  "total": 421,
  "expired": 38,
  "live": 383,
  "max_items": 10000,
  "evictions": 0,
  "total_hits": 15472,
  "total_misses": 1309,
  "hit_rate": 0.9220,
  "per_prefix": [
    {"prefix": "market", "hits": 4091, "misses": 203, "sets": 211, "hit_rate": 0.953},
    ...
  ]
}
```

Surfaces:

* `GET  /admin/cache`       → HTML dashboard (admin only).
* `GET  /admin/cache/stats` → same data as JSON.
* `POST /admin/cache/clear` → calls `ttl_invalidate.everything()`, logged to the audit table.

---

## Eviction

`TTLCache(max_items=10_000)` by default. When the cache is full and a new
**different** key comes in, we drop the entry with the **soonest expiry** —
that's an O(n) scan, acceptable because n is bounded. Resetting an existing
key does not trigger eviction and does not consume from the budget.

If you find yourself raising `max_items`, investigate first: it usually means
a writer forgot to invalidate after a state change, or a key schema is
accidentally user-scoped where it should be global.

---

## When NOT to use the sync TTL cache

* **Multi-worker deploys.** Each worker holds its own copy; a write in worker
  A won't evict in worker B. Move to the async Redis-backed `cache/service.py`
  if you need cross-worker coherence.
* **Async factories.** The `get_or_compute` API is sync. For an async data
  source, either pre-compute to a thread-safe dict and pass a plain-sync
  factory, or use the async service with `await cache.get_or_set(...)`.
* **Large payloads (>1 MB).** The cache holds values by reference in memory.
  If you're caching a fat response, add a size sanity check before calling
  `set()`.
* **Secrets or per-request user state.** Don't cache API keys, session
  tokens, password hashes, or anything user-private at shared prefixes.

---

## Testing

Unit tests: `tests/test_cache.py`, `tests/test_cache_invalidation.py`,
`tests/test_cache_service.py`. 51 cases total covering:

* Core ops (get/set/delete/delete_prefix/clear).
* `get_or_compute` single-flight behaviour and exception propagation.
* TTL expiration.
* Thread safety under 100-thread race.
* Max-item eviction and "soonest-expiry" victim selection.
* Per-prefix hit/miss attribution.
* Every `ttl_invalidate.on_*` helper.
* `/admin/cache` page gating + stats-shape contract.

Run in isolation:

```bash
cd gateway
python3 -m pytest tests/test_cache.py tests/test_cache_invalidation.py tests/test_cache_service.py -v
```

---

## Changelog

| Version | Date       | Change                                                                                                    |
|---------|------------|-----------------------------------------------------------------------------------------------------------|
| 1.0     | 2026-04-21 | Initial sync `TTLCache` + `ttl_invalidate` facade. Reader wrappers in `insider_routes` and `search_routes`.|
| 1.1     | 2026-04-22 | Add `on_subscription_change` invalidation across all billing paths + webhook/reconcile jobs.              |
| 1.2     | 2026-04-22 | Wrap `api_v1.py` sources list + source detail reads. Extend cache admin page with per-prefix stats.       |
| 1.3     | 2026-04-23 | `credibility_consensus:{slug}` reader endpoint lands at `/api/v1/markets/{slug}/consensus` (60s TTL).     |
