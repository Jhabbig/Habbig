# Design audit — `gate.html` + `pages/gate.css`

**Date**: 2026-05-15
**Files audited**:
- `/Users/shocakarel/Habbig/gateway/static/gate.html`
- `/Users/shocakarel/Habbig/gateway/static/pages/gate.css`

**Standard**: narve-design skill (monochrome only, 3 typefaces, tokens not hardcoded values, AA contrast both themes, ≥ 44×44 tap targets on mobile, ≥ 16px input font).

---

## Summary

| Category | Count |
|---|---|
| Monochrome violations | 0 |
| Typeface violations | 0 |
| Token / hardcoded-value violations | 14 |
| Theme / contrast (AA) violations | 2 |
| Mobile tap-target / input-font violations | 2 |
| Motion / a11y violations | 2 |
| Anti-pattern (inline `style=`, etc.) violations | 3 |
| **Total findings** | **23** |

The page is monochrome (no hue used to encode meaning, error message uses `--text-secondary` not red — compliant). Typeface count is within budget — only `--font-ui` (Inter) and `--font-display` (Instrument Serif Italic) are referenced; Geist Mono is unused but its absence isn't a violation. The real problems are hardcoded values, an invisible focus ring on light theme, an input that triggers iOS auto-zoom, and a small logo tap target.

---

## Findings

### Critical (blocks design-system claims)

**1. Input font-size < 16 px — iOS auto-zoom trigger** — `gate.css:48`
```css
.gate-input { font-size: 0.9rem; }   /* 14.4 px — fails ≥ 16 px rule */
```
narve-design hard rule: *"`<input>` / `<select>` / `<textarea>` ≥ 16 px font on mobile (defeats iOS auto-zoom)."* Use `var(--text-md)` (= 16 px). Pasting a token into an existing token is the canonical fix.

**2. Focus ring fails AA contrast on light theme** — `gate.css:54`
```css
.gate-input:focus-visible { box-shadow: 0 0 0 3px rgba(240,240,240,0.04); }
```
`rgba(240,240,240,0.04)` on white `--bg-base` is effectively invisible (< 1.1:1, well below the 3:1 focus-ring floor). The hardcoded `240,240,240` matches the *dark*-theme `--text-primary` — clearly copy-paste from a dark mock that never got theme-flipped. Use `outline: var(--focus-ring); outline-offset: 2px;` (token already exists in `tokens.css:287`).

**3. Logo link tap target < 44 × 44 px on mobile** — `gate.html:18-21` + `gate.css:12-30`
The `.gate-logo` anchor is a 28 × 28 icon + 1.1 rem text inline-flex with `gap: 10px`. The total clickable height is ≈ 28 px — under the 44 px floor. Either bump the click region with padding (`padding: var(--space-2);` ≈ 8 px on all sides → 44 px) or apply `min-height: 44px; display: inline-flex; align-items: center;` while keeping the visual icon size.

### High — hardcoded values that bypass tokens

**4. Hardcoded paddings, margins, gaps, top/left positions** — `gate.css`
| Line | Rule | Value | Token |
|---|---|---|---|
| 14 | `.gate-logo` `top` | `24px` | `var(--space-5)` |
| 15 | `.gate-logo` `left` | `24px` | `var(--space-5)` |
| 18 | `.gate-logo` `gap` | `10px` | (closest: `var(--space-3)` = 12 px — or add `--space-2-5`) |
| 22 | `.gate-logo-icon` `width/height` | `28px` | (no token — keep as literal, but document) |
| 32 | `.gate-card` `padding` | `24px` | `var(--card-pad)` (resolves to 16 px comfortable / 12 px compact — design intent may want `--space-5` = 24 px instead, but pick a token) |
| 32 | `.gate-card` `max-width` | `340px` | (no token — acceptable layout literal, but worth surfacing as `--gate-card-max`) |
| 38 | `.gate-heading` `margin-bottom` | `1.5rem` | `var(--space-5)` |
| 43 | `.gate-input` `padding` | `0 16px` | `0 var(--space-4)` |
| 59 | `.gate-btn` `margin-top` | `10px` | (closest: `var(--space-3)` = 12 px) |
| 73 | `.gate-error` `margin-top` | `10px` | same as above |
| 74 | `.gate-error` `min-height` | `20px` | (layout literal — acceptable but flag) |

**5. Hardcoded font-sizes (bypass `--text-*` scale)** — `gate.css`
| Line | Selector | Hardcoded | Token |
|---|---|---|---|
| 27 | `.gate-logo-text` | `1.1rem` (~17.6 px) | `var(--text-md)` (16 px) or `var(--text-lg)` (18 px) |
| 37 | `.gate-heading` | `1.2rem` (~19.2 px) | `var(--text-lg)` (18 px) or `var(--text-xl)` (20 px) |
| 48 | `.gate-input` | `0.9rem` | `var(--text-md)` (also fixes finding #1) |
| 65 | `.gate-btn` | `0.9rem` | `var(--text-md)` |
| 71 | `.gate-error` | `0.8rem` (~12.8 px) | `var(--text-xs)` (11 px) or `var(--text-sm)` (13 px) |

narve-design hard rule: *"Sizes use the `--text-*` token scale (xs through 5xl). No raw px values for type."* `rem` is not on that list either — only tokens.

**6. Heights hardcoded (44 px is correct but should be tokenised)** — `gate.css:42, 58`
```css
.gate-input { height: 44px; }
.gate-btn   { height: 44px; }
```
44 px is the correct tap-target minimum, but tokens like `--space-7` (48 px) or a new `--control-h` would prevent drift. Not a violation — flag for follow-up.

**7. Letter-spacing literal** — `gate.css:28`
```css
.gate-logo-text { letter-spacing: -0.04em; }
```
No `--tracking-*` token in the system, so this is currently the convention. Flag as a "future token" candidate, not a violation.

### High — motion / a11y

**8. No `prefers-reduced-motion` honoured for `.shake`** — `gate.css:78-83`
```css
@keyframes shake { … translateX(±6px) … }
.shake { animation: shake 0.4s ease-in-out; }
```
The skill hard rule: *"`prefers-reduced-motion` MUST be respected."* Wrap:
```css
@media (prefers-reduced-motion: reduce) {
  .shake { animation: none; }
}
```

**9. Easing is `ease-in-out`, not canonical curve** — `gate.css:83`
narve-design canonical easing is `cubic-bezier(0.2, 0, 0, 1)` (= `var(--ease)` in tokens.css:262). `ease-in-out` is generic and not on the sanctioned list.

**10. Animation duration `0.4s` hardcoded** — `gate.css:83`
Matches `var(--duration-slow)` numerically. Replace literal with the token.

**11. `transition: border-color var(--duration-fast)` lacks easing** — `gate.css:51`
Token used for duration (good), but no `var(--ease)` — defaults to `ease`. Replace with `transition: border-color var(--duration-fast) var(--ease);`. Same on `.gate-btn` `transition: background var(--duration-fast);` at line 67.

### Medium — anti-patterns

**12. Inline `style="…"` blocks in HTML** — `gate.html:33-37`
```html
<p style="font-size:11px;color:var(--text-tertiary);margin-top:18px;line-height:1.5">
  By entering, you agree to our
  <a href="/terms" style="color:var(--text-secondary);text-decoration:underline">Terms</a>
  and <a href="/privacy" style="color:var(--text-secondary);text-decoration:underline">Privacy Policy</a>.
```
Three inline `style=` attributes (one `<p>`, two `<a>`). narve-design hard rule: *"❌ Write `style='…'` inline in HTML for anything other than the rare per-card colour variable."* Plus `font-size:11px` is a raw px literal where `var(--text-xs)` would do, and `margin-top:18px` doesn't map to any space token (closest: `--space-4`/16 or `--space-5`/24). Move all this to `pages/gate.css` under a `.gate-fineprint` / `.gate-fineprint a` selector. Use `var(--text-xs)`, `var(--space-4)` (or `--space-5`), and `var(--text-tertiary)` / `var(--text-secondary)`.

**13. `gate.css` ships an inline `*` reset** — `gate.css:8`
```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
```
This is a page-local global reset. `gateway.css` ships the canonical reset; duplicating it in a per-page file is a maintenance hazard and risks specificity surprises. Delete the line — let `gateway.css` own resets.

**14. `<script>` for theme cookie is page-inline** — `gate.html:13`
This anti-FOUC inline script is sanctioned by the design rules (skill: *"Anti-FOUC inline script in every page reads it before paint"*), so this is **not** a violation. Flag only as: matches the cookie pair `narve-theme` / `betyc-theme` correctly.

**15. Bottom-of-body scripts** — `gate.html:50-53`
`scroll-animations.js`, `theme.js`, `cmdk.js`, `share_menu.js` are loaded with `defer`. `cmdk.js` and `share_menu.js` are unused on a gate page (no command palette, no share button) — they ship unnecessary JS to every gated request. Not a design-rule violation, but worth flagging for a perf pass. Confidence: low (these may be bundled by middleware and harmless).

### Low — observations

**16. `.gate-card` uses `--font-display` for `.gate-heading` at 1.2rem** — `gate.css:33-39`
Instrument Serif Italic is sanctioned for "hero copy and page-level *feature* headings only" at ~72–80 px desktop / 48–56 px mobile. At ~19 px the italic serif loses its display character and reads as small body italic. Consider switching `.gate-heading` to `var(--font-ui)` at `var(--text-xl)` weight 500, or upsize the heading to `var(--text-3xl)` / `var(--text-4xl)` to honour the typeface's intended scale. Subjective — not a hard violation but flags as design intent drift.

**17. `box-sizing` reset uses `*` (universal)** — `gate.css:8`
Same as finding #13. Universal selectors are slow and override global resets unpredictably. Already covered.

**18. `gate-error` colour `--text-secondary`** — `gate.css:72`
Compliant with monochrome rule (no red). Confirmed correct: error is differentiated by `role="alert"` + shake animation, not hue. Good.

**19. `:empty` hide on `.gate-error`** — `gate.css:76`
Good pattern — prevents an empty 20 px spacer when no error. Compliant.

**20. Logo invert on dark theme** — `gate.css:22-23`
`[data-theme="dark"] .gate-logo-icon { filter: invert(1); }` — correct per skill rule "Logo must `filter: invert(1)` on dark backgrounds."

**21. Body uses `--bg-void`** — `gate.css:10`
This token is in the spec list (`--bg-void` mentioned in skill under Surfaces). Compliant.

**22. `<input type="password" autocomplete="off">`** — `gate.html:28-29`
Plus `-webkit-text-security: disc;` (`gate.css:53`) — redundant since `type="password"` already masks. Harmless. The `autocomplete="off"` is appropriate for a site-access token.

**23. `narve-sr-only` label** — `gate.html:27`
Correct accessibility pattern — visually hidden label for screen readers. Compliant.

---

## Top 3 by severity

1. **Invisible focus ring on light theme** (finding #2, `gate.css:54`) — `box-shadow: 0 0 0 3px rgba(240,240,240,0.04)` is a near-zero-alpha near-white halo on a white background. Keyboard users get no focus feedback. AA violation.
2. **Input font 14.4 px triggers iOS auto-zoom** (finding #1, `gate.css:48`) — `font-size: 0.9rem` on the access-token input. iOS Safari zooms in on focus, breaking the gate layout. Hard rule.
3. **Logo tap target ≈ 28 px** (finding #3, `gate.html:18-21`) — `.gate-logo` anchor doesn't reach 44 × 44 px clickable area on mobile. Hard rule.

---

## Suggested fix order (cheapest first)

1. Add `@media (prefers-reduced-motion: reduce) { .shake { animation: none; } }` to `gate.css` — 1 line.
2. Replace `box-shadow: 0 0 0 3px rgba(240,240,240,0.04)` with `outline: var(--focus-ring); outline-offset: 2px; box-shadow: none;` — fixes finding #2.
3. Replace `font-size: 0.9rem` on `.gate-input` and `.gate-btn` with `var(--text-md)` — fixes finding #1 + #5.
4. Add `min-height: 44px; padding: var(--space-2);` to `.gate-logo` — fixes finding #3.
5. Move the legal-text `<p>` block into `gate.css` as `.gate-fineprint` — fixes finding #12.
6. Sweep remaining rem / px literals to tokens — finding #4, #5, #6, #10, #11.
7. Delete the `*` reset at top of `gate.css` once gateway.css reset is verified to cover the gate-isolated body — finding #13.
