# Design audit — API surfaces (`api_docs.html` + `settings_api_keys.html`)

Auditor: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
Date: 2026-05-15.
Scope:

- `gateway/static/api_docs.html` (556 lines)
- `gateway/static/pages/api_docs.css` (513 lines)
- `gateway/static/settings_api_keys.html` (83 lines) — note: file is named
  `settings_api_keys.html`, not `api_keys.html`. `admin_api_keys.html`
  exists separately (36 lines) and was spot-checked; main focus is the
  user-facing `/settings/api-keys` surface.
- `gateway/static/pages/settings_api_keys.css` (326 lines)

Checks (per narve-design rules):

1. **Monochrome only** — no hex/rgb/colour names anywhere; no red / green /
   amber for status. Hierarchy by weight / position / typography.
2. **Three typefaces only** — Inter (`--font-ui`), Geist Mono
   (`--font-mono`), Instrument Serif (`--font-display`). Source Serif 4
   (`--font-body`) is permitted as a 4th editorial face site-wide (defined
   in `tokens.css`; the skill SKILL.md predates this addition and should
   be updated). No raw system fallback chains, no hardcoded
   `ui-monospace` / `Helvetica` / `system-ui`.
3. **Tokens (no raw values)** — colours, spacing, type sizes, radii must
   reference CSS variables. No `padding: 16px`, no `#fff`, no `12px`
   font-size.
4. **AA contrast** — every text element ≥ 4.5 : 1 in both themes; uses
   tokenised text colours, never opacity tricks for hierarchy.
5. **Mobile** — tap targets ≥ 44 × 44 px (inline prose links exempt);
   form inputs ≥ 16 px font-size (defeats iOS auto-zoom); honours
   reduced-motion.
6. **No decorative chrome on code samples** — code samples should be
   recessed (background-only) and scannable; gratuitous borders,
   rounded-corner cards, or shadow on `<pre>` is over-decoration.
7. **Geist Mono for endpoint paths / code blocks** — every API path,
   header string, code sample, JSON body, key prefix, hostname uses
   `var(--font-mono)`.
8. **Theme pre-paint script** — every page needs the inline cookie-read
   `<script>` to set `data-theme` before paint (anti-FOUC). Canonical
   form: `(function(){try{var m=document.cookie.match(/narve-theme=/);…})()`.
9. **No `<style>` blocks in page templates** — CSS lives in
   `pages/<slug>.css`, not inline in the HTML head.
10. **No emoji in chrome** — titles, buttons, nav, error messages.
    Typographic glyphs (`→`, `←`, `·`, `…`, `—`) are not emoji.
11. **Scannability** — endpoint paths visually distinct from prose;
    section breaks crisp; no buried CTAs; TOC + anchors land cleanly.

Notation: **V** = violation, **OK** = pass, **N/A** = not applicable.

Numeric font-sizes inside CSS are flagged when they bypass the
`--text-*` token scale, regardless of whether the token exists.

---

## `gateway/static/api_docs.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour, no hex, no rgb in markup. Method chips described as monochrome via outline + weight, never coloured. |
| 2. Three (+1) typefaces | OK | No `font-family` overrides; relies on `--font-ui` body inherit + class-level `--font-mono` / `--font-display` swaps. |
| 3. Tokens | OK | No raw px or hex. All chips/buttons/colours via class. |
| 4. AA contrast | OK | Uses token pairs only. Trusts `--text-primary / -secondary / -tertiary`. |
| 5. Mobile tap targets | OK | TOC links inline in prose context (44 px not required); `.apidoc-meta__chip` chips at `height: 32px` are inline links (HIG-exempt) but bordered targets; verified ≥ 32 × 44 px via padding + height on mobile spec at line 487 of CSS. Borderline — see CSS row 5. |
| 6. No decorative chrome on code | **V** | Every `<pre class="apidoc-code">` is wrapped in a bordered, rounded card. See CSS audit (row 6). The HTML correctly applies the class — violation lives in the CSS — but the markup commits to the card pattern (every code sample is `apidoc-code`, not a minimal `<pre>`). |
| 7. Geist Mono for code | OK | Every endpoint path (`.apidoc-path`), inline code (`.apidoc-mono`), method chip (`.apidoc-method`), auth tag, version chip, and `<pre>` block resolves to `--font-mono`. Consistent. |
| 8. Theme pre-paint script | OK | Line 9: inline script reads `narve-theme` cookie (with legacy `betyc-theme` fallback) and sets `data-theme` before paint. |
| 9. No `<style>` blocks | OK | None. Page CSS at `pages/api_docs.css` (+ shared `gateway.css`, `components.css`). |
| 10. No emoji in chrome | OK | `&larr;` (line 28), `&middot;` (line 505), `&hellip;` (lines 152, 168, 472, 498) are typographic entities, not emoji. No actual emoji. |
| 11. Scannability | OK | Hero → meta chips → TOC (2-col grid) → sections with `<h2>` rules and lede. Endpoint cards expose `[METHOD] [PATH] [AUTH-TAG]` row before description. Reads top-to-bottom and via TOC anchors equally well. |

**Violations: 1.**

Additional observations (not strict V, but worth noting):

- The copy-button feature (lines 510–552) is injected client-side via a
  per-page `<script>` block in the body. The script is self-contained
  and small. Site-wide pattern would normally route this through
  `pwa_middleware`, but a single-purpose copy widget is reasonable as
  page-local script. No violation, just noted.
- `apidoc-method--post / --patch / --delete` invert background to convey
  "mutating verb" via contrast, not colour (line 286–294 of CSS). Stays
  monochrome — good.
- Method-chip `min-width: 56px` (line 283) — using raw px for chip width.
  Acceptable since it's a content-driven minimum, not a layout value, but
  worth tokenising for consistency.

---

## `gateway/static/pages/api_docs.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No hex, no rgb, no named colours. All colours via `--text-*`, `--bg-*`, `--border-*`, `--interactive-*` tokens. |
| 2. Three (+1) typefaces | OK | Only `--font-ui`, `--font-mono`, `--font-display`. No raw system fallbacks. |
| 3. Tokens | **V** | Three raw-px font-sizes inside `@media` blocks bypass the `--text-*` scale: line 480 `font-size: 12px;` (mobile `.apidoc-code`), line 500 `font-size: 12px;` (`.apidoc-meta__chip` at very small width). Line 487 `min-height: 32px` and line 488 `min-width: 44px` on `.apidoc-copy` are raw px — acceptable for tap-target floors (the rule itself is "≥ 44 px") but should reference `--tap-target-min` if such a token is added. Line 283 `min-width: 56px` on `.apidoc-method` is also raw px. Line 131 `padding-left: 22px` (TOC list-indent) is raw px. Aggregate: ~6 raw-px values where a token alternative exists. |
| 4. AA contrast | OK | `.apidoc-auth-tag` uses `--text-tertiary` — relies on the AUDIT #4 floor that lifted tertiary above 4.5 : 1. No opacity-driven hierarchy. |
| 5. Mobile tap targets | OK | `.apidoc-copy` floors at 32 px height on mobile (line 486) — strictly under the 44 × 44 rule, but the comment on line 484–486 acknowledges this and asserts "the 44×44 tap-target rule applies regardless." min-width: 44px is set; min-height is 32px. **Borderline V.** Apple HIG floor is 44 × 44; this is 32 × 44. Either lift `min-height` to 44 px or document the exception. Calling this a soft V. |
| 6. No decorative chrome on code | **V** | `.apidoc-code` (line 326–340) applies: `border: 1px solid var(--border-ghost)`, `border-radius: var(--radius-md)`, `background: var(--bg-inset)`, padding. Per the design rule (your prompt: "no decorative chrome on code samples"), code samples should be recessed via background alone — the bordered, rounded "card" treatment around every `<pre>` is exactly the decoration to remove. Auth cards (`.apidoc-auth`) and endpoint cards (`.apidoc-endpoint`) also use this same border + radius treatment, creating nested-card-inside-card visuals when their internal `<pre>` ALSO has its own border (lines 207–211 + 326–340). The double-card stack is the canonical "decorative chrome" failure mode. **Top finding.** |
| 7. Geist Mono for code | OK | `.apidoc-mono`, `.apidoc-endpoint code`, `.apidoc-auth code`, `.apidoc-code`, `.apidoc-method`, `.apidoc-path`, `.apidoc-auth-tag`, `.apidoc-table td:first-child`, `.apidoc-auth__badge` all reference `--font-mono`. |
| 8. Theme pre-paint | N/A | (HTML-level concern.) |
| 9. No `<style>` blocks | N/A | (HTML-level concern; nothing in CSS file scope.) |
| 10. No emoji in chrome | OK | None in CSS content. |
| 11. Scannability | OK | Type scale steps tasteful; spacing tokens consistent; section borders use `--border-ghost` for low chrome. |

**Violations: 2** (plus 1 borderline). Notable that the file is otherwise
disciplined — comments on line 6–8 explicitly call out the three-typeface
contract.

Additional observations:

- `.apidoc-table-wrap` (line 378) and `.apidoc-toc` (line 114) also use
  the bordered-rounded-card treatment. The TOC card is justified (it IS
  a separate navigable block). The table wrap pattern is conventional
  for horizontal-scroll tables. Neither is a code-sample violation, but
  the file's overall aesthetic is heavily "card on card on card." A
  single design pass to remove one ring of borders from `.apidoc-code`
  + `.apidoc-auth pre` + `.apidoc-endpoint pre` would significantly cut
  the chrome density.
- Comments on line 6–8 are exemplary: "Strict monochrome: tokens only,
  no hardcoded colours, no decorative chrome." The decorative-chrome
  promise is partially broken by the code-sample border.

---

## `gateway/static/settings_api_keys.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour in markup. (All colour violations live in the CSS.) |
| 2. Three (+1) typefaces | OK | No `font-family` overrides in markup. |
| 3. Tokens | OK | No inline `style="..."` raw values. |
| 4. AA contrast | N/A | Decided by CSS (see CSS audit). |
| 5. Mobile tap targets | OK in HTML — see CSS for `.ak-input` font-size. |
| 6. Code chrome | OK in markup; inline `<code class="ak-mono">` is single-line, not block. |
| 7. Geist Mono for code | OK | `.ak-mono` used wherever a key string, prefix, or hostname appears. |
| 8. **Theme pre-paint script** | **V** | **MISSING.** The `<head>` (lines 1–13) loads `gateway.css` + `components.css` + page CSS, but there is no inline `(function(){try{var m=document.cookie.match(/narve-theme=…)})()` block. Compare with `api_docs.html` line 9 — that page does this correctly. Dark-mode users on `/settings/api-keys` will see a flash-of-light-theme on every navigation. **High priority — anti-FOUC is a hard rule.** |
| 9. No `<style>` blocks | OK | None inline; CSS at `pages/settings_api_keys.css`. |
| 10. No emoji in chrome | OK | `←` (line 18) is typographic, not emoji. |
| 11. Scannability | OK | Backlink → H1 → lede → quota banner → keys list → create form. Single-column, top-down. The `<em>` on "shown _once_" (line 29) is appropriate emphasis. |

**Violations: 1** (high severity).

---

## `gateway/static/pages/settings_api_keys.css`

This file is the **single biggest design-system regression in the audited
surface.** It predates the strict-token discipline visible in the sibling
files and reads like a hand-coded one-off. Violations are dense.

| Check | Result | Notes |
|---|---|---|
| 1. **Monochrome** | **V (severe, multi-instance)** | Hex/rgb/colour-name violations throughout. The whole file uses fallback chains like `var(--surface, #141414)`, `var(--border, rgba(255,255,255,0.06))`, `var(--bg, #0d0d0d)`, `var(--text-primary, #fff)`, `var(--text-muted, #888)` — but the fallback isn't just a fallback, it's a hardcoded dark-theme assumption. Worse, semantic colour appears: line 190 `.ak-badge-ok { background: rgba(34, 197, 94, 0.12); color: #22c55e; }` (green for "OK") and line 234–242 `.ak-btn-danger { color: #ef4444; ... }` plus `rgba(239, 68, 68, 0.4)` / `rgba(239, 68, 68, 0.06)` (red for "danger"). Both directly violate the "no colour for categorisation, branding, or semantic meaning" hard rule and the "status indicators use position, weight, icons, or text — not red / green / yellow" rule. **Top finding.** Raw rgba alphas (`rgba(255,255,255,0.04)`, `rgba(255,255,255,0.05)`, `rgba(255,255,255,0.06)`, `rgba(255,255,255,0.08)`, `rgba(255,255,255,0.12)`, `rgba(255,255,255,0.15)`, `rgba(255,255,255,0.35)`, `rgba(0,0,0,0.3)`) appear on lines 75, 94, 95, 109, 119, 168, 183, 195, 213, 223, 253, 280, 281, 290 — at least 14 distinct raw rgba expressions. None of them route through tokens. Both are completely broken in light theme. |
| 2. Three (+1) typefaces | OK | All `font-family` references go through `--font-ui`, `--font-body`, `--font-mono`. No raw chains. (One minor note: line 20 has `-apple-system, BlinkMacSystemFont, sans-serif` as a fallback after `var(--font-ui)`; this is a manual repetition of the chain already inside `--font-ui` and bloats the cascade. Cosmetic, not a violation.) |
| 3. **Tokens (raw px / hex)** | **V (severe, multi-instance)** | Raw px and hex everywhere. Sample: line 27 `padding: 40px 24px 80px`, line 33 `margin-bottom: 8px`, line 36 `gap: 16px`, line 42 `font-size: 26px`, line 44 `letter-spacing: -0.01em` (acceptable per CSS conventions), line 52 `font-size: var(--text-sm, 13px)` (the fallback `13px` should never be reachable — token exists in `tokens.css`), line 60–66 raw px throughout `.ak-lede`, line 82 `font-size: 11px`, line 87 `margin: 36px 0 14px`, line 93–98 `padding: 12px 16px`, `font-size: 14px`, `border-radius: var(--radius-sm, 6px)`, `margin-bottom: 20px`, lines 110–112 `var(--radius-md, 10px)` (note: actual token is 8 px, fallback is `10px` — these disagree). Lines 117–158 raw px on every property in `.ak-row`, `.ak-row-head`, `.ak-row-meta`, `.ak-meta-k`. Lines 164–172 `.ak-mono` with `font-size: 12px`, `padding: 2px 6px`, `border-radius: 3px` (not a defined token). Lines 178–205 `.ak-badge*` raw px. Lines 211–246 `.ak-btn*` raw px. Lines 255–325 `.ak-create-card`, `.ak-form`, `.ak-field`, `.ak-label`, `.ak-input`, `.ak-hint`, `.ak-scope-*`, `.ak-disabled-note` — raw px on virtually every line. **No `--space-*` token used anywhere.** **No `--text-*` token used directly** (only via dead fallbacks like `var(--text-sm, 13px)`). Aggregate: 50+ raw-px values. |
| 4. AA contrast | **V** | Heavy use of `opacity: 0.55`, `0.6`, `0.62`, `0.65`, `0.7`, `0.78`, `0.88` for text hierarchy (lines 48, 55, 86, 144, 156, 274, 296, 302, 313, 323). The narve rule is hierarchy via tokenised `--text-primary / -secondary / -tertiary / -quaternary`, not opacity. Opacity-driven hierarchy is fragile across themes (light theme on a white background with `opacity:0.55` yields different contrast than the dark assumption baked into the rgba fallbacks). At least 10 opacity uses where a text-token swap belongs. |
| 5. **Mobile tap targets / 16 px form font** | **V** | Line 286: `.ak-input { font-size: var(--text-base, 14px); }`. The token `--text-base` is 16 px (per `tokens.css`), but the fallback `14px` is below the iOS auto-zoom defeat threshold. More importantly, `.ak-input` is the only input style in the file — there is no `@media (max-width: 640px)` override raising it to ≥ 16 px even if the token resolves. Hard rule: "inputs ≥ 16 px font on mobile (defeats iOS auto-zoom)." Likely fine in production thanks to `--text-base = 16px`, but the file is one token-rename away from regressing. Line 211 `.ak-btn { padding: 8px 14px; font-size: 12px; }` — final button height computes to ≈ 32 px (8 + 12 + 8 + line-height padding), well under 44 × 44 px tap floor for the primary "Create key" CTA. **V.** Line 318 `.ak-scope-row input { margin-top: 3px }` — checkbox tap area is whatever the UA gives a default `<input type="checkbox">`, typically ≤ 18 × 18 px. The wrapping `<label>` extends the hit-area, but no explicit `min-height: 44px` is set. |
| 6. No decorative chrome on code | N/A | This file's "code" is inline `.ak-mono` only; no `<pre>` blocks. Inline chrome (background, padding, radius) is acceptable for inline. |
| 7. Geist Mono for code | OK | `.ak-mono` (line 163) and `.ak-input-mono` (line 294) use `var(--font-mono)`. |
| 8. Theme pre-paint | N/A | (HTML-level.) |
| 9. No `<style>` blocks | N/A | (HTML-level.) |
| 10. No emoji in chrome | OK | None. |
| 11. Scannability | OK | Layout decisions are sensible (single column, generous padding, clear hierarchy). The issue is purely token discipline, not layout. |
| 12. `--radius-md` mismatch | **V** | Line 110, 254: `var(--radius-md, 10px)`. Actual token value in `tokens.css` line 225 is `8px`. The fallback `10px` is what renders if (e.g.) the page loads with `tokens.css` not yet applied — guarantees a flash-of-wrong-radius on slow connections. Either drop the fallback or fix it to `8px`. |
| 13. Border-radius pill 10px | **V** | Line 182: `.ak-badge { border-radius: 10px; }` — raw px, not `var(--radius-full)` (which the design system specifies for pills) or `var(--radius-sm)` (6 px) / `--radius-md` (8 px). |

**Violations: 6 distinct categories, with 50+ underlying instances.** This
file needs a tokenisation pass, not a patch.

Additional observations:

- The file's `body` selector (line 16) overrides the global `--font-ui`
  with raw fallback chain `var(--font-ui), -apple-system, ...` which
  duplicates the chain already in the token. Cosmetic but redundant.
- The narve-design SKILL.md lists three typefaces (Inter, Geist Mono,
  Instrument Serif) but `tokens.css` line 246–253 defines a 4th
  (`--font-body` = Source Serif 4) and the file legitimately uses it for
  editorial body copy on settings surfaces. The skill text should be
  updated to acknowledge the fourth face. Not a CSS violation; a skill
  documentation gap.
- Comments on lines 1–14 are clear about intent (use serif for body,
  Inter for chrome, Mono for keys). The intent is good. Execution
  bypasses tokens.

---

## Aggregate

| Surface | Violations |
|---|---|
| `api_docs.html` | 1 |
| `pages/api_docs.css` | 2 (+1 borderline) |
| `settings_api_keys.html` | 1 |
| `pages/settings_api_keys.css` | 6 |
| **Total** | **10** |

### Top 3 violations (by impact)

1. **`pages/settings_api_keys.css` — semantic colour for status (lines
   189–192 green-OK badge, lines 234–242 red-danger button).** Direct
   violation of the monochrome-only hard rule. `#22c55e` (green) and
   `#ef4444` (red) appear three times each. Replace with weight,
   icons, or text — e.g. the OK badge should be a `--bg-inset`
   background + `--text-primary` foreground, the danger button should
   be a ghost outline with bold weight or an Inter caps label.

2. **`pages/settings_api_keys.css` — wholesale failure to use tokens
   (50+ raw px values, ~14 raw rgba expressions, hardcoded dark-theme
   hex fallbacks like `#0d0d0d`, `#141414`, `#fff`, `#888`).** The file
   is functionally dark-theme-only because every fallback assumes dark
   surfaces. In light theme, if `tokens.css` ever fails to load (or
   loads after this file), the page paints black. Needs a full
   tokenisation sweep against `--space-*`, `--text-*`, `--bg-*`,
   `--border-*`, `--text-primary/secondary/tertiary`.

3. **`pages/api_docs.css` — bordered card chrome on every code sample.**
   `.apidoc-code` (lines 326–340) wraps every `<pre>` in
   `border + border-radius + padding`. Combined with the parent
   `.apidoc-endpoint` and `.apidoc-auth` cards (also bordered), the
   page renders as nested cards-within-cards on every endpoint with a
   code sample. Per narve rules (and your audit brief), code samples
   should be recessed via background only. Removing the border and
   keeping `background: var(--bg-inset)` + `border-radius:
   var(--radius-sm)` (or no radius) cuts the chrome and makes
   endpoints read faster.

### Honourable mentions (not in top 3 but worth fixing)

- `settings_api_keys.html` missing theme pre-paint script (high impact
  for any dark-theme user; one-line fix).
- `pages/settings_api_keys.css` opacity-driven hierarchy (`opacity:
  0.55 / 0.62 / 0.78`) instead of `--text-secondary / -tertiary`.
- `.ak-btn` final tap-target ≈ 32 px (under 44 px floor).
- `--radius-md` fallback mismatch: file uses `var(--radius-md, 10px)`,
  token resolves to `8px`. The two disagree.

### Where the audit found nothing to flag

- `api_docs.html` markup is exemplary: clean tokens via classes, theme
  script present, no emoji, no inline styles, no `<style>` block,
  scannable hero / TOC / sections / footer.
- `api_docs.css` typography discipline is correct: Geist Mono for every
  endpoint path, method chip, code block, header string, version chip,
  auth tag, inline `<code>`.
- Method chips correctly stay monochrome (weight + invert, not red /
  green / blue).

### Recommended order of fixes

1. Drop `#22c55e` and `#ef4444` from `settings_api_keys.css` —
   30-minute fix, lifts the file to single-digit violations.
2. Add theme pre-paint script to `settings_api_keys.html` — copy line 9
   from `api_docs.html`. 1-minute fix.
3. Remove border on `.apidoc-code` in `api_docs.css` (keep background).
   2-minute fix.
4. Tokenisation sweep on `settings_api_keys.css` — replace raw px with
   `--space-*` / `--text-*` and raw rgba with `--bg-*` / `--border-*`.
   30–60 minute fix; touches ~80 lines.
5. Lift `.ak-btn` tap floor to 44 × 44 px on mobile.
