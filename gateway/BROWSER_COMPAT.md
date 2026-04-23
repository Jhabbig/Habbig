# Browser & Device Compatibility

This document captures the supported browser + viewport matrix for
narve.ai, the automated checks that enforce it, the known quirks we
work around, and the manual-QA runbook used before every promotion to
production. It's a working document — update the Manual QA section
below after every full run.

> Scope reminder: IE11, pre-iOS-16 Safari, Android 10 or older, and
> Samsung Internet < 20 are **out of scope**. Nothing in the app
> promises those work.

---

## 1 · Supported matrix

| Browser           | Versions                   | Tier |
|-------------------|----------------------------|------|
| Chrome            | latest + latest-1          | 1    |
| Safari            | latest + latest-1 (macOS)  | 1    |
| Mobile Safari     | iOS 16+                    | 1    |
| Mobile Chrome     | Android 11+                | 1    |
| Firefox           | latest                     | 1    |
| Edge (Chromium)   | latest                     | 2 (smoke only) |
| Samsung Internet  | 20+                        | 3 (best-effort) |
| Twitter / FB in-app browser | latest iOS variants | 2 |

_Tier 1_ = every release is tested end-to-end before deploy. _Tier 2_ =
smoke tested via Playwright + UA spoof. _Tier 3_ = we'll fix reported
issues but don't gate releases on it.

## 2 · Supported viewports

Automated sweep runs on Chromium for every combo of:

| Name         | W × H       | Class   |
|--------------|-------------|---------|
| desktop_16   | 1440 × 900  | desktop |
| desktop_fhd  | 1920 × 1080 | desktop |
| laptop_13    | 1280 × 800  | desktop |
| tablet_10    | 1024 × 768  | tablet  |
| mobile_plus  |  414 × 896  | mobile  |
| mobile_std   |  375 × 812  | mobile  |
| mobile_sm    |  360 × 780  | mobile  |

Mobile viewports fail the build if any of:

- `document.scrollWidth > clientWidth` (horizontal scroll)
- any visible `<input|textarea|select>` has `font-size < 16px` (iOS auto-zoom)
- any visible button / `[role=button]` has a bounding box smaller than 32×32
  (stricter 44×44 bar enforced manually — see §6)

## 3 · Automated tests

Playwright-driven suite lives under `gateway/tests/browser/` and skips
by default when the `playwright` package isn't installed so non-browser
CI jobs pay no cost. Entry points:

    gateway/scripts/run-browser-tests.sh                   # install + run all
    gateway/scripts/run-browser-tests.sh --engines chromium  # fast path
    gateway/scripts/run-browser-tests.sh --headed          # open a window

Files:

| File                                    | What it asserts |
|-----------------------------------------|-----------------|
| `test_visual_regression.py`             | 200, no-hscroll, input ≥ 16px, hit-target ≥ 32px, screenshot diff |
| `test_critical_flows.py`                | Homepage + `/gate` usable on chromium, firefox, webkit; no console errors; no UA sniffing |
| `test_mobile_quirks.py`                 | 100dvh over 100vh; `manifest.json` well-formed; SW registers on WebKit; Twitter in-app UA works |

Screenshot baselines are captured in `tests/browser/screenshots/` and
are *not* committed — they're a local artifact for humans to diff.
Future work could commit 1× baselines and add pixelmatch for strict
regression, but the screenshot-diff loop needs human eyes right now.

## 4 · Responsive breakpoint audit

Current breakpoints in use across the `static/*.css` + inlined Python
shells:

| Breakpoint | Hits | Verdict |
|-----------|------|---------|
| 480px     | 6    | ✅ mobile baseline — keep |
| 640px     | 5    | ✅ tablet floor — keep |
| 720px     | 7    | ⚠ overlaps with 768px. Consolidate on next pass. |
| 768px     | 2    | ✅ tablet breakpoint — keep |
| 860px     | 3    | ⚠ one-off — fold into 900 or 960 |
| 960px     | 2    | ⚠ overlaps with 1024. Fold upward. |
| 1024px+   | 0    | ✅ desktop floor via absence — keep |
| 1080px    | 2    | ✅ admin shell cap — keep |
| 520/560/600/440/320/375 | 6 | ⚠ one-offs. Audit on next design pass. |

Agreed canonical scale (update with every design pass):

- **mobile**   — up to 640px
- **tablet**   — 641–1023px
- **desktop**  — 1024px+
- **wide**     — 1440px+

The `prefers-reduced-motion: reduce` block appears six times — good.
The `prefers-color-scheme` block appears once; the rest of the theme
is user-selectable via `[data-theme]` cookie, which is the intended
behaviour (auto is a fallback on pages that don't load the theme JS
before first paint).

## 5 · Known quirks + workarounds

### iOS Safari
- `100vh` crops when the URL bar retracts. Use `100dvh` with a `100vh`
  fallback. **Enforced by `test_css_uses_dvh_not_raw_vh_for_hero_heights`.**
- Input focus auto-zooms when `font-size < 16px`. **Enforced by the
  mobile viewport sweep.**
- `navigator.mediaDevices.getDisplayMedia` requires a user gesture. No
  feature in narve.ai currently calls it; if one is added, the prompt
  MUST live behind a button click, not an effect.
- WebSocket in background tab: older iOS versions (16.0–16.3) drop the
  socket silently. Realtime code already falls back to polling.

### Firefox
- `backdrop-filter` unsupported in <= 103. Our use is decorative only;
  we ship opaque fallbacks via `@supports not (backdrop-filter: blur())`.
- `-webkit-*` prefixes are ignored. Visual regression tests on Firefox
  catch any prefix-only styling that slipped through.

### In-app browsers (Twitter / X, Facebook, Instagram)
- `localStorage` can be blocked. Widget dismissal and theme preference
  both degrade gracefully — the UI still renders, the user just doesn't
  remember the choice.
- Some in-app browsers strip the `Referer` header; analytics beacons
  tolerate missing values.

### Touch + mouse hybrid
- Hover styles must be paired with `:focus-visible` and must not
  persist on tap. The `@media (hover: hover)` guard is the pattern
  used in gateway.css; enforce in new CSS too.

## 6 · Manual QA runbook

Run **before every production deploy**. Tick each row; paste results
into the Change Log table at the bottom.

### iPhone (any iOS 16+ device)
- [ ] `/register`: on-screen keyboard doesn't obscure the submit button.
- [ ] `/dashboard`: feed scrolls smoothly (no jank on list virtualisation).
- [ ] `/dashboard`: swipe left/right on tabs doesn't trigger browser back.
- [ ] PWA "Add to Home Screen" appears on 2nd visit while logged in.
- [ ] Push notification permission flow completes.
- [ ] Tap targets hit at 44×44 minimum (use Accessibility Inspector).

### Android (any Android 11+ device)
- [ ] Address-bar collapse doesn't drop or cover sticky UI.
- [ ] Back button doesn't blow away state on modal close.
- [ ] `Add to Home screen` → opens the PWA standalone.
- [ ] Screen reader (TalkBack) navigates the main feed cleanly.

### iPad (any iPadOS 16+ device)
- [ ] Layout looks tablet-native — not a big phone, not a tiny desktop.
- [ ] Split-screen at 50/50 still works without horizontal scroll.
- [ ] Trackpad click registers without a double-fire.

### Mac desktop (Safari + Chrome + Firefox)
- [ ] Trackpad left/right swipe doesn't trigger unexpected back.
- [ ] Safari Reader mode works on `/sources/{handle}` + market detail.
- [ ] Text-scaling up to 200% (Cmd + multiple times) stays usable.

### Windows desktop (Edge + Firefox)
- [ ] High-contrast mode preserves text/border visibility.
- [ ] NVDA walks through the homepage + signup without dead regions.

### Twitter in-app browser (iOS)
- [ ] Homepage renders with full styling.
- [ ] `/gate` form submits without localStorage errors in console.
- [ ] Tap "Open in Safari" works from the share menu.

## 7 · Progressive enhancement

| Without …              | Behaviour |
|------------------------|-----------|
| JavaScript             | `/`, `/pricing`, `/about`, `/privacy`, `/terms` render correctly. Auth flows fail gracefully with form submission (no `fetch`). |
| cookies                | Login fails with a visible message; public pages continue to work. |
| third-party cookies    | No behaviour change — we use first-party session cookies only. |
| ad-blockers            | No regressions — we don't load ad-tech. |
| localStorage           | Theme + banner dismissal reset on each visit; core UX unaffected. |
| WebSockets             | Realtime falls back to polling `/api/*/updates` every 30s. |
| service workers        | Offline shell unavailable; install banner suppressed (it's gated behind `beforeinstallprompt`, which only fires with SW). |

## 8 · Feature-detection policy

Browser sniffing via `navigator.userAgent` is **banned** in frontend
code except for three legitimate cases:

1. Mac-vs-rest platform detection for Cmd / Ctrl keyboard shortcut
   rendering (`switcher.js`, `command-palette.js`).
2. Bot detection in analytics to exclude headless + crawler traffic.
3. In-app-browser heuristics where WebKit / Blink can't be told apart
   via feature APIs (currently unused).

The Playwright test `test_feature_detection_not_browser_sniffing`
scans every same-origin `<script>` on `/` for forbidden patterns and
fails the suite if it finds an unpaired UA sniff. The three
exceptions above each carry a `// UA-allowlist: <reason>` comment so
the check knows to skip them.

## 9 · Change log

| Date       | What ran              | Engines | Findings |
|------------|-----------------------|---------|----------|
| _template_ | full matrix + manual  | cx/ff/wk + iPhone 15 | paste notes here |
