# Subproduct Dashboards — Shared-Secret HMAC Audit

**Scope:** All `~/Habbig/*-dashboard/` directories.
**Date:** 2026-05-15
**Audit run:** read-only — no code changes.

## What was checked

For each dashboard, three controls:

1. **HMAC middleware enforced** — `X-Gateway-Secret` verified against
   `GATEWAY_SSO_SECRET` for *every* request (not just at the per-route auth
   layer), using `hmac.compare_digest` for constant-time compare.
2. **Direct-origin blocked in prod** — server binds to `127.0.0.1` so it is
   only reachable through the local gateway reverse proxy, not exposed on
   `0.0.0.0` where attackers can hit the box's public IP and forge the
   gateway identity headers.
3. **Gateway SSO trust validated** — once HMAC passes,
   `X-Gateway-User-Id` / `X-Gateway-User-Email` / `X-Gateway-User-Tier` are
   read and used to gate paid/admin routes. Tier-rank checks present where
   relevant.

A dashboard passes only when **all three** are correct. `==` instead of
`hmac.compare_digest` is flagged as a finding (timing-leak surface).

## Per-dashboard results

### 1. annoyance-dashboard — PASS (with caveat)
- **HMAC:** Enforced via `auth.require_paid_user` / `auth.require_admin`
  called from `_guard_api(request)` on every `/api/*` route. Uses
  `hmac.compare_digest` (auth.py:75). **No global middleware** — relies on
  per-route enforcement, which is verified at every `@app.get/@app.post`.
- **Bind:** `127.0.0.1` (config.py:24), enforced by
  `auth.assert_bound_to_localhost` at startup (server.py:228, 747).
- **SSO trust:** `get_session_user` decodes gateway headers with tier check
  (`free`/`pro`/`super_admin`). `require_admin` additionally requires
  localhost origin (auth.py:138).
- **Caveat:** Per-route enforcement is brittle — adding a new `/api/*`
  route without calling `_guard_api` silently leaves it open. A global
  `@app.middleware("http")` would be belt-and-suspenders.
- **Not in gateway/config.json** — annoyance is not currently routed by the
  gateway (no `annoyance` key under `dashboards`). If/when it is added,
  the in-process HMAC check will activate.

Files: `/Users/shocakarel/Habbig/annoyance-dashboard/auth.py`,
`/Users/shocakarel/Habbig/annoyance-dashboard/server.py`,
`/Users/shocakarel/Habbig/annoyance-dashboard/config.py`.

### 2. centralbank-dashboard — PASS
- **HMAC:** Global `@app.middleware("http") async def gateway_auth`
  (server.py:679-693). `hmac.compare_digest` against `GATEWAY_SSO_SECRET`.
  Bypass list: `/health`, `/healthz`, `/favicon.ico`, `/manifest.webmanifest`,
  `/static/*`. `_DEV_MODE` only bypasses when secret is also unset.
- **Bind:** `BIND_HOST` default `127.0.0.1` (server.py:890).
- **SSO trust:** No per-user gating in this dashboard (read-only public data),
  but `/api/_sentry-test` checks `NARVE_ADMIN_EMAIL` against
  `x-gateway-user-email` and falls back to loopback (server.py:763-772).

File: `/Users/shocakarel/Habbig/centralbank-dashboard/server.py`.

### 3. climate-dashboard — **FAIL (critical)**
- **HMAC:** **None.** Flask app, no `before_request` handler, no middleware,
  no `GATEWAY_SSO_SECRET` reference anywhere. Every endpoint is fully open.
- **Bind:** `app.run(host="0.0.0.0", port=PORT, ...)` (server.py:1669).
  Listens on every interface, including the public one.
- **SSO trust:** Not implemented. Gateway-injected identity headers are
  ignored, so even if someone forwarded a valid user the dashboard cannot
  distinguish paid from free.
- **Severity:** Highest. The container is on a public port (7052 per
  Dockerfile EXPOSE 7052) and grants full read access to all paid endpoints
  to anyone who can reach the box's IP directly.

File: `/Users/shocakarel/Habbig/climate-dashboard/server.py`.

### 4. crypto-dashboard — **FAIL (multiple)**
- **HMAC:** Partial. `_get_session_user` checks `x-gateway-secret`
  (server.py:94-95) but uses `==` instead of `hmac.compare_digest`
  (timing-leak surface). Not enforced as middleware — only inside
  `_check_auth`, which is called per-route.
- **Bind:** `uvicorn.run(app, host="0.0.0.0", port=8000)` (server.py:1904).
  Direct origin not blocked.
- **SSO trust:** Reads `x-gateway-user-id` / `x-gateway-user-email` and
  promotes user to `"tier": "admin"` (server.py:103). Localhost bypass
  also grants admin (server.py:111-112) — anything on the same host is
  super-user.
- **Severity:** High. `==` is a timing-leak, but the bigger issue is the
  binding: a remote attacker who can reach port 8000 can simply set the
  cookie-based session OR replay the password (`CRYPTOEDGE_PASSWORD`
  fallback at server.py:78, default `"cryptoedge2024"`).

File: `/Users/shocakarel/Habbig/crypto-dashboard/server.py`.

### 5. disasters-dashboard — **FAIL (critical)**
- **HMAC:** Reads `GATEWAY_SSO_SECRET` env (server.py:69-70) but **never
  enforces it**. The string `x-gateway-secret` does not appear in the
  server.py at all. The warning on line 70 ("gateway-fronted requests will
  be rejected") is misleading — they will *not* be rejected; they will
  succeed unauthenticated.
- **Bind:** `app.run(host="0.0.0.0", port=PORT, ...)` (server.py:890).
- **SSO trust:** None.
- **Severity:** Highest. Flask app like climate; identical exposure
  pattern. Anyone who can reach port 7060 directly bypasses paywall.

File: `/Users/shocakarel/Habbig/disasters-dashboard/server.py`.

### 6. love-dashboard — PASS
- **HMAC:** Global `@app.middleware("http") async def gateway_auth`
  (server.py:104-118). `hmac.compare_digest`. Standard bypass list.
- **Bind:** `BIND_HOST` default `127.0.0.1` (server.py:544).
- **SSO trust:** `/api/_sentry-test` validates admin via gateway email
  (server.py:415-420) with loopback fallback.

File: `/Users/shocakarel/Habbig/love-dashboard/server.py`.

### 7. midterm-dashboard — **FAIL (multiple)**
- **HMAC:** Only inside `require_auth` (backend/main.py:142) — uses `==`
  not `hmac.compare_digest` (timing-leak). No global middleware. Routes
  that forget to depend on `require_auth` are open.
- **Bind:** `uvicorn.run(..., host="0.0.0.0", ...)` (backend/main.py:1097).
- **SSO trust:** Reads `x-gateway-user-id` and synthesises a `"tier":
  "pro"` user if no local row exists (backend/main.py:156-161). The admin
  user is provisioned by `deploy.sh` with password `changeme123!`
  (deploy.sh:43) — separate concern but worth flagging.
- **Severity:** High. Same exposure as crypto.

File: `/Users/shocakarel/Habbig/midterm-dashboard/backend/main.py`.

### 8. sports-dashboard — **FAIL (multiple)**
- **HMAC:** Only inside `get_current_user` (sports_dashboard.py:370) —
  uses `==` not `hmac.compare_digest`. No global middleware.
- **Bind:** `uvicorn.run(app, host="0.0.0.0", port=8888, ...)`
  (sports_dashboard.py:6065).
- **SSO trust:** Reads `x-gateway-user-id` / `x-gateway-user-email` and
  synthesises a user. `is_admin` flag is unrelated to gateway tier.
- **Severity:** High. Same exposure pattern as crypto/midterm.

File: `/Users/shocakarel/Habbig/sports-dashboard/sports_dashboard.py`.

### 9. stock-dashboard — **FAIL (critical, but not gateway-routed)**
- **HMAC:** **None.** No `GATEWAY_SSO_SECRET` reference anywhere. The
  server uses raw `http.server.HTTPServer`, no FastAPI/Flask.
- **Bind:** `HTTPServer(("0.0.0.0", args.port), ...)`
  (stock_dashboard.py:965).
- **SSO trust:** None.
- **Severity:** Highest if deployed. Note: not currently in
  `gateway/config.json` `dashboards` block, so it is likely a local/dev
  tool rather than a production subproduct. If it is ever surfaced via
  the gateway it must be rewritten with proper auth.

File: `/Users/shocakarel/Habbig/stock-dashboard/stock_dashboard.py`.

### 10. top-traders-dashboard — **FAIL (critical)**
- **HMAC:** **None.** No `GATEWAY_SSO_SECRET` reference. The file
  (185 lines) has no auth code at all.
- **Bind:** `uvicorn.run(app, host="0.0.0.0", port=PORT, ...)`
  (server.py:185).
- **SSO trust:** None.
- **Severity:** Highest. This **is** in `gateway/config.json` (key
  `top_traders`, target 8052, $12.99/mo). Subscribers pay; non-subscribers
  with direct IP access get it free.

File: `/Users/shocakarel/Habbig/top-traders-dashboard/server.py`.

### 11. voters-dashboard — PASS
- **HMAC:** Global `@app.middleware("http") async def security_and_auth`
  (server.py:123-153). `hmac.compare_digest`. `/healthz` is the only
  bypass.
- **Bind:** `BIND_HOST` default `127.0.0.1` (server.py:1480).
- **SSO trust:** `_user_from_request` strictly requires gateway headers,
  reviewer/admin role via email allowlist (server.py:80-94).

File: `/Users/shocakarel/Habbig/voters-dashboard/server.py`.

### 12. whale-dashboard — PASS
- **HMAC:** Global `@app.middleware("http") async def gateway_auth`
  (server.py:149-165). `hmac.compare_digest`. Standard bypass list.
- **Bind:** `BIND_HOST` default `127.0.0.1` (server.py:719).
- **SSO trust:** `_user_from_request` requires verified gateway headers.

File: `/Users/shocakarel/Habbig/whale-dashboard/server.py`.

### 13. world-health-dashboard — PASS
- **HMAC:** Global `@app.middleware("http") async def gateway_auth`
  (server.py:194-208). `hmac.compare_digest`. Standard bypass list.
- **Bind:** `BIND_HOST` default `127.0.0.1` (server.py:982).
- **SSO trust:** `/api/_sentry-test` validates admin via gateway email
  (server.py:843-848).

File: `/Users/shocakarel/Habbig/world-health-dashboard/server.py`.

### 14. world-state-dashboard — **FAIL (critical)**
- **HMAC:** **None.** Searching for `GATEWAY_SSO_SECRET`, `hmac`,
  `secret`, `auth`, `gateway` in the 3625-line server.py returns zero
  meaningful matches.
- **Bind:** `uvicorn.run(app, host="0.0.0.0", port=8070)` (server.py:3625).
- **SSO trust:** None.
- **Severity:** Highest. In `gateway/config.json` as `world` (target 7050,
  $5.99/mo). Wide-open subscriber data + no rate limit, no admin gate.
  Note the port mismatch too: gateway routes `world` to **7050** but the
  server hardcodes **8070**. Either it's running on a different port than
  the gateway expects (so the gateway returns 502) or somebody changed
  one without changing the other — worth verifying which is live.

File: `/Users/shocakarel/Habbig/world-state-dashboard/server.py`.

## Summary

**Inspected:** 14 dashboards.

**Pass (5):** annoyance, centralbank, love, voters, whale, world-health.
Counted as 6 in total — annoyance is "pass with caveat" because it relies
on per-route guards rather than middleware.

**Fail (8):** climate, crypto, disasters, midterm, sports, stock,
top-traders, world-state.

### Gap categories (worst first)

1. **No HMAC at all, binds 0.0.0.0** — climate, disasters, top-traders,
   world-state, stock. Anyone with the box's IP gets paid features for
   free. *Five dashboards.*
2. **HMAC uses `==` not `hmac.compare_digest`** — crypto, midterm, sports.
   Timing-leak surface; also all three bind 0.0.0.0 so the leak isn't even
   the worst problem.
3. **HMAC only at the per-route layer, no global middleware** — annoyance
   (mitigated by `_guard_api` being called in every reviewed route, and
   by 127.0.0.1 bind), crypto, midterm, sports. Risk surface: a new route
   that forgets to call the helper.

### Recommended fix pattern

The whale/love/world-health/voters/centralbank middleware is the canonical
shape:

```python
import hmac, os
from fastapi.responses import JSONResponse

_SSO_SECRET = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"
_AUTH_BYPASS_EXACT = {"/health", "/healthz", "/favicon.ico", "/manifest.webmanifest"}

@app.middleware("http")
async def gateway_auth(request, call_next):
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)
    if path in _AUTH_BYPASS_EXACT or path.startswith("/static/"):
        return await call_next(request)
    if _DEV_MODE and not _SSO_SECRET:
        return await call_next(request)
    if not _SSO_SECRET:
        return JSONResponse({"error": "service misconfigured"}, status_code=503)
    client_secret = request.headers.get("x-gateway-secret", "")
    if not hmac.compare_digest(client_secret, _SSO_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)
```

Plus bind to `BIND_HOST` defaulting to `127.0.0.1`.

The Flask dashboards (climate, disasters) need an equivalent
`@app.before_request` handler with the same logic.

The raw `http.server` dashboard (stock) should be rewritten on FastAPI or
left out of the gateway entirely.

## Out-of-scope notes

- `polymarket_weather_dashboard/` is routed in `gateway/config.json` as
  `weather` (target 5050) but lives outside the `*-dashboard/` glob the
  request specified, so it was not inspected here.
- `gateway/config.json` itself was read only to confirm routing intent;
  no audit of the gateway proxy logic is part of this report.
