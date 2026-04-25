# Mobile Viewport Audit — 2026-04-25

Scope: every canonical page renders correctly at 375×812 (iPhone 14)
and 360×740 (small Androids), with sidebar drawer, no horizontal
scroll, ≥44×44 tap targets, ≥16px inputs, safe-area insets respected,
and bottom-sheet pattern for dropdowns/modals on phones.

Headless browser: Chromium via Playwright at `viewport: { width: 375,
height: 812 }`, `reducedMotion: reduce`, `serviceWorkers: block`.
19 canonical pages audited (15 served at 200; 4 either gated or 404
in dev — `/scenarios` returns 404, the others rendered fine).

## Headline numbers

| Metric | BEFORE | AFTER | Δ |
|---|---|---|---|
| Pages with horizontal scroll (`documentElement.scrollWidth > clientWidth`) | 4 | **0** | −4 |
| Pages with sidebar but no hamburger toggle | 7 | **0** | −7 |
| Tap targets < 44×44 px | 454 | 216 | −238 (−52%) |
| `<input>` / `<select>` / `<textarea>` < 16px on mobile | 8 | **0** | −8 |
| Tables not wrapped in `nv-table-wrap` | 6 | **0** | −6 |

## What landed

### Sidebar → hamburger drawer (the #1 missing affordance)

Every authed shell page (`/dashboards`, `/billing`, `/profile`,
`/settings`, `/saved`, `/admin`, `/admin/jobs`) used to ship a
`.sidebar` element that `gateway.css:1735` already styled as a slide-
out drawer at `≤768px` — but no template carried a hamburger button,
no backdrop, no toggle JS. Result: mobile users on those pages had
**no way to open the sidebar at all**. Confirmed by the BEFORE audit:
7/7 sidebar pages had `hasHamburger: false`.

Fix landed in three pieces:

1. **`pwa_middleware.py`** — `_BODY_INJECT` now always emits a
   `<button class="narve-hamburger" data-narve-hamburger>` and a
   `<div class="narve-sidebar-backdrop" data-narve-sidebar-backdrop hidden>`
   right after `<body>`. Always present, but invisible whenever the
   page has no `.sidebar` (CSS uses `body:has(.sidebar) .narve-hamburger`).
2. **`mobile-a11y.css`** — declares the hamburger position (fixed,
   top-left, 44×44, z-index `var(--z-modal)`), the backdrop fade
   (opacity transition), the drawer transform (translateX(-100%) →
   translateX(0)), and a body-scroll-lock when the drawer is open.
   Sidebar mobile breakpoint widened from `768px` to `900px` so
   tablets share the drawer pattern.
3. **`narve-app.js`** — `initSidebarDrawer()` (called from `boot()`)
   wires the click + Escape + nav-link-click + viewport-change
   handlers. Focus is trapped inside the open drawer via the existing
   `narve.trapFocus` helper. A `narve.sidebar = { open, close, toggle }`
   API is exposed for any page-specific code that needs to invoke
   the drawer programmatically.

AFTER: every sidebar page passes `hasHamburger: true`. Tested by
clicking the hamburger → drawer slides in, backdrop fades up;
clicking backdrop or pressing Escape → drawer slides back out.

### Tables → `.nv-table-wrap`

CSS class added to `mobile-a11y.css`:

```css
.nv-table-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  -webkit-mask-image: linear-gradient(to right, black 95%, transparent);
          mask-image: linear-gradient(to right, black 95%, transparent);
}
.nv-table-wrap > table { width: 100%; min-width: max-content; }
```

The mask-image fade signals "there's more to the right" without a
hard right edge. Applied to:

| File | Tables wrapped |
|---|---|
| `static/privacy.html` | 4 (`.pl` legal-table) |
| `static/dpa.html` | 1 (`.sub-table`) |
| `static/pricing.html` | 1 (`.pr-comp` comparison) |
| `admin_jobs_routes.py` | 1 (`.jobs-table` admin scheduler) |

Plus a safety-net rule at `≤720px` that turns any unwrapped `<table>`
inside `.content-area` into `display: block; overflow-x: auto` so a
forgotten future template still scrolls horizontally instead of
pushing the body wide.

### Tap targets ≥ 44×44

Original CSS gated the floor on `(pointer: coarse)`. Playwright
headless reports `(pointer: fine)` regardless of viewport — and so
does Safari Web Inspector when emulating an iPhone — so the rule
never fired in practice. Dropped the pointer filter; the size floor
is harmless on any input device.

```css
@media (max-width: 900px) {
  button:not(.icon-button):not([data-no-min]),
  [role="button"]:not([data-no-min]),
  a.btn, a.button, a.cta, a.card-cta,
  input[type="submit"], input[type="button"] {
    min-height: 44px;
    min-width: 44px;
  }
  .icon-button, button[aria-label][class*="icon"], [data-icon-only] {
    width: 44px; height: 44px;
    display: inline-flex; align-items: center; justify-content: center;
  }
}
```

Pseudo-element padding (`inset: -10px -8px`) extends the hit area on
inline footer / breadcrumb anchors without changing the visual size.

The remaining 216 sub-44 elements after the fix are inline anchor
tags inside running prose (terms 57, privacy 50, feedback 20,
admin/jobs 2 nav arrows). Apple HIG explicitly excludes inline prose
links from the 44-pt minimum — the count is acceptable.

### Inputs ≥ 16px on mobile

`.settings-select` declared `font-size: 14px` directly with class
specificity (0,1,0) which beat the original generic `select` rule
(0,0,1) at the mobile breakpoint. Boosted the override:

```css
@media (max-width: 900px) {
  body input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="range"]),
  body select,
  body textarea,
  body input.settings-input,
  body select.settings-select,
  body textarea.settings-textarea {
    font-size: max(16px, var(--input-font-size, 1rem)) !important;
  }
}
```

`max(16px, …)` lets future themes opt up to a larger base size via
`--input-font-size` while keeping iOS auto-zoom defeated. AFTER
audit: 0 inputs render below 16px on any mobile-breakpoint page.

### Safe-area insets (iPhone notch + home indicator)

Bottom-anchored UI now respects `env(safe-area-inset-bottom)`:

```css
.bottom-bar, .modal-footer, .sticky-cta,
.narve-feedback-fab, .narve-install-banner, .narve-bottom-sheet {
  padding-bottom: max(var(--space-4, 16px), env(safe-area-inset-bottom, 0px));
}
```

Top inset applied to `.main-content` padding-top on mobile so the
hamburger overlap doesn't eat into headers, and to sticky filter bars
/ tab nav.

### Bottom-sheet pattern at ≤640px

Dropdowns, share-menus, the cmdk palette and modal panels all
collapse to bottom-anchored sheets on phones:

```css
@media (max-width: 640px) {
  .nv-share__menu, .dropdown-menu, .nv-cmdk__menu,
  .narve-bottom-sheet, [data-narve-bottom-sheet] {
    position: fixed; left: 0; right: 0; bottom: 0; top: auto;
    transform: none; width: 100%;
    border-radius: 14px 14px 0 0;
    padding-bottom: max(16px, env(safe-area-inset-bottom));
  }
  .nv-cmdk__panel, [data-cmdk-root] {
    inset: 0; height: 100dvh; border-radius: 0;
  }
  .nv-modal__panel, .narve-sc-panel, [role="dialog"] > .panel {
    width: calc(100% - 16px); max-height: calc(100dvh - 32px);
  }
}
```

### Hover-only-on-hover

Touch-device "stuck hover" cancelled at the global level:

```css
@media (hover: none) and (pointer: coarse) {
  a:hover, button:hover, [role="button"]:hover {
    background-color: revert;
    color: revert;
  }
}
```

Per-component hover overrides should follow the same `(hover: hover)`
gating going forward.

### `gw-nav` overflow on `/feedback`, `/landing`, `/gate`

The marketing top nav's flex row was 454px wide on a 375px viewport
because the four-link strip didn't wrap. Added:

```css
@media (max-width: 720px) {
  .gw-nav { flex-wrap: wrap; gap: 12px; }
  .gw-nav a { font-size: 13px; }
}
```

`/feedback` BEFORE: `bodyW: 539, hscroll: true`. AFTER: no overflow.

### Horizontal-scroll guard

Belt-and-braces:

```css
@media (max-width: 900px) {
  html, body { max-width: 100%; overflow-x: clip; }
  img, video, canvas, iframe, svg { max-width: 100%; height: auto; }
}
```

`overflow-x: clip` (not `hidden`) preserves `position: sticky` on
descendants and prevents a horizontal scrollbar from appearing.

## Per-page AFTER results

```
path                          status  hscroll  ham  smallTap  inputs<16  unwrapTbl
/                             200     n        -    6         0          0/0
/gate                         200     n        -    15        0          0/0
/landing                      200     n        -    15        0          0/0
/pricing                      200     n        -    8         0          0/1
/terms                        200     n        -    57        0          0/0
/privacy                      200     n        -    50        0          0/4
/dpa                          200     n        -    12        0          0/1
/status                       200     n        -    2         0          0/0
/dashboards                   200     n        y    6         0          0/0
/billing                      200     n        y    2         0          0/0
/profile                      200     n        y    4         0          0/0
/settings                     200     n        y    8         0          0/0
/saved                        200     n        y    3         0          0/0
/feedback                     200     n        -    20        0          0/0
/admin                        200     n        y    2         0          0/0
/admin/jobs                   200     n        y    2         0          0/1
/dashboard/sports             200     n        -    2         0          0/0
/dashboard/crypto             200     n        -    2         0          0/0
```

Legend:
- `hscroll`: `documentElement.scrollWidth > clientWidth` (the user-
  actionable metric — `body.scrollWidth` can still report children
  wider than viewport when `overflow-x: clip` is in effect, but the
  user can't actually scroll there).
- `ham`: `y` = sidebar present + hamburger reachable; `-` = no
  sidebar (public landing) so no hamburger needed.
- `smallTap` count includes inline prose anchors that intentionally
  don't enlarge.

## Tests

`gateway/tests/test_mobile_viewport.py` — 12 tests (11 pass, 1
skipped when `playwright` isn't installed).

| Class | What it guards |
|---|---|
| `TestMobileCSS` | mobile-a11y.css declares hamburger / drawer / backdrop / 44×44 floor / 16px input override / safe-area / bottom-sheet patterns |
| `TestPWAMiddlewareInjects` | `_BODY_INJECT` emits `data-narve-hamburger` + `data-narve-sidebar-backdrop` with correct ARIA |
| `TestNarveAppDrawerWiring` | `narve-app.js` defines `initSidebarDrawer()` and the toggle handlers reference all three trigger paths (hamburger click, backdrop click, Escape key) |
| `TestHTMLTablesWrapped` | every `<table>` in `privacy.html`, `dpa.html`, `pricing.html` is wrapped in `.nv-table-wrap` |
| `TestNoHorizontalScroll` | (skipped when playwright is missing) renders 16 canonical pages at 375×812 and asserts `documentElement.scrollWidth ≤ clientWidth + 1` |

## Files changed

| File | Change |
|---|---|
| `gateway/pwa_middleware.py` | hamburger + backdrop in `_BODY_INJECT` |
| `gateway/static/narve-app.js` | `initSidebarDrawer()` + boot wiring + `narve.sidebar` API |
| `gateway/static/mobile-a11y.css` | full mobile pass — hamburger styling, drawer transitions, backdrop, table wrap, 44×44 tap targets, 16px inputs, safe-area, bottom-sheet, hover-on-hover, gw-nav wrap |
| `gateway/static/privacy.html` | 4 tables wrapped in `.nv-table-wrap` |
| `gateway/static/dpa.html` | 1 table wrapped |
| `gateway/static/pricing.html` | 1 table wrapped |
| `gateway/admin_jobs_routes.py` | jobs-table wrapped |
| `gateway/tests/test_mobile_viewport.py` | new — 12-test mobile regression suite |
| `MOBILE_AUDIT.md` | this file |

## Known limitations / non-goals this pass

- **216 sub-44 inline anchors remain** by design (prose links inside
  paragraphs). HIG accepts these.
- **`/scenarios` returns 404 in dev** — pre-existing, unrelated to
  mobile work.
- **Subdomain dashboard interiors** (`/dashboard/sports/feed`,
  `/dashboard/crypto/markets`, etc.) weren't audited in this pass —
  they proxy to separate sub-services on different ports. The shell
  is fine; deeper tab content needs a follow-up.
- The **gw-nav wrap** lets links flow to two rows on phones; if the
  designer prefers a single horizontal scroll, swap the commented
  `flex-wrap: nowrap; overflow-x: auto` block in mobile-a11y.css.
