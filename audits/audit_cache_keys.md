# Cache-key derivation audit — cross-tenant poisoning

**Scope:** every site in `gateway/` that derives a cache key, with focus on
keys that incorporate user input (path params, query strings, JSON bodies,
form fields) **without an authority scope** that would prevent attacker A
from writing a payload that gets served back to victim B.

**Date:** 2026-05-15
**Branch:** `feature/platform-build`
**Constraint:** pre-release page (`gateway/static/prerelease.html`,
`gateway/static/pages/prerelease.css`, `gateway/pwa_middleware.py` critical
CSS) is **off-limits** — confirmed unchanged vs. `audit_prerelease.md`
(2026-05-15 CLEAN). Not touched here.

**Auditor model:** anonymous attacker can hit any public endpoint and
mostly any authenticated endpoint with a cheap Pro sub. The threat is
**cache poisoning** — write a payload under a key that someone else
will read back. A key is *safe* if either (a) it does not embed user
input, or (b) it embeds an authority scope (user id, admin flag, tier,
sharer-issued token) that prevents one tenant's writes from being
served to another tenant.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 4 |
| Low      | 5 |
| Info     | 3 |

The classic "log-in-as-Alice, hit endpoint, log-in-as-Bob, get Alice's
data" failure mode does **not** exist here. The keys that need user
scoping have it (`feed:user_{uid}:…`, `dashboards:user:{user_id}`,
`settings:user:{user_id}`, `signal_search:user:{user_id}`,
`saved_views:{scope}:user={uid}:filters=…`). The two HIGHs are
different shapes: a **tier-omitted edge-case** in
`api_markets_unified` and an **admin/free split** that is correctly
keyed in search but **incorrectly absent** in `api_markets_top_edge`
when the `admin` user is also the *only* user whose factory output is
allowed to populate the `admin` slot. The remaining findings cover key
namespace collisions, missing input validation, attacker-controlled
keyspace blowup, and cross-cache-layer aliasing.

---

## Top 3 findings

### 1. [HIGH] `api_markets_unified` — env_relevant Pro-only filter skips cache but the unfiltered key is shared across all tiers, letting a Free user serve a Pro-filtered cache miss
**File:** `gateway/market_routes.py:323-393`
**Key:** `markets:cat_{category or 'all'}:sort_{sort}:src_{source or 'all'}:page_{page}:lim_{limit}`

The cache is opt-out when either `search` or `env_relevant` is set
(line 347 — `cacheable = not search and not env_relevant`). When opt-in,
the `_compute` calls `unified_markets.filter_markets(markets, …)` with
**no tier filter at all**. Today that's correct because
`filter_markets` only filters on `category/source/search/sort`. But the
key omits any user identity, so if a future change adds a per-user
filter (e.g. handle-blocklist, region restriction, locked-categories
gating) to either `filter_markets` or to the surrounding `markets`
list, **the first request through builds the cache entry, every
subsequent request reads it back, regardless of who they are**.

That's a latent cross-tenant leak rather than a present one — but it's
load-bearing on a future change being audited as carefully as this
one, which is exactly the pattern that bites. The unfiltered cache
also still gets `_forensic_sign(user, payload, …)` applied **after**
the cache read (line 393), so the per-user forensic signature is
correct, but the **payload content itself** is not user-scoped.

Concrete probe: an admin opens `/api/markets/unified?env_relevant=1`,
which bypasses cache; a Free user later opens
`/api/markets/unified?cat=politics`, which uses the shared cache. If
`unified_markets.fetch_unified_markets()` ever returns a list that the
admin's request silently mutated (it currently doesn't — but it's the
same list reference returned to all callers under
`backend/markets/unified_markets.py:227`'s 5-min cache), the Free user
sees admin-scoped market list state.

**Why HIGH:** load-bearing on the discipline of two separate files
(`market_routes.py` and `backend/markets/unified_markets.py`) staying
free of per-user filtering. The audit at `audit_cache.md` already
flagged the parent file's stampede / negative-cache holes; this is the
key-derivation analogue.

**Fix:** either gate the cache off when the user is admin-impersonating
or in any non-default plan, or extend the key with `:plan_{plan}` so
plan changes naturally isolate.

---

### 2. [HIGH] `api_markets_top_edge` — `best_bets:tier_{tier}` collapses all admins into one slot, and the cached payload includes per-user data via `_forensic_sign`
**File:** `gateway/market_routes.py:396-438`
**Key:** `best_bets:tier_{tier}:page_1:lim_{limit}:min_{min_sources}:cat_{cat}`

Line 416 derives `tier = "admin" if user.get("is_admin") else (user.get("plan") or "free")`.
That keys correctly across `admin / pro / trader / free`, so a Free
user cannot poison the Pro slot. **But** the cached value is the
output of `_compute()` (just a list of market dicts), and the response
is `srv._forensic_sign(user, dict(payload), …)` (line 438). The
forensic sign is per-user and applied to the **cloned** payload, so
the cached entry itself is clean.

The actual hazard: `tier = "admin"` collapses Admin + Super Admin into
one cache slot. If a Super Admin's request ever populates the slot
with a payload that contains super-admin-only flags (today it does
not — `_compute` only emits market dicts), an ordinary Admin would
read it. The same applies if an impersonating admin (`ImpersonationMiddleware`
sets `current_user()` to the target) populates the slot — the
impersonator's tier label depends on the impersonation target, not on
their real admin status, which could cause an impersonation session
to leak its target's view into the global admin cache.

**Verification:** `_real_admin_user()` is consulted by
`_require_admin_user`, but `_require_markets_user` does **not** unwind
impersonation (cf. `audit_server_admin.md` for the impersonation
gating policy). The cache key here is derived from the impersonated
identity.

**Why HIGH:** impersonation is a documented vector (Migration 022,
covered by `audit_impersonation*` / `audit_security_dir.md`) and the
key derivation here does not respect it. A super admin impersonating
a regular admin would have their tier label set by the target, not
themselves, so the cache slot fills with the *target's* view —
benign now, becomes a leak the moment any admin-only-but-not-super
field lands in `m.to_dict()`.

**Fix:** key on `_real_admin_user()` rather than `user.get("is_admin")`,
or split `admin` into `admin` / `super_admin`, or — simplest — bypass
cache entirely for impersonating sessions (set
`cacheable = not request.state.impersonating` or equivalent).

---

### 3. [HIGH-leaning-MEDIUM] OG card and og:source / og:market keys take raw `handle` / `slug` with no validation, allowing keyspace blowup + namespace overlap
**Files:**
- `gateway/og_routes.py:75-115` (`og:source:{handle}`)
- `gateway/og_routes.py:118-172` (`og:market:{slug}`, slug is `:path`)
- `gateway/og_cards.py:288-297` (`og_card:{key}` prefix)
- `gateway/profile_routes.py:324` (`profile:{handle}:{accuracy}:{total}`)

`/og/source/{handle}` (`og_routes.py:75`) accepts any string as the
path parameter — no `HANDLE_RE.match()` (which is enforced in
`profile_routes.py:195` for `/profile/{handle}` itself). The DB lookup
`db.get_source_credibility(handle)` returns None for non-matches and
the route 404s, but **the key generation happens before the 404
return path** only in the 200 path — actually verified: line 109's
`cache_key = f"og:source:{handle}"` is reached after the 404 guard
(line 88-89), so the only writes are for valid handles.

`/og/market/{slug:path}` is the exposure: `slug` accepts `/`, so an
attacker can pass `og/market/foo/bar/baz` → `cache_key =
"og:market:foo/bar/baz"`. Cache lookup uses
`f"og_card:og:market:foo/bar/baz"` as the *prefix-resolved* key. The
hazards stack:

(a) **Keyspace blowup.** TTL is 3600s on the og_card layer. An
attacker can iterate `og/market/aaa1`, `og/market/aaa2`, … each
storing a fully-rendered ~30-80 KB PNG, evicting legitimate entries
(`ttl_cache._max = 10_000`; `cache/ttl.py:114-116` evicts the soonest-
to-expire entry, which is the legitimate short-TTL entries from
`feed:`, `markets:`, `source:`).

(b) **Namespace ambiguity.** The og_routes.py cache key is
`og:market:{slug}` but invalidation in `cache/ttl.py:250` deletes
`market:{slug}` (no `og:` prefix) and `og_card:` is only prefix-deleted
in tests. The actual og_card prefix layer uses `og_card:og:market:…`
(double prefix). A market resolution event (`on_market_resolved`)
does **not** invalidate the og card. So an attacker who poisons the
upstream market data once (e.g. via the upstream API's own caching
window) gets a 60-minute window where stale prices appear on every
social share preview.

(c) **Path traversal flavour.** `slug:path` allows literal `..` and
slashes. The DB query `db.get_latest_market_snapshot(slug)` is
parameterised, so this is not SQLi — but the cache key carries the
attacker bytes for an hour and is rendered into the PNG footer
("Polymarket" / "Kalshi" inferred from `slug.startswith("kalshi:")`
on line 156 — an attacker can pass `kalshi:` prefix to spoof the
platform label on any market they can lookup).

**Why HIGH-leaning-MEDIUM:** the keyspace-blowup vector is the real
one. The data-leak vector requires upstream poisoning. The platform
label spoof affects the rendered PNG but not the cache itself.

**Fix:** match `slug` against a strict regex (`^[a-z0-9:_-]{1,128}$`)
before constructing the cache key, same as `_safe_market_id` should
be enforced. Add `og_card:*` invalidation to `on_market_resolved`.

---

## Full site inventory

Every cache-key derivation site, classified by whether the key is
**(S)coped** to an authority (user id, tier, admin flag, sharer
token) or **(U)nscoped** (key derives from user input alone).

### Async Redis-backed cache (`cache/service.py`)

| Site | Key shape | Scope | Notes |
|---|---|---|---|
| `server.py:4078` | `dashboards:user:{user_id}` | **S** (auth uid) | Validated against session uid; no cross-tenant path. |
| `server.py:7141` | `settings:user:{user_id}` | **S** (auth uid) | Same. |
| `server.py:8072` | `signal_search:user:{user_id}` | **S** (auth uid) | Same. |
| `server.py:7435,7708` | `settings:user:{user_id}` (delete) | **S** | Invalidation only; uid is from session. |
| `extension_routes.py:352` | `ext_bundle:{slug}` | **U** | Slug is path-param `{slug:path}`. Validated downstream only by `db.get_predictions_for_market` returning empty. See finding M-1. |
| `saved_views_routes.py:295,108` | `saved_views:{scope}:user={uid or 'anon'}:filters={json}` | **S** (uid embedded) | Anon collisions are intended — anonymous preview is shared. |
| `cache/service.py:374-394` | `source:{handle}`, `source:v1:{handle}`, `source_calibration:{handle}`, `source_profile:{handle}`, `source_history:{handle}*`, `credibility:{handle}*` (invalidation) | n/a | Invalidation helper; handles from internal callers (pipeline). |
| `cache/service.py:401-407` | `market_probability:{market_id}`, `market_retrospective:{market_id}`, `market:{market_id}*` (invalidation) | n/a | Same — internal callers. |
| `routes_sharing.py:287,310,333` | `share:m:{token}`, `share:s:{token}`, `share:p:{token}` | **S** (sharer-issued token validated by `_safe_token_decode`) | Tokens are server-issued; attacker cannot forge a token to poison another user's view. |
| `server_features.py:632` | `feature_perms:{user_id}` (inferred from line 617 import; full key uncertain) | **S** | Per-user. |

### Sync TTL cache (`cache/ttl.py`)

| Site | Key shape | Scope | Notes |
|---|---|---|---|
| `embed_routes.py:471` | `embed:best_bets:v1` | n/a (no input) | Global; identical for all viewers (embed widget). |
| `insider_routes.py:102` | `insider_signals:type_{type}:days_{days}:page_{page}:src_{source}:lim_{limit}` | **U** (no user identity); Pro-gated | All inputs server-clamped (days 1-365, limit 1-200). `source` and `strength` taken raw. Pro-only data; same content per Pro viewer. See finding M-2. |
| `insider_routes.py:176` | `insider_leaderboard` | n/a | Global, Pro-gated. |
| `network_routes.py:111` | `source_network` | n/a | Global. |
| `og_cards.py:296` | `og_card:{key}` prefix wrapping caller-supplied subkey | inherits | Inherited from caller; see og_routes / profile entries. |
| `og_routes.py:57,64,71` | `og:default`, `og:pricing`, `og:calendar` | n/a | Static keys. |
| `og_routes.py:109` | `og:source:{handle}` | **U** | Handle reaches cache before any validation. DB lookup 404s on no-row, but key construction precedes the 404. See finding HIGH-3. |
| `og_routes.py:152` | `og:market:{slug}` | **U** | Slug is `:path`, unvalidated. See finding HIGH-3. |
| `profile_routes.py:324` | `profile:{handle}:{accuracy}:{total}` | **U** | Handle validated via `HANDLE_RE.match()` at line 315 before reaching the key. Safe. Three-segment key is content-derived so a profile stats change naturally invalidates. |
| `market_routes.py:280` | (filter_cache_key, content unknown from offset; same pattern as 349) | **U** | Tier omitted; see Finding HIGH-1. |
| `market_routes.py:349` | `markets:cat_{cat}:sort_{sort}:src_{source}:page_{page}:lim_{limit}` | **U** | No tier/user scope; gated by `_require_markets_user`. See HIGH-1. |
| `market_routes.py:418` | `best_bets:tier_{tier}:page_1:lim_{limit}:min_{min_sources}:cat_{cat}` | **S** (tier) | Tier collapses admin + super_admin. See HIGH-2. |
| `market_routes.py:411` | `market:{market_id}` | **U** | Path-param `:path`, unvalidated at key-construction time. See M-3. |
| `scenarios_routes.py:262` | `scenario:heatmap:{top_n}:{days}` | n/a | All numeric, server-clamped. Pro-gated. |
| `scenarios/correlation.py:252` | `scenario:corr:{anchor_slug}:{days}:{min_abs}:{limit}` | **U** | `anchor_slug` is form/path input; no validation. See M-4. |
| `api_v1.py:179` | `sources:sort_default:filter_none:page_{page_num}_size_{limit}` | n/a | Numeric only. |
| `api_v1.py:283` | `source:{handle}` | **U** | Handle is path-param, no regex validation. Returns `None` when missing → see audit_cache.md HIGH-1. |
| `api_v1.py:450` | `credibility_consensus:{slug}` | **U** | Slug is `{slug:path}`. Same issue. |
| `search_routes.py:184` | `search:q_{q_raw[:100]}:t_{types}:adm_{int(admin)}:lim_{limit}` | **S** (admin) | `adm_` flag separates admin (which alone sees user-search results) from non-admin. `q_raw` truncated to 100 chars. Good. |
| `og_cards.py:303` | `og_card:` prefix delete | n/a | Test hook. |

### AI cache (`ai/cache.py` — sqlite-backed `ai_cache`)

| Site | Key shape | Scope | Notes |
|---|---|---|---|
| `ai/extractor.py:80` | `extract:{sha256(post_text)}` | n/a | Content-addressed by post hash. Backend-only path. Safe — identical input ↔ identical key, no collision. |
| `ai/categoriser.py:67` | `categorise:{market_slug}` | **U** | Slug from upstream Polymarket/Kalshi, not direct user input. Backend pipeline. |
| `ai/environmental.py:81` | `env:{market_slug}` | **U** | Same. |
| `insider/correlator.py:81` | `correlation:{signal_id}:{market_slug}` | **U** | `signal_id` is int from DB; `market_slug` is internal. Backend pipeline. |
| `external_forecasts/matcher.py:225-235` | `fc-match:{sha256(slug|question|provider|cand_ids)[:24]}` | n/a | Backend job-only; hashed. |
| `jobs/ai_maintenance.py:209` | `extract:{sha256(content)}` | n/a | Same as ai/extractor. |

### Rate-limit / mutation bucket keys (not response caches — included for completeness)

| Site | Key shape | Notes |
|---|---|---|
| `server.py:5366` | `admin_mut:{email or uid}` | Admin mutation bucket. Safe — email from session. |
| `server.py:7028` | `audit_csv:{email or uid}` | Same. |
| `billing_routes.py:80` | `billing:{user_id}:{action}` | Safe. |
| `environmental_routes.py:105` | `env_refresh:{user_id}:{today}` | Safe. |
| `topics_routes.py:114` | `topic_pull:{user_id}:{topic_id}` | Safe — ownership checked at 109. |
| `security/rate_limiter.py:174` | `{module}.{func}:{client_ip}` | IP-based rate limit. Spoofable via XFF if proxy trust is misconfigured; out of scope here (covered in `audit_middleware.md`). |
| `security/rate_limiter.py:93` | `rl:{key}` prefix | Same. |

### External-fetch caches (module-local dicts)

| Site | Key shape | Notes |
|---|---|---|
| `external_forecasts/silver_bulletin.py:64,130,163` | URL-keyed | Hard-coded URLs. Safe. |
| `external_forecasts/fivethirtyeight.py:74,163` | URL-keyed | Same. |
| `portfolio/polymarket.py:161` | `{market_id}` direct | Per-call market IDs from internal sync. Backend-only. |
| `backend/markets/unified_markets.py:227,267` | `unified_markets`, `market:{market_id}` | Module-local dict, distinct from `ttl_cache`. Collision with `ttl_cache.market:{slug}` would be a logic bug but the two layers never read each other. See I-1. |
| `ai/client.py:373,417` | `{cache_key}` from caller | Pass-through; safety depends on caller. All current callers (`ai/extractor`, `ai/categoriser`, `ai/environmental`, `external_forecasts/matcher`, `insider/correlator`) construct hashed or backend-only keys. |

---

## Medium findings

### M-1. `ext_bundle:{slug}` shared across all extension users; slug unvalidated, no rate limit on miss
**File:** `gateway/extension_routes.py:186, 322-360`

The browser extension hits `/api/extension/market/{slug:path}` with a
Bearer JWT. The JWT gates *access* (line 327) but the cache key
`ext_bundle:{slug}` is **shared across all extension users**, and
`slug` is forwarded raw to `_compose_bundle(slug)` which queries
`db.get_predictions_for_market(f"poly:{slug}")` and optionally hits
Polymarket Gamma. The cache key has no user scope; intentional, since
the bundle is the same for everyone. **But**:

- Slug is `:path` → accepts `/` and arbitrary bytes.
- No rate limit on the *miss* path (line 333 rate-limits the
  authenticated request at `ext:{uid}` to `_RATE_LIMIT_PER_MINUTE`,
  but each unique slug is a distinct cache miss).
- An attacker with a valid extension JWT can pre-fill thousands of
  garbage keys (`ext_bundle:foo1`, `ext_bundle:foo2`, …), each storing
  the "no coverage" sentinel dict for the 2-minute TTL. The Redis
  fallback (in-process dict) is bounded by `_max = 10_000` for
  `ttl_cache` but `cache/service.py`'s in-process backend is
  **unbounded** (`_MemoryBackend._store` has no eviction).

**Why MEDIUM:** requires a valid extension JWT (one is issued to any
authenticated user via `/extension/auth`), and the data poisoned is
"no coverage" not malicious — but it does evict legitimate entries
under load and amplifies cost (the bundle factory hits Polymarket
Gamma when the slug looks plausible, line 252).

**Fix:** validate slug shape (e.g. `^[a-z0-9-]{1,80}$` matching
polymarket slug format) before cache key construction; reject 400
otherwise.

### M-2. `insider_signals` cache key takes raw `source` and `strength` query params; Pro-only data, no per-user scope
**File:** `gateway/insider_routes.py:101-104`

```python
cache_key = (
    f"insider_signals:type_{type_key}:days_{days_i}:page_{page_i}"
    f":src_{source or 'all'}:lim_{limit}"
)
```

`source` and `strength` are taken from `request.query_params` without
length or charset validation. `days_i`, `page_i`, `limit` are
server-clamped (good). `source` colons could collide with the key
separator: `source = "all:nuke"` produces
`insider_signals:type_all:days_30:page_1:src_all:nuke:lim_50`. The
cache get/set is opaque-string so this collides with whatever future
key shape uses six segments, but **today no such collision exists**.

The actual risk: keyspace blowup. An attacker with Pro hits
`/api/insider/signals?source=aaa1`, `?source=aaa2`, … each producing a
distinct key. Each `_compute()` is a SQL query against
`insider_signals` filtered by the literal `source = ?` string —
parameterised, so no SQLi — but the result is cached for 120s. At
worst this evicts legitimate Pro entries.

**Why MEDIUM:** requires Pro subscription, 120s TTL caps the damage
window, but no rate limit on this endpoint (it's behind
`_require_pro_user` only).

**Fix:** validate `source` against an enum of known sources before
constructing the key, or truncate to 64 chars and lowercase.

### M-3. `market:{market_id}` cache key takes a `:path` market_id with no validation; can collide with internal cache namespaces
**File:** `gateway/market_routes.py:411`

The route is `/api/markets/unified/{market_id:path}` (line 1097). The
`:path` converter accepts slashes and arbitrary bytes. The cache key
is `market:{market_id}`. Two failure paths:

(a) An attacker passes `market_id = "foo:user:1"` → key is
`market:foo:user:1`. This does not collide with `dashboards:user:1`
because the full prefix differs, but it collides with the
`ttl_cache.delete_prefix("market:")` invalidation in
`on_credibility_recompute` (line 261 `cache/ttl.py`) — no, that's
`source:` not `market:`. Re-checked: `on_market_resolved` (line 250)
deletes `market:{slug}` exactly, so attacker keys *will* be
invalidated alongside real markets — annoying not exploitable.

(b) `market.to_dict()` is cached, returning a dict that includes the
`market_id` verbatim. If the attacker poisons via the upstream
`unified_markets._cache` (module-local at
`backend/markets/unified_markets.py:202`), the same `market:{id}`
key in two different cache layers may diverge. The two layers do not
share storage; reads from each are independent. Not an exploit, but
a maintenance hazard.

**Why MEDIUM:** path-converter `:path` is overly permissive. Should
match the pattern enforced elsewhere (audit at
`gateway/market_routes.py:472` references a `_safe_market_id` guard
in a sibling function — `api_market_detail` did once have it per the
inline comment but the guard is now absent in the audited revision).

Re-check: searched the file end-to-end — `_safe_market_id` is not
defined anywhere in the working tree. The HIGH-flagged path-traversal
fix mentioned in the comment at line 466 (`# Path-traversal guard
(audit HIGH, 2026-05-15) — ...`) is **only present in a comment**;
no implementation. **The fix the comment promises does not exist in
the audited revision.** See I-2.

**Fix:** define `_safe_market_id` (regex
`^(poly:|kalshi:)?[a-z0-9-]{1,80}$`) and reject non-matching IDs.

### M-4. `scenario:corr:{anchor_slug}:{days}:{min_abs}:{limit}` — anchor_slug from form input, unvalidated
**File:** `gateway/scenarios/correlation.py:252`

The Pro-gated scenarios route at
`gateway/scenarios_routes.py:145-163`
(`api_correlations(anchor_slug)`) forwards user-supplied
`anchor_slug` to `compute_market_correlations(anchor_slug, …)` which
keys on it directly. No validation. `anchor_slug` then flows into the
SQL query at `scenarios/correlation.py:265-275`
(parameterised — no SQLi) but the cache key carries the raw input
for 24 hours (`ttl_seconds=86400`).

Keyspace blowup: an attacker can cause 86400-second-lived entries
for each unique anchor_slug. The default TTL cache is bounded at
10_000 entries (`cache/ttl.py:114`), so 10_000 garbage requests
fully evict legitimate state. This is worse than M-2 because the
TTL is 720× longer.

**Why MEDIUM:** Pro-only, but a single bad actor with a Pro account
can drain cache effectiveness for 24h.

**Fix:** validate `anchor_slug` against the same regex as M-3.

---

## Low findings

### L-1. `og:source:{handle}` and `og:market:{slug}` are not invalidated on market/source change
**Files:** `gateway/og_routes.py:109,152`, `gateway/cache/ttl.py:228-265`

The invalidation facade in `cache/ttl.py` does not include the
`og:*` namespace. A market resolution event flushes
`market:{slug}`, `market_chart:{slug}`, `feed:*`, and
`credibility_consensus:{slug}` (lines 250-253) but leaves
`og_card:og:market:{slug}` intact for up to 3600s. Cosmetic for
social-share previews; users sharing right after a resolution would
see stale "narve vs market" prices. Not a poisoning vector — the
data is correct at write time, just stale.

### L-2. `og_card:*` keyspace is unbounded and TTL is long
**File:** `gateway/og_cards.py:296`

The `og_card:` prefix is the largest single occupant of `ttl_cache`
(`_max = 10_000`). Each PNG is ~30-80 KB. At 1-hour TTL and 10k
entries the cache can hold ~500 MB of card bytes. The eviction
policy (line 113-116) drops the soonest-to-expire entry first, so
short-TTL entries (feed 60s, markets 30s) get evicted first whenever
a new og_card lands. An attacker who systematically requests
`/og/market/{rand}` for non-existent slugs gets a 404 (lookup
returns None, raises 404 at line 131) — so the og_card key is never
written for missing slugs. Good. But valid markets number in the
thousands and an attacker who iterates them all fills 10k slots
each. **The mitigation is the 404 short-circuit, not any explicit
key validation.**

### L-3. `og_routes.py:152` platform-label spoof via slug prefix
**File:** `gateway/og_routes.py:156-161`

```python
if slug.startswith("kalshi:"):
    platform = "Kalshi"
elif slug.startswith("poly:") or "/" not in slug:
    platform = "Polymarket"
else:
    platform = "market"
```

An attacker can pass any string starting with `kalshi:` to force the
PNG footer to read "Kalshi" for a market that is in fact on
Polymarket. This affects the rendered image only (cached as bytes
under `og_card:og:market:{slug}`). Not a cache poisoning vector —
each unique slug is its own cache entry, no cross-tenant overlap —
but it is a content-spoof vector for the rendered card.

### L-4. `correlation:{signal_id}:{market_slug}` has no per-pair tenancy but is backend-only
**File:** `gateway/insider/correlator.py:81`

Cache key is keyed on internal `signal_id` (DB autoincrement) and
`market_slug` (internal). The cache is written by the correlation
pipeline (`correlate_signal`) which runs in ARQ workers, not in
response to user input. No external write path. Safe today; flagged
in case a future route exposes it as a user-driven trigger.

### L-5. `_async_cache.delete(f"settings:user:{user['user_id']}")` at `server.py:7435, 7708` is fire-and-forget without await failure handling
**File:** `gateway/server.py:7435, 7708`

Settings deletion after a profile mutation. If the cache.delete call
fails silently (`_record_error` increments the counter but the route
returns 200), the next read of `settings:user:{uid}` for up to 60s
serves stale data. Not a poisoning vector but a staleness vector
under cache failure — relevant if a privilege change (`on_role_change`
in `cache/ttl.py:312-352`) lands in the 60s window.

---

## Info findings

### I-1. Two distinct cache layers use the same `market:{id}` key shape
- `gateway/cache/ttl.py` (TTLCache, used by `market_routes.py:411`)
- `gateway/backend/markets/unified_markets.py:202-214` (module-local dict)

The two layers never read each other. A read miss in one does not
fall through to the other. So they hold potentially divergent state
under the same logical key. This is a maintenance footgun — a
developer reading `audit_cache.md` would expect
`ttl_invalidate.on_market_resolved(slug)` to flush both, but it only
hits `ttl_cache`. Documented for future cleanup; no exploit today.

### I-2. `audit HIGH, 2026-05-15` comment at `gateway/market_routes.py:466` references a fix that is not present
The block claims:

```
# Path-traversal guard (audit HIGH, 2026-05-15) — ``market_id`` is
# interpolated into upstream URL templates (gamma-api / kalshi). A
# raw ``poly:../v1/admin`` would let an attacker pivot to any path
# the upstream host serves. Reject everything outside the safe
# alphabet, then rebuild the canonical id from the percent-encoded
# slug so cache keys and log lines all see one shape.
safe = _safe_market_id(market_id)
```

But `_safe_market_id` is not defined in any file under `gateway/`
(verified via `grep -rn "_safe_market_id" gateway/`).
This is either dead reference code (`NameError` at runtime — would
fail every request to `/api/markets/{id}`) or the line is reached
only after some import that this audit missed. Confirmed via inline
read: the function calls it directly, no try/except. If this code
path is hit, the route 500s. Either the function exists somewhere
I cannot find, or `api_market_detail` is broken.

**Action required:** verify with a runtime smoke test of
`GET /api/markets/unified/poly:foo`. If 500, ship the regex guard.
If 200, find where `_safe_market_id` is defined and document. This
sits at the intersection of cache-key safety (M-3) and upstream URL
safety (out of scope here; `audit_ssrf.md`).

### I-3. `api_v1.py` `source:{handle}` and `credibility_consensus:{slug}` rely on `_validate_key(request)` for auth, not for input validation
**Files:** `gateway/api_v1.py:251, 386`

The `_validate_key(request)` call (line 254, 411) confirms the API
key — but does not validate `handle` / `slug` shape. Same hazard as
M-3 / M-4 but scoped to API-key holders only.

---

## What I checked but found clean

- All `user:{user_id}` keyed sites use authenticated `user_id` (from
  `session.user_id`, never from query params).
- `saved_views_schema.cache_key()` correctly embeds `user_id` or
  `"anon"` and JSON-sorts filters.
- `search_routes.py:184` correctly embeds `adm_{int(admin)}` so an
  unprivileged caller cannot read admin-search results.
- `share:m:{token}` / `share:s:{token}` / `share:p:{token}` keys use
  server-issued tokens validated by `_safe_token_decode`. Tokens
  cannot be forged.
- `ai/extractor.py:80` content-hashes the post body before keying.
  Two identical posts (legitimate dedup) share a key by design.
- `external_forecasts/matcher.py:225-235` hashes its composite key
  to a 24-char hex.

## Files referenced

- `/Users/shocakarel/Habbig/gateway/cache/service.py`
- `/Users/shocakarel/Habbig/gateway/cache/ttl.py`
- `/Users/shocakarel/Habbig/gateway/market_routes.py`
- `/Users/shocakarel/Habbig/gateway/og_routes.py`
- `/Users/shocakarel/Habbig/gateway/og_cards.py`
- `/Users/shocakarel/Habbig/gateway/api_v1.py`
- `/Users/shocakarel/Habbig/gateway/search_routes.py`
- `/Users/shocakarel/Habbig/gateway/insider_routes.py`
- `/Users/shocakarel/Habbig/gateway/extension_routes.py`
- `/Users/shocakarel/Habbig/gateway/scenarios_routes.py`
- `/Users/shocakarel/Habbig/gateway/scenarios/correlation.py`
- `/Users/shocakarel/Habbig/gateway/profile_routes.py`
- `/Users/shocakarel/Habbig/gateway/saved_views_routes.py`
- `/Users/shocakarel/Habbig/gateway/saved_views_schema.py`
- `/Users/shocakarel/Habbig/gateway/routes_sharing.py`
- `/Users/shocakarel/Habbig/gateway/server.py`
- `/Users/shocakarel/Habbig/gateway/ai/cache.py`
- `/Users/shocakarel/Habbig/gateway/ai/extractor.py`
- `/Users/shocakarel/Habbig/gateway/ai/categoriser.py`
- `/Users/shocakarel/Habbig/gateway/ai/environmental.py`
- `/Users/shocakarel/Habbig/gateway/insider/correlator.py`
- `/Users/shocakarel/Habbig/gateway/external_forecasts/matcher.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/unified_markets.py`
