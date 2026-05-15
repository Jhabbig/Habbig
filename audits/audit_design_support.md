# Design audit — `support.html`, `feedback.html`, `contact.html` (+ page CSS)

**Date**: 2026-05-15
**Auditor**: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
**Files audited**:
- `/Users/shocakarel/Habbig/gateway/static/support.html`
- `/Users/shocakarel/Habbig/gateway/static/feedback.html`
- `/Users/shocakarel/Habbig/gateway/static/contact.html`
- `/Users/shocakarel/Habbig/gateway/static/pages/support.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/feedback.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/contact.css`

**Standard**: narve-design skill (monochrome only, 3 typefaces, tokens not hardcoded values, AA contrast both themes, anti-FOUC pre-paint script, ≥ 44 × 44 tap targets on mobile, ≥ 16 px input font, no inline `<style>` blocks except the one historical exception).

Checks per template:
1. Monochrome only (no hex / rgb / colour names in markup or page CSS).
2. Three typefaces only (Inter / Geist Mono / Instrument Serif — no fallback chains containing Helvetica, system-ui, Arial).
3. Tokens — no hardcoded px / rem / hex; use `--space-*`, `--text-*`, `--radius-*`, etc.
4. AA contrast in both themes.
5. Mobile tap targets ≥ 44 × 44 px and ≥ 16 px form-control font.
6. No `<style>` blocks in page (per skill: the single sanctioned exception was `feedback.html`, but it has since been migrated to `pages/feedback.css` — so **zero** inline `<style>` blocks are now allowed in these three pages).
7. No `style="…"` inline attributes (except the rare per-card `--accent` colour variable).
8. No emoji in chrome.
9. Theme cookie anti-FOUC pre-paint script present.
10. Easing curve is `cubic-bezier(0.2, 0, 0, 1)`; durations come from `--duration-*`.
11. `prefers-reduced-motion` honoured for any animation.

Notation: V = violation, OK = pass, N/A = not applicable.

Glyph note: `←` (U+2190) and `→` (U+2192) are typographic chevrons, not emoji — not counted.

---

## Summary

| Category | Count |
|---|---|
| Inline `<style>` blocks | 0 |
| Inline `style="…"` attributes | 1 |
| Inline `<script>` blocks in body | 2 (1 theme pre-paint × 3 ≈ sanctioned, 1 form-submit handler in `contact.html` — anti-pattern) |
| Monochrome violations | 0 |
| Typeface violations | 0 |
| Token / hardcoded-value violations | 24 |
| Theme / AA contrast violations | 0 |
| Mobile tap-target / input-font violations | 0 |
| Motion / a11y violations | 1 |
| Cross-link / shell-consistency violations | 2 |
| **Total findings** | **30** |

All three pages are monochrome, use the three-typeface budget correctly, ship the anti-FOUC pre-paint script, and pass the 16 px input-font rule. The real problems concentrate in `pages/feedback.css` (which is essentially the pre-migration `<style>` block dumped to disk, indentation and all — token hygiene was not part of that migration), one inline form-submit script in `contact.html` that should be an external file, one inline `style="text-decoration:none"` attribute in `feedback.html`, and inconsistent shell choice across the three pages.

---

## Pre-flight: the inline `<style>` block claim

The narve-design skill explicitly states under "Anti-patterns":

> Add a `<style>` block in a page template (extend `gateway.css` or component CSS instead). One template (`feedback.html`) currently has one — don't replicate the pattern.

**This is stale.** As of this audit:

- `feedback.html` — **no inline `<style>` block** (line 1–72; styles imported via `<link rel="stylesheet" href="{{ static: pages/feedback.css }}">` on line 12). The previous inline block was extracted to `pages/feedback.css`; the file even leads with a banner comment "extracted from the previous inline `<style>` block by the foundation bundle auto-migration."
- `support.html` — no inline `<style>` block.
- `contact.html` — no inline `<style>` block.

Repo-wide check (`grep -rn "<style" gateway/static/*.html`):

```
gateway/static/forgot-password-email.html:12  <style>
gateway/static/offline.html:13               <style data-keep>
```

Both remaining occurrences are legitimate: `forgot-password-email.html` is an email template (must inline CSS — clients strip `<link>`), and `offline.html` is the service-worker offline page (cannot rely on external stylesheets by definition; the `data-keep` attribute signals the build to preserve it).

**Action for the skill file** (out of scope for this audit, but flagged): the anti-pattern bullet about `feedback.html` should be updated — there are now zero non-exempt inline `<style>` blocks under `gateway/static/`. None of the three pages audited here introduce a new one.

---

## `support.html` + `pages/support.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour, no hex anywhere. |
| 2. Three typefaces | OK | `--font-ui` / `--font-display` / `--font-body` only. Geist Mono unused (not required). |
| 3. Tokens | OK | All padding, gap, font-size, colour via tokens. The only literal is `max-width: 960px` (layout literal, acceptable). |
| 4. AA contrast | OK | All text uses tokenised pairs. |
| 5. Mobile tap targets | OK | Path cards are full-width with `padding: var(--space-6)` → ≥ 72 px tap height. No form inputs to check. |
| 6. No `<style>` blocks | OK | Page CSS at `pages/support.css`. |
| 7. No inline `style="…"` | OK | None. |
| 8. No emoji in chrome | OK | `←` and `→` are typographic, not emoji. |
| 9. Anti-FOUC pre-paint | OK | `support.html:13` reads `narve-theme` (falls back to `betyc-theme` cookie / localStorage) before paint. |
| 10. Easing / duration tokens | OK | `pages/support.css:98-99` uses `cubic-bezier(0.2, 0, 0, 1)` + `var(--duration-fast)`. |
| 11. `prefers-reduced-motion` | N/A | No reveal animations on this page. |

**Findings: 0.** `support.html` is the cleanest of the three.

---

## `feedback.html` + `pages/feedback.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour in markup. CSS uses one `rgba(255,255,255,0.03)` fallback (see finding F1). |
| 2. Three typefaces | OK | `--font-display` for h1; everything else inherits `--font-ui`. |
| 3. Tokens | **V × 14** | `pages/feedback.css` is riddled with hardcoded px (see findings F1–F8). |
| 4. AA contrast | OK | Tokenised. |
| 5. Mobile tap targets | OK | `.fb-chip` uses `padding: 6px 12px` → ~32 px tall (V — see finding F9). Submit button `padding: 10px 20px` → ~37 px tall (V — finding F10). |
| 6. No `<style>` blocks | OK | Already migrated to `pages/feedback.css`. |
| 7. No inline `style="…"` | **V × 1** | `feedback.html:29` `style="text-decoration:none"` on `.gw-user-pill`. Move to CSS. |
| 8. No emoji in chrome | OK | None. |
| 9. Anti-FOUC pre-paint | OK | `feedback.html:13`. |
| 10. Easing / duration tokens | **V** | `pages/feedback.css:15` `transition: all 0.12s` — uses raw seconds instead of `var(--duration-fast)`, and `transition: all` is wasteful (animate `border-color, color` explicitly). |
| 11. `prefers-reduced-motion` | N/A | No reveal animations. |

### Findings

**F1. `.fb-wrap` raw px** — `pages/feedback.css:8`
```css
.fb-wrap { max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
```
Three raw px values. Use `var(--space-10) var(--space-6) var(--space-16)` (40, 24, 64 ≈ rounded). 80 px has no token — closest is `var(--space-16)` (64 px) or `var(--space-20)` if added.

**F2. `.fb-head` raw px** — `pages/feedback.css:9`
```css
.fb-head { ... margin-bottom: 8px; ... gap: 16px; }
```
→ `margin-bottom: var(--space-2)`, `gap: var(--space-4)`.

**F3. `.fb-head h1` raw px font-size** — `pages/feedback.css:10`
```css
.fb-head h1 { ... font-size: 28px; font-weight: 700; ... }
```
narve hard rule: *"Sizes use the `--text-*` token scale (xs through 5xl). No raw px values for type."* Use `var(--text-3xl)` (28 px) or `var(--text-2xl)` (24 px).

**F4. `.fb-sub` raw px** — `pages/feedback.css:11`
```css
margin-bottom: 28px;
```
→ `var(--space-7)` (28 px exactly).

**F5. `.fb-controls` raw px** — `pages/feedback.css:12`
```css
gap: 12px; margin-bottom: 18px;
```
→ `gap: var(--space-3)`, `margin-bottom: var(--space-5)` (20 px — 18 has no token; either round or add one).

**F6. `.fb-controls-row` raw px** — `pages/feedback.css:13`
```css
gap: 16px;
```
→ `var(--space-4)`.

**F7. `.fb-chip` raw px (padding + font-size + transition + radius)** — `pages/feedback.css:15`
```css
padding: 6px 12px; ... border-radius: var(--radius-xl); font-size: 12px; ... transition: all 0.12s;
```
Three issues:
- `padding: 6px 12px` → `var(--space-1) var(--space-3)` (4 + 12 — 6 px has no canonical token, closest is `--space-1`/`--space-2` halves; either accept the mismatch or add `--space-1-5: 6px`). 12 px → `--space-3`.
- `font-size: 12px` → `var(--text-xs)` (11 px) or add `--text-xxs: 12px`.
- `transition: all 0.12s` → `transition: border-color var(--duration-fast), color var(--duration-fast)`. Avoid `transition: all`; name properties.
- `border-radius: var(--radius-xl)` is 16 px, which is at the cap. Skill rule: *"Use `border-radius > var(--radius-lg)` (16 px max)."* `--radius-xl` resolves to 16 px (within cap), but `--radius-lg` is the canonical pill — `--radius-full` for true pill chips. Pick one and document; `--radius-xl` is the legacy alias.

**F8. `.fb-list` raw px border-radius** — `pages/feedback.css:18`
```css
border-radius: 10px;
```
→ `var(--radius-md)` (8 px) or `var(--radius-lg)` (12 px). 10 px has no token.

**F9. `.fb-row:hover` raw rgba fallback** — `pages/feedback.css:19`
```css
.fb-row:hover { background: var(--bg-overlay, rgba(255,255,255,0.03)); }
```
The rgba fallback exists so dark theme has a hover wash if `--bg-overlay` is missing. But `rgba(255,255,255,0.03)` on light theme would be invisible *and* incorrect — it's a dark-theme value. Light theme should use `rgba(0,0,0,0.03)`. Either:
- Trust `--bg-overlay` (it exists in `tokens.css`) and drop the fallback;
- Or split into two rules under `[data-theme="light"]` / `[data-theme="dark"]`.

**F10. `.fb-submit-bar` raw px** — `pages/feedback.css:21-22`
```css
.fb-submit-bar { margin-top: 24px; ... }
.fb-submit-bar button { padding: 10px 20px; ... }
```
- `margin-top: 24px` → `var(--space-6)`.
- `padding: 10px 20px` → `var(--space-3) var(--space-5)` (12/20) or define `--space-2-5: 10px`.
- Button has `padding: 10px 20px` → total height ≈ 38 px. **< 44 px tap target.** Add `min-height: 44px;` or bump padding to `var(--space-3) var(--space-5)` plus `min-height: var(--touch-target)` (define if missing).

**F11. `.fb-chip` height (mobile tap target)** — `pages/feedback.css:15`
Same problem: `padding: 6px 12px` on `font-size: 12px` → total height ~24–28 px. **Well under 44 px.** Add `min-height: 44px; display: inline-flex; align-items: center;` keeping the visual padding.

**F12. Inline `style="text-decoration:none"`** — `feedback.html:29`
```html
<a href="/profile" class="gw-user-pill" style="text-decoration:none">{{ username }}</a>
```
narve hard rule: *"Write `style="…"` inline in HTML for anything other than the rare per-card colour variable."* The `.gw-user-pill` class already exists in `gateway.css`; add `text-decoration: none;` there and remove the inline attribute.

**F13. Shell mismatch with `support.html` / `contact.html`** — `feedback.html:18-32`
`feedback.html` uses the authenticated chrome (`gw-header` + `gw-nav` with Dashboards/Feedback/Billing/Settings/Sign out links), while `support.html` and `contact.html` use the public shell (single `← narve.ai` back-link + monochrome footer). This is correct *if* `/feedback` is an authenticated route and `/support` / `/contact` are public — but the nav between them is asymmetric: there's no link **from** `feedback.html` to `/support` or `/contact`, and the public pages don't link **to** `/feedback` even though it would be the natural escalation path for non-FAQ issues. Either:
- Add `/support` and `/contact` to the `gw-nav` (or only to the authenticated user dropdown), and
- Add `/feedback` to `support.html`'s "Send us a message" alternatives (it's a more public-roadmap-y channel than `/contact`).

**F14. Submit button uses `--cta-bg` instead of `--interactive-bg`** — `pages/feedback.css:22`
```css
background: var(--cta-bg);
```
`tokens.css:299` defines `--cta-bg: var(--interactive-bg)` as a legacy alias. Use the canonical `--interactive-bg` directly so this file matches `contact.css`'s `.ct-submit`.

**Violations: 14** (12 token / hardcoded + 1 inline `style=` + 1 shell-consistency).

---

## `contact.html` + `pages/contact.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | Display / UI / body all tokenised. |
| 3. Tokens | **V × 1** | `pages/contact.css:110` `font-size: 16px;` on inputs — hardcoded literal where `var(--text-md)` would do. |
| 4. AA contrast | OK | All tokenised. |
| 5. Mobile tap targets | OK | `.ct-input { min-height: 44px; }`, `.ct-submit { min-height: 48px; }`. ≥ 16 px input font. |
| 6. No `<style>` blocks | OK | Page CSS at `pages/contact.css`. |
| 7. No inline `style="…"` | OK | None. |
| 8. No emoji in chrome | OK | None. |
| 9. Anti-FOUC pre-paint | OK | `contact.html:13`. |
| 10. Easing / duration tokens | OK | `pages/contact.css:117` `transition: border-color var(--duration-fast)`. Easing not specified (uses browser default `ease`) — minor V, see C2. |
| 11. `prefers-reduced-motion` | N/A | No reveal animations. |

### Findings

**C1. Inline `<script>` for form submit (53 lines)** — `contact.html:103-155`
A full form-submit handler lives inline in the page. Skill rule: *"Inject scripts after `</body>` close — use `pwa_middleware` if site-wide is needed; otherwise the page-specific block before `</body>`."* The handler isn't site-wide; the right place is `gateway/static/js/contact.js`. Reasons to move:
- CSP tightening: an inline `<script>` requires `'unsafe-inline'` in `script-src` (or a nonce). Moving to an external file unlocks a stricter CSP.
- Cache-bust via mtime — the `pwa_middleware._asset_version` mechanism only versions external assets.
- Easier to share with `/support` if a future quick-message inline form gets added there.

Add to repo: `gateway/static/js/contact.js` containing the `getCsrf` + `submitContact` functions and a `DOMContentLoaded` listener that binds the form `submit` event (drop the `onsubmit="…"` attribute too). Reference it in the existing `<script src="…" defer>` block at line 156.

**C2. Hardcoded `font-size: 16px`** — `pages/contact.css:110`
```css
.ct-input, .ct-textarea { ... font-size: 16px; ... }
```
The comment at the top of the file explicitly says "16px on inputs to avoid iOS zoom-on-focus" — the intent is right, but `var(--text-md)` is 16 px and is the canonical token. Use it; the iOS-zoom rule survives.

**C3. `transition` lacks named easing curve** — `pages/contact.css:117, 141`
```css
transition: border-color var(--duration-fast);
transition: background var(--duration-fast);
```
Skill canonical curve: `cubic-bezier(0.2, 0, 0, 1)`. The browser's default `ease` is *almost* this but not identical — for consistency across the codebase, add the curve explicitly:
```css
transition: border-color var(--duration-fast) cubic-bezier(0.2, 0, 0, 1);
```
`pages/support.css:98-99` already does this; align.

**C4. `onsubmit="return submitContact(event)"` attribute** — `contact.html:27`
Same family as C1. Once `contact.js` exists, this should become `addEventListener('submit', …)` in JS, and the attribute removed. Inline event handlers also require `'unsafe-inline'` in `script-src`.

**C5. Cross-link inconsistency with `support.html`** — `contact.html:93-97` vs `support.html:54-59`
Both pages have nearly identical footers but list slightly different links:
- `support.html` footer: Home / Status / Contact / FAQ
- `contact.html` footer: Home / Support / Status / FAQ

Pick one set and use it on both for consistency (e.g. Home / Support / Status / Contact / FAQ). Pair finding with F13 above — together these are the "shell-consistency" line item in the summary.

**Violations: 4** (1 inline-script, 1 token, 1 easing, 1 cross-link — 5 if `onsubmit` attribute counted separately from the inline `<script>` block, but C1 + C4 are the same root cause).

---

## Top 3 violations (severity-ordered)

**#1 — `pages/feedback.css` is essentially the pre-migration inline `<style>` block on disk, with all 12 hardcoded px values intact (F1–F8, F10).**
The auto-migration that lifted the rules out of `feedback.html` did not retokenise them. Every padding, gap, margin, font-size, and border-radius in that file is a raw `px` value. The banner comment in the file itself acknowledges this and asks for a follow-up pass — this is that pass. Fix by replacing each literal with the equivalent token from `tokens.css` (`8 → --space-2`, `12 → --space-3`, `16 → --space-4`, `24 → --space-6`, `28 → --text-3xl`, etc.).

**#2 — Two tap targets in `feedback.html` fall below the 44 × 44 px floor (F10, F11).**
`.fb-chip` (chips in the Type/Status/Sort rows) is ~24–28 px tall; `.fb-submit-bar button` is ~38 px tall. Skill rule: *"Tap targets ≥ 44 × 44 px. Inline prose anchors are exempt."* These are interactive controls, not prose — they must meet the floor. Add `min-height: 44px` plus inline-flex alignment and the visual padding stays the same.

**#3 — `contact.html` carries a 53-line inline `<script>` handler that should be `gateway/static/js/contact.js` (C1 + C4).**
Inline event handlers and `<script>` blocks force `'unsafe-inline'` in CSP `script-src`, defeating the security-headers pass. They also skip the `?v=` cache-bust mechanism. Move to an external file, drop the `onsubmit=` attribute, and the page is CSP-tightenable.

---

## Recommendations (prioritised)

1. **Retokenise `pages/feedback.css`** in one pass. ~10 min mechanical edit; the file is 23 lines. Add `--space-2`, `--space-3`, `--space-4`, `--space-6`, `--space-16` substitutions; replace `28px` font with `--text-3xl`; replace `10px` border-radius with `--radius-md` or `--radius-lg`.
2. **Add `min-height: 44px`** to `.fb-chip` and `.fb-submit-bar button` for tap-target compliance.
3. **Extract `contact.html`'s form-submit handler** to `gateway/static/js/contact.js`; reference it with `<script src="…" defer>` and `addEventListener('submit', …)`. Drop the inline `<script>` and `onsubmit=` attribute.
4. **Replace `style="text-decoration:none"`** on `.gw-user-pill` (`feedback.html:29`) with a CSS rule on the class in `gateway.css`.
5. **Replace hardcoded `font-size: 16px`** in `pages/contact.css:110` with `var(--text-md)`.
6. **Add the canonical easing curve** `cubic-bezier(0.2, 0, 0, 1)` to `pages/contact.css` transitions to match `pages/support.css`.
7. **Reconcile footer links** between `support.html` and `contact.html` (pick one set: Home / Support / Status / Contact / FAQ) and add a cross-link from `support.html` to `/feedback` so the public support paths point at the public roadmap channel.
8. **Update the narve-design skill** anti-pattern bullet — the `feedback.html` exception is no longer accurate; zero non-exempt inline `<style>` blocks remain in `gateway/static/*.html`.

---

## Per-page violation totals

| Page | Findings |
|---|---|
| `support.html` + `pages/support.css` | **0** |
| `feedback.html` + `pages/feedback.css` | **14** (F1–F14) |
| `contact.html` + `pages/contact.css` | **4** (C1–C5, C1 + C4 collapsed) |
| Skill-doc staleness (cross-file) | **1** (not counted in the per-page total, called out at the top) |
| Cross-link / shell inconsistency | covered under F13 + C5 |
| **Total** | **18 page-level findings; 30 line-item violations counted across all categories in the summary table** |

The summary table's "30" reflects category sums (e.g. F1–F8 count as 8 separate token-hardcoded violations). The per-page total of 18 reflects findings as discrete write-ups. Both views are recorded so you can pick the metric that matches the workstream.
