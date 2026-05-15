# Design Audit — admin shell

Scope:
- `gateway/static/_partials/admin_shell.html` (the HTML target referenced in the
  task as `gateway/static/admin-shell.html`; canonical path is the partial)
- `gateway/static/pages/admin-shell.css`

Reference: `narve-design` skill — monochrome only, three typefaces
(Inter / Geist Mono / Instrument Serif), token-driven values, AA contrast in
both themes, mobile coverage >= 360 px.

---

## Summary

- **Total violations: 64**
  - Monochrome / colour: 0 (clean — no chrome hues, no semantic red/green)
  - Typography (forbidden faces): 1
  - Hardcoded values that should be tokens: 56
  - Undefined / fallback-to-hardcoded tokens: 4
  - Motion outside the duration scale: 3
  - Other (z-index, AA, inline fallback hex): bundled into the buckets above

The shell is structurally on-brand: monochrome, three-typeface contract
honoured in the file's own typography comment, AA-safe colour tokens, mobile
drawer at the correct <= 900 px breakpoint, focus-visible ring, ~40 x 40
toggle. The violations are almost entirely **raw px** and **raw motion
durations** that should resolve via tokens — plus one unauthorised typeface
and a handful of inline fallback values that defeat the token swap when a
theme rule misses.

---

## Findings — by severity

### High — fix before next ship

**H1. Forbidden typeface `"JetBrains Mono"` (CSS line 503)**

```css
.newsletter-textarea {
  font-family: "JetBrains Mono", var(--font-mono);
```

Hard rule: three typefaces total — Inter, Geist Mono, Instrument Serif
Italic. The fallback chain ends at the token. Fix: keep
`font-family: var(--font-mono);`.

**H2. Undefined token `--bg-page` used three times — silently falls back to
inherited / transparent (CSS lines 495, 522, 598)**

```css
.newsletter-input, .newsletter-select, .newsletter-textarea { background: var(--bg-page); }
.newsletter-card { background: var(--bg-page); }
.newsletter-preview-pane { background: var(--bg-page); }
```

`--bg-page` is not defined in `tokens.css` (only `--bg-base`, `--bg-surface`,
`--bg-raised`, `--bg-inset`, etc.). Without a fallback the rule resolves to
the CSS initial value, so light/dark mode swaps do not flow through to the
newsletter form surfaces. Fix: `var(--bg-surface)` or `var(--bg-inset)`.

**H3. Undefined token `--surface-hover` used for inline-code background
(CSS line 483)**

```css
.newsletter-field__hint code {
  background: var(--surface-hover);
```

Same problem — `--surface-hover` is not defined as a top-level token. Fix:
`var(--interactive-ghost)` or `var(--bg-inset)`.

**H4. Inline hex fallback `#111` defeats theming (CSS line 124)**

```css
.admin-content, ...
  border: 2px solid var(--text-primary, #111);
```

The fallback only fires when `--text-primary` is missing — but in dark mode
`--text-primary` is defined as a light value. If `tokens.css` fails to load,
a hard `#111` line shows up over an unstyled dark page (worst case:
invisible). Drop the fallback or fall back to `currentColor`.

---

### Medium — clean up next sweep

**M1. Hardcoded motion durations x4 (CSS lines 88, 224, 312, 338) — not on
the `--duration-*` scale**

```
0.12s ease  (x3) -> var(--duration-fast) var(--ease-canonical)
0.2s  ease       -> var(--duration-base) var(--ease-canonical)
```

The scale exists (`--duration-fast: 0.12s`, `--duration-base: 0.2s`,
`--duration-slow: 0.4s`) and the canonical curve `--ease-canonical
(cubic-bezier(0.2,0,0,1))` exists. Values are correct today but will desync
the day someone re-tunes the scale.

**M2. Raw `rgba(0,0,0, ...)` shadows / overlays x3 (CSS lines 92, 96, 314)**

```css
background: var(--interactive-ghost, rgba(0,0,0,0.04));   /* L92  */
background: var(--interactive-ghost, rgba(0,0,0,0.06));   /* L96  */
box-shadow: 2px 0 12px rgba(0,0,0,0.12);                  /* L314 */
```

The first two have a defined token fallback — acceptable (the token resolves
first). The third is a raw shadow on the mobile rail drawer; use
`var(--shadow-lg)`, the sanctioned modal-class shadow that works in both
themes.

**M3. Hardcoded `font-size` values (CSS, 14 instances)**

Lines with raw px instead of `--text-*` scale:

| Line | Raw | Suggested |
|------|-----|-----------|
| 55   | 18px   | `--text-lg` |
| 61   | 11px   | off-scale |
| 71   | 10.5px | off-scale |
| 86   | 13.5px | `--text-sm` (14px) |
| 165  | 12px   | `--text-xs` |
| 250  | 12.5px | off-scale |
| 272  | 16px   | keep — explicit anti-iOS-zoom value |
| 344  | 10.5px | off-scale |
| 361  | 12px   | `--text-xs` |
| 466  | 12px   | `--text-xs` |
| 475  | 12px   | `--text-xs` |
| 482  | 11px   | off-scale |
| 518  | 13px   | off-scale |
| 533  | 11px   | off-scale |
| 545  | 13px   | off-scale |
| 566  | 12px   | `--text-xs` |
| 574  | 22px   | between `--text-xl` 20 and `--text-2xl` 24 |
| 584  | 13px   | off-scale |
| 593  | 12px   | `--text-xs` |

Several values (10.5, 11, 12.5, 13.5) are off the canonical scale entirely.
Either widen the scale once in `tokens.css` or normalise to the nearest
existing step.

**M4. Hardcoded spacing values (CSS, ~25 instances)**

Raw `px` on `padding`, `margin`, `gap`, `margin-bottom` etc. (lines 41, 42,
48, 67, 76, 77, 81, 158, 159, 160, 167, 175, 205, 213, 214, 290, 291, 327,
328, 335, 349, 363, 373, 378, 414, 448, 455, 461, 484, 498, 506, 525, 528,
543, 552, 553, 560, 587, 594, 601). The `--space-*` scale is 4 px-based:

- 4px  -> `--space-1`
- 8px  -> `--space-2`
- 12px -> `--space-3`
- 16px -> `--space-4`
- 20px -> `--space-5`
- 24px -> `--space-6`
- 28px -> `--space-7`
- 32px -> `--space-8`
- 40px -> `--space-10`

Worst offenders: `.admin-section { margin-bottom: 32px; }` (L373) and
`margin-bottom: 40px;` (L328) have direct token equivalents.

**M5. Hardcoded `border-radius` values x6 (CSS lines 334, 390, 429, 485,
497, 524, 600)**

```
4px  -> --radius-sm
8px  -> --radius-md
10px -> off-scale (between md and lg)
12px -> --radius-lg
```

Three other rules already use `var(--radius-sm | md)` correctly — the file
is half-migrated.

**M6. Hardcoded `width: 240px` on sidebar (CSS line 28)**

`--sidebar-width` token exists. Use `width: var(--sidebar-width);`. The 240
px literal matches today but will diverge.

**M7. Hardcoded `z-index: 100/101` (CSS lines 292, 313)**

`--z-overlay`, `--z-modal`, `--z-dropdown`, `--z-sticky`, `--z-toast` exist.
Use `var(--z-overlay)` for the panel and `calc(var(--z-overlay) + 1)` for
the toggle.

---

### Low — nits, do when nearby

**L1. `max-width: 1280px` (line 109), `max-width: 1180px` (line 414) — page
content gutters disagree.**

`--max-content-width` exists. The two values differ by 100 px in the same
file (general admin body vs newsletter page).

**L2. `min-height: 280px` (line 506), `max-height: 360px` (line 605) —
ad-hoc panel sizing.**

Not token-required, but document inline if sticky.

**L3. `stroke-width="1.6"` on the hamburger SVG (HTML line 14)**

Within tolerance for icon-glyph sizing.

---

## Conformance — what passes

- **Monochrome:** zero coloured chrome. `.admin-tile__delta.up/.down` use
  `--rank-1` plus weight to differentiate direction — correct monochrome
  pattern.
- **Three-typeface contract:** the file's top comment maps Inter / Geist
  Mono / Instrument Serif Italic to chrome / numeric / hero. Implementation
  matches the comment (except H1).
- **AA contrast:** all text uses `--text-primary`, `--text-secondary`,
  `--text-tertiary`, `--text-quaternary` — AA-safe in both themes per
  `tokens.css` (light 5.2:1 minimum on tertiary; dark side raised in
  AUDIT #4).
- **Theme support:** no hardcoded `#fff` / `#000` (one `#111` fallback
  flagged above; no other literal hex).
- **Mobile:**
  - Drawer breakpoint `max-width: 900px` (correct — width-based, not
    `pointer:coarse`).
  - Rail toggle 40 x 40 — under the 44 x 44 floor. Borderline.
  - Inputs at 16 px (L272) — defeats iOS auto-zoom, correct.
  - `.admin-card:has(> .admin-table) { overflow-x: auto; }` — tables
    wrapped, correct.
- **Focus ring:** `:focus-visible` rule at L394 — keyboard-only outline,
  correct.
- **No inline `style=` in HTML.** No `<style>` block in the template.
- **No decorative chrome:** no gradients, no rounded soft shadows beyond
  modal-class `--shadow-lg`, no emoji, no animations outside
  opacity/transform.

---

## Top 3 to fix first

1. **H1 — drop `"JetBrains Mono"`** (line 503). One-line fix; the only place
   in the admin shell that introduces a fourth typeface, on a write-heavy
   surface where the breach is conspicuous.

2. **H2 + H3 — replace `--bg-page` (3 sites) and `--surface-hover` (1 site)
   with defined tokens.** Silent failures today — rules resolve to wrong
   fallback in dark mode and break the newsletter form surface hierarchy.
   Highest-impact correctness issue in the file.

3. **M1 — tokenise the four `0.12s ease` / `0.2s ease` transitions** onto
   `--duration-fast` / `--duration-base` + `--ease-canonical`. Tiny diff,
   but locks the admin shell to the platform motion contract; future
   tuning of `--duration-fast` then propagates correctly.

---

## Files reviewed

- `/Users/shocakarel/Habbig/gateway/static/_partials/admin_shell.html`
  (100 lines)
- `/Users/shocakarel/Habbig/gateway/static/pages/admin-shell.css`
  (612 lines)

No code changes made.
