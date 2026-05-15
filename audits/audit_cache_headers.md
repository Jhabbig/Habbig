# Static-Asset Cache Headers Audit

**Date:** 2026-05-15
**Scope:** Cache-Control / Vary / immutable headers on every static-asset response
served by the narve.ai gateway. Mount under `/_gateway_static/*`, plus the
root-served `favicon.ico`, `manifest.json`, `sw.js`, and the in-template
references that bypass the cache-bust helper.
**Branch:** `feature/platform-build`
**Method:** Synchronous read of `gateway/server.py`, route modules, every HTML
template under `gateway/static/`, the existing CDN config in
`gateway/CLOUDFLARE_CHANGES.md`, and the cache-header tests in
`gateway/tests/test_health.py` + `gateway/tests/test_foundation_bundle.py`. No
HTTP probes, no live curl (Cloudflare/origin are pre-release off-limits per
the run brief).
**Auditor focus:**
- `Cache-Control` correctness on long-lived assets under `/_gateway_static/*`
- Presence of the `immutable` directive
- Absence of `Cache-Control: private` on truly shared assets
- Cache-bust query strings everywhere a long-lived asset is referenced
- `Vary` correctness (compression / personalisation)

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0     |
| High     | 2     |
| Medium   | 3     |
| Low      | 4     |
| Info     | 3     |

Headline: the **core static mount is correct** — `_CachedStaticFiles` in
`gateway/server.py:799-813` attaches `public, max-age=2592000, immutable` plus
`Vary: Accept-Encoding` to every 200 response under `/_gateway_static/*`, and
no route in the codebase emits `Cache-Control: private` on a shared asset.

The real risk lives in the **clients of that mount**: 573 in-template references
to `/_gateway_static/...` (logo, fonts, several JS bundles) bypass the
`{{ static: … }}` content-hash helper and ship without a `?v=` query, so the
30-day `immutable` directive means a deploy that changes one of those files
leaves stale copies in browsers for a month. Per-user avatar uploads are
served from the same mount and inherit `immutable` even though the file at
`avatars/{user_id}.webp` is mutated in place — the timestamp cache-bust in
the URL saves the next page load, but every other client with the old URL
cached holds the previous avatar for 30 days.

---

## Top 3 findings

### 1. [HIGH] `immutable, max-age=2592000` applied to assets referenced without a `?v=` cache-bust — stale-for-30-days deploy hazard
**Locations:**
- Mount: `gateway/server.py:799-820` (`_CachedStaticFiles.get_response`)
- Helper that *would* fix it: `gateway/server.py:836-860` (`static_url`)
  and the `{{ static: <path> }}` substitution at `server.py:2491-2495`
- Unbust references: 573 hits of `/_gateway_static/...` in
  `gateway/static/*.html` that do not pass through `{{ static: … }}` or carry
  a hand-rolled `?v=` — sample:
  - `gateway/static/_base.html:27`  `fonts/Inter-Variable-subset.woff2` (preload)
  - `gateway/static/_base.html:31-32`  `img/logo.png` (favicon + apple-touch)
  - `gateway/static/_base.html:53`  `js/toast.js`
  - `gateway/static/403.html:37-38`, `account.html:77-78`, `about.html:111-112`,
    every `admin-*.html` etc.  `js/cmdk.js`, `js/share_menu.js`

The mount declares the response `immutable`. RFC 8246 says clients that see
`immutable` MUST NOT issue a conditional revalidation for the lifetime of the
freshness window. With `max-age=2592000` that's 30 days. Combined with the
"never change the path, change the query" deploy contract spelled out in the
class docstring, this is correct **only** when callers participate. They
mostly don't:

```
$ grep -rn "_gateway_static/" gateway/static/*.html | grep -v "{{ static\|?v=" | wc -l
573
```

Counter-example that does it right (also in `_base.html:28-29`):
```html
<link rel="stylesheet" href="{{ static: gateway.css }}">
```
This expands to `/_gateway_static/gateway.css?v=abc12345`, where `abc12345`
is `md5(file_bytes)[:8]`. Change the file, the hash rolls, browsers fetch the
new bytes immediately.

Concrete deploy hazard: ship a new `toast.js` with a bug-fix or
`Inter-Variable-subset.woff2` with an added glyph and 30 days of return
visitors keep the stale copy. `theme.js` already side-stepped this with a
hand-rolled `?v=2` at `_base.html:54`, which (a) proves the team noticed the
problem case-by-case and (b) is *itself* broken because `?v=2` is a global
literal that nobody updated when `theme.js` last changed.

**Fix:** route every `/_gateway_static/...` reference in every template
through `{{ static: <path> }}`. The substitution is implemented at
`server.py:2491-2495`; no new code needed. The 573 unbust sites are
mechanical to rewrite. Until that's done, lower the `immutable` directive
**or** the `max-age` on `_CachedStaticFiles` — keeping both is the worst of
both worlds.

---

### 2. [HIGH] Per-user avatars share the long-lived `immutable` policy of the static mount, with only a timestamp query for cache-bust
**Locations:**
- Mount that decorates them: `gateway/server.py:807-813`
- Writer + URL minter: `gateway/profile_routes.py:502-512`
- On-disk path: `gateway/static/avatars/{user_id}.webp`

A user uploading a new avatar overwrites the same file:
```python
out_path = _AVATARS / f"{user['user_id']}.webp"
img.convert("RGB").save(out_path, format="WEBP", quality=85, method=4)
avatar_url = f"/_gateway_static/avatars/{user['user_id']}.webp?v={int(time.time())}"
profile_q.update_avatar_url(user["user_id"], avatar_url)
```

The mount then serves the new bytes with:
```
Cache-Control: public, max-age=2592000, immutable
Vary: Accept-Encoding
```

`?v=…` is set to `int(time.time())`, not a content hash, and is persisted
into the DB. So every render of *that user's* profile picks up the new
timestamp and refetches — fine.

The hazard is **every other surface** that already cached the old timestamped
URL — comment threads, follower lists, leaderboards, OG preview cards,
in-flight email renders, indexed search results. They keep serving the
30-day-stale avatar with `immutable` set, so no revalidation request ever
fires. There is no `Vary: Cookie` to scope the cached representation to the
viewer, but that's fine — avatars are intentionally public. The real bug is
the `immutable` claim on a file that is, by definition, mutable.

`Cache-Control: private` would be **wrong** here (avatars are intentionally
shared via Cloudflare, and the audit confirms no asset on this mount carries
`private`) — but `immutable` is equally wrong for mutable per-user content.

**Fix:** carve avatars out of `_CachedStaticFiles` (serve via a dedicated
`@app.get("/_gateway_static/avatars/{user_id}.webp")` handler that returns
`public, max-age=86400, stale-while-revalidate=604800` *without* `immutable`),
or write avatars to content-addressed paths (`avatars/{user_id}-{hash}.webp`)
and update `avatar_url` on every upload. The latter also gives you free
deletion of the previous frame.

---

### 3. [MEDIUM] Root-level `/favicon.ico`, `/manifest.json`, `/sw.js` skip `Vary: Accept-Encoding`; `/sw.js` correctly opts out of long caching but the favicon's 7-day `max-age` lacks `immutable` *or* a revalidation fallback
**Locations:** `gateway/server.py:1207-1254`

These three are served by hand-rolled `FileResponse` handlers because they
have to live at the apex path, not under `/_gateway_static/`. The
`Cache-Control` they set:

| Path             | Cache-Control                | Vary  | Comment |
|------------------|------------------------------|-------|---------|
| `/favicon.ico`   | `public, max-age=604800`     | absent | 7-day, no immutable, no SWR. Browser still revalidates after expiry, but the missing `Vary: Accept-Encoding` means Cloudflare can serve a `Content-Encoding: br` body to a `Accept-Encoding: identity` client. |
| `/manifest.json` | `public, max-age=86400`      | absent | 24-hour, fine. Manifest is small; impact low. |
| `/sw.js`         | `no-cache`                   | absent | **Correct.** Service workers MUST revalidate so a deploy can replace SW logic. The `no-cache` (not `no-store`) lets the browser keep a copy and use `If-None-Match`/`If-Modified-Since`, which is the recommended pattern. |

`/sw.js` also sends `Service-Worker-Allowed: /` which is needed because the
file is at the apex, good.

**Fix (low cost):** add `"Vary": "Accept-Encoding"` to the headers dict on
all three handlers. Optionally upgrade `/favicon.ico` to
`public, max-age=2592000, immutable` and point it at a content-hashed path
emitted from `static_url("img/logo.png")` so a re-brand can propagate without
a 7-day delay.

---

## Findings summary table

| # | Severity | Where | Issue |
|---|----------|-------|-------|
| 1 | HIGH     | 573 template sites + `gateway/server.py:799-820` | `immutable, max-age=2592000` applied to assets referenced without `?v=` cache-bust |
| 2 | HIGH     | `gateway/profile_routes.py:502-512` + mount | Per-user avatars get `immutable` despite being overwritten in place |
| 3 | MEDIUM   | `gateway/server.py:1207-1254` | Apex `/favicon.ico` + `/manifest.json` + `/sw.js` lack `Vary: Accept-Encoding` |
| 4 | MEDIUM   | `gateway/static/_base.html:54` | Hand-rolled `?v=2` literal on `theme.js` instead of `{{ static: theme.js }}` — stale-forever footgun |
| 5 | MEDIUM   | `gateway/server.py:836-860` (`static_url`) | MD5 hash truncated to 8 hex chars (32-bit space). Collision probability is irrelevant for cache-busting, but a single-bit content change has 1 in 4.3 billion odds of producing the same hash and silently keeping the stale copy. Use first 16 chars or switch to `sha1` (`usedforsecurity=False`). |
| 6 | LOW      | `gateway/server.py:799-813` | `_CachedStaticFiles.get_response` decorates **every** 2xx (not just 200) — but only checks `== 200`. 206 partial-content responses for range requests (fonts, video, large PDFs) inherit no cache headers at all. |
| 7 | LOW      | `gateway/server.py:811-812` | No `Cross-Origin-Resource-Policy` on static responses. The site-wide middleware sets `same-origin`, but if a partner ever wants to embed a narve `og/*.png` from a different origin (legitimate use case for embed widgets), the global policy will block it. Worth setting `cross-origin` explicitly on the static mount and keeping the global default for everything else. |
| 8 | LOW      | `gateway/server.py:807-813` | No `Last-Modified` or `ETag` on static responses. `StaticFiles`' parent class normally emits both; this subclass replaces but doesn't preserve them. Result: with `immutable` set, no revalidation happens anyway, so impact is theoretical — but the day `immutable` is removed (per finding 1), `If-None-Match` 304s won't work and every refresh re-downloads the bytes. |
| 9 | LOW      | `gateway/CLOUDFLARE_CHANGES.md:390-399` (rule 3.2) | Edge TTL 30 days, Browser TTL 7 days. Browser is shorter than origin (`max-age=2592000` = 30d). Cloudflare's "Browser TTL Override" silently rewrites the `Cache-Control` it forwards. Origin says 30d, CDN tells the client 7d — they disagree. Not wrong, just confusing. Document the override or remove it. |
| 10 | INFO    | `gateway/server.py:811`        | `immutable` is supported by every browser >2018 (Firefox 49+, Chrome 76+, Safari 11+); no need to feature-detect or fall back. |
| 11 | INFO    | `gateway/cache/` directory     | The `cache/` subpackage is the *application-layer* cache (Redis/in-memory). Distinct from this audit. Cross-reference: `audits/audit_cache.md`. |
| 12 | INFO    | `gateway/tests/test_health.py:126-144` + `test_foundation_bundle.py:56-62` | Cache-header tests exist and assert `public`, `max-age=2592000`, `immutable`, plus `Vary: Accept-Encoding`. They cover `/_gateway_static/gateway.css` and `/og/default` — but skip if `r.status_code != 200`, so they silently pass when the asset is missing. Add `assertEqual(r.status_code, 200)` so a missing CSS file fails the test loud. |

---

## What is correct (do not change)

- `_CachedStaticFiles` correctly emits `public, max-age=2592000, immutable`
  on every 200 for `/_gateway_static/*`. Matches the CDN rule documented in
  `CLOUDFLARE_CHANGES.md` §3.2.
- `Vary: Accept-Encoding` is set on the static mount, so brotli/gzip/identity
  variants don't poison each other.
- `Cache-Control: no-cache, no-store, must-revalidate` is enforced on every
  `text/html` response by `SecurityHeadersMiddleware` at
  `server.py:968-970` (`server.py:992-999` comment) — HTML never gets CDN-cached,
  so a session cookie can't get baked into an edge cache.
- `Cache-Control: no-store` (or `no-store, max-age=0`) is correctly used on
  every API response that returns per-user data: `embed_routes.py:265`,
  `server.py:3343`, `server.py:7040`, `admin_routes.py:2455`,
  `admin_test_emails_routes.py:351`, `status_routes.py:275`.
- **No route in the codebase emits `Cache-Control: private`** — verified by
  grep across all `*.py`. Public assets are correctly tagged `public`;
  per-user responses are correctly tagged `no-store`. The two-bucket policy
  is consistently applied.
- `Cache-Control: no-cache` on `/sw.js` is correct per the W3C
  Service-Worker spec.
- `/api/...` and `/admin/...` paths are explicitly bypassed at the CDN per
  `CLOUDFLARE_CHANGES.md` §3.1, so even if origin forgot `no-store`, the
  edge would not cache them.

---

## Recommended remediation order

1. **Finding 4** — fix `theme.js?v=2` (`gateway/static/_base.html:54`) to
   `{{ static: theme.js }}`. 1-line, immediate.
2. **Finding 1** — sweep templates: every `/_gateway_static/<path>` becomes
   `{{ static: <path> }}`. Mechanical, ~573 sites. Until then, drop
   `immutable` from the mount.
3. **Finding 2** — switch avatar URLs to content-addressed paths *or*
   carve avatars out of the immutable mount.
4. **Finding 3** — add `Vary: Accept-Encoding` to favicon/manifest/sw
   handlers.
5. Findings 5-9 — sweep at leisure; none are user-visible bugs today.

---

*Audit run synchronously, no live HTTP probes (pre-release off-limits).
Evidence is line-and-file from the working tree on `feature/platform-build`.*
