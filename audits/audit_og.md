## OG Image Generation Audit — `gateway/og_routes.py` + `gateway/og_cards.py`

Audit date: 2026-05-15
Branch: `feature/platform-build`
Scope: `gateway/og_routes.py` (183 lines), `gateway/og_cards.py` (304
lines), `gateway/cache/ttl.py` (TTL cache backing the renderer), plus
the OG handlers that share the renderer in `gateway/routes_sharing.py`
(`/og/shared/*`).

Methodology: read both modules end-to-end; cross-checked the cache key
schema (`gateway/cache/ttl.py:18-33`) against the keys actually emitted
by handlers; grepped for `Image.open`, `MAX_IMAGE_PIXELS`, `truetype`,
`load_default`, `decompression`, rate-limit decorators/inline helpers
targeting `/og/*`, and every `ttl_invalidate.*` callsite to see whether
OG keys get busted on the events that change the rendered numbers.
Hard rule observed: synchronous bash only; pre-release `/` page untouched.

## Headline

The OG card path is functional and the in-memory cache absorbs the load,
but **cache invalidation is missing for the two dynamic cards** (`/og/market/{slug}`
and `/og/source/{handle}`). After a market resolves or credibility recomputes,
the data routes flush correctly via `ttl_invalidate.on_market_resolved` /
`on_credibility_recompute`, but neither helper touches `og_card:market:*` or
`og_card:source:*`. Cards therefore serve stale numbers for up to **3600 s**
after resolution / recompute — a social-preview embarrassment but not a
data-leak vector.

Secondary findings: TTL drift between the canonical schema (600 s for
`og_card:market:*`) and the handler (3600 s), no PIL `MAX_IMAGE_PIXELS`
guard on the bundled-logo `Image.open` (low risk because the input is a
checked-in asset, not user-supplied), no OG-specific rate-limit bucket
(global 600/min middleware applies but a single IP can still spin every
slug variant once per minute), and a `load_default()` fallback on font
failure that silently degrades the card layout.

## Severity counts

- Critical: 0
- High: 1
- Medium: 3
- Low: 4
- Info: 3

## Top 3 findings

### 1. [High] Dynamic OG cards are NOT invalidated when their underlying numbers change

**Where:** `gateway/og_routes.py:109` (`cache_key = f"og:source:{handle}"`),
`gateway/og_routes.py:152` (`cache_key = f"og:market:{slug}"`), versus
`gateway/cache/ttl.py:245-265` (`on_market_resolved` /
`on_credibility_recompute`).

**What:** `on_market_resolved(slug)` deletes `market:{slug}`,
`market_chart:{slug}`, `feed:*`, and `credibility_consensus:{slug}` —
but not `og_card:market:{slug}`. Same for `on_credibility_recompute`:
it `delete_prefix("source:")` and `delete_prefix("source_history:")`
but never touches `og_card:source:*`. Result: after a market resolves
YES/NO, the OG image keeps rendering the pre-resolution
`yes_price`/`narve_prob` for up to one hour. After the nightly
credibility job at `jobs/ai_maintenance.py:156`, source cards keep
the old score for up to one hour. Effectively the "version" axis the
task asks about (per slug + per data version) isn't part of the cache
key — only TTL drift saves the cards.

**Why it matters:** the cards are public, link-unfurl-able, and
embedded in tweets. A resolved market that still shows "Market 42%
vs narve.ai 67%" is a credibility hit on every share that scrapes the
image after resolution. The same renderer is also used by
`/og/shared/{token}` (`routes_sharing.py:285-343`) with a **24-hour** TTL —
the same staleness problem, 24× worse.

**Fix:** add OG-prefix deletes to the existing invalidation helpers:

```python
# cache/ttl.py — on_market_resolved
removed += ttl_cache.delete(f"og_card:market:{slug}")
removed += ttl_cache.delete(f"og_card:share:m:*")  # or by individual token

# cache/ttl.py — on_credibility_recompute
removed += ttl_cache.delete_prefix("og_card:source:")
# (the share-token variants don't render live credibility — see
# render_shared_source_card docstring — so they can stay)
```

A separate `on_market_resolved` callsite already exists in
`gateway/jobs/resolution_jobs.py:110,151`, so this is a one-line change
that picks up the right call frequency for free. For shared-prediction
cards (`og_card:share:p:*`) there is no live data so no bust needed.

### 2. [Medium] Per-slug TTL drift between canonical schema and handler

**Where:** `gateway/cache/ttl.py:32` (`og_card:market:{slug}` ttl=600)
vs `gateway/og_routes.py:40` (`_CACHE_TTL = 3600`).

**What:** The schema docstring in `cache/ttl.py` — the
"change in one place" canonical key registry — declares
`og_card:market:{slug}` at 600 s. The handler uses 3600 s for every
route via the shared `_CACHE_TTL` constant. Either the schema is
documentation-only and silently wrong (likely), or the renderer is
caching 6× longer than the designer intended. Combined with finding #1,
this means the stale-data window is 6× the documented one.

**Fix:** either (a) split `_CACHE_TTL` into `_CACHE_TTL_STATIC = 3600`
and `_CACHE_TTL_DYNAMIC = 600` and use the latter for `/og/market` and
`/og/source`, then update the response `Cache-Control` `max-age` for
those endpoints to match; or (b) update the schema doc to reflect the
3600 s reality. Pick (a) — the dynamic cards are the ones where
freshness matters.

### 3. [Medium] No `MAX_IMAGE_PIXELS` / decompression-bomb guard on `Image.open(LOGO_PATH)`

**Where:** `gateway/og_cards.py:88` (`logo = Image.open(LOGO_PATH).convert("RGBA")`).

**What:** Pillow ships with `MAX_IMAGE_PIXELS` set to ~89 megapixels and
emits a `DecompressionBombWarning` (or raises if the limit is more
than 2× exceeded). The OG card path never sets `Image.MAX_IMAGE_PIXELS`
to a renderer-appropriate value (e.g. 4 MP — the logo is 8 KB, ~50×50
target), and `Image.open()` returns a lazy `PIL.Image` whose pixel
dimensions are only resolved on `.convert()`/`.resize()`. Today the
input is `gateway/static/img/logo.png` (8.9 KB, checked into the repo),
so this is purely a defence-in-depth concern: if a deployment swaps
`logo.png` to a maliciously crafted file (16-bit, deeply tiled,
multi-frame APNG, or just very large) the worker process would happily
allocate the pixel buffer and either OOM or stall.

Severity is Medium not High because (a) input is not user-supplied,
(b) the gateway runs single-worker, so a bomb only takes that one
worker down and (c) the wider audit (`gateway/profile_routes.py:485`)
shows the codebase already knows to `img.verify()` user uploads —
just not internal assets.

**Fix:** harden `_paste_logo` to (a) cap the pixel count before
decode and (b) bail to `_draw_logo_mark` on any anomaly:

```python
from PIL import Image, ImageFile
Image.MAX_IMAGE_PIXELS = 4_000_000  # module-level, once

def _paste_logo(img, x, y, size=44):
    if not LOGO_PATH.exists():
        _draw_logo_mark(ImageDraw.Draw(img), x, y + 10); return
    try:
        with Image.open(LOGO_PATH) as probe:
            if probe.width * probe.height > 4_000_000:
                raise ValueError("logo too large")
            logo = probe.convert("RGBA")
            # ...resize/paste as before
    except (Image.DecompressionBombError, ValueError, OSError) as exc:
        log.warning("logo decode rejected: %s", exc)
        _draw_logo_mark(ImageDraw.Draw(img), x + 60, y + 10)
```

## Other findings (Medium / Low / Info)

### [Medium] No OG-specific rate-limit bucket

**Where:** all `/og/*` routes; only the global middleware
(`gateway/server.py:1814-1840`, `GlobalRateLimitMiddleware`) applies.

**What:** A scraper can iterate every market slug once per minute from
a single IP and stay under the 600/min global cap. Each cold-cache slug
forces a full PIL render (1200×630 PNG, ~30–80 KB output, plus a
SQLite `get_latest_market_snapshot` + `get_predictions_for_market` +
`calculate_betyc_probability`). At ~30 ms per render, 600 unique-slug
cold misses = ~18 s of CPU per minute from one IP — survivable, but
trivially amplified by N IPs in a botnet. The `og_card:market:{slug}`
cache absorbs **repeats of the same slug**, not enumeration.

**Fix:** add an inline rate-limit on the two dynamic routes only:

```python
# og_routes.py — top of og_source / og_market
if _is_rate_limited(f"og:{handle or slug}:{ip}", 30, 60):
    raise HTTPException(status_code=429)
```

A 30/min/IP/route cap still serves the unfurl traffic of every social
crawler while shutting down enumeration. Static `/og/default`,
`/og/pricing`, `/og/calendar` need no extra bucket — they each render
once per process lifetime.

### [Low] Font fallback to `ImageFont.load_default()` silently degrades the card

**Where:** `gateway/og_cards.py:54`.

**What:** If all three of `Helvetica.ttc`, `/usr/share/.../DejaVuSans*`,
and the bare `DejaVuSans-Bold.ttf` lookup miss, the renderer falls
through to `ImageFont.load_default()` — which on older Pillow is a
fixed-size 11px bitmap font that visibly breaks the 1200×630 layout
(headings cap at 11 px wide). Modern Pillow ≥ 10.1 made
`load_default` size-aware, but the codebase pins via the lockfile in
`requirements.lock`, so the behaviour depends on what's installed in
the deploy image.

This is not exploitable, just a quality regression on any host without
DejaVu installed (Alpine-based containers, slim distroless images).

**Fix:** ship `DejaVuSans.ttf` + `DejaVuSans-Bold.ttf` under
`gateway/static/fonts/` (they're free-as-in-beer Bitstream) and point
the candidate list at them with an absolute path so the fallback is
always layout-correct. Adds ~700 KB to the repo, removes the visual
fragility.

### [Low] `Image.open(LOGO_PATH)` is not wrapped in a `with` block — file descriptor lifetime

**Where:** `gateway/og_cards.py:88`.

**What:** `Image.open` does not eagerly load image data; closing the
file descriptor is tied to garbage collection of the `Image` object.
The current code immediately calls `.convert("RGBA")` (which loads),
then `.resize(...)` (which returns a new image and drops the old one),
then re-`paste`s the pixel data via `rgba.load()`. By the end of the
function the FD is in GC's hands. Under load — say, a thousand cold
slugs in a minute — this can briefly leak descriptors on CPython
because GC is not deterministic for cycles. Pillow ≥ 8 manages this
better via `_close__fp`, but a `with Image.open(...) as logo:` block is
the right pattern and matches `profile_routes.py:484`'s shape.

**Fix:** wrap in `with`. One-line change.

### [Low] Per-pixel logo inversion is O(W × H) in Python

**Where:** `gateway/og_cards.py:94-97`.

**What:** The inner loop `for j in range(logo.height): for i in range(logo.width):`
walks every pixel via the `rgba.load()` `PixelAccess` object to invert
colours. For a 44-px logo (1936 pixels) this is fine; if anyone ever
ups `size=44` to a larger value, this becomes the dominant cost in
`_render`. Pillow's `ImageOps.invert()` (RGB) + an alpha-channel split
is C-implemented and ~50–100× faster.

**Fix:** replace the loop with:

```python
from PIL import ImageOps, ImageChops
r, g, b, a = logo.split()
rgb = ImageOps.invert(Image.merge("RGB", (r, g, b)))
logo = Image.merge("RGBA", (*rgb.split(), a))
```

### [Low] No `Vary: Accept` / etag — Cloudflare CDN can mis-share cards across content negotiation

**Where:** `gateway/og_routes.py:41-44` (`_HEADERS`).

**What:** Response declares `Cache-Control: public, max-age=3600,
stale-while-revalidate=86400` but no `ETag`, no `Last-Modified`, and
no `Vary`. Today every endpoint returns PNG bytes — no content
negotiation — so `Vary` isn't strictly needed; but the absence of an
`ETag` means once the in-process cache evicts an entry, the next
unfurl re-renders from scratch even when the bytes haven't changed.
Cloudflare caches `Cache-Control: public, max-age=3600` regardless,
so user-facing latency is dominated by the CDN; this only matters for
cold-cache origins.

**Fix:** add a stable `ETag` derived from `(handle, last_resolved_at)`
or `(slug, snapshotted_at)` so Cloudflare's revalidation flow can
short-circuit the render. Defer until finding #1 is in — the
invalidation story has to be coherent first.

### [Info] Path-converter on `/og/market/{slug:path}` accepts `/` in the slug

**Where:** `gateway/og_routes.py:118` (`@router.get("/og/market/{slug:path}")`).

**What:** The `:path` converter is wider than typical slugs need —
the existing handler then heuristically detects `kalshi:` /
`/` to pick a platform name (lines 156-161). That heuristic is a
display-only string, so passing `/og/market/foo/bar/baz` just yields
a card with `platform="Polymarket"` and a heading containing the slug.
No injection — `get_latest_market_snapshot` runs parameterised SQL.
Cache-key collision is the only concern: `og:market:a/b` and a separate
real slug `a/b` would collide, but slugs in this codebase are dash-
separated and the collision risk is theoretical.

**Fix:** none required. Worth a comment line near the route declaration
calling out that `slug:path` is intentional (kalshi slugs contain `:`,
which `:str` would still accept; `path` is here purely for forward-
compat).

### [Info] All OG handlers are `async def` but the renderer is synchronous

**Where:** `gateway/og_routes.py:52,62,69,76,119`.

**What:** Every handler is `async def` but the body is fully
synchronous (PIL render, `sqlite3.Row` lookup, dict iteration). The
PIL render is 20–50 ms on a cold cache and blocks the FastAPI event
loop for that duration. On a hot cache it's a dict get — microseconds —
so this is only painful at the cold-start storm of a new release.

This matches the explicit choice documented in `cache/ttl.py:11-13`
("Sync API. Factories must be synchronous"), and the comments at
`cache/ttl.py:151-156` say the factory runs outside the lock. So
this is by design.

**Fix:** none. Calling out for the next reader.

### [Info] No `X-Robots-Tag: noindex` on the image responses

**Where:** `gateway/og_routes.py:42-44`.

**What:** The PNGs end up indexed by image search engines because
`Cache-Control: public, max-age=3600` paired with no `X-Robots-Tag`
is a green light. That's likely desirable for OG (social previews want
to render the image directly), but worth deciding intentionally.

**Fix:** consider `"X-Robots-Tag": "noindex"` if we don't want the
cards showing in Google Images. No-op otherwise.

## Verdict

The OG image system is **safe-by-default** (no user-supplied image input,
no font-from-network, no SVG, no inline HTML in cards) and the cache
shape is right. The one high-severity defect — stale dynamic cards
after market resolution / credibility recompute — is a one-line fix in
`cache/ttl.py` and pairs naturally with the existing invalidation
helpers. Everything else is defence-in-depth or documentation drift.

Pre-release page (`/`) is not touched by these routes; the audit
respects the hard constraint that the pre-release surface is
off-limits.
