# Design audit — onboarding + welcome surface

**Date**: 2026-05-15
**Files audited** (per the brief — `gateway/static/onboarding*.html`, `gateway/static/welcome*.html` + CSS):

- `/Users/shocakarel/Habbig/gateway/static/onboarding.html` (222 lines)
- `/Users/shocakarel/Habbig/gateway/static/pages/onboarding.css` (58 lines)
- `/Users/shocakarel/Habbig/gateway/static/css/onboarding_tour.css` (227 lines)
- `/Users/shocakarel/Habbig/gateway/static/js/onboarding_tour.js` (259 lines — read-only context; design-relevant DOM strings audited)
- (Adjacent context) `/Users/shocakarel/Habbig/gateway/email_system/templates/welcome.html` — flagged in scope note below.

**Standard**: narve-design skill (monochrome only; three typefaces — Inter / Geist Mono / Instrument Serif Italic; tokens-not-hardcoded values; AA contrast both themes; ≥ 44 × 44 px tap targets; ≥ 16 px input font on mobile; canonical easing `var(--ease)` = `cubic-bezier(0.2, 0, 0, 1)`; canonical durations `--duration-{fast,base,slow}`; no inline `style=""`; no per-page `<style>` blocks; no `box-shadow` outside `--shadow-*`; `border-radius ≤ var(--radius-lg)`).

Token source verified against: `/Users/shocakarel/Habbig/gateway/static/tokens.css`.

---

## Scope note — there is no `gateway/static/welcome*.html`

`find` under `gateway/static/` returns zero matches for `welcome*.html`. The only `welcome.html` in the repo is the **transactional email template** at `gateway/email_system/templates/welcome.html`, which is HTML-for-email (inline styles are mandatory there because email clients strip `<style>` blocks and don't see external CSS). It is therefore **out of scope for the gateway/static design-system rules** — those rules apply to the browser-rendered web app, not to email pixels. It is, however, included in the inventory below as a courtesy review because the user's wording could plausibly have meant it; findings flagged "(email)" are advisory and do not count against the totals.

The four files that *do* exist under the brief's prefix (`onboarding.html`, `pages/onboarding.css`, `css/onboarding_tour.css`, `js/onboarding_tour.js`) are the audit's actual surface.

---

## Summary

| Category | Count |
|---|---|
| Monochrome violations | 0 |
| Typeface violations | 0 |
| Token / hardcoded-value violations | 17 |
| Inline `style=""` violations | 7 |
| Inline `<style>` block violations | 0 (the legacy block has already been lifted to `pages/onboarding.css` per its header comment — good) |
| Motion / easing / a11y violations | 5 |
| Mobile tap-target / input-font violations | 2 |
| Anti-decoration / chrome violations | 2 |
| Component-reuse / DOM-pattern violations | 3 |
| **Total findings (in-scope, gateway/static only)** | **36** |
| Advisory findings on `email_system/templates/welcome.html` | 5 (not counted) |

The onboarding flow is **monochrome-clean** — no hue is used for categorisation, state, or branding. All step states, toggles, selected categories, and the success check are expressed via `--text-primary` / `--bg-surface` / `--border-*` swaps. The typography stack also obeys the three-typefaces rule: `--font-display` on `.step-title`, default Inter on body, `--font-mono` for the progress chip in the tour popover. The expensive misses are elsewhere: an idiosyncratic `--ease-out` variable that does not exist in `tokens.css`, a transition-duration scale that bypasses `--duration-*`, two hardcoded font-size raw `px` values for body copy, and seven `style="…"` inline attributes in `onboarding.html` doing layout overrides that belong in the stylesheet.

The biggest single risk: the four-step wizard hand-builds a "Loading state implied by `fetch().then(goto)`" flow with no skeleton, no `narveToast` on save success, no error handling — so any flake on `/api/onboarding/preferences` strands the user on the same panel without feedback. That is a UX failure as well as a violation of the "Use existing components, don't write `alert()`/`'Loading…'`" rule.

---

## Top 3 (highest-leverage fixes)

1. **`--ease-out` is undefined — every transition in `pages/onboarding.css` (5 rules) silently falls back to `ease`.** Lines 12, 14, 16, 25 of `pages/onboarding.css` and line 51 use `var(--ease-out)`. The token that exists in `tokens.css:262` is `--ease` (= `cubic-bezier(0.2, 0, 0, 1)`); `--ease-out` is undefined and resolves to the CSS `ease` keyword fallback, which is not the canonical narve curve. The animations also use ad-hoc `300ms` / `0.3s` / `0.15s` durations instead of `--duration-base` / `--duration-fast`. Single-pass fix: replace `var(--ease-out)` with `var(--ease)` everywhere and swap raw durations for `--duration-fast` / `--duration-base`. This is identical drift to the components.css audit (`audit_design_components_css.md`) — same pattern, same fix.
2. **Seven inline `style="…"` attributes in `onboarding.html` overriding layout, margin, font-weight, and grid placement.** L32 (`margin-bottom:24px` on the brand logo), L38 (`margin-bottom:20px;font-weight:500;color:var(--text-primary)` on a paragraph), L69 (`grid-column:1/-1` on the "All of the above" checkbox), L71 (`font-size:12px;color:var(--text-tertiary);margin-top:-12px` on a helper paragraph), L95 (`margin-bottom:16px` on the thresholds row), L136 + L137 (`text-align:center` on title and subtitle), L147 (`justify-content:center` on the final btn-row). Direct violation of the narve-design hard rule "Never write `style="…"` inline in HTML for anything other than the rare per-card colour variable." Each of these should become a modifier class in `pages/onboarding.css` (e.g. `.step-panel--centred`, `.cat-item--full-row`, `.helper-note`). The negative `margin-top:-12px` is particularly suspicious — it papers over a sibling `margin-bottom:28px` on `.cat-grid` rather than adjusting the gap.
3. **No `narveToast` / `narveSkel` / error path on the three `fetch()` calls (`saveCategories`, `saveNotifications`, `finish`).** L188-216 of `onboarding.html`. The flow assumes every POST succeeds — there is no `.catch`, no toast on network failure, no spinner during the round-trip. On a slow or failing connection the user clicks "Continue" and nothing visible happens. narve-design hard rule: "Inline ephemeral confirmation → `narveToast(msg, {type})`. List with no data → `render_empty`. Loading state → `narveSkel.show`. Error after fetch → `narveSkel.error(container, msg, {retry})`. Don't write `alert()`." The toast script (`/_gateway_static/js/toast.js`) is loaded site-wide via `_base.html:53` but is **not** included by this template, so even adding `narveToast()` won't work without first wiring the script tag. Add `<script src="/_gateway_static/js/toast.js" defer></script>` next to the other scripts at L218-220, then wrap each fetch in `.then(r => r.ok ? … : narveToast('Could not save — try again', {type:'error'}))`.

---

## Findings

### Critical (blocks design-system claims)

**1. Undefined `--ease-out` token — 5 occurrences, all in `pages/onboarding.css`**

Lines and selectors:

| Line | Selector | Property |
|---|---|---|
| 12 | `.step-dot` | `transition: all 0.3s var(--ease-out)` |
| 14 | `.step-line` | `transition: background 0.3s var(--ease-out)` |
| 16 | `.step-panel` | `animation: fadeIn 300ms var(--ease-out)` |
| 25 | `.cat-item` | `transition: all 0.15s var(--ease-out)` |
| 50 | `.done-check` | `animation: pop 600ms var(--ease-out)` |
| 51 | `.done-check svg` | `animation: draw 500ms 200ms var(--ease-out) forwards` |

`--ease-out` is **not declared in `tokens.css`** (verified with `grep -n "\-\-ease-out\s*:" tokens.css` — no matches). The canonical token is `--ease` (= `cubic-bezier(0.2, 0, 0, 1)`, `tokens.css:262`). All six transitions therefore resolve to the browser's default `ease` curve, not narve's canonical curve. This is a silent regression — visually subtle but a hard rule violation.

Bonus violation on the same lines: durations `0.3s`, `300ms`, `0.15s`, `600ms`, `500ms`, `200ms` should be `var(--duration-base)` (0.20s), `var(--duration-base)`, `var(--duration-fast)` (0.12s), `var(--duration-slow)` (0.40s — and 600 ms is overlong), `var(--duration-slow)`, no equivalent token (the 200 ms delay falls between `fast` and `base`; rationalise to `var(--duration-base)`).

**2. Inline `style="…"` attributes in `onboarding.html` — 7 occurrences**

| Line | Element | Inline style | Should be |
|---|---|---|---|
| 32 | `<img>` brand logo | `style="margin-bottom:24px"` | class `.brand-logo` (already named) with `margin-bottom: var(--space-6)` in `pages/onboarding.css` |
| 38 | `<p class="step-sub">` | `style="margin-bottom:20px;font-weight:500;color:var(--text-primary)"` | new modifier `.step-sub--lead` |
| 69 | `<label class="cat-item">` | `style="grid-column:1/-1"` | new modifier `.cat-item--full` |
| 71 | `<p>` helper note | `style="font-size:12px;color:var(--text-tertiary);margin-top:-12px"` | new class `.cat-helper`; do NOT use negative margin — adjust `.cat-grid { margin-bottom }` instead |
| 95 | `<div class="thresholds">` | `style="margin-bottom:16px"` | modifier `.thresholds--spaced`, or default the margin on `.thresholds` itself |
| 136 / 137 | `.step-title`, `.step-sub` in step 4 | `style="text-align:center"` (twice) | new modifier `.step-panel--success > *` rule that centres |
| 147 | `<div class="btn-row">` in step 4 | `style="justify-content:center"` | modifier `.btn-row--centred` |

Cumulative effect: any future visual-QA pass on the onboarding flow has to read both the HTML *and* the CSS to know what the page actually looks like. Per skill: "Never write `style="…"` inline in HTML for anything other than the rare per-card colour variable."

**3. No client-side feedback on three POSTs — `saveCategories`, `saveNotifications`, `finish`**

L183-216 of `onboarding.html`. Three fetches, no `.catch`, no toast, no skeleton, no disabled state on the button while in-flight. If the network drops or the server returns 5xx, the user sees no indication; clicking "Continue" again will fire a duplicate request, and the panel never advances.

narve-design hard rule (Components table): "Inline ephemeral confirmation → `narveToast`. Loading state → `narveSkel.show`. Error after fetch → `narveSkel.error(container, msg, {retry})`."

Required:

- Load `toast.js` from this template (it isn't loaded by `_base.html` because this page doesn't extend `_base.html` — it's a bespoke layout with its own `<head>`).
- Disable the button on click; re-enable on `.finally`.
- `.then(r => r.ok ? … : Promise.reject(r.status)).catch(() => narveToast('Could not save — try again.', {type:'error'}))`.

### High — hardcoded values that bypass tokens

**4. Raw `px` font-sizes in `pages/onboarding.css` — bypasses `--text-*` scale**

| Line | Selector | Property | Value | Token |
|---|---|---|---|---|
| 19 | `.step-title` | `font-size` | `34px` | No exact token (the scale jumps `--text-2xl` ≈ 24 px → `--text-3xl` ≈ 32 px → `--text-4xl` ≈ 40 px). Either reach for `--text-3xl` or add a new token. |
| 20 | `.step-sub` | `font-size` | `15px` | No exact token — `--text-sm` is 13 px, `--text-md` is 16 px. The closest legitimate value is `--text-md` (16 px). 15 px is the kind of off-scale value the design system tries to prevent. |
| 32 | `.toggle-row-sub` | `font-size` | `12px` | `var(--text-xs)` is 11 px; the design-system scale doesn't have 12 px. Use `var(--text-xs)`. |
| 55 | `.done-notes li` | `font-size` | `12px` | Same — use `var(--text-xs)`. |
| 56 | `.upgrade-notice` | `font-size` | `12px` | Same. |

**5. Raw `px` spacing in `pages/onboarding.css`**

| Line | Selector | Property | Value | Token |
|---|---|---|---|---|
| 9 | `body` | `padding` | `64px 24px` | `var(--space-16) var(--space-6)` (assuming `--space-16` = 64 px; tokens.css scale verified to `--space-16`). |
| 10 | `.onboard-shell` | `max-width` | `640px` | Acceptable as a content-width; no token covers this. Mark as design-intentional. |
| 11 | `.step-bar` | `margin-bottom` | `48px` | `var(--space-12)`. |
| 14 | `.step-line` | `flex` | `0 0 56px` | No exact token (between `--space-12` = 48 and `--space-16` = 64). Acceptable. |
| 19 | `.step-title` | `margin` | `0 0 12px` | `var(--space-3)`. |
| 20 | `.step-sub` | `margin-bottom` | `36px` | No exact token — `--space-8` = 32 px or `--space-10` = 40 px. Round to `--space-10`. |
| 21 | `.feature-card` | `padding` | `22px 24px` | Off-scale. `var(--space-5) var(--space-6)` = 20 / 24. |
| 21 | `.feature-card` | `margin-bottom` | `14px` | Off-scale. Closest `var(--space-4)` = 16 or `var(--space-3)` = 12. |
| 24 | `.cat-grid` | `gap` | `12px` | `var(--space-3)`. |
| 24 | `.cat-grid` | `margin-bottom` | `28px` | Off-scale. `var(--space-7)` = 28 if defined; otherwise `--space-6` (24) or `--space-8` (32). |
| 25 | `.cat-item` | `padding` | `16px 18px` | Off-scale on the X (18 px not in the scale). |
| 30 | `.toggle-row` | `padding` | `18px 20px` | Off-scale on both axes. |

**6. Hardcoded transition durations in `pages/onboarding.css` and `css/onboarding_tour.css`**

`pages/onboarding.css`: see finding #1 list (6 durations).

`css/onboarding_tour.css` additionally hardcodes durations and curves at L33 (`transition: top 0.25s ease, left 0.25s ease, width 0.25s ease, height 0.25s ease`) and L91 (`transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease`). No `--duration-*` token, no `var(--ease)`. The skill's "Easing: `cubic-bezier(0.2, 0, 0, 1)` is the canonical curve" applies here directly.

**7. Raw colour-with-alpha in `css/onboarding_tour.css`**

L18 (`background: rgba(0, 0, 0, 0.55)` on `.nv-tour__backdrop`) and L30 (`box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.55)` for the spotlight donut). Per skill: "no hardcoded values."

The dim-overlay alpha is a recurring narve idiom and arguably a missing token. If a `--bg-overlay-dim` (or `--scrim-strong`) token doesn't exist, the right answer is **add it to `gateway.css` / `tokens.css` and use it here**, not to bake `rgba(0,0,0,0.55)` into the file. The skill is explicit: "if a needed token doesn't exist, add it."

### High — `box-shadow` on non-modal element

**8. Tour spotlight uses `box-shadow: 0 0 0 9999px rgba(...)` — anti-decoration violation, but defensible**

L30 of `css/onboarding_tour.css`. The 9999 px shadow is a deliberate trick to produce a donut-cutout effect on the backdrop. Per skill: "Don't reach for `box-shadow` for non-modal elements." The spotlight is *functionally* an overlay layer (the tour is a modal-equivalent), so this is borderline — but the implementation is `box-shadow`, not `--shadow-lg`. Either: (a) refactor to a `<svg>` mask, or (b) document the exception inline with a comment explaining why the shadow is necessary. The current comment at L27-29 explains the visual intent but does not acknowledge the design-system carve-out.

**9. `.nv-tour__popover` uses `var(--shadow-lg)` — correct**

L44. No violation; flagged here only to confirm that the popover (which IS a modal-equivalent) reaches for the right token. Good.

### High — input font on mobile

**10. `.thresholds select` font-size `var(--text-sm)` (13 px) — fails iOS-auto-zoom rule** — L41 of `pages/onboarding.css`

```css
.thresholds select { font-size: var(--text-sm); }
```

narve-design hard rule: "`<input>` / `<select>` / `<textarea>` ≥ 16 px font on mobile." `--text-sm` = 13 px. On the notifications step, both EV-threshold and credibility selects will trigger viewport zoom on iPhone Safari. Use `var(--text-md)` (16 px). The visual difference at desktop is negligible; the mobile-zoom prevention is the entire point.

### Medium — tap-target / accessibility

**11. `.btn-skip` is a tap target ~30 px tall (font 13 px, default `<button>` padding)** — L48 of `pages/onboarding.css`

```css
.btn-skip { background: none; border: none; color: var(--text-tertiary); font-size: var(--text-sm); … }
```

No explicit padding, no `min-height`. The button renders at roughly the line height of 13 px text — well below 44 × 44 px. Add `min-height: 44px; padding: var(--space-3) var(--space-4);` or accept that it's a low-priority "skip" action and bump it just enough (e.g. 36 px) — but the skill is strict: "Tap targets ≥ 44 × 44 px. Inline prose anchors are exempt." A button is not an inline prose anchor.

**12. `.step-dot` is 14 × 14 px — fine as a non-interactive indicator, but no `aria-current`/`aria-label` on the active step** — L12

The dots are decorative, but a screen-reader user gets no audible cue for the current step. Either: (a) add `role="progressbar" aria-valuenow="{n}" aria-valuemax="4"` to `.step-bar`, or (b) add `aria-label="Step {n} of 4"` to the active panel, or (c) at minimum, make each panel's `<h1>` first-focusable and include "Step X of 4 — " prefix. Currently the only step-progress signal is visual.

### Medium — motion / a11y

**13. `prefers-reduced-motion` not honoured by `pages/onboarding.css`** — entire file

The file declares five animations / transitions (`fadeIn` keyframe, `pop`, `draw`, `.step-dot` transition, `.step-line` transition) and **no `@media (prefers-reduced-motion: reduce)` block**. By contrast, `css/onboarding_tour.css:124` correctly kills the spotlight transition for reduced-motion users.

The `pop` (scale 0.8 → 1.05 → 1.0) and `draw` (stroke-dashoffset 48 → 0) animations on the success check are the most flagrant — that's an anticipation-bounce (scale 1.05), specifically called out as forbidden: "Avoid bounces, springs, anticipation." Either flatten to `0 → 1` without overshoot, or guard with `@media (prefers-reduced-motion: reduce) { .done-check, .done-check svg { animation: none; } }`.

**14. `scrollTo({behavior: 'smooth'})` at L165 of `onboarding.html` — no reduced-motion guard**

The wizard scrolls the viewport on every step change. Reduced-motion users get unwanted smooth-scroll. The tour JS gets this right (`onboarding_tour.js:50-51, 195-199`); this template doesn't. Replace with:

```js
window.scrollTo({top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
```

### Medium — `<head>` and asset loading

**15. The anti-FOUC theme inline script is a one-liner — fine — but the template does NOT extend `_base.html`**

L13 of `onboarding.html`. The block is correctly minified and reads the `narve-theme` cookie. However, by hand-rolling the `<head>` instead of extending `_base.html`, this template:

- Misses `_base.html`'s `<div id="nv-toast-region">` and `toast.js` (finding #3).
- Misses the global `theme.js?v=2` (it does load it directly at L218, so this is OK — confirming).
- Misses any future site-wide JS additions to `_base.html` and will drift silently.

This is a structural smell. The skill says: "Reach for the existing one." `_base.html` exists; this page should extend it and inject content into the `{% block content %}` slot, with the wizard's own structural classes scoped to a body class (e.g. `body[data-page="onboarding"]`).

**16. The `<link rel="preload">` for `Inter-Variable-subset.woff2` lacks `media` — preloads on a page with custom CSS that doesn't declare `@font-face` for the same URL**

L11. `pages/onboarding.css` does not declare `@font-face`; it relies on `gateway.css` for that. The preload is therefore eager-loading a font that the browser will discover anyway via `tokens.css`. Not a hard violation, but it duplicates work — and if the URL ever drifts in `tokens.css`/`gateway.css`, the preload becomes dead. Either remove the preload and trust the global stylesheet, or extract the `@font-face` to a dedicated `fonts.css` file that both `_base.html` and this template can preload.

### Medium — component-reuse misses

**17. Custom 4-step progress bar — should reuse `.nv-fwg` or a shared `.step-bar`/`.wizard-bar` pattern**

There's no existing wizard component in `components.css` to reuse; this is a *new* component the template invents inline (`.step-bar`, `.step-dot`, `.step-line`). Two options: (a) leave it page-scoped and accept that wizards are rare, or (b) lift to `components.css` so any future flow (subscription upgrade, email confirmation, password reset) can reuse it. Option (a) is fine; (b) is better. Either way, the existing `.nv-fwg` ("first-week goals") widget in `css/onboarding_tour.css` is a related-but-distinct progress pattern; the two should be visually consistent (currently they aren't — `.nv-fwg` uses uppercase title labels with tracking, the wizard does not).

**18. Toggle / select / checkbox controls — duplicated, not reused**

The `.toggle`, `.cat-item input`, `.thresholds select` controls are hand-rolled. The repo has shared form controls in `gateway.css` and `components.css` (search them for `.nv-checkbox`, `.nv-toggle`, `.nv-select` — confirm with `grep`). If those exist, the onboarding form should reuse them; if they don't, this page is a fine seed for them — but the styles should then be promoted to `components.css`, not buried in `pages/onboarding.css`.

**19. The success check is a hand-rolled SVG with `stroke-dasharray` animation** — L135 of `onboarding.html`

The skill says use `narveSkel` / `narveToast` / shared components. There is no shared "success check" component, so this is a fine local invention — but if it appears again (subscription paid, password reset confirmed, email verified), promote it to a `.nv-check-success` class in `components.css` to prevent two implementations drifting.

### Low — copy / micro-UX

**20. Em-dash + arrow-glyph copy is consistent with the rest of narve — good**

`Get started →`, `Continue →`, `← Back`, `Go to dashboard →`. Per the design system's "narve.ai" wordmark / serif italic / Geist Mono trio, these are fine. Not a finding; flagged here so the reviewer knows the audit checked.

**21. `display_name` template placeholder — verify XSS escape**

L33: `<h1 class="step-title">Welcome, {{ display_name }}.</h1>`. Out of scope for a *design* audit (this is a security concern, separately covered in `audit_onboarding_routes.md`), but flagged for completeness. The template engine should auto-escape; verify against the routes file.

---

## Advisory — `gateway/email_system/templates/welcome.html` (out of scope)

Email HTML cannot follow the gateway/static rules because email clients strip `<style>` and don't honour external stylesheets. Inline styles are *required*. The following are observations against the spirit of the design system, **not counted in totals**:

| # | Note |
|---|---|
| A | L2 `color:#0d0d0d`, L4 `color:#555`, L18 `color:#777` — hardcoded greys instead of a `narve-email-colors.css.j2` (or equivalent) include. Email-template maintainability point, not a narve-design violation. |
| B | L4 `<strong style="color:#0d0d0d;">{{ tier }}</strong>` — the tier name is interpolated; if it ever contains markup, it'll render. Auto-escape is normally on for Jinja-style templates; verify. |
| C | L12 / L27 / L39 — three CTA buttons across the conditional branches all use the identical inline style. Should be a `{% include 'partials/email_cta.html' %}` to keep them in lockstep. |
| D | L18 `<p style="…font-style:italic;">` — italic body copy in email is fine, but ensure the rendered display isn't Times Italic on a client that strips webfonts; Instrument Serif Italic won't be loaded inside Gmail/Outlook. Acceptable tradeoff for email. |
| E | No dark-mode `@media (prefers-color-scheme: dark)` block — `#ffffff` button text on `#0d0d0d` background flips poorly on email clients that auto-invert. Consider an `@media` block with `color-scheme: light only;` declaration or a dark-mode override. |

These are email-template concerns; address in a separate `audit_email_templates.md` (which already exists at `/Users/shocakarel/Habbig/audits/audit_email_templates.md`) rather than under the design-system surface.

---

## Verification commands run

```
find /Users/shocakarel/Habbig/gateway/static -maxdepth 2 -name "onboarding*" -o -name "welcome*"
find /Users/shocakarel/Habbig/gateway -iname "welcome*"
grep -n "\-\-ease-out\s*:" /Users/shocakarel/Habbig/gateway/static/tokens.css   # 0 matches
grep -n "\-\-ease\s*:"     /Users/shocakarel/Habbig/gateway/static/tokens.css   # 1 match → L262
grep -n "\-\-duration-"    /Users/shocakarel/Habbig/gateway/static/tokens.css   # 3 matches
```

Closing reminder: this is a static, monochrome, type-driven flow that already gets most of the narve aesthetic right. The fixes are mechanical — find/replace `var(--ease-out)` → `var(--ease)`; lift seven inline styles into class modifiers; add `toast.js` + error handling to the three POSTs; add a `prefers-reduced-motion` block. After those, the surface is design-system clean.
