# Service Worker Audit — `gateway/static/sw.js`

**Date:** 2026-05-15
**Scope:** `/Users/shocakarel/Habbig/gateway/static/sw.js` (408 lines)
**Auditor commit (HEAD):** `c16fe36` on `feature/platform-build`
**Related:** `gateway/server.py` (sw.js handler L1214-1227, offline route registration), `gateway/offline_routes.py` (offline shell at `/offline`), `gateway/static/narve-app.js` L31-40 (registration), `gateway/static/settings_offline.html` L86 (settings-page registration), `gateway/tests/test_pwa_v2.py` (existing PWA test suite).

## Severity counts

| Severity | Count |
|----------|-------|
| HIGH     | 1     |
| MEDIUM   | 3     |
| LOW      | 3     |
| INFO     | 3     |
| **Total findings** | **10** |

## Checklist (requested criteria)

| Criterion | Status | Notes |
|-----------|--------|-------|
| Scope is `/` root | PASS | Registration sites use `{ scope: '/' }` (`narve-app.js:33`, `settings_offline.html:86`). The SW file is served at `/sw.js` with the `Service-Worker-Allowed: /` header (`gateway/server.py:1224`), so even if the SW URL were nested the scope upgrade is explicitly authorised. |
| No caching of `/admin/*` | PASS | `NEVER_CACHE_PREFIXES` (L61-69) includes `'/admin/'` and `'/api/admin'`. The fetch handler hits this check before any cache strategy (L121-123) and returns early (does not call `event.respondWith`, so the request goes straight to the network with no SW involvement). |
| Cache version includes git SHA | FAIL | `CACHE_V = 'narve-v2'` is a hard-coded string (L22). There is no build-time substitution and no git SHA suffix. Static asset bundles use a stable filename (`/_gateway_static/narve-app.js`) with `Cache-Control: no-cache` only on the SW itself — assets are cache-first under `STATIC_CACHE`, so a deploy that changes `narve-app.js` without bumping `CACHE_V` will be served stale until the user hits Settings → Clear cache or until `CACHE_V` is manually bumped. See **HIGH-1**. |
| Offline fallback to `/offline.html` | PASS (with caveat) | The SW falls back to **`/offline`** (the FastAPI route in `gateway/offline_routes.py:35`), not the literal file `offline.html`. The route reads `gateway/static/offline.html` and returns it as `HTMLResponse`. Functionally equivalent; the requested filename is served behind a route. The SW also has an inline `<!doctype html>` worst-case fallback (L223-228). |
| No API mutations cached | PASS | `if (req.method !== 'GET') return;` at L115 unconditionally bypasses the SW for every non-GET. The single exception is `POST /api/predictions`, which is **not cached** — it is forwarded to the network and only on a `fetch()` rejection (true network failure) is the request body persisted to IndexedDB for background-sync replay. Server 4xx/5xx responses pass through unmodified. |

## Findings

### HIGH-1 — Cache version does not include git SHA; deploys can serve stale assets
**Lines:** 22 (`const CACHE_V = 'narve-v2';`)
**Evidence:** All four caches (`STATIC_CACHE`, `API_CACHE`, `IMG_CACHE`, `RUNTIME_CACHE`) derive their names from this literal. `STATIC_ASSETS` includes `/_gateway_static/narve-app.js`, `/_gateway_static/gateway.css`, and `/_gateway_static/mobile-a11y.css` — three files that are likely to change on every deploy. Once a client has these in `STATIC_CACHE`, `cacheFirst()` (L181-192) returns the cached copy and never revalidates until `CACHE_V` is bumped manually or the user clears cache.
**Impact:** After a deploy users may run mismatched HTML + JS for hours/days. UI bugs and feature flags may appear broken; security fixes to `narve-app.js` (e.g. CSRF token handling) won't propagate until the SW is re-installed. The activate-time eviction (L91-100) only fires when `CACHE_V` actually changes.
**Recommendation:** Inject the deploy SHA into the SW at request time. Easiest path: change `gateway/server.py:1214` to read `sw.js`, replace a sentinel token (e.g. `__CACHE_V__`) with `os.environ.get('GIT_SHA', 'dev')[:8]` or a startup-captured value, and return the substituted body with `Cache-Control: no-cache`. Then `CACHE_V` becomes e.g. `'narve-v2-abc1234'`. The existing test at `test_pwa_v2.py:155` already cross-checks the manifest version against `CACHE_V`, so update that test in tandem.

### MEDIUM-1 — `NEVER_CACHE_PREFIXES` check returns without `event.respondWith`, but cached HTML for an admin nav can still be served
**Lines:** 121-123, 147-152
**Evidence:** The never-cache early-return only fires before the strategy branches. However, the navigate-mode HTML strategy (`networkFirstWithOffline`, L209-230) caches **every** 200 HTML response into `RUNTIME_CACHE` unless the path matched a never-cache prefix earlier. Today `/admin/` is in the prefix list so admin HTML is fine. But the `NEVER_CACHE_PREFIXES` list is `startsWith`-only — a path like `/admins-dashboard` or a future admin-adjacent route under a different prefix (`/internal/`, `/staff/`, `/ops/`) is **not** covered and its HTML would be cached. This is a minor footgun for future routes.
**Recommendation:** Document the list as the source of truth and add a test asserting that any path returning admin/staff content is enumerated. Consider also matching on response headers (`Cache-Control: no-store` from the server) as a belt-and-braces check before writing to cache in `networkFirstWithOffline` and `staleWhileRevalidate`.

### MEDIUM-2 — `staleWhileRevalidate` and `cacheFirst` write `set-cookie`-bearing responses to cache
**Lines:** 187, 199, 213
**Evidence:** All three write strategies do `cache.put(request, fresh.clone())` whenever the response is 200, with no check on `Cache-Control: private`, `Vary: Cookie`, or presence of `set-cookie`. The Cache API itself strips `set-cookie` headers on `put()` (per spec), but a response with `Cache-Control: private, no-store` from the server is still cached anyway. Combined with the cache-first behaviour for static assets and the SWR for whitelisted API endpoints, a personalised response that the SW mistakes for a public read can leak to a different user on a shared device after logout.
**Mitigation already present:** `NEVER_CACHE_PREFIXES` covers `/api/auth`, `/api/admin`, `/api/billing`, `/auth/`, `/admin/`, `/billing/`, `/stripe/` (L61-69). The cacheable API list (L48-56) is explicitly public-data prefixes. Risk is bounded but not zero.
**Recommendation:** In all three write paths, check `fresh.headers.get('Cache-Control')` for `private` or `no-store` and skip the `cache.put`. Same for any response with `Vary: Cookie`. Costs ~3 lines per strategy.

### MEDIUM-3 — Background-sync queue replays POST with original headers; can replay stale CSRF token
**Lines:** 247-274 (queuing), 308-329 (replay)
**Evidence:** `queuePredictionOnFailure` serialises every header from the failed request (`headers: Array.from(request.headers.entries())`, L250) and `flushPendingPredictions` replays them verbatim (L312). The CSRF token in the header is the value at queue time. If the server rotates CSRF tokens (per `gateway/server.py:1245` the cookie is 2h max-age) the replayed request will be rejected on validation — silently dropped because the SW treats 4xx as "server refused, drop it" (L319-322). A user's offline prediction can disappear without feedback.
**Recommendation:** Strip and re-acquire the CSRF token on replay. On replay, read the current `pm_csrftoken` (or whatever the cookie is named) from `document.cookie` via a `Clients.matchAll()` + message round-trip, OR add a server endpoint `/api/predictions/replay` that accepts a special header from the SW and re-validates via the session cookie alone. Also log the drop so users know the submission was rejected (currently a 4xx vanishes with no UX surface).

### LOW-1 — Inline HTML fallback uses a non-breaking apostrophe escape that may render oddly
**Lines:** 226 (`'<h1>You’re offline</h1>...'`)
**Impact:** Cosmetic only. The unicode is intentional but worth noting it lives in a tertiary fallback that users rarely hit.

### LOW-2 — `image/avif` is in the regex but not in the precache list
**Lines:** 136 (`/\.(webp|png|jpg|jpeg|svg|gif|ico|avif)$/i`)
**Impact:** Fine functionally — runtime cache-first will pick them up. Note for consistency with `STATIC_ASSETS` (L32-41) which only includes PNGs and the favicon.

### LOW-3 — `withCacheHeaders` reads the entire response body into a `Blob` on every cache hit
**Lines:** 164-179
**Impact:** For large API responses (paginated feeds, signals list) this doubles memory use per request and adds a microtask. Not a hot path but noticeable on low-end mobile. Could be replaced with a header-only modification via the Response stream constructor — keeping a streaming response.

### INFO-1 — `Service-Worker-Allowed: /` is set on the response (defensive but unused)
**Lines:** `gateway/server.py:1224`
The SW is served from `/sw.js` (root), so the header is redundant — scope `/` is already the default. Harmless. Documented here so a future move of `sw.js` to a nested path doesn't surprise anyone.

### INFO-2 — No `skipWaiting` / `clients.claim` on install
**Lines:** comment at L5-7
The SW author deliberately did not call `self.skipWaiting()` at install; they only call it on receiving a `SKIP_WAITING` message (L284-287). This is correct behaviour, called out for the audit log: an in-flight session keeps its current assets until the next navigation. The trade-off is that a critical fix needs both a `CACHE_V` bump AND a forced reload UX in the app shell.

### INFO-3 — Push handler uses `data.url` without origin validation
**Lines:** 389, 397, 405
`payload.url` from the push payload is passed to `clients.openWindow(target)` without checking it is same-origin. Push payloads come from the narve.ai backend over an authenticated VAPID channel so an attacker would need to compromise the push key first; still, a strict origin check would be cheap defence-in-depth. Not in scope of this audit's criteria.

## Top 3 issues (ranked)

1. **HIGH-1** — `CACHE_V = 'narve-v2'` is hard-coded; bake the git SHA into the SW at serve time so deploys invalidate `STATIC_CACHE` automatically (currently relies on a manual constant bump or user "Clear cache" click).
2. **MEDIUM-3** — Background-sync replay sends the original CSRF header which may be expired (2h cookie rotation); 4xx replies are silently dropped, so offline predictions can vanish without user-visible failure.
3. **MEDIUM-2** — SW caches 200 responses unconditionally; add a `Cache-Control: private | no-store` and `Vary: Cookie` check before `cache.put` in all three write strategies to harden against personalised-response caching on shared devices.

## Files referenced

- `/Users/shocakarel/Habbig/gateway/static/sw.js` — the audited file (408 lines)
- `/Users/shocakarel/Habbig/gateway/server.py:1214-1227` — `/sw.js` route handler
- `/Users/shocakarel/Habbig/gateway/offline_routes.py:35-58` — `/offline` shell route
- `/Users/shocakarel/Habbig/gateway/static/offline.html` — the precached shell body
- `/Users/shocakarel/Habbig/gateway/static/narve-app.js:31-40` — SW registration in the main bundle
- `/Users/shocakarel/Habbig/gateway/static/settings_offline.html:78-91` — toggle-driven registration
- `/Users/shocakarel/Habbig/gateway/tests/test_pwa_v2.py` — existing PWA contract tests (CACHE_V vs manifest, never-cache list, IDB shape)
