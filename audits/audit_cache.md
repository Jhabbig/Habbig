# Adversarial audit — `gateway/cache/service.py` + `gateway/cache/ttl.py`

**Scope:** `gateway/cache/service.py` (419 lines) and `gateway/cache/ttl.py` (363 lines) — the two co-located cache layers shared across the gateway. `service.py` is the **async, Redis-backed** cache (with in-process dict fallback). `ttl.py` is the **sync, single-worker, in-memory** TTLCache. Both expose `get_or_*` / `delete` / `delete_pattern` / `delete_prefix` and invalidation namespaces.

**Date:** 2026-05-15
**Branch:** `feature/platform-build`
**Auditor focus:**
- Cache-key derivation (does user-scope get baked into the key to prevent cross-user reads?)
- TTL boundaries (negative cache, zero TTL, clamps, eviction)
- Cache poisoning via crafted input (glob/wildcard chars in handles, slugs)
- Stampede protection on miss (single-flight, race amplification on factories)
- Sensitive data accidentally cached (auth tokens, raw passwords, wallet creds)

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0     |
| High     | 2     |
| Medium   | 5     |
| Low      | 6     |
| Info     | 4     |

No tokens / passwords / API keys are written through this module today. The two highest-severity issues are both **availability/correctness**, not data-leak: a negative-cache miss-counted-as-miss pattern in both `get_or_compute` and `get_or_set` that lets an attacker nullify cache effectiveness, and total absence of single-flight / stampede protection. Cache-key derivation is **user-scoped where required** (`dashboards:user:{id}`, `settings:user:{id}`, `signal_search:user:{id}`, `feed:user_{uid}:…`) and **admin/non-admin split where required** (search), with one notable architectural gap.

---

## Top 3 findings

### 1. [HIGH] `get_or_compute` / `get_or_set` treat `None` as a cache-miss — negative-cache is fundamentally broken, factory re-runs every request
**Locations:** `gateway/cache/ttl.py:143–161`, `gateway/cache/service.py:285–303`

`ttl.py`:
```python
def get_or_compute(self, key, factory, ttl_seconds):
    cached = self.get(key)
    if cached is not None:
        return cached
    value = factory()
    self.set(key, value, ttl_seconds)
    return value
```

`service.py` is identical in spirit:
```python
async def get_or_set(self, key, factory, ttl_seconds):
    cached = await self.get(key)
    if cached is not None:
        return cached
    value = await factory()
    await self.set(key, value, ttl_seconds)
    return value
```

The docstring at `service.py:299–301` even acknowledges the bug:

> *"None is a legitimate response body (e.g. "source not found"). Store a sentinel dict instead so we still cache the "no data" answer and stop hammering the DB."*

…but the implementation **never actually does the sentinel substitution**. Both helpers set `None` into the backend and then ignore it on the next read.

Concrete impact path (`gateway/api_v1.py:282`):
```python
payload = ttl_cache.get_or_compute(
    f"source:{handle}", _compute, DEFAULT_TTLS["source"],
)
if payload is None:
    raise HTTPException(404, "Source not found")
```

`_compute` returns `None` for a non-existent handle. That `None` lands in the cache, but on the next request `get_or_compute` sees `None` and treats it as a miss — running `_compute` again — running the DB queries again. An attacker hitting `/api/v1/sources/<random>` in a loop (`/api/v1/sources/aaa1`, `/aaa2`, `/aaa3`, …) bypasses the cache entirely on every request:

- `db.get_source_credibility(handle)` — index lookup, but uncached
- `db.get_all_category_credibilities(handle)` — full-table scan if no index hit
- `db.get_credibility_snapshots(handle, 10)`
- `db.get_source_calibration(handle)`

Four DB hits per request, infinitely repeatable. Same vector applies to `market:{slug}` (`market_routes.py:410`), `credibility_consensus:{slug}` (`api_v1.py:450`), `_bundle_cache_key(slug)` (`extension_routes.py:352`), and every other call that may legitimately return `None`.

**Recommendation:** store a sentinel (e.g. `{"__none__": True}`) on miss, or change the contract so factories must never return `None`. The docstring at `service.py:299–301` already promised this fix — it never landed.

**Why HIGH:** This is a remotely-triggerable DoS amplifier. Every endpoint that can legitimately return "not found" is a free DB-hammering vector for unauthenticated users (extension bundle, source detail, market detail, consensus, OG card).

---

### 2. [HIGH] No single-flight / stampede protection — concurrent first-misses all run the factory
**Locations:** `gateway/cache/ttl.py:143–161` (sync); `gateway/cache/service.py:285–303` (async)

Both `get_or_compute` and `get_or_set` are explicitly documented as **not** single-flight:

> *"Beware: the factory runs outside the lock to avoid stalling other readers on a slow DB query. A racing second caller may run the factory too; last-writer-wins on `set()`."* — `ttl.py:151–154`

> *"Exceptions from `factory` propagate — they are the caller's to handle and we don't want to cache error responses."* — `service.py:291–294` (no mention of single-flight)

The cache is supposed to protect SQLite from heavy reads. But on a cold start, on TTL expiry, or after an invalidation, *N* concurrent requests for the same key all see the miss and each run the factory. Realistic concurrency vectors:

- After `ttl_invalidate.on_new_prediction(...)` (called by `pipeline/extract_step.py:119` — runs in pipeline workers per ingest), `feed:*` and `best_bets:*` are wiped. The next *N* feed/best-bets readers each rebuild the materialised feed from SQL. For a heavy `_compute` (joins across `predictions`, `markets`, `source_credibility`), a thundering-herd amplifies DB load by *N×* exactly when load is already heightened (just-published prediction).
- `og_card:*` entries (3600 s TTL) miss at the same time every hour. With a popular shared link, that's a synchronised herd at TTL boundary.
- The async cache's `dashboards:user:{user_id}` (60 s) refresh window is small; a logged-in user reloading rapidly while the cache rolls over could fire 2-3 builds in 100 ms (`db.list_subscriptions(user_id)` plus `_user_plan_info` plus subproduct walks).

Compounded with finding #1: an attacker requesting *N* non-existent slugs in parallel **never** caches them, and every request executes 3-4 DB queries.

**Recommendation:** add a per-key in-flight registry (`dict[str, Future]`). The first miss creates the Future; followers await it. Standard pattern; trivial to retrofit since both `get_or_*` are tiny.

**Why HIGH:** documented limitation, no compensating layer (no Redis Lua script, no in-process lock around factory). Combined with finding #1 the cache provides **zero** protection for the "this thing does not exist" surface.

---

### 3. [MEDIUM] Wildcard / glob characters in user-influenced key fragments can poison invalidation patterns
**Locations:**
- `gateway/cache/service.py:78–86` (`_MemoryBackend.delete_pattern` uses `fnmatch.fnmatchcase`)
- `gateway/cache/service.py:254–283` (`delete_pattern` passes through to Redis `SCAN MATCH`)
- `gateway/cache/service.py:370–408` (`invalidate.source(handle)`, `invalidate.market(market_id)` build globs with f-strings)
- `gateway/cache/ttl.py:125–134` (`delete_prefix` uses `startswith` — safe)
- `gateway/cache/ttl.py:231–243` (`ttl_invalidate.on_new_prediction(handle, market_slug)` etc.)

`invalidate.source(handle)` (`service.py:371–380`):
```python
removed += await cache.delete_pattern(f"source_history:{handle}*")
removed += await cache.delete_pattern(f"credibility:{handle}*")
```

If `handle` contains a `*` or `?`, the call becomes a wildcard that matches much more than intended. Example: `invalidate.source("*")` calls `delete_pattern("source_history:**")`, which in fnmatch and Redis SCAN MATCH glob means "everything under `source_history:`". Same for `invalidate.market(market_id)` → `cache.delete_pattern(f"market:{market_id}*")`.

Today's writer call sites (per `CACHE.md:115–116`) pass already-validated handles, but the **architectural defense is missing**: the cache module trusts callers to never let an attacker influence the handle. With the validation living in the route handlers (`/api/sources/{handle}` regex matches `[a-z0-9_-]+`), this is defense-in-depth-shaped, not actively exploitable today.

Latent risk: any future caller that passes a less-validated identifier (e.g. a user-supplied alias, a Polymarket question_id with mixed chars, a "search-and-purge" admin tool) opens a path to over-deletion. With `invalidate.market(user_input)` and `user_input = "*"`, an attacker could nuke `market:*` from anywhere that endpoint reaches.

**Recommendation:** escape `*` and `?` (and `[`) inside f-string slot values in every `invalidate.*` helper. fnmatch has no escape function for non-bracket characters, so the cleanest fix is `handle.replace("*", "[*]").replace("?", "[?]")` before string interpolation. Or — more architecturally — validate that f-string slots contain only `[a-zA-Z0-9_-]` at the helper boundary.

---

## Per-file sections

### `gateway/cache/service.py` — async Redis-backed cache

#### Architecture summary

- Singleton `CacheService` (`cache`) at module scope (`service.py:352`).
- Lazy Redis connect on first use (`_ensure_connected` — `service.py:131–160`).
- Failure mode: if `REDIS_URL` is unset or unreachable, falls through to `_MemoryBackend` (`service.py:52–90`).
- All keys prefixed with `narve:cache:v1:` (`CACHE_KEY_PREFIX`, line 41) — disjoint from `rl:*` (rate limiter) and `arq:*` (job queue).
- Values JSON-serialised with `default=str` (line 218); deserialised with `json.loads` (line 195).
- Two invalidation helper classes: `invalidate.source / market / environmental / all_sources / everything`.

#### Findings

##### [HIGH] Negative-cache `None` re-runs factory
See **Top 3 finding #1**. Lines 285–303.

##### [HIGH] No single-flight protection
See **Top 3 finding #2**. Lines 285–303.

##### [MEDIUM] Wildcard injection in `invalidate.source` / `invalidate.market` helpers
See **Top 3 finding #3**. Lines 370–408.

##### [MEDIUM] `_MemoryBackend` has no max-size bound — DoS via unbounded growth
**Location:** `service.py:52–90`

```python
class _MemoryBackend:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}
        self._lock = Lock()
```

No `max_items`, no LRU, no soonest-expiry eviction (which the sister class `TTLCache` in `ttl.py:80–117` *does* have). When Redis is down — exactly when the fallback is engaged — the in-memory dict grows without bound.

An attacker who finds *any* cached endpoint that takes user input in the key (search, scenario heatmap with `top_n`/`days`, embed slugs, og_card slugs, …) can flood the cache with millions of unique entries. Each value is a JSON-encoded payload up to whatever the factory produces (no per-value size cap either). The process eventually OOMs and the worker is killed.

Worst case: production Redis goes down → graceful degradation kicks in → in-memory fallback → unbounded growth → kernel OOM-killer terminates the worker → load balancer fails over → cascade.

**Recommendation:** mirror the `TTLCache.max_items=10_000` bound (lines 80–117 of `ttl.py`) in `_MemoryBackend`. The soonest-expiry victim selection is fine for this scale.

##### [MEDIUM] No per-value size enforcement — single huge value can fill the cache
**Location:** `service.py:208–234`

```python
async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
    raw = json.dumps(value, default=str)
    await self._redis.set(full, raw, ex=ttl_seconds)
```

`raw` can be megabytes. Nothing in the cache layer checks it. `CACHE.md:183–185` recommends callers add a size sanity check before `set()` — but the cache itself has no defense.

An attacker who triggers a cached endpoint that returns a large payload (e.g. `/api/v1/sources?limit=999999` if pagination weren't bounded — it is, at 200, so OK there) can write a multi-MB blob to Redis. With 10 such keys, the in-memory fallback eats 50 MB; with Redis, you've increased memory pressure on a shared instance.

**Recommendation:** cap `raw` at e.g. 256 KB; log + skip on overflow, same shape as the `json.dumps` exception path on lines 219–224.

##### [LOW] `json.dumps(..., default=str)` is too permissive — auth-token-shaped objects could land in cache
**Location:** `service.py:218`

```python
raw = json.dumps(value, default=str)
```

`default=str` runs `str(obj)` on anything non-serialisable. If a callable accidentally puts a `pydantic.SecretStr`, a `httpx.Request` with bearer headers, or a custom credential wrapper into a cached dict, the result depends on that class's `__str__`:

- `SecretStr(secret)` → `"**********"` (safe).
- A custom `Token(value=...)` class with `__str__` returning `self.value` → token cached as plaintext.
- A `sqlite3.Row` from `users` table (which has `password_hash`, `pbkdf2_salt`) → fields stringify directly; `default=str` actually fails here because `sqlite3.Row` is not iterable in the dict sense and `json.dumps` would TypeError → falls to except and logs only — OK.

Today's call sites all hand-pick fields before caching, so no real leak. But this is the only defense-in-depth between a sloppy `_compute()` and a secret-on-the-wire. The CACHE.md docs explicitly say "don't cache secrets" — the code doesn't enforce it.

**Recommendation:** use an explicit serialiser whitelist (e.g. a `cache_safe(v)` wrapper), or at least add a regex screen on the serialised string that fails closed if it looks tokenish (`AIza…`, `sk-…`, `xox[bpoa]-…`, base64-pattern lengths ≥ 32). Cheap to compute; rules out the "developer accidentally caches `user_row`" footgun.

##### [LOW] `delete_pattern` SCAN batching can stall the loop on large keyspaces
**Location:** `service.py:254–283`

```python
async for key in self._redis.scan_iter(match=full, count=500):
    batch.append(key)
    if len(batch) >= 500:
        removed += int(await self._redis.delete(*batch))
        batch.clear()
```

`scan_iter(count=500)` is async but `redis.delete(*batch)` sends a single DEL command with 500 args. For a cache with 100 K keys matching, that's 200 round-trips. Under `invalidate.everything()` (called by admin "clear" button — `admin_routes.py:1181`), this runs **without** rate limiting and **without** chunking the DEL.

Not a security vuln directly, but: an admin who clicks "clear cache" while production has 1 M keys can saturate the Redis connection for tens of seconds, blocking all `cache.get` waiting on the same client. Combined with `socket_timeout=1.0` (line 146), the gets time out, fall back to "cache error" (record_error, return None) and every reader becomes a cold-cache reader.

**Recommendation:** chunk the DEL into ≤ 100 keys, await between chunks, or move bulk deletion to a server-side Lua script.

##### [LOW] `_record_error` is the only failure signal — silent regressions possible
**Location:** `service.py:186–204`, `:225–234`, `:240–252`

Every backend exception is caught, logged at WARNING, and `_record_error()` is bumped. There is no alert wiring, no log-level escalation when error rate spikes, no circuit-breaker that disables the cache after N consecutive errors. If Redis flaps, every request silently degrades to "no cache" while Redis errors accumulate in the log.

**Recommendation:** export error rate as a metric; trip a flag when `errors / (hits + misses + errors)` exceeds threshold and force-disable temporarily so the round-trip to a broken Redis stops adding latency.

##### [INFO] `_connect_attempted = True` before the actual ping succeeds
**Location:** `service.py:138–160`

```python
self._connect_attempted = True
try:
    ... client = aioredis.from_url(...) ...
    await client.ping()
    self._redis = client
except Exception:
    self._redis = None
```

If `aioredis.from_url` raises (e.g. malformed REDIS_URL), `_connect_attempted` is already `True`. Subsequent `_ensure_connected()` calls early-return. That's the intended "fail closed; don't keep retrying" behaviour. Documented at line 130. Not a vuln; flag because the order makes it look like retries should happen on raise.

##### [INFO] `decode_responses=True` is correct, but no auth-mode enforcement on URL
**Location:** `service.py:143–148`

`aioredis.from_url(self._redis_url, ...)` accepts any scheme: `redis://`, `rediss://`, `unix://`. Production should use `rediss://` (TLS) or `unix://` (socket). No check enforces this. If misconfigured to `redis://` over the public internet, the cache traffic — including any sensitive payloads that snuck in — is plaintext on the wire.

**Recommendation:** assert `_redis_url.startswith(("rediss://", "unix://"))` when `ENVIRONMENT == "production"`.

---

### `gateway/cache/ttl.py` — sync in-memory TTLCache

#### Architecture summary

- Singleton `TTLCache` (`ttl_cache`) at module scope (line 217).
- Single-worker only by design (line 7–10 docstring).
- `max_items=10_000`, soonest-expiry eviction (lines 80–117).
- Per-prefix hit/miss attribution for the admin panel (lines 87–89).
- `ttl_invalidate` namespace mirrors `invalidate` from `service.py`, plus user-scoped helpers `on_subscription_change(user_id)` and `on_role_change(user_id)` that fire-and-forget across the **async** cache too.

#### Findings

##### [HIGH] Negative-cache `None` re-runs factory
See **Top 3 finding #1**. Lines 143–161.

##### [HIGH] No single-flight protection on `get_or_compute`
See **Top 3 finding #2**. Lines 143–161.

##### [MEDIUM] `on_subscription_change` / `on_role_change` cross-event-loop bust uses deprecated `asyncio.get_event_loop()`
**Locations:** `ttl.py:296–307`, `:340–351`

```python
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(_bust())
    else:
        loop.run_until_complete(_bust())
except RuntimeError:
    asyncio.run(_bust())
```

`asyncio.get_event_loop()` is deprecated in 3.10+ and raises `DeprecationWarning` in 3.12 when no running loop is bound. In 3.14 it's slated to raise `RuntimeError`. The current control flow relies on catching that `RuntimeError` to fall back to `asyncio.run` — which is *one* of the documented patterns — but the deprecation warning leaks through tests in 3.12 and the behaviour will break in 3.14.

The security-relevant angle: if the `try` block raises any non-`RuntimeError` (e.g. event loop closed mid-task during a worker restart), `except Exception` (line 305) swallows it. The async cache keys (`dashboards:user:{uid}`, `settings:user:{uid}`, `signal_search:user:{uid}`) are **not invalidated**. The user's old plan/role payload — including subscription state and admin status — remains cached for 30–60 s. On a demotion-from-admin event, that user keeps admin views until the TTL expires.

Today's payloads (per the comment on line 318–322) "don't embed role-gated fields" so it's not actively a privilege-escalation vector — but the same code path will gate role-relevant fields tomorrow and any silently-swallowed exception is a latent privilege-leak window.

**Recommendation:** use `asyncio.get_running_loop()` (3.10+) for the in-loop case; explicit catch for the "no running loop" case. Log at WARN (not silent) when the async bust fails so a regression doesn't hide.

##### [MEDIUM] `get_or_compute` factory exceptions kill the request — but **after** running the SQL
**Location:** `ttl.py:143–161`

```python
cached = self.get(key)
if cached is not None:
    return cached
value = factory()
self.set(key, value, ttl_seconds)
return value
```

If `factory()` raises, the request 500s and **nothing** is cached. The next request re-runs the factory and re-raises. This is the documented behaviour ("Factory exceptions propagate (don't cache errors)" — line 149), and it's correct for transient DB hiccups. But combined with **no rate limiting at the cache layer**, this gives an attacker who can trigger a deterministic factory exception (e.g. a SQLite-violating handle, a Unicode normaliser bug) a way to:

- Spam `/api/v1/sources/<exception-trigger>` at 1000 rps.
- Every request runs `_compute()` → `db.get_source_credibility(handle)` etc. → exception → 500.
- No cache absorbs the load.

The route-level rate limit (`@rate_limit(120/60s)` on search; nothing visible on `api_v1.py:282`) is the only defense.

**Recommendation:** at minimum, log a `cache: factory_exception count={N}` metric every 100 occurrences so operations sees the cache being driven through. Optionally: cache a "transient_error" sentinel for 1–5 s on factory failure so the *N*-th request doesn't hit DB. (Trade-off: hides incidents.)

##### [MEDIUM] `delete_prefix` accepts arbitrary string — admin clear-prefix endpoint is one ACL bug from arbitrary cache wipe
**Location:** `ttl.py:125–134`

```python
def delete_prefix(self, prefix: str) -> int:
    victims = [k for k in self._data if k.startswith(prefix)]
    for k in victims:
        self._data.pop(k, None)
    return len(victims)
```

`startswith("")` matches every key. There is no minimum-length check, no namespace whitelist. Today the only public caller of `delete_prefix` outside of the `ttl_invalidate` helpers is `og_cards.py:303` (`"og_card:"`) and `search_routes.py:412` (`"search:"`, in tests). Both are static strings.

The admin `/admin/cache/clear` button calls `ttl_invalidate.everything()` → `ttl_cache.clear()` (line 136) — fine, that path is intentional. But the moment an admin endpoint exposes a user-supplied prefix (think: "clear cache for prefix `<input>`" diagnostic tool), an authenticated admin with a typo could nuke everything. Combine with a stored-XSS on the admin panel and a regular user can trigger it.

**Recommendation:** require minimum prefix length (e.g. ≥ 4 chars) and reject `""`, `"*"`, `"?"`. Or: only accept prefixes in `DEFAULT_TTLS.keys()`.

##### [LOW] Eviction picks soonest-expiry — predictable, exploitable as cache-thrashing primitive
**Location:** `ttl.py:111–117`

```python
if key not in self._data and len(self._data) >= self._max:
    victim_key = min(self._data, key=lambda k: self._data[k][0])
    self._data.pop(victim_key, None)
    self._evictions += 1
```

The victim is whichever key has the lowest expiry timestamp. An attacker who can predict TTLs (which they can — they're in `DEFAULT_TTLS`, all 60–3600 s, and the doc lists them) can fill the cache with their own short-TTL keys (`feed:user_<theirs>:cat_x:sort_y:page_z` for synthetic combinations) and effectively cause **legitimate** entries with shorter remaining TTL to be evicted. Since legit feeds have 60 s TTL and the attacker can mint thousands of variations, they can knock the cache hit-rate to ~0 for any cached endpoint.

`max_items=10_000` is small — at 100 unique cache keys per request, an attacker hits the cap in 100 requests. The "soonest-expiry" rule means freshly-written entries with full 60s TTL survive; the most stale (and most likely to be a real user mid-session) gets evicted first. Counter-intuitively this is the inverse of an LRU and amplifies the attack — newer keys, including attacker-controlled ones, are protected.

**Recommendation:** track a per-prefix entry count and reject `set()` when a single prefix exceeds (say) 30% of `max_items`. Or move to a proper LRU.

##### [LOW] `_prefix(key)` for stats can leak structural data through `/admin/cache`
**Location:** `ttl.py:66–69`

```python
def _prefix(key: str) -> str:
    i = key.find(":")
    return key if i < 0 else key[:i]
```

For `source:fedwatcher` the prefix is `source` — safe. For a malformed key without a `:` (e.g. an accidentally non-prefixed key from a typo: `"sourcefedwatcher"`), the full key becomes the prefix and lands in `stats()` output verbatim. Admin panel renders it. Today nothing puts a `:`-less key in, but `cache.set("debug-key", payload, ttl=10)` would leak `"debug-key"` to the admin UI.

Not security-critical; flagged because the `_prefix` function is the contract between callers and the admin surface, and that contract is implicit.

##### [LOW] `clear()` returns count, but invalidate.`everything()` ignores it
**Location:** `ttl.py:136–141`, `:360–362`

```python
def clear(self) -> int:
    with self._lock:
        n = len(self._data)
        self._data.clear()
        return n
```

Then:
```python
@staticmethod
def everything() -> int:
    return ttl_cache.clear()
```

`ttl_cache.clear()` returns `n` correctly. But `_evictions`, `_hits`, `_misses`, `_sets` counters are **not** reset — only the data dict. The next stats call shows hit_rate against an empty cache with the old counter totals, producing nonsense stats until the next `reset_stats()` call. Doesn't affect security; will confuse oncall.

##### [INFO] `RLock` (re-entrant) rather than `Lock` — correct, but expensive
**Location:** `ttl.py:84`

`threading.RLock()` is heavier than `Lock` on CPython. The choice is correct because some `get_or_compute` paths could theoretically call back through the cache, but it's worth noting that under heavy concurrency the lock acquisition is the bottleneck. The `service.py` sibling uses a plain `Lock` (line 57). Consistency wouldn't hurt — pick one.

##### [INFO] No metric for "factory raised after we got past the get() check"
**Location:** `ttl.py:156–161`

If `self.get(key)` returns `None` and then `factory()` raises, that's distinct from `factory()` returning `None`. Today both are observationally identical in stats (miss + no set). Operators can't distinguish "100% miss because nothing ever fits TTL" from "100% miss because the factory always raises". Minor.

---

## Cross-cutting observations

### Cache-key user-scoping audit (per request)

| Cache key pattern | User-scoped? | Evidence | Risk |
|------------------|--------------|----------|------|
| `dashboards:user:{user_id}` | YES | `server.py:4055` | OK |
| `settings:user:{user_id}` | YES | `server.py:7118` | OK — but caches wallet address and Kalshi member_id |
| `signal_search:user:{user_id}` | YES | `server.py:8048` | OK |
| `feed:user_{uid}:cat_*:sort_*:page_*` | YES (schema) | `ttl.py:19, 279` | Only invalidated, no reader site found |
| `search:q_*:t_*:adm_{0,1}:lim_*` | NO user_id, admin-flag YES | `search_routes.py:184` | Two non-admins share results. Admin payload separate. Safe today; payload doesn't include user-personalised ranking. |
| `saved_views:{scope}:user={uid}:filters=…` | YES | `saved_views_schema.py:481` | OK. Anonymous users share `user=anon` bucket — acceptable since payload is just count. |
| `source:{handle}` | NO (global) | `api_v1.py:283` | OK — public data. |
| `market:{slug}` | NO (global) | `market_routes.py:411` | OK — public data. |
| `best_bets:tier_{tier}:page_{p}` | NO user_id, tier YES | `market_routes.py:369` | Users on same tier share — OK by spec. |
| `og_card:*` | NO | `og_cards.py:297` | OK — public OG cards. |
| `ext_bundle:{slug}` | NO | `extension_routes.py:187` | OK — bundle is public-equivalent. |
| `insider_signals:type_*:days_*:page_*` | NO | `insider_routes.py:128` | OK — global signal feed. |

Conclusion: **no cross-user-read vulnerability** in current keys. The architectural concern is that the cache module itself **provides no enforcement** that a user-tied payload uses a user-scoped key. A new endpoint with `cache.set("dashboard_state", {personal stuff}, ttl)` would compile, run, and cross-leak. Documented warning at `CACHE.md:186–187` is policy, not code.

### Cached sensitive-data sweep

Inspected every `cache.set` / `cache.get_or_*` call site in `gateway/**.py`:

- **No tokens cached.** Kalshi token and Polymarket session keys live in `market_credentials` table, never enter the cache. Even `_get_market_connections` (`server.py:7757`) builds a sanitised view containing only `wallet_address` and `member_id` — not the auth secret.
- **No password / password-hash cached.** PBKDF2 hashes and salts only leave `users` table via authenticated session lookups; none cache user-row dicts whole.
- **No API keys cached.** `api_keys_*` table reads are uncached.
- **No session tokens cached.** Auth session validation is uncached (intentional — see `gateway/security/sessions.py`).
- **PII-grade data in `settings:user:{user_id}`:** wallet address, member_id, bankroll, env preferences. Acceptable because the key is user-scoped; would be a problem only if a Redis dump or admin debug surface exposed the raw cache.

### Cache poisoning surface

- **Glob-character injection:** see Top 3 finding #3.
- **Key collision:** `_make_key` (`service.py:44–46`) just concatenates `CACHE_KEY_PREFIX + key`. A handle `"foo:bar"` for `source:` makes `source:foo:bar` which collides with `source_history:bar` — except no, the prefix is `source:` vs `source_history:`, so the prefix segment differs. With reasonable handle restrictions, no collision. With a maliciously-crafted handle like `"x"` against `source:`, you get `source:x` which can be set by *anyone* — the cache itself has no per-key write authorization.
- **JSON parser:** `json.loads` (`service.py:195`) on attacker-controlled JSON only if the attacker writes to the cache. They can't directly; they write to *factories*. The factory output is what gets serialised. So JSON injection requires a vulnerable factory (covered in upstream route audits).
- **Log injection:** `log.warning("cache.get bad JSON for %s: %s", key, exc)` (`service.py:199`) — `key` is the un-prefixed key, supplied by the caller. If a caller passes a key containing `\n`, the log line is split. Realistic only if a caller embeds user input directly into the key without sanitisation. Most callers use static prefixes plus validated handles; safe today.

### TTL boundary review

- **TTL ≤ 0 clamped to 60** in both `service.py:213–214` and `ttl.py:109–110`. Means a caller cannot explicitly disable caching for a single `set()` (must use `cache.delete` instead). Not a vuln; a footgun if you wanted a "write-through zero-TTL" pattern.
- **Memory backend (`service.py`):** TTL enforced lazily on read (line 65). Expired entries persist in memory until next access. With a never-read attacker-set entry, memory leaks until process restart. Combined with **no max-size**, see [MEDIUM] above.
- **TTLCache (`ttl.py`):** TTL enforced lazily on read (line 101). Same lazy expiry. But `max_items=10_000` caps growth.
- **No TTL upper bound.** A caller can `cache.set(k, v, ttl_seconds=10**9)`. The `scenarios_routes.py:340` passes `ttl_seconds=86400` (1 day) which is fine. Nothing prevents `100**9`. Latent footgun but not exploitable externally.

### Stampede summary

Across both modules: no single-flight, no negative-cache sentinel, no factory rate-limiting. The cache is effectively **opt-in only on success**. Every "not found" returns to DB. Every TTL expiry runs a herd. Every wipe-after-write does the same. For a workload that's mostly read-cached writes, the design is OK; for one with hot mutation events (new prediction) cascading invalidations on a popular feed, this amplifies DB load measurably.

---

## Recommended fix priority

1. **Negative-cache sentinel** (`service.py:285–303` and `ttl.py:143–161`). Smallest diff, highest payoff. Eliminates the DoS amplifier across `/api/v1/sources/`, `/api/v1/markets/`, `/extension/bundle/`, and OG-card endpoints.
2. **Single-flight on factory** (same two methods). Add a per-key `dict[str, asyncio.Future]` for the async path and per-key `threading.Lock` for the sync path. ~30 LoC each.
3. **Glob escape in `invalidate.*` helpers** (`service.py:370–408`). One-liner per slot value. Defense-in-depth against future ACL bugs.
4. **`_MemoryBackend` size cap** (`service.py:52–90`). Copy the eviction logic from `ttl.py:111–117`. Prevents Redis-down → OOM scenario.
5. **Modernise `asyncio.get_event_loop()`** in `ttl.py:296` and `:341`. 3.14 hard-fail risk.
6. Optional: per-value size cap in `cache.set`, log-on-async-bust-failure, `rediss://`-only check in production.
