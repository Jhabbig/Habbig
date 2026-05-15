# CORS audit — gateway/server.py

Scope: `gateway/server.py` (8674 lines, branch `feature/platform-build`).
Method: grep for `CORSMiddleware`, `allow_origins`, `allow_credentials`,
`Access-Control-Allow-*`, manual preflight short-circuits, and Origin
checks. Cross-referenced with all middleware registrations and the
sibling gateway package (`gateway/**/*.py`).

## Headline

`gateway/server.py` does NOT mount `fastapi.middleware.cors.CORSMiddleware`
at all. There is no `app.add_middleware(CORSMiddleware, …)` call, no
manual `Access-Control-Allow-*` header emission, and no OPTIONS
preflight short-circuit anywhere in the gateway package.

The gateway therefore relies on the browser same-origin policy plus
two defensive Origin checks (HTTP mutating CSRF middleware + WebSocket
upgrade handler) to keep cross-origin callers out. That is a defensible
posture for a same-origin SaaS — and arguably safer than a misconfigured
`CORSMiddleware` — but the original audit checklist (restrictive
`allowed_origins`, `allow_credentials=True` paired with specific origins,
preflight paths) does not literally apply because the middleware is absent.

## Severity counts

- Critical: 0
- High: 0
- Medium: 1
- Low: 2
- Info: 3

## Top 3 findings

### 1. [Medium] No CORSMiddleware — relies on browser SOP + Origin checks
**Where:** `gateway/server.py` (no occurrence of `CORSMiddleware`,
`allow_origins`, or `Access-Control-Allow-*` anywhere in the file or
the gateway package).
**What:** FastAPI/Starlette emits no `Access-Control-Allow-Origin`,
`Access-Control-Allow-Credentials`, `Access-Control-Allow-Methods`, or
`Access-Control-Allow-Headers` headers. Browsers will block any
cross-origin XHR/fetch from a non-allowed origin by default; that is the
desired outcome. The risk is implicit, not exploitative: a future
contributor may "fix" a CORS error from a partner integration by
adding a permissive middleware (e.g. `allow_origins=["*"]` +
`allow_credentials=True`, which Starlette would silently downgrade to
echoing the request Origin — a textbook credentialed-CORS bypass).
**Fix:** Add an explicit comment near the other `app.add_middleware`
calls (e.g. above `SecurityHeadersMiddleware` at L974) stating that
omission is deliberate and any future CORS exposure MUST use an
explicit whitelist drawn from `ALLOWED_DOMAINS`, never `"*"`. No code
change required today.

### 2. [Low] CSRF Origin check is production-only — dev cross-origin requests pass
**Where:** `gateway/server.py:1349-1362` (inside `CSRFMiddleware.dispatch`).
**What:** The secondary Origin/Referer check that compares
`request.headers["origin"]` against the request `Host` only runs when
`IS_PRODUCTION` is truthy:
```py
if origin and IS_PRODUCTION:
    ...
    if origin_host != req_host and _apex(origin_host) != _apex(req_host):
        return JSONResponse({"error": "Invalid origin"}, status_code=403)
```
The CSRF token comparison still runs in dev, so this is not exploitable
in practice — but it means E2E or staging environments that set
`PRODUCTION=0` will not catch cross-origin regressions before they ship.
**Fix:** Drop the `IS_PRODUCTION` guard on the origin comparison
(keep token validation as-is). The check is idempotent for same-origin
requests and only rejects clearly mismatched origins. Doing this in
follow-up work — not now, since this branch is feature/platform-build
and the task says pre-release is off-limits.

### 3. [Low] WS Origin allowlist derives from ALLOWED_DOMAINS but does not log denials with full context
**Where:** `gateway/server.py:8568-8589` (`websocket_proxy`).
**What:** The WebSocket upgrade handler explicitly validates the
`Origin` header against `ALLOWED_DOMAINS` and rejects empty Origin in
production — exactly right. The denial logs (`log.warning(...)`) record
origin + host but not the resolved subdomain key or the source IP.
Under a credential-stuffing or cross-site WS hijack attempt this makes
correlation with the rest of the audit log harder.
**Fix:** Append `sub=<key>` and the IP-hash already computed elsewhere
in this file to the two `log.warning` lines. Pure observability — no
behaviour change.

## Other findings (info)

- **[Info] No preflight-only routes are declared.** Because there is no
  CORSMiddleware, the wildcard catch-all at L8513-8517 accepts OPTIONS
  alongside every other method:
  `methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]`.
  OPTIONS requests therefore traverse the full middleware stack
  (Security, CSRF, Gate, Sessions, Impersonation, RateLimit, Logging,
  GZip) before being proxied. CSRF middleware treats OPTIONS as
  non-mutating (L1312: `is_mutating = method in ("POST", "PATCH", "PUT", "DELETE")`),
  so OPTIONS is not blocked there. Net effect: OPTIONS preflights
  from any origin reach the proxy and get whatever response the upstream
  produces (typically 405 or 404 absent CORS headers), which is
  acceptable. No preflight short-circuit is needed because no CORS
  response is expected.
- **[Info] `Cross-Origin-Resource-Policy: same-origin` and
  `Cross-Origin-Opener-Policy: same-origin`** are set on every response
  by `SecurityHeadersMiddleware` (L894, L899). These complement the
  absent CORS by blocking cross-origin reads via `<img>`/`<script>`
  probes and Spectre-class side channels. Good.
- **[Info] CSP `connect-src 'self' https: https://api.stripe.com`** at
  L920 allows the page to fetch from arbitrary HTTPS origins, which is
  wider than strictly needed for a same-origin SaaS — but unrelated to
  CORS server-side and noted only for completeness.

## Cross-references

- `SecurityHeadersMiddleware`: L947-973 (sets COOP/CORP).
- `CSRFMiddleware`: L1286-1395 (with Origin check at L1349-1362).
- `GateMiddleware`: L1466 (gate cookie enforcement, allows OPTIONS).
- WebSocket Origin check: L8568-8589.
- `ALLOWED_DOMAINS`: L71-73, derived from `config.json` domain +
  `domain_aliases`.

## Verdict

CORS exposure is currently zero because `CORSMiddleware` is not
mounted. The active defenses — same-origin policy plus two explicit
Origin checks (HTTP mutating + WS upgrade) — are correct for a
single-origin SaaS surface. No critical or high findings. Recommend
adding a deliberate "no CORS" comment near the middleware stack
(item 1) so a future contributor cannot accidentally introduce a
credentialed wildcard. Items 2 and 3 are post-release hygiene.
