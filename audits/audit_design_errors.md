# Design Audit — Error Templates (404 / 5xx / Offline)

Audit date: 2026-05-15
Scope (requested): `gateway/static/404.html`, `gateway/static/505.html`,
`gateway/static/503.html`, `gateway/static/offline.html`, and their
`gateway/static/pages/*.css` companions.
Standard: `narve-design` skill — monochrome only, three typefaces, tokens
only, AA contrast in both themes, mobile-friendly.

---

## Scope reconciliation (read this first)

The four templates named in the request do not all exist as separate
files. The codebase consolidates HTTP-error rendering in a single
template, and 505 has no handler at all. This audit therefore covers
the actual surface that ships:

| Requested file | What actually exists | Notes |
|---|---|---|
| `gateway/static/404.html` | **not present** — rendered via `gateway/static/error_page.html` | 404-specific copy / search box / curated links assembled in `gateway/error_handlers.py::render_error_page` |
| `gateway/static/503.html` | **not present** — same `error_page.html` route | 503-specific `<p class="nv-error__extra">` extra-line injected by `error_handlers.py` |
| `gateway/static/505.html` | **not present anywhere** | Neither template nor handler. 505 ("HTTP Version Not Supported") is not in `_STATUS_TO_TITLE` / `_STATUS_TO_MESSAGE` / `_STATUS_TO_SLUG` in `error_handlers.py`. If it ever fires it falls through to the catch-all `"Error" / "Something went wrong."` strings. |
| `gateway/static/offline.html` | exists, standalone, **inline `<style data-keep>`** | Service-worker offline shell; deliberately self-contained so it renders with zero network. |

Related siblings audited for completeness:

- `gateway/static/error_page.html` (the actual 404 / 5xx template)
- `gateway/static/pages/error_page.css` (the shared error-page styles)
- `gateway/static/403.html` + `gateway/static/pages/403.css` (sibling
  error page that shares `error_page.css` plus its own override file)

Files inspected (absolute paths):

- `/Users/shocakarel/Habbig/gateway/static/error_page.html`
- `/Users/shocakarel/Habbig/gateway/static/403.html`
- `/Users/shocakarel/Habbig/gateway/static/offline.html`
- `/Users/shocakarel/Habbig/gateway/static/pages/error_page.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/403.css`
- `/Users/shocakarel/Habbig/gateway/error_handlers.py`
- `/Users/shocakarel/Habbig/gateway/static/tokens.css` (token reference)
- `/Users/shocakarel/Habbig/gateway/static/mobile-a11y.css` (contrast nets)

---

## Top 3 violations

1. **`offline.html` violates the three-typeface rule and bypasses tokens
   wholesale.** The inline `<style data-keep>` declares
   `font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter,
   system-ui, sans-serif;` (line 47) — system-ui as primary, Inter only
   as a fallback. The narve rule is the opposite: Inter is the body
   face, with `font-display: swap`. The same `<style>` block redefines a
   parallel token universe (`--fg`, `--fg-muted`, `--bg`, `--surface`,
   `--border`, `--accent`) with hardcoded hex values (`#111`, `#6b7280`,
   `#ffffff`, `#f5f5f5`, `#e5e7eb`, `#0d0d0d`, `#9ca3af`) and theme
   switching is driven by `@media (prefers-color-scheme: dark)`, not the
   `[data-theme]` cookie that every other page honours. Net effect:
   offline.html ignores the user's chosen theme, uses the wrong fonts,
   and hard-codes colours that drift from `tokens.css`. (The "must work
   with zero network" constraint is real, but the fix is to embed the
   canonical token values, not invent a parallel set — and to preload
   Inter via the service worker rather than fall back to system-ui.)

2. **`error_page.css` uses `--text-quaternary` (#bbbbbb light / #6e6e6e
   dark) for the Request-ID meta line — that token is documented as
   decorative-only and fails AA at 4.5:1.** Rule at line 163:
   `.nv-error__meta { font-family: var(--font-mono); font-size:
   var(--text-xs, 11px); color: var(--text-quaternary, #bbbbbb); }`. The
   token comment in `tokens.css` line 119 is explicit:
   *"Quaternary is decorative-only — separators, divider glyphs,
   inactive ticks. Fails AA body-text contrast; never use for meaningful
   copy. (If you see a contrast violation pointing at this variable,
   the fix is to switch the rule to --text-tertiary, not to bump this
   token.)"* The Request ID is meaningful copy — users are told on the
   same page to "Quote your request ID to support@narve.ai" — so it
   must read. `#bbbbbb` on `#ffffff` measures ~1.96:1; `#6e6e6e` on
   `#0d0d0d` measures ~3.91:1. Both fail AA in both themes. Fix: swap to
   `var(--text-tertiary)`.

3. **`403.css` (and by extension the `.denied-*` page) breaks the
   monochrome rule and the typography density rules.** Two sub-issues:
   - `.denied-icon { … background: var(--interactive-ghost); … }` plus
     `.denied-icon svg { width: 40px; height: 40px; … }` is decorative
     chrome — narve's anti-decoration policy forbids ornamental icons
     at the page level. The active `403.html` template (audited) does
     not actually render `.denied-icon` (it uses the `.nv-error`
     structure from `error_page.css`), so most of `403.css` is dead
     CSS. But the file still ships and any future regression that
     reverts to `.denied-card` markup will re-introduce the icon.
   - `.denied-title { font-size: 28px; font-weight: 700; }` and
     `.denied-body { font-size: 15px; }` use raw px values instead of
     the `--text-*` token scale (`--text-2xl`, `--text-md`, etc.), and
     `28px / 700` is the wrong style register for a page-level error
     headline — the canonical pattern (used correctly in `.nv-error__
     title` at `error_page.css:29`) is Instrument Serif Italic /
     weight 400 / `clamp(2rem, 6vw, 3.5rem)`. The two surfaces
     therefore look like they belong to different design systems.

---

## All violations

### A. Typography

| # | File | Line(s) | Violation |
|---|---|---|---|
| A1 | `offline.html` | 47 | `font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif` — system-ui leads, Inter is fallback. Rule is Inter-first with `font-display: swap`. (See Top-3 #1.) |
| A2 | `403.css` | 13 | `.denied-title { font-size: 28px; font-weight: 700; }` — raw px, wrong register. Should mirror `.nv-error__title` (Instrument Serif Italic, 400, clamped). |
| A3 | `403.css` | 14 | `.denied-body { font-size: 15px; }` — raw px. Use `var(--text-md)` (16px). 15px also undershoots the iOS-auto-zoom 16px floor for form contexts. |
| A4 | `403.css` | 17 | `.denied-btn { font-size: var(--text-base); }` — `--text-base` is 14px; tokens correct, but the button itself is dead CSS (current `403.html` uses `.nv-error__actions`). |
| A5 | `403.css` | 20 | `.denied-footer { font-size: 12px; }` — raw px. Use `var(--text-xs)`. |
| A6 | `offline.html` style block | 58, 65, 71, 83, 107, 113, 121, 135, 145 | Mixed token usage: some sizes (`var(--text-xs)`, `var(--text-base)`) reference the canonical scale (with fallback aliases at the top), others use raw `12px`, `28px`, `15px`. Pick one path; the canonical answer is tokens only. |

### B. Tokens / hardcoded values

| # | File | Line(s) | Violation |
|---|---|---|---|
| B1 | `offline.html` | 27–32, 35–41 | `:root` and `@media (prefers-color-scheme: dark)` redefine `--fg`, `--fg-muted`, `--bg`, `--surface`, `--border`, `--accent` with raw hex (`#111`, `#6b7280`, `#f5f5f5`, `#e5e7eb`, `#0d0d0d`, `#171717`, `#262626`, `#9ca3af`). These bypass `tokens.css` entirely. (See Top-3 #1.) |
| B2 | `offline.html` | 65 | `h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.01em; }` — narve uses Instrument Serif Italic at this register, not Inter 28/600. |
| B3 | `offline.html` | 78 | `border-radius: 10px;` — not in the radius scale (`--radius-sm` 6, `--radius-md` 8, `--radius-lg` 16). Use `--radius-md`. |
| B4 | `offline.html` | 79 | `padding: 20px;` — not on the 4-px / token scale. |
| B5 | `offline.html` | 95 | `padding: 6px 0;` — raw. Use `var(--space-1)` / `var(--space-2)`. |
| B6 | `offline.html` | 120 | `.retry { padding: 10px 18px; … }` — raw. Same density rule applies. |
| B7 | `offline.html` | 140 | `.status-pill { padding: 4px 10px; … }` — raw px. |
| B8 | `error_page.css` | 19, 20, 26, 35, 41, 47, 51, 60, 65, 66, 71, 82, 89, 99, 104, 105, 109, 127, 128, 129, 137, 145, 146, 151, 155, 167, 170 | Every token reference includes a hardcoded `px` fallback in the `var(--token, NNpx)` form. This works, but the fallbacks lock in old values; if `--space-4` is ever retuned, this file silently won't follow. Acceptable for SW-offline rendering (the original justification per the file header) — flagged as smell, not blocker. The actual file is only loaded over `<link>` so the fallbacks are unreachable in practice; recommend dropping them. |
| B9 | `403.css` | 17 | `padding: 13px 28px;` — raw. `13px` is not on the scale; use `var(--space-3) var(--space-6)` (12/32). |
| B10 | `403.css` | 8 | `padding: 32px;` — should be `var(--space-6)`. |
| B11 | `403.css` | 10 | `width: 80px; height: 80px;` icon dimensions — raw values. The icon itself is the violation; see Top-3 #3. |

### C. Colour / contrast (AA in both themes)

| # | File | Line(s) | Violation |
|---|---|---|---|
| C1 | `error_page.css` | 166 | `.nv-error__meta { color: var(--text-quaternary, #bbbbbb); }` — token explicitly forbidden for body copy; fails 4.5:1 in light (~1.96) and dark (~3.91). This is the Request-ID line shown for every 5xx, the one piece of info the page actively asks the user to copy. (See Top-3 #2.) |
| C2 | `offline.html` | 28, 36 | `--accent: #0d0d0d;` light / `#f5f5f5` dark — and `.retry` then uses `background: var(--accent); color: var(--bg);` (line 124–125). On the resulting button the text colour is `var(--bg)` = `#ffffff` light / `#0d0d0d` dark. Both pass contrast, but the wiring is brittle: any future tweak that flips `--accent` to anything but pure black/white silently breaks the inverse. Should route through `--interactive-bg` / `--interactive-text` instead — which is what `tokens.css` exists to do. |
| C3 | `offline.html` | 105 | `li a:hover, li a:focus-visible { color: var(--accent); … }` — fine in current values but couples link hover to whatever `--accent` happens to be. Per the monochrome rule, hover should change weight / decoration, not pure colour. |
| C4 | `offline.html` | 47 (system-ui fallback effect on contrast) | system-ui's metrics differ between OSes — on Windows ClearType + Segoe UI the same colour values can perceive differently from the macOS measurement. Routing through Inter (the audited target) means the contrast numbers in `mobile-a11y.css` actually apply. |
| C5 | `error_page.css` | 27, 41, 46, 51, 135, 152, 170, 171 | All use `--text-tertiary` (#6e6e6e light ≈ 5.10:1 on #fff, #909090 dark ≈ 4.97:1 on #0d0d0d). Pass AA. **Caveat:** `mobile-a11y.css` line 454 documents that `--text-tertiary` *"still fails at 4.5:1"* on certain gray backgrounds. `.nv-error` sits on `--bg-base` (#ffffff / #0d0d0d), so the numbers above are the operative ones — passes. But the `.nv-error__support` line wraps a `mailto:` link in `--text-tertiary` colour with `text-decoration: underline` only; that satisfies axe's `link-in-text-block` rule via underline, fine. Flag is informational. |
| C6 | `403.css` | 14, 16, 20 | References `var(--text-secondary)` and `var(--text-muted)` (legacy alias → `--text-tertiary`). `--text-muted` is a deprecated alias; new code shouldn't use it. |

### D. Light + dark theme handling

| # | File | Line(s) | Violation |
|---|---|---|---|
| D1 | `offline.html` | 33 | Theme switching driven by `@media (prefers-color-scheme: dark)` only, not `[data-theme="dark"]`. The rest of the site uses the `narve-theme` cookie with an inline anti-FOUC script (see `error_page.html:9`, `403.html:14`). A user who has explicitly chosen light on a dark-OS will see dark offline.html, contradicting their setting site-wide. |
| D2 | `offline.html` | — | No theme cookie read at all. The standalone constraint is real, but the cookie read is a few lines of JS that runs before paint; reproduce the snippet from `error_page.html:9` inline. |
| D3 | `offline.html` | 9 | `<meta name="theme-color" content="#0d0d0d">` — hardcoded dark. Should be flipped per theme (`#ffffff` light / `#0d0d0d` dark), set after the cookie read or via a `prefers-color-scheme` `<meta>` pair. |

### E. Mobile / a11y

| # | File | Line(s) | Violation |
|---|---|---|---|
| E1 | `error_page.css` | 56–92 | `.nv-error__search input` has no explicit `font-size` at the mobile-zoom-floor of 16px. The body rule sets `var(--text-base, 14px)` which is 14px — under the iOS 16px no-auto-zoom threshold. Tap into the search input on iOS Safari and the page will zoom. Add a `@media (max-width: 640px)` bump to 16px (or apply `font-size: max(16px, var(--text-base))`). |
| E2 | `error_page.css` | 82–92, 101–122 | `.nv-error__search button` and `.nv-error__actions a` have no `min-height` floor. Padding `var(--space-2, 8px) var(--space-4, 16px)` at `--text-sm` (13px) gives a real height around 33–35px — below the 44×44 tap-target floor. `403.css:17` got this right with explicit `min-height: 44px` — `error_page.css` should match. |
| E3 | `offline.html` | 168 | `.retry` button: `padding: 10px 18px; font-size: var(--text-base, 14px);` — heights to ~36px. Below 44×44. |
| E4 | `error_page.css` | — | No mobile breakpoint at all. The `.nv-error` rule sets `max-width: 540px; margin: 12vh auto; padding: 0 var(--space-4)`. At 360×740 this is workable (16px side gutter, 540px ≤ viewport so content fills then truncates), but the `clamp(2rem, 6vw, 3.5rem)` headline at 360px → ~21.6px → smaller than `--text-2xl`. Acceptable; flagged as smell only. |
| E5 | `offline.html` | 53 | `.wrap { max-width: 520px; margin: 48px auto; }` — outer body has `padding: 32px 24px` so no horizontal overflow at 360px. OK. |
| E6 | `error_page.html` | — | No `class="brand-logo"` / wordmark anywhere. The page is text-only header, which is consistent with `403.html`. The narve-design wordmark pattern (`.wordmark` Instrument Serif Italic + " / " + Geist Mono slug) is not used; OK because this is a chrome-less error page, but worth noting that breadcrumb context is fully absent — a user landing on a 503 has no way to navigate the brand back to home except via the action button. |
| E7 | `error_page.html` | 9 | Theme bootstrap script is correct and matches the canonical anti-FOUC snippet — good. |
| E8 | `offline.html` | 150 | `@keyframes pulse` runs at 1.8s ease-in-out infinite with no `prefers-reduced-motion` guard. Narve rule: "Reveal-on-scroll animations … `prefers-reduced-motion` MUST be respected." Status-pill pulse is a continuous animation; wrap in `@media (prefers-reduced-motion: no-preference)` or set `animation: none` under `prefers-reduced-motion: reduce`. |
| E9 | `offline.html` | — | No focus-visible styling on `.retry` button — relies on UA default. Other narve buttons get an explicit 2px outline ring (see `gateway.css:716`). |

### F. Component / pattern violations

| # | File | Line(s) | Violation |
|---|---|---|---|
| F1 | `403.css` | 8–20 | Entire `.denied-*` ruleset is dead code — `403.html` uses `.nv-error` markup. Either delete `403.css` (the page already loads `error_page.css` on line 13 of `403.html`) or strip it down to whatever overrides are still in use. Carrying dead CSS that contradicts the live template's design is how the next regression accidentally restores the icon. |
| F2 | `403.html` | 19–23 | The comment block ("Audit flagged the previous version as over-decorated (icon + countdown + auto-redirect)") confirms the icon-based design was already rejected. Removing `403.css` would finish that cleanup. |
| F3 | `error_page.html` | 18 | `<p class="nv-error__code">Error · {{ status }}</p>` — copy is `Error · 404`. Narve uses middot in nav (`narve.ai · Terms · Privacy`) so this is consistent. Good. |
| F4 | `error_page.html` | 31–32 | Scripts loaded: `cmdk.js`, `share_menu.js`. Share menu on a 404 is unusual — there's nothing to share. Not a design-system rule violation per se, but worth removing to keep the error surface chrome-free. |
| F5 | `error_page.html` | — | No `<noscript>` fallback. The page renders fine without JS (only cmdk + share need it), so this is fine. |
| F6 | `offline.html` | 13–152 | Inline `<style data-keep>` block. The narve-design anti-pattern list says: *"Add a `<style>` block in a page template (extend `gateway.css` or component CSS instead). One template (`feedback.html`) currently has one — don't replicate the pattern."* Offline.html replicates that pattern, with the explicit `data-keep` marker telling the foundation lint to leave it alone. The "must work offline" justification is valid for the *page* but not for the *style block as token re-invention*. Inline the canonical token values (light + dark) and the canonical Inter font-face data URL, and the file can both ship offline-safe AND honour the design system. |

### G. 505 specifically

| # | Surface | Violation |
|---|---|---|
| G1 | `error_handlers.py` | 505 is not in `_STATUS_TO_SLUG`, `_STATUS_TO_TITLE`, or `_STATUS_TO_MESSAGE`. A live 505 would render with the catch-all `"Error" / "Something went wrong."` strings — fine as a safety net, but the design audit cannot verify a 505-specific page because none is designed. |
| G2 | — | 505 ("HTTP Version Not Supported") is exceedingly rare in practice (an HTTP/2-only origin behind an HTTP/1.0 client). If the spec genuinely expects a designed surface, add the title + message to the three dicts in `error_handlers.py` and the existing template handles the rest. No new HTML file is needed. |

---

## Severity rollup

- **Blocking (fix before next deploy):** C1 (request-ID line fails AA in
  both themes — a contrast regression on a page literally asking users
  to read that string), E2 (tap targets), E1 (iOS zoom on 404 search
  input).
- **High (next sprint):** A1 / B1 / D1 / D2 (offline.html token + theme
  + font misalignment), F1 (dead `403.css`).
- **Medium:** A2–A5, B2–B11, E3, E8, E9.
- **Low / informational:** B8 (px fallback smell), C5 (token note),
  F4 (share_menu on error page).

## Violations count

- **Hard violations across audited surfaces: 38**
  - A. Typography: 6
  - B. Tokens / hardcoded values: 11
  - C. Colour / contrast: 6 (of which C1 fails AA)
  - D. Theme handling: 3
  - E. Mobile / a11y: 9 (of which E1–E3 are tap-target / zoom blockers)
  - F. Component / pattern: 6 (F1 is the dead-CSS one)
  - G. 505 coverage gap: 1 design-system gap (no surface to audit)
- Of these, **3 fail AA** in at least one theme on either light or dark
  (C1 in both themes; D3 is hardcoded-dark `theme-color`; C2 is brittle
  but currently passing).
- **Top 3** captured at the top of this file.

---

*End of audit. No code was changed by this audit pass — it is read-only.*
