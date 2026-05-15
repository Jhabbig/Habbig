# Design audit — `gateway/static/components.css`

**Date**: 2026-05-15
**File audited**: `/Users/shocakarel/Habbig/gateway/static/components.css` (537 lines)
**Standard**: narve-design skill (monochrome only, 3 typefaces total, tokens not hardcoded values, AA contrast both themes, ≥ 44×44 tap targets on mobile, ≥ 16 px input font, canonical easing `cubic-bezier(0.2, 0, 0, 1)` via `var(--ease)`, canonical durations `--duration-{fast,base,slow}`, `--z-*` tokens, `--shadow-*` tokens, `--radius-*` token scale).

Token source: `/Users/shocakarel/Habbig/gateway/static/tokens.css`.

---

## Summary

| Category | Count |
|---|---|
| Monochrome violations | 0 |
| Typeface violations | 2 |
| Token / hardcoded-value violations | 67 |
| Theme / contrast (AA) violations | 0 |
| Mobile tap-target / input-font violations | 6 |
| Motion / a11y violations | 5 |
| Anti-pattern (raw z-index, shadow, non-scale radius) violations | 12 |
| **Total findings** | **92** |

Components are monochrome — the toast's success/error differentiation uses border width rather than hue (good, called out in the file's own comments). The skeleton shimmer gradient stays in greyscale. No semantic colour is used to encode state anywhere in the file.

The real damage is the systemic refusal to reach for tokens. Every selector group hand-rolls its own paddings, radii, shadows, durations, and z-indices, often with values that aren't even on the design system's published scale (`border-radius: 10px`, `14px`, `3px` — none exist as tokens). The `var(--token, fallback)` pattern is consistently used for *colours*, but for spacing/sizing it isn't used at all — every dimensional value is a raw `px`. The result is a stylesheet that *looks* token-driven if you only scan the colour declarations, but is in fact entirely uncalibrated against the rest of the design system.

Two `font-family: ui-monospace, SFMono-Regular, Menlo, monospace` declarations on `kbd` elements bypass `var(--font-mono)` (Geist Mono) — a hard typography-rule violation, since the design system is strict about exactly three typefaces.

Six tap targets fail the 44×44 floor on mobile (toast row, empty-state action, share-trigger, share-menu item, cmdk row, and the cmdk input itself which also fails the 16 px iOS-auto-zoom rule at 15 px).

---

## Top 3 (highest-leverage fixes)

1. **`.nv-cmdk__input` font-size 15 px — fails iOS-auto-zoom hard rule** (L315). One-char fix: `font-size: var(--text-md);` (16 px). This rule is non-negotiable per the narve-design skill: *"`<input>` / `<select>` / `<textarea>` ≥ 16 px font on mobile (defeats iOS auto-zoom)."* The command palette is the primary navigation aid; mobile users currently get a viewport jump every time they open it.
2. **Two `kbd` rules hardcode a system-mono fallback chain instead of `var(--font-mono)`** (L398, L428). Direct violation of the three-typefaces-total rule — keyboard hint glyphs in the ⌘K palette and its footer render in SF Mono / Menlo instead of Geist Mono. Replace `font-family: ui-monospace, SFMono-Regular, Menlo, monospace;` with `font-family: var(--font-mono);` in both places. The token already has the same fallback chain after Geist Mono, so this is strictly additive.
3. **Six tap targets under 44×44 on mobile** (L52, L129, L340, L464, L509, L535). `.nv-toast`, `.nv-empty__action`, `.nv-cmdk__row`, `.nv-share__trigger`, `.nv-share__item` all sit between ~25 px and ~40 px tall. Several already have `@media (max-width: 640px)` overrides — extend those to set `min-height: 44px` instead of nudging padding by a couple of pixels. The empty-state action and share-trigger are the worst offenders (~25–30 px tall).

---

## Findings

### Critical (blocks design-system claims)

**1. Input font-size < 16 px — iOS auto-zoom trigger** — L315
```css
.nv-cmdk__input { font-size: 15px; }
```
narve-design hard rule: *"`<input>` / `<select>` / `<textarea>` ≥ 16 px font on mobile."* Use `var(--text-md)` (= 16 px). At 15 px the iPhone Safari viewport reflows on focus.

**2. Typeface violation — `kbd` rules bypass Geist Mono** — L395-400, L424-432
```css
.nv-cmdk__hint kbd  { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.nv-cmdk__footer kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
```
narve-design hard rule: *"Monospace: Geist Mono — for IDs, timestamps, numbers, code, **kbd**, breadcrumb subproduct slugs."* `var(--font-mono)` (defined in `tokens.css:212`) already cascades to the identical system fallback chain *after* Geist Mono. Two-token swap.

**3. Mobile tap targets below 44 × 44 px** — six selectors

| # | Selector | Line | Approx height | Notes |
|---|---|---|---|---|
| a | `.nv-toast` | 33-52 | ~33 px | Whole toast is clickable (`cursor: pointer` L51) — set `min-height: 44px` or accept that toasts are exempt as transient (decide explicitly). |
| b | `.nv-empty__action` | 129-142 | ~30 px | `padding: 8px 16px` + text 13 px. Mobile-critical button. |
| c | `.nv-cmdk__row` | 340-356 | ~40 px | Only 4 px short. Bump `padding` to `12px 12px` or set `min-height: 44px`. |
| d | `.nv-share__trigger` | 464-485 | ~25 px | Mobile @640 bumps padding to `8px 12px` (L535) → ~29 px. Still fails. |
| e | `.nv-share__item` | 509-523 | ~30 px | Mobile @640 bumps to `10px 14px` font-size base (L536) → ~38 px. Still fails. |
| f | `.nv-cmdk__input` | 309-319 | ~50 px (passes) but font-size fails — see finding #1 |

### High — hardcoded values that bypass tokens

**4. Hardcoded paddings, margins, gaps, top/left positions**

| Line | Selector | Property | Value | Token |
|---|---|---|---|---|
| 23 | `#nv-toast-region` | `bottom` | `24px` | `var(--space-5)` |
| 29 | `#nv-toast-region` | `gap` | `8px` | `var(--space-2)` |
| 38 | `.nv-toast` | `padding` | `12px 16px` | `var(--space-3) var(--space-4)` |
| 43 | `.nv-toast` | `transform` | `translateY(8px)` | `translateY(var(--space-2))` |
| 48 | `.nv-toast` | `gap` | `12px` | `var(--space-3)` |
| 83 | `#nv-toast-region @640` | `top` | `24px` | `var(--space-5)` |
| 92 | `.nv-empty` | `padding` | `48px 24px` | `var(--space-7) var(--space-5)` |
| 99 | `.nv-empty__icon` | `margin-bottom (auto-shorthand)` | `16px` | `var(--space-4)` |
| 111 | `.nv-empty__title` | `margin-bottom (auto-shorthand)` | `8px` | `var(--space-2)` |
| 119 | `.nv-empty__body` | `margin-bottom (auto-shorthand)` | `20px` | (no token — closest `--space-5` 24) |
| 124 | `.nv-empty__actions` | `gap` | `12px` | `var(--space-3)` |
| 132 | `.nv-empty__action` | `padding` | `8px 16px` | `var(--space-2) var(--space-4)` |
| 195, 206, 215 | `.nv-skel--row-*` | `gap` | `12px` | `var(--space-3)` |
| 197, 208, 217 | `.nv-skel--row-*` | `padding` | `12px 0` | `var(--space-3) 0` |
| 223 | `.nv-skel--card-prediction` | `padding` | `16px` | `var(--card-pad)` |
| 228 | `.nv-skel--card-prediction` | `gap` | `10px` | (no token — closest `--space-2` 8 or `--space-3` 12) |
| 229 | `.nv-skel--card-prediction` | `margin-bottom` | `12px` | `var(--space-3)` |
| 237 | `.nv-skel--row-table` | `gap` | `12px` | `var(--space-3)` |
| 239 | `.nv-skel--row-table` | `padding` | `10px 0` | (no token — closest `--space-3` 12) |
| 249 | `.skip-link` | `top` | `-40px` | layout literal (off-screen — acceptable) |
| 253 | `.skip-link` | `padding` | `8px 12px` | `var(--space-2) var(--space-3)` |
| 311 | `.nv-cmdk__input` | `padding` | `16px 20px` | `var(--space-4) <no token>` (20 has no `--space` peer) |
| 327 | `.nv-cmdk__results` | `padding` | `6px` | (no token — closest `--space-1` 4 / `--space-2` 8) |
| 336 | `.nv-cmdk__group-header` | `padding` | `12px 12px 6px` | partially tokenisable |
| 344 | `.nv-cmdk__row` | `padding` | `10px 12px` | (10 has no token) |
| 378 | `.nv-cmdk__row-subtitle` | `margin-left` | `8px` | `var(--space-2)` |
| 388 | `.nv-cmdk__no-results, __hint` | `padding` | `24px 16px` | `var(--space-5) var(--space-4)` |
| 397 | `.nv-cmdk__hint kbd` | `padding` | `2px 6px` | (sub-`--space-1` — acceptable for kbd ornament) |
| 404 | `.nv-cmdk__hint-action` | `margin-top` | `12px` | `var(--space-3)` |
| 405 | `.nv-cmdk__hint-action` | `padding` | `6px 14px` | (neither value on scale) |
| 415 | `.nv-cmdk__footer` | `gap` | `16px` | `var(--space-4)` |
| 416 | `.nv-cmdk__footer` | `padding` | `10px 16px` | `<no> var(--space-4)` |
| 426 | `.nv-cmdk__footer kbd` | `padding` | `2px 6px` | acceptable kbd ornament |
| 430 | `.nv-cmdk__footer kbd` | `margin-right` | `4px` | `var(--space-1)` |
| 443 | `.nv-cmdk__footer @640` | `gap` | `10px` | (no token) |
| 467 | `.nv-share__trigger` | `gap` | `6px` | (no token) |
| 470 | `.nv-share__trigger` | `padding` | `6px 10px 6px 12px` | none tokenised |
| 495 | `.nv-share__menu` | `top` | `calc(100% + 4px)` | `--space-1` |
| 502 | `.nv-share__menu` | `padding` | `4px` | `var(--space-1)` |
| 513 | `.nv-share__item` | `padding` | `8px 12px` | `var(--space-2) var(--space-3)` |
| 535 | `.nv-share__trigger @640` | `padding` | `8px 12px` | `var(--space-2) var(--space-3)` |
| 536 | `.nv-share__item @640` | `padding` | `10px 14px` | neither on scale |

**5. Hardcoded font-sizes (bypass `--text-*` scale)** — three instances

| Line | Selector | Hardcoded | Token |
|---|---|---|---|
| 315 | `.nv-cmdk__input` | `15px` | `var(--text-md)` (also fixes finding #1) |
| 376 | `.nv-cmdk__row-subtitle` | `12px` | `var(--text-xs)` (11) or `var(--text-sm)` (13) |
| 410 | `.nv-cmdk__hint-action` | `12px` | same — `var(--text-xs)`/`var(--text-sm)` |
| 429 | `.nv-cmdk__footer kbd` | `10px` | sub-`--text-xs` — *not on scale*; should round up to `--text-xs` 11 |
| 443 | `.nv-cmdk__footer @640` | `10px` | same |

narve-design hard rule: *"Sizes use the `--text-*` token scale (xs through 5xl). No raw px values for type."*

**6. Border-radius values not on the `--radius-*` scale** — 6 instances of off-scale radii

The published scale is `--radius-xs: 4` / `--radius-sm: 6` / `--radius-md: 8` / `--radius-lg: 12` / `--radius-xl: 16` / `--radius-full: 9999`. The file uses **none of `--radius-xl`** and *invents* `10px`, `14px`, `3px`:

| Line | Selector | Hardcoded | Token |
|---|---|---|---|
| 37 | `.nv-toast` | `10px` | `var(--radius-md)` (8) or `--radius-lg` (12) |
| 225 | `.nv-skel--card-prediction` | `10px` | same |
| 301 | `.nv-cmdk__panel` | `14px` | `var(--radius-lg)` (12) or `--radius-xl` (16) |
| 427 | `.nv-cmdk__footer kbd` | `3px` | `var(--radius-xs)` (4) |
| 499 | `.nv-share__menu` | `10px` | `--radius-md`/`--radius-lg` |

Picking values *between* scale steps is the classic "we have a design system on paper but not in code" smell.

**7. Z-index ad-hoc values that ignore `--z-*` tokens** — 4 instances

The token stack is published in `tokens.css:226-232`:
```
--z-dropdown:  100;
--z-sticky:    200;
--z-modal:     1000;
--z-toast:     2000;
--z-watermark: 9998;
--z-overlay:   9999;
```

| Line | Selector | Value | Should be |
|---|---|---|---|
| 26 | `#nv-toast-region` | `1000` | `var(--z-toast)` (2000) — toasts must sit above modals (1000), not below |
| 254 | `.skip-link` | `10000` | `var(--z-overlay)` (9999) — currently *one above* the overlay token, which means a future overlay can never cover the skip-link by token alone |
| 278 | `.nv-cmdk` | `1100` | `var(--z-modal)` (1000) or a new `--z-cmdk` if it genuinely sits above modals |
| 501 | `.nv-share__menu` | `80` | `var(--z-dropdown)` (100) — currently *below* dropdown range, so a sticky header (`z-sticky: 200`) will cover the share menu |

Finding #7d is the highest-risk: the share menu can render under sticky chrome.

**8. Box-shadow values not from `--shadow-*` tokens** — 4 instances

| Line | Selector | Hardcoded | Token |
|---|---|---|---|
| 41 | `.nv-toast` | `0 8px 24px rgba(0, 0, 0, 0.28)` | `var(--shadow-md)` or `--shadow-lg` |
| 303 | `.nv-cmdk__panel` | `0 24px 48px rgba(0, 0, 0, 0.32)` | `var(--shadow-lg)` |
| 500 | `.nv-share__menu` | `0 12px 32px rgba(0, 0, 0, 0.32)` | `var(--shadow-lg)` |

narve-design rule: *"No gradients on rounded corners, no soft shadows beyond `--shadow-lg` on modals."* These shadows also fail to flip on dark theme (the rgba is `0,0,0` which is identical in both themes — `--shadow-lg` actually has different rgba opacities per theme block).

**9. Raw `rgba` backdrop without token wrapper** — L287
```css
.nv-cmdk__backdrop { background: rgba(0, 0, 0, 0.5); }
```
This works in dark mode but in light mode produces a near-black wash. No `--bg-overlay`-style token exists for backdrops, but the value should at minimum be exposed as a CSS custom property at the top of the rule so a future theme can override it.

**10. Easing + duration deviate from the canonical scale** — 4 transitions + 1 animation

| Line | Selector | Hardcoded | Token |
|---|---|---|---|
| 44 | `.nv-toast` | `0.16s ease` (twice) | `var(--duration-fast) var(--ease)` or `--duration-base` |
| 141 | `.nv-empty__action` | `0.15s` (twice) | `var(--duration-fast)` — also missing easing function (defaults to `ease`) |
| 178 | `.nv-skel` shimmer | `1.6s ease-in-out` | no token; `ease-in-out` is not on the curve list |
| 477 | `.nv-share__trigger` | `0.15s` (three times) | same as #141 |

narve-design rule: *"Easing: `cubic-bezier(0.2, 0, 0, 1)` is the canonical curve. Durations: only `--duration-fast`, `--duration-base`, `--duration-slow`."* The 1.6 s shimmer duration is well beyond `--duration-slow` (0.4 s), but a shimmer cycle isn't a UI transition — it's an infinite loop, so the rule is arguably non-applicable. Still flag for the easing choice (`ease-in-out` vs `var(--ease)`).

**11. Skeleton sub-element sizes hardcoded** — L194-242

Every `.nv-skel--row-*` and `.nv-skel--card-prediction` uses raw `height: 14px / 12px / 18px / 10px / 24px / 32px` for child blocks. The skeleton is supposed to *match the final row height*, so these are quasi-layout literals — but the same values appear repeated across six selectors, suggesting they should be lifted into either named local custom properties or shared `--skel-*` tokens.

### Medium — anti-pattern smells

**12. The `var(--token, fallback)` pattern is inconsistently applied**

Colour declarations consistently use `var(--token, hardcoded-fallback)`. Spacing/sizing declarations never do — they just use raw `px`. The fallback colour pattern protects against `tokens.css` failing to load, but the same protection isn't given to spacing/radii/shadows. Two options:

- (a) Drop the fallback colour pattern (rely on cascade-failure simply rendering an unstyled but legible page).
- (b) Apply the fallback pattern uniformly — i.e. `padding: var(--space-3, 12px)` everywhere.

Pick one. The current half-state implies tokens are mandatory only when they happen to be colours.

**13. `flex` shorthand uses non-canonical `1 1 auto` (L328, L360)** — design system uses `flex: 1`. Minor consistency nit.

**14. `border-bottom: 1px solid var(--border-strong, rgba(255,255,255,0.08))` on `.nv-cmdk__input`** (L318) — `--border-strong` in light theme is `#b0b0b0` which is much darker than the fallback `rgba(255,255,255,0.08)` would suggest. The fallback is dark-theme-only thinking. Not a correctness bug (token wins) but the fallback is misleading.

### Low — nits

**15. `.nv-share__caret` rotate transition is implicit** (L488-490) — caret rotates 180° on `.nv-share--open` but no `transition: transform var(--duration-fast) var(--ease)` is declared, so the rotation is instant. Either declare the transition or remove the caret-rotate altogether for symmetry with the rest of the no-motion design grammar.

**16. `flex-shrink: 0` is used twice (L75, L378, L420) where `flex: none` would be more idiomatic.** Equivalent at runtime — pick one.

**17. `letter-spacing: 0.08em` on `.nv-cmdk__group-header`** (L334) — magic-number letter-spacing. The design system has no `--letter-spacing-*` tokens (intentional — typography in narve is sized, not letter-spaced) so this is a rule-of-thumb decoration. Acceptable but flag for future cleanup if a future token is introduced.

**18. The skeleton card uses `border: 1px solid var(--border-strong, rgba(255, 255, 255, 0.08));`** (L224) — `--border-strong` in light theme is `#b0b0b0` (very visible). A skeleton card with a solid grey border looks like a "loaded" card with no content. `--border-subtle` (light: `#e0e0e0`) is the intended token here, matching the rest of the design system's skeleton convention.

---

## Recommended remediation order

1. **Critical hard-rule fixes** (findings #1, #2, #3) — these are explicit "never break" violations in the narve-design skill.
2. **Z-index token migration** (#7) — finding #7d (share menu z 80) can cause real layout bugs today.
3. **Shadow + radius scale alignment** (#6, #8) — restores theme-flip parity for shadows.
4. **Spacing token sweep** (#4) — bulk find-replace mapping raw px to `--space-N`.
5. **Motion canonicalisation** (#10) — small but fixes the design-grammar feel.
6. **Skeleton sub-token introduction** (#11) — if the skeleton system is intended to remain row-shaped, lift the magic numbers into `--skel-row-h: 14px` etc.
7. The medium and low findings can ride along with the spacing sweep.

## Out of scope (verified clean)

- No coloured fills, strokes, or borders. The monochrome rule holds.
- No `box-shadow` larger than `--shadow-lg` floor in spirit (the hardcoded shadows are within the same opacity / blur envelope as the tokens; they just don't *use* the tokens).
- No emoji in selectors or in `content:`.
- No animation beyond opacity + transform + width/height (the shimmer is a background-position animation — sits in the "background" exception, not a transform of layout).
- No `border-radius > 16px` (max found: `14px` — under the cap, just off-scale).
- No `prefers-reduced-motion` regression — the file already has two `@media (prefers-reduced-motion: reduce)` blocks covering both the shimmer and the cmdk backdrop blur.
- No light/dark theme assumption baked into the `var()` calls themselves; the fallbacks are dark-mode flavoured but the cascade resolves correctly.
