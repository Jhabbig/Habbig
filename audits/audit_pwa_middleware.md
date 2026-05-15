# Adversarial audit — `gateway/pwa_middleware.py`

- File: `/Users/shocakarel/Habbig/gateway/pwa_middleware.py` (307 LOC)
- Date: 2026-05-15
- Auditor focus: HTML-injection in injected snippets (user-controlled values
  reaching innerHTML), CSP header consistency, cache-bust query param
  tampering, service-worker scope (must be `/` root, never sub-path),
  font-loading SSRF, hardcoded inline JS XSS surface.
- Method: full static read of the middleware + dependent modules
  (`subproduct.py`, `server.py` security middleware + static mount + sw/manifest
  routes, `static/sw.js`, `static/manifest.json`). Tracked every f-string
  interpolation back to its data source; mapped middleware-order to confirm
  which response headers reach the rebuilt `Response`; checked CSP against the
  exact resources the middleware injects.

## Severity counts

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High     | 0 |
| Medium   | 2 |
| Low      | 4 |
| Info     | 3 |

Total: 9 findings.

## Top 3 issues (by exploitability × blast-radius)

1. **M1 — `dict(response.headers)` flattens multi-valued headers on every
   HTML response that passes through the middleware.** Lines 296–305: the
   middleware buffers the body, then reconstructs the `Response` with
   `headers=dict(response.headers)`, dropping every duplicate header key.
   For text/html responses today this is mostly latent (CSRF / session
   cookies are set OUTSIDE PWA by middlewares added later, and the only
   text/html paths that emit multiple `Set-Cookie` are auth flows that
   return `RedirectResponse` (302) — short-circuited at line 284). It
   becomes a real bug the moment any HTML route handler calls
   `response.set_cookie(...)` twice on the same `HTMLResponse`, sets two
   `Link:` preload headers, or emits any other duplicated header. The
   failure mode is silent header loss, not an error.
2. **M2 — Cache-bust `?v=<mtime>` collides across simultaneous edits and
   never bumps on a `cp -p` / restore-from-backup.** `_asset_version()`
   (lines 37–42) keys on `int(p.stat().st_mtime)` — one-second resolution.
   Two edits within the same wall-clock second produce identical `v=`
   strings (deploy + hotfix scenario, or `make build` + immediate ratchet
   touching multiple files). Cloudflare's edge keys on the full URL incl.
   query, so it serves the stale bytes. The reverse fails too: any backup
   restore that preserves mtimes (`tar -p`, `rsync -t`, `cp -p`) emits an
   *older* `v=` than the version already in production caches — clients
   refuse to refetch the rolled-back file. There's no content-hash or
   build-id fallback; `_static_hash_cache` (used by `static_url()` in
   `server.py:790`) is the existing content-hash path but the middleware
   does not call it.
3. **M3 (Low, but worth surfacing as a top-3) — Idempotency sentinel
   `narve-pwa-head` is a literal substring `b'narve-pwa-head' in body`.**
   Any upstream response that happens to contain the string `narve-pwa-head`
   anywhere in the body — a copy-pasted PR description in admin shell, a
   comment in user-generated content rendered inside a profile / take page,
   or just `<meta name="description" content="see narve-pwa-head behaviour">` —
   suppresses the entire head injection. The check is unanchored and uses
   the comment literal verbatim. The same flaw applies to `narve-skip-link`
   (line 265) and `narve-og-` (line 235). Today this is theoretical (none of
   the strings is in any user-controlled corpus), but the check costs
   nothing to harden via a unique base64 token, and the absence of a
   regression test gives no early warning when it does collide.

## Findings — detail

### M1. Multi-valued response headers silently flattened — Medium
**Location:** `pwa_middleware.py:296-305`.

```python
headers = dict(response.headers)
# Content-Length has to be recomputed; let Starlette do it.
headers.pop("content-length", None)

return Response(
    content=new_body,
    status_code=response.status_code,
    headers=headers,
    media_type=ctype,
)
```

`response.headers` is Starlette's `MutableHeaders`, which is a list of
`(name, value)` tuples — `Set-Cookie`, `Link`, `WWW-Authenticate`,
`Proxy-Authenticate`, `Vary`, `Warning` etc. legitimately appear more than
once. `dict(...)` collapses to one value per key, last-write-wins.

The mitigations are currently lucky, not designed:

- The only text/html routes that emit duplicate `Set-Cookie` are auth flows
  (`logout` at line 3933 clears two cookies, then sets CSRF). Those return
  `RedirectResponse(status_code=302)` with `content-type: text/html` body of
  length 0 — but the body is technically text/html so line 284's
  `"text/html" in ctype` would NOT short-circuit if the redirect renders an
  HTML body. (FastAPI's `RedirectResponse` actually uses
  `media_type="text/html"` for the empty response body — verified.) So
  Starlette's redirect WITH duplicate `Set-Cookie` does flow through this
  branch. If gzip or any later middleware doesn't restore the redirect
  bypass first, header loss is realised.
- `CSRFMiddleware` (server.py:1352) sits OUTSIDE PWA in the middleware
  stack (added later → outer). The CSRF cookie is set after PWA returns the
  rebuilt response, so it survives the dict-flattening.
- `SecurityHeadersMiddleware` (server.py:931) is INSIDE PWA (added first →
  innermost). CSP, Cache-Control, X-Frame-Options etc. are already on the
  response when PWA buffers it. `dict()` preserves them because they're
  single-valued.

**Impact today:** medium-low. Latent until any route emits two values for
the same response header on an HTML body. **Fix shape:** copy headers
preserving the raw list, e.g.

```python
raw = MutableHeaders(raw=list(response.raw_headers))
raw.pop("content-length", None)
return Response(content=new_body, status_code=response.status_code,
                headers=dict(raw), media_type=ctype)
```

…or, better, drop the wholesale `Response()` rebuild and mutate
`response.body` + `response.headers["content-length"]` in place, returning
the original object. The current rebuild also discards `response.background`,
silently breaking any background-task scheduling on HTML responses.

### M2. Cache-bust `?v=<mtime>` is 1s-granular and reversible — Medium
**Location:** `pwa_middleware.py:37-50`.

```python
def _asset_version(rel_path: str) -> str:
    try:
        p = Path(__file__).parent / "static" / rel_path
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"
```

Failure modes:

1. **Sub-second double-write.** Two writes inside the same wall clock
   second produce the same `v=`. With Cloudflare keying on full URL incl.
   query, the second write never invalidates the edge cache. Hot-fix loops
   that rebuild and `touch` multiple assets land in this window routinely.
2. **mtime regression.** `tar -p`, `rsync -t`, `cp -p`, `git checkout`,
   `git restore`, container image layer extraction — all preserve a file's
   *original* mtime. After a rollback or a backup restore, the `v=` in the
   injected `<link>` decreases. Clients with cached `mobile-a11y.css?v=99999`
   refuse to refetch `?v=99998` (same URL → cached).
3. **`OSError` fallback returns `"0"`.** Boot-time stat failure (transient
   I/O, asset not yet uploaded by a partial deploy) bakes `v=0` into every
   subsequent injection for the process lifetime — no re-stat, no recovery.
   The value caches in `_MOBILE_A11Y_VER` etc. at import time.

**Impact:** medium for the cache-coherency property, low for direct
security. The existing `static_url()` in `server.py:793` already computes a
content-hash (MD5 of file contents, 8 chars) — using it here would close
all three failure modes for one extra hash per asset at boot.

### M3. Idempotency sentinels are unanchored substring matches — Low
**Location:** `pwa_middleware.py:225, 235, 265, 271`.

```python
if b'narve-pwa-head' not in body:        # 225
if b'og:image' not in body and b'narve-og-' not in body:  # 235
if b'narve-skip-link' not in body:       # 265
if _MAIN_OPEN in body:                   # 271
```

Any upstream HTML containing the substring (in a comment, in user content
rendered server-side, in an attribute value, in raw text) suppresses the
matching injection. Easy adversarial cases:

- `/changelog` rendered HTML that quotes the PWA commit message.
- Admin shell pages that show middleware source for debugging.
- A `<meta name="description" content="…narve-pwa-head…">` from any SSR
  template that mentions the middleware name.

Today no template contains any of these literals (verified by
`grep -r 'narve-pwa-head' gateway/`). The risk is regression — future
edits could trip the check silently, dropping the entire PWA head block
for that page. **Fix shape:** key the check on a unique 16-byte token
`narve-pwa-head:<random-hex>` regenerated per major version, AND add a
unit test that injects a page containing the literal string and asserts
the head block STILL renders (with a different sentinel).

### L1. Service worker scope is correct, but the registration is not
guarded for cross-origin embed contexts — Low
**Location:** `static/narve-app.js:33`, `pwa_middleware.py:198`.

```js
navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(...)
```

The SW is served from `/sw.js` (server.py:1192) with
`Service-Worker-Allowed: /` and `Cache-Control: no-cache`, controls the
full origin (scope `/`), and `manifest.json` declares `"scope": "/"`. All
correct against the brief.

Issue: `narve-app.js` is injected via `pwa_middleware._BODY_INJECT` into
**every** text/html response, including `/embed/*` widgets that are
designed to load inside a partner iframe. The embed CSP
(`EMBED_CSP_DEFAULT` at server.py:1006) does not list
`worker-src 'self'`, which would block SW registration — but the embed
route handler (`embed_routes.py:794`) installs its own CSP that may
override before the SW registration call runs. If a partner iframes a
narve embed page on their own origin, the SW registration call still
fires inside the iframe's same-origin context (narve.ai) but is gated by
the *site* CSP fetched from the parent narve response, not the embed
CSP — actual behaviour depends on which CSP the browser binds to the
iframe document. **Verify:** load `https://narve.ai/embed/<widget>` in a
DevTools network panel, check that `worker-src 'self'` is present in the
CSP delivered with the embed HTML, and confirm `/sw.js` is fetched or
silently blocked. **Fix shape:** skip `_BODY_INJECT` (or at least the
SW-registering scripts) when `request.url.path.startswith("/embed/")`.

### L2. Font preconnect / preload reaches Google Fonts unconditionally — Low
**Location:** `pwa_middleware.py:133-136`.

```python
'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
'<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@1'
'&family=Source+Serif+4:opsz,wght@8..60,200..900&display=swap" rel="stylesheet">\n'
```

Hardcoded URL, no user input → no SSRF surface in the strict sense
(`font-loading SSRF` per the brief is N/A). Tradeoff notes:

- Every text/html response — including auth funnels, admin pages, embeds
  meant to load in partner contexts, and gated `/gate` redirects — opens
  preconnects to Google. That's a 100% leak of "user visited narve.ai"
  to Google Fonts on every page, including pre-login. The CSP allows it
  (`style-src ... https://fonts.googleapis.com`, `font-src 'self'
  https://fonts.gstatic.com`), so it's intentional, but the privacy
  posture is worth flagging given the `Cross-Origin-Resource-Policy:
  same-origin` and HSTS preload-list submission listed in the security
  audit.
- The `family=` query is static. No `f'{}'` interpolation, no Host-header
  data, no slug. Confirmed by grep — the literal is exactly the bytes
  in the file.

**Fix shape (optional):** self-host both fonts under `_gateway_static/fonts/`
(GeistMono already is) and drop the Google Fonts link. Bonus: removes the
`preconnect` round-trips and the third-party CSP allow-listing.

### L3. og:image filename lookup uses unbounded subproduct dict, but is
trusted by construction — Low (informational)
**Location:** `pwa_middleware.py:237-255`.

```python
sub = subproduct_for_host(host)
if sub:
    slug = sub.get("slug")
    key = sub.get("dashboard_key")
    if slug and (_OG_DIR / f"{slug}.png").exists():
        og_block = _og_block_for_key(slug)
    elif key and (_OG_DIR / f"{key}.png").exists():
        og_block = _og_block_for_key(key)
```

Adversarial review of the path-traversal class: `slug` and `key` come from
`SUBPRODUCTS` dict literals in `subproduct.py` (verified — lines 42–320 are
all hardcoded strings). The Host header (line 293) is filtered through
`subproduct_for_host()` which returns `SUBPRODUCTS.get(first)` — only
canonical entries, none containing `..`, `/`, or null bytes. So
`(_OG_DIR / f"{slug}.png").exists()` cannot escape `_OG_DIR`. The
`_og_block_for_key()` URL interpolation (line 171) is also from the same
trusted set.

**No vulnerability today.** Flagged because the trust boundary depends
entirely on the discipline of `SUBPRODUCTS` staying a hardcoded literal.
A future PR that adds slugs from `config.json` or environment variables
without re-establishing the constraint silently makes this a path-traversal
or header-injection sink. **Fix shape:** validate `slug` against
`r'\A[a-z][a-z0-9_-]{0,30}\Z'` before the disk check + URL render, so the
guarantee is local to the file using the value.

### L4. `Response` rebuild drops `background` tasks on HTML responses — Low
**Location:** `pwa_middleware.py:300-305`.

```python
return Response(
    content=new_body,
    status_code=response.status_code,
    headers=headers,
    media_type=ctype,
)
```

`response.background` (Starlette's `BackgroundTask` / `BackgroundTasks`
attached via `FileResponse(..., background=...)` or `HTMLResponse(...,
background=...)`) is not copied into the new `Response`. Any text/html
route that schedules a post-response task is silently broken — the task
never fires. Not in active use today (verified by `grep -n
'background=' gateway/server.py`), but the failure is silent.

**Fix shape:** `Response(..., background=response.background)` or, again,
mutate-in-place rather than rebuild.

### I1. CSP allows the injected resources — Informational
The middleware injects:

- `<style>` inline block → `style-src 'self' 'unsafe-inline'` — allowed.
- `<link rel="stylesheet" href="/_gateway_static/...">` → `style-src 'self'`
  — allowed.
- `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?...">` →
  `style-src ... https://fonts.googleapis.com` — allowed.
- `<link rel="preload" as="font" href="/_gateway_static/fonts/...">`,
  `<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>`
  → `font-src 'self' https://fonts.gstatic.com` — allowed.
- `<script src="/_gateway_static/...">` (narve-app, shortcuts,
  shortcuts-discovery, feedback_button) → `script-src 'self'` — allowed.
- `<meta property="og:image" content="https://narve.ai/...">` → no CSP
  directive applies (meta tags consumed by social-card crawlers, not the
  browser fetch pipeline).

**No inline JS is injected by this middleware.** The only "inline JS XSS
surface" the brief asks about is therefore confined to whatever theme-init
or analytics blob lives in the per-page `<head>` templates — not in scope
here, and not added by `pwa_middleware`. The `<script>` tags injected have
`src` only, no inline body, no `nonce` and no `integrity` attribute.

The CSP and the injection set are coherent. The single coupling risk is
that *removing* `'unsafe-inline'` from `style-src` would break the inline
`<style>` block at line 60, even though the goal is FOUC defeat — that's
a known cost of inline critical CSS. Documented for the next CSP tightening
pass.

### I2. Sub-resource integrity (SRI) absent on all injected scripts — Info
**Location:** `pwa_middleware.py:198-209`.

```python
f'<script src="/_gateway_static/narve-app.js?v={_NARVE_APP_VER}" defer></script>\n'
f'<script src="/_gateway_static/shortcuts.js?v={_SHORTCUTS_VER}" defer></script>\n'
f'<script src="/_gateway_static/js/shortcuts-discovery.js?v={_SHORTCUTS_DISC_VER}" defer></script>\n'
f'<script src="/_gateway_static/feedback_button.js?v={_FEEDBACK_BTN_VER}" defer></script>\n'
```

All four scripts are same-origin (`/_gateway_static/`), so SRI is not
required for cross-origin tampering. It would still defend against
disk-tampering on the gateway host (an attacker with write access to
`static/` cannot poison the bundle without also editing the middleware
to mint a new hash). Same applies to `mobile-a11y.css`,
`narve-polish.css`, `narve-redesign.css`. Worth adding when bundling
gains a build step that can compute hashes at deploy time.

### I3. Service-worker scope verification — Info (no finding)
**Location:** `static/manifest.json:6` (`"scope": "/"`), `server.py:1201`
(`Service-Worker-Allowed: /`), `static/narve-app.js:33`
(`register('/sw.js', { scope: '/' })`).

All three agree: the SW controls the entire origin. The middleware does
NOT directly register the SW — it injects `narve-app.js`, which does. The
brief flagged this as something to verify ("scope must be `/` root, never
sub-path"). **Verified correct.**

The associated `start_url` in the manifest is `/dashboards` (line 5) —
that's a navigation-target choice, not a scope. Correct.

Cache version `narve-v2` (sw.js:22) is namespaced, and old caches are
cleared on `activate` (lines 91–99). No stale-cache leak across
deployments. `skipWaiting` is intentionally NOT called on install
(documented at lines 5–7), so in-flight tabs keep their existing assets
until the next navigation — also correct for the cache-coherency
property.

## Cross-cutting observations (not findings)

- **Middleware ordering review.** Verified `app.add_middleware` adds new
  middleware to the OUTSIDE of the existing stack. The effective
  response-side order (innermost → outermost) is:
  `SecurityHeaders → PWA → StagingProxy → CSRF → Gate → Subscription →
  HardenedSession → Impersonation → GlobalRateLimit → BulkData →
  LoggingContext → GZip → RequestTiming`. PWA buffers the body BEFORE
  gzip and AFTER security headers, which is correct for content
  injection (we need to inject into uncompressed HTML and we need CSP
  on the rebuilt response).
- **Streaming responses are forcibly buffered.** `async for chunk in
  response.body_iterator` (line 290) reads the entire body into memory
  before injecting. For very large HTML responses (admin tables, log
  dumps) this is a memory footprint multiplier vs. true streaming. Not
  a security finding — flagged as a perf / DoS-amplifier characteristic
  to track alongside `MAX_REQUEST_BODY` (1 MB) on the request side.
- **No Host header allow-list.** `TrustedHostMiddleware` is not
  registered (`grep -n TrustedHost gateway/server.py` → zero hits). The
  Host header reaches `subproduct_for_host()` unfiltered. In this
  middleware's specific code path, the only place Host data is used is
  to choose an OG card from a fixed list of disk files — safe. Flagged
  here because the *gateway-wide* absence of TrustedHost is a different
  audit's concern; mentioning so the PWA reader doesn't conclude "Host
  is filtered upstream."

## Verification commands run

```bash
# Confirm sentinel literals don't appear in any HTML template:
find /Users/shocakarel/Habbig/gateway -name "*.html" -exec \
  grep -l "narve-pwa-head\|narve-skip-link" {} \;
# (no matches)

# Confirm SUBPRODUCTS keys are static literals:
grep -n "SUBPRODUCTS\b" /Users/shocakarel/Habbig/gateway/subproduct.py

# Confirm Host trust path:
grep -n "subproduct_for_host\|SUBPRODUCTS.get" \
  /Users/shocakarel/Habbig/gateway/subproduct.py

# Confirm middleware-add order:
grep -n "app.add_middleware" /Users/shocakarel/Habbig/gateway/server.py

# Confirm SW scope / Service-Worker-Allowed:
grep -n "Service-Worker-Allowed\|register..sw\.js" \
  /Users/shocakarel/Habbig/gateway/server.py \
  /Users/shocakarel/Habbig/gateway/static/narve-app.js \
  /Users/shocakarel/Habbig/gateway/static/manifest.json

# Confirm no inline JS injected by middleware:
grep -n "script" /Users/shocakarel/Habbig/gateway/pwa_middleware.py
```

## Out of scope (per brief)

- No code changes proposed in this audit.
- Inline JS surfaces inside per-page templates (theme-init, analytics) are
  outside `pwa_middleware.py` — not audited here.
- The PWA push-notification path (`sw.js` `push` handler) accepts
  attacker-controlled JSON payloads but is exercised only through Web
  Push, which requires a VAPID subscription this codebase manages
  out-of-band; covered by a separate audit.
