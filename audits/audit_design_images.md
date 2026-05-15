# Design audit — static image assets

**Date**: 2026-05-15
**Scope**:
- `/Users/shocakarel/Habbig/gateway/static/img/*` (raster brand + PWA icons)
- `/Users/shocakarel/Habbig/gateway/static/og/*` (subproduct social cards)
- Favicon / `apple-touch-icon` / `<link rel="icon">` / `<img src>` /
  `og:image` references across every `*.html`, `*.py`, `*.css`, `*.js`,
  `*.json` template under `gateway/`
**Standard**:
- PWA: icon at 192 & 512, plus maskable 192 & 512 (per `manifest.json`).
- Subproduct OG: one 1200×630 PNG per active slug (13 = 12 active + `love`).
- No broken references: every URL emitted from a template or middleware
  must resolve to a file on disk or a registered FastAPI route.
- Apple touch icon: present (180×180 ideal but iOS will scale a smaller
  square PNG — not a hard floor).
**Method**: enumerated every image file on disk, then full-tree grep of
every `/_gateway_static/(img|og)/…\.(png|jpg|jpeg|webp|svg|gif|ico)`
reference and every `/og/…` non-extension route reference, cross-checked
against `manifest.json`, `pwa_middleware.py`, `og_routes.py`,
`routes_sharing.py`, `profile_routes.py`. PNG dimensions verified with
`file(1)`.

---

## Summary

| Category | Count |
|---|---|
| Broken references (URL emitted but no file/route serves it) | **0** |
| Missing PWA icon sizes (per `manifest.json` declarations) | **0** |
| Missing subproduct OG cards (12 active subdomains) | 0 |
| Missing OG card for inactive `love` subproduct | 0 (also present) |
| Files on disk under `static/img/` unreferenced anywhere | 0 |
| Files on disk under `static/og/` unreferenced anywhere | 0 |
| Image-file type/extension mismatches | **1** (`tobias.jpg` is PNG bytes) |
| Cosmetic / hardening notes | 5 |

**Top-line:** every PWA icon listed in `manifest.json` exists at the
declared pixel size with the declared purpose. Every OG card slug
derived from `SUBPRODUCTS` exists at 1200×630. Every image URL emitted
by any HTML template, middleware, push-notification payload, or service
worker pre-cache list resolves to a file or a registered route. Zero
broken references.

The only structural issue is one cosmetic: `static/img/tobias.jpg` is
actually a PNG (`file(1)` reports `PNG image data, 772 x 708, 8-bit/color
RGBA, non-interlaced`). It loads fine in browsers — the JPEG extension
is a lie that the `<source srcset>` chain on `impressum.html` papers
over with a `.webp` first — but it's an audit-worthy hygiene fail and a
real footgun if anyone ever pipes the file through a tool that trusts
the extension.

---

## PWA icon inventory vs manifest

`manifest.json` declares four icons. Each is verified present on disk
at the exact declared pixel dimensions, in PNG format, with the correct
`purpose`.

| Declared in manifest | File on disk | `file(1)` says | Match |
|---|---|---|---|
| `/_gateway_static/img/icon-192.png` (192×192, any) | `gateway/static/img/icon-192.png` (20,423 bytes) | PNG 192×192 RGBA | OK |
| `/_gateway_static/img/icon-512.png` (512×512, any) | `gateway/static/img/icon-512.png` (141,330 bytes) | PNG 512×512 RGBA | OK |
| `/_gateway_static/img/icon-maskable-192.png` (192×192, maskable) | `gateway/static/img/icon-maskable-192.png` (10,904 bytes) | PNG 192×192 RGBA | OK |
| `/_gateway_static/img/icon-maskable-512.png` (512×512, maskable) | `gateway/static/img/icon-maskable-512.png` (82,427 bytes) | PNG 512×512 RGBA | OK |

**Missing PWA icons list: none.** Both pixel sizes (192, 512) and both
purposes (`any`, `maskable`) are present.

Additional PWA-related rasters used outside the manifest:

| Asset | Purpose | File on disk | `file(1)` says |
|---|---|---|---|
| `/_gateway_static/img/badge-72.png` | Web Push notification badge (Android) — used in `sw.js:387` and `push.py:257` fallback | present (3,146 bytes) | PNG 72×72 RGBA |
| `/_gateway_static/img/logo.png` | Notification icon fallback (`sw.js:386`), root-level `/favicon.ico` (`server.py:1165`), every `<link rel="icon">` (109 templates), every `<link rel="apple-touch-icon">` (54 templates), the sidebar logo (`sidebar.py:255`), all `<img class="brand-logo">` instances, the schema.org `Organization.logo` (`seo.py:119`, `about.html:25`, `landing.html:23`), the two transactional email headers (`market_mover_alert.html:12`, `morning_briefing.html:12`), and the `admin_shell.py:199` admin chrome | present (8,981 bytes) | PNG **128×128** RGBA |

**N1 (cosmetic) — `apple-touch-icon` is 128×128, not 180×180.** Every
`<link rel="apple-touch-icon">` in the codebase points at the same
`logo.png` (128×128). Apple's published Human Interface Guidelines call
for 180×180 for the home-screen icon on retina iOS devices; iOS will
upscale a smaller square PNG, but the result is softer than a
purpose-built 180. The PWA icons (192 / 512) are sized for Android +
desktop, not iOS Add-to-Home-Screen — they live in `manifest.json`,
which Safari ignored for Add-to-Home until iOS 16.4 and still treats
unevenly. Recommend adding `gateway/static/img/apple-touch-icon-180.png`
(180×180, rounded corners flattened — iOS adds them itself) and a
dedicated `<link rel="apple-touch-icon" sizes="180x180" href="…">` in
`_base.html` + every page that already declares the rel. Not blocking.

**N2 (cosmetic) — no `<link rel="icon" sizes="32x32">` / `sizes="16x16"`
variants.** Every favicon declaration uses `logo.png` (128×128) without
a `sizes=` attribute; the browser scales down. For sharp 16/32 px tab
icons (especially Chrome's rendering at certain DPIs) a dedicated
`favicon-32.png` + `favicon-16.png` (or a single multi-resolution
`.ico`) would be measurably crisper. Not blocking.

---

## Subproduct OG cards inventory

`gateway/pwa_middleware.py` injects a per-subproduct OG card via
`_og_block_for_key(slug)` whenever the request `Host` matches a
SUBPRODUCT subdomain *and* `static/og/<filename>.png` exists. The lookup
falls back from `slug` → `dashboard_key` (so `cb` → `centralbank.png`
and `health` → `world_health.png`) and ultimately to `default.png`.

Cross-checked the 14 OG PNGs on disk against the 13 SUBPRODUCT entries
(`subproduct.py:42-320`, slugs: `sports`, `weather`, `world`, `crypto`,
`midterm`, `traders`, `whale`, `voters`, `climate`, `disasters`, `cb`,
`health`, `love`). All entries are also confirmed by the canonical
subproducts list (12 active per `memory/narve_subproducts.md`, plus
`love`).

| Subproduct slug | `dashboard_key` | Resolved OG file | Present | Dimensions |
|---|---|---|---|---|
| `sports` | `sports` | `sports.png` (slug-match first) | OK | 1200×630 RGB |
| `weather` | `weather` | `weather.png` | OK | 1200×630 RGB |
| `world` | `world` | `world.png` | OK | 1200×630 RGB |
| `crypto` | `crypto` | `crypto.png` | OK | 1200×630 RGB |
| `midterm` | `midterm` | `midterm.png` | OK | 1200×630 RGB |
| `traders` | `top_traders` | `traders.png` (slug-match wins; `top_traders.png` would be the fallback path) | OK | 1200×630 RGB |
| `whale` | `whale` | `whale.png` | OK | 1200×630 RGB |
| `voters` | `voters` | `voters.png` | OK | 1200×630 RGB |
| `climate` | `climate` | `climate.png` | OK | 1200×630 RGB |
| `disasters` | `disasters` | `disasters.png` | OK | 1200×630 RGB |
| `cb` | `centralbank` | `centralbank.png` (slug `cb.png` not present → falls back to `dashboard_key`) | OK | 1200×630 RGB |
| `health` | `world_health` | `world_health.png` (slug `health.png` not present → falls back to `dashboard_key`) | OK | 1200×630 RGB |
| `love` | `love` | `love.png` | OK | 1200×630 RGB |
| (apex) | — | `default.png` (no Host match → default block) | OK | 1200×630 RGB |

**Missing OG cards: none.** Every subproduct subdomain has either a
slug-named or dashboard_key-named card on disk; the fallback rule
guarantees no host-driven 404.

**N3 (cosmetic) — slug-vs-key asymmetry for `cb` and `health`.** The
PWA middleware preferentially looks for `<slug>.png` first
(`pwa_middleware.py:247`) then falls back to `<dashboard_key>.png`
(line 249). On disk we have `centralbank.png` and `world_health.png`
(dashboard_key-named), but no `cb.png` or `health.png` — so the apex
domain serves the right card via the fallback. This works, but is
asymmetric with the other 11 subproducts where the slug-named file
exists. Recommend a hardlink / symlink (`cb.png → centralbank.png`,
`health.png → world_health.png`) or a duplicate file, so the first
branch wins for every subproduct. Not blocking — pure consistency.

---

## All HTML / Python / JS / JSON image references — every URL resolves

Every static-image URL the codebase emits, grouped by file path. Each
URL was verified against `gateway/static/img/` and `gateway/static/og/`.

| URL | Sites of reference (count) | Resolves to | Status |
|---|---|---|---|
| `/_gateway_static/img/logo.png` | 195 (109 `<link rel="icon">` + 54 `<link rel="apple-touch-icon">` + 32 `<img src>` / schema.org refs) | `gateway/static/img/logo.png` | OK |
| `/_gateway_static/img/icon-192.png` | 4 (manifest, `sw.js:36` precache, `sw.js:386` push fallback, `push.py:257` push fallback) | `gateway/static/img/icon-192.png` | OK |
| `/_gateway_static/img/icon-512.png` | 1 (manifest only) | `gateway/static/img/icon-512.png` | OK |
| `/_gateway_static/img/icon-maskable-192.png` | 1 (manifest only) | `gateway/static/img/icon-maskable-192.png` | OK |
| `/_gateway_static/img/icon-maskable-512.png` | 1 (manifest only) | `gateway/static/img/icon-maskable-512.png` | OK |
| `/_gateway_static/img/badge-72.png` | 1 (`sw.js:387` push badge fallback) | `gateway/static/img/badge-72.png` | OK |
| `/_gateway_static/img/tobias.jpg` | 1 (`impressum.html:31` `<img>` fallback after `<source srcset webp>`) | `gateway/static/img/tobias.jpg` | OK (but see N4) |
| `/_gateway_static/img/tobias.webp` | 1 (`impressum.html:30` `<source srcset>`) | `gateway/static/img/tobias.webp` | OK |
| `/_gateway_static/og/default.png` | 2 (both in `pwa_middleware.py`: og:image + twitter:image meta blocks) | `gateway/static/og/default.png` | OK |
| `https://narve.ai/_gateway_static/og/<key>.png` (dynamic, key ∈ all subproduct slug/dashboard_key set) | Computed in `pwa_middleware.py:_og_block_for_key()`; only emitted when `_OG_DIR/<key>.png` is verified to exist (lines 247-250) | guarded — never emits a broken URL | OK by construction |
| `/favicon.ico` | 4 (browser auto-fetch handled at `server.py:1157`, `sw.js:35` precache + `sw.js:129` skip, `offline.html:197` cache predicate, `_CSRF_SKIP_PREFIXES`) | served from `static/img/logo.png` via `@app.get("/favicon.ico")` route | OK |
| `/manifest.json` | injected on every HTML response (`pwa_middleware.py:110`), `offline.html:196` cache predicate | served via `@app.get("/manifest.json")` from `static/manifest.json` | OK |
| `/og/default`, `/og/pricing`, `/og/calendar`, `/og/source/{handle}`, `/og/market/{slug}` | Various templates + `_base.html` default; canonical fallback in `seo.py:53` | dynamically rendered via `gateway/og_routes.py:51-172` (PIL → PNG buffer with 1h cache) | OK |
| `/og/profile/{handle}` | `profile_routes.py:276` per-profile share | rendered route, `profile_routes.py` | OK |
| `/og/shared/market/{token}`, `/og/shared/source/{token}`, `/og/shared/prediction/{token}` | `shared_*.html` templates + `routes_sharing.py:387-403` | rendered routes, `routes_sharing.py:277-323` | OK |

**Total raw `/_gateway_static/(img\|og)/…\.(png\|jpg\|webp)` references
counted: 207. Broken references: 0.**

---

## Files on disk — every one is referenced

Enumerated every file under `gateway/static/img/` and `gateway/static/og/`
and confirmed each is reached by at least one of: manifest, middleware,
HTML template, service worker, push notification module, schema.org
fragment, or email template. No orphan assets.

| File | Bytes | Dimensions | Reached from |
|---|---|---|---|
| `img/badge-72.png` | 3,146 | 72×72 | `sw.js:387`, `push.py:257` |
| `img/icon-192.png` | 20,423 | 192×192 | manifest, `sw.js:36,386`, `push.py:257` |
| `img/icon-512.png` | 141,330 | 512×512 | manifest |
| `img/icon-maskable-192.png` | 10,904 | 192×192 | manifest |
| `img/icon-maskable-512.png` | 82,427 | 512×512 | manifest |
| `img/logo.png` | 8,981 | 128×128 | 195 references (see table above) |
| `img/tobias.jpg` | 714,727 | 772×708 | `impressum.html:31` |
| `img/tobias.webp` | 15,374 | 772×708 | `impressum.html:30` |
| `og/centralbank.png` | 28,000 | 1200×630 | `pwa_middleware.py` (host=cb.*) |
| `og/climate.png` | 26,543 | 1200×630 | `pwa_middleware.py` (host=climate.*) |
| `og/crypto.png` | 30,167 | 1200×630 | `pwa_middleware.py` (host=crypto.*) |
| `og/default.png` | 24,237 | 1200×630 | `pwa_middleware.py` default + every apex page |
| `og/disasters.png` | 26,054 | 1200×630 | `pwa_middleware.py` (host=disasters.*) |
| `og/love.png` | 35,737 | 1200×630 | `pwa_middleware.py` (host=love.*) |
| `og/midterm.png` | 28,130 | 1200×630 | `pwa_middleware.py` (host=midterm.*) |
| `og/sports.png` | 26,317 | 1200×630 | `pwa_middleware.py` (host=sports.*) |
| `og/traders.png` | 24,484 | 1200×630 | `pwa_middleware.py` (host=traders.*) |
| `og/voters.png` | 24,098 | 1200×630 | `pwa_middleware.py` (host=voters.*) |
| `og/weather.png` | 31,656 | 1200×630 | `pwa_middleware.py` (host=weather.*) |
| `og/whale.png` | 20,847 | 1200×630 | `pwa_middleware.py` (host=whale.*) |
| `og/world.png` | 26,379 | 1200×630 | `pwa_middleware.py` (host=world.*) |
| `og/world_health.png` | 30,013 | 1200×630 | `pwa_middleware.py` (host=health.*, via dashboard_key fallback) |

**Orphans: zero.**

---

## Findings

### N1. Apple touch icon is 128×128, not the 180×180 iOS HIG ideal — cosmetic
**Location:** every page that declares `<link rel="apple-touch-icon">`
points at `/_gateway_static/img/logo.png` (128×128). 54 templates +
`_base.html`.

iOS Add-to-Home-Screen will use this image and upscale to 180. Not a
broken reference — just a sharpness loss on retina home screens. Add a
purpose-built `apple-touch-icon-180.png` + `sizes="180x180"` to close
the gap.

### N2. No dedicated favicon-16 / favicon-32 / `.ico` — cosmetic
**Location:** every `<link rel="icon" type="image/png">` points at the
single 128×128 `logo.png`. Browsers downscale on the fly.

Add `favicon-32.png` (32×32) + `favicon-16.png` (16×16) and declare
them with `sizes=` attributes for crisp tab-row rendering. Not blocking.

### N3. Subproduct OG cards use `dashboard_key` names for two slugs
**Location:** `static/og/centralbank.png` and `static/og/world_health.png`
exist; `cb.png` and `health.png` do not.

`pwa_middleware.py` handles this with a `slug → dashboard_key` fallback
(lines 247-250), so the right card is served — but the asymmetric naming
means a maintainer scanning the directory has to know about the fallback
to understand why `cb` and `health` "have no card". Recommend either
duplicating the files (`cp centralbank.png cb.png`) or creating
hardlinks so the slug-named lookup hits first for every subproduct.

### N4. `static/img/tobias.jpg` is actually PNG bytes — cosmetic
**Location:** `gateway/static/img/tobias.jpg` declared `image/jpeg` by
extension; `file(1)` reports `PNG image data, 772 x 708, 8-bit/color
RGBA, non-interlaced`.

The browser sniffs the magic bytes and renders correctly, so the
visible UX is unaffected. But: (a) any future tooling that trusts the
extension (image optimizers, CDN content-type rules, mod_mime) will
mis-treat the file; (b) the `<source type="image/webp">` /
`<img src="…jpg">` chain is the standard responsive-image pattern — the
fallback ought to be a real JPEG (smaller than the current PNG for a
photo), which would shave most of the 715 KB the file currently weighs.
Recommend re-encoding to actual JPEG quality 85, or renaming to
`tobias.png` and updating the one `impressum.html` reference.

### N5. The 715 KB `tobias.jpg` is a perf footgun on `impressum.html` — cosmetic
**Location:** `gateway/static/img/tobias.jpg` (714,727 bytes).

The WebP sibling at `tobias.webp` is 15,374 bytes (97% smaller) and the
`<picture><source srcset="…webp">` chain prefers it for every browser
that supports WebP — which is every browser that matters in 2026. The
PNG-disguised-as-JPG only ships to users disabling WebP, on
non-supporting bots, or via the Schema.org / OG crawl. Still, having a
715 KB asset in `static/img/` is the kind of thing the next perf audit
will flag — re-encoding to a real JPEG ~50 KB would drop the apex of
the `tobias.*` weight from 730 KB to ~65 KB.

---

## What was checked but is correct

- **PWA: all four icons (192 / 512 / maskable-192 / maskable-512)
  present and dimensionally correct.** No missing sizes.
- **All 12 active subproducts plus the inactive `love` subproduct have
  a 1200×630 PNG OG card on disk** (via slug name or dashboard_key
  fallback).
- **Every `<img>`, `<link rel="icon">`, `<link rel="apple-touch-icon">`,
  `og:image`, `twitter:image`, schema.org `logo`, and service-worker
  pre-cache entry resolves to an existing file or a registered route.**
  Zero 404s by reference.
- **Service worker precache list (`sw.js:35-37`) only includes assets
  that exist on disk** (`/favicon.ico`, `/_gateway_static/img/icon-192.png`,
  `/_gateway_static/img/logo.png`).
- **Push notification defaults (`sw.js:386-387`, `push.py:257`) point at
  existing assets** (`icon-192.png`, `badge-72.png`).
- **`/og/default` dynamic route** is registered (`og_routes.py:51`) and
  the PWA middleware also has a *static* PNG fallback at
  `/_gateway_static/og/default.png` — so even if the dynamic renderer
  is offline, the og:image meta tag still resolves to a real image.
- **`favicon.ico` is served from `logo.png` via the FastAPI route**
  (`server.py:1157`), so the lack of a `gateway/static/favicon.ico` file
  is intentional and not a broken reference.
- **No image is referenced as URL but missing on disk; no image is on
  disk but never referenced.** Tight inventory.

---

## Out of scope (per brief)

- No code changes proposed; this is read-only audit output.
- Email-template MIME inlining (`market_mover_alert.html`,
  `morning_briefing.html` embed `{{ app_url }}/_gateway_static/img/logo.png`
  via absolute URL — Cloudflare serves them, no broken reference, but a
  separate email-rendering audit could opine on inlining vs. remote-load
  privacy trade-offs).
- OG card *rendering quality* (PIL output, kerning, brand consistency)
  is a design audit, not an asset-inventory audit.
- `static/og/*` byte-size optimisation (the cards average ~26 KB — well
  under the 8 MB Twitter / 200 KB Slack OG limits — but PNG-8 indexed
  palettes could shave another 30-40%).
- Sub-resource integrity (SRI) for image assets — not applicable, only
  scripts.

---

## Verification commands run

```bash
# Inventory on-disk image files:
find /Users/shocakarel/Habbig/gateway/static -type f \
  \( -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" \
     -o -name "*.webp" -o -name "*.gif" -o -name "*.svg" \
     -o -name "*.ico" \)

# Verify PNG dimensions:
file /Users/shocakarel/Habbig/gateway/static/img/*.png \
     /Users/shocakarel/Habbig/gateway/static/img/*.jpg \
     /Users/shocakarel/Habbig/gateway/static/img/*.webp \
     /Users/shocakarel/Habbig/gateway/static/og/*.png

# Every static-image URL emitted by any source file:
grep -rEon '/_gateway_static/(img|og)/[a-zA-Z0-9_./-]+\.(png|jpg|jpeg|webp|svg|gif|ico)' \
  /Users/shocakarel/Habbig/gateway/ \
  --include="*.py" --include="*.html" --include="*.css" \
  --include="*.js" --include="*.json" | \
  grep -oE '/_gateway_static/(img|og)/[a-zA-Z0-9_./-]+\.(png|jpg|jpeg|webp|svg|gif|ico)' | \
  sort | uniq -c | sort -rn

# Every /og/ dynamic-route reference (no extension):
grep -rEon "/og/[a-zA-Z0-9_./-]+" \
  /Users/shocakarel/Habbig/gateway/ --include="*.py" --include="*.html" | \
  grep -oE "/og/[a-zA-Z0-9_./-]+" | sort -u

# Cross-check SUBPRODUCTS slugs & dashboard_keys against disk:
grep -n "\"slug\":\|\"dashboard_key\":" \
  /Users/shocakarel/Habbig/gateway/subproduct.py
ls /Users/shocakarel/Habbig/gateway/static/og/
```
