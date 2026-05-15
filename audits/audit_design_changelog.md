# Design audit — `/changelog` page

**Scope:** `gateway/static/changelog.html`, `gateway/static/pages/changelog.css`, and the supporting partials (`_partials/changelog_widget.html`, `changelog_widget.css`, `changelog_widget.js`) plus the server renderer at `gateway/changelog_routes.py:render_entry_html` that emits the per-release card markup the CSS targets.

**Standard:** `~/.claude/skills/narve-design` — monochrome only, three-typeface ceiling (Inter / Geist Mono / Instrument Serif Italic), tokens-only, dense, no decorative chrome, RSS link visible. User overlay: dates must be in monospace, "released" headers must be Italic Instrument Serif, RSS link visible.

**Result:** 12 violations. Visual chrome is mostly tokenised and monochrome — the failures cluster around the typography ceiling, the per-release "released" heading face, density, and a stray third-party font network call.

---

## Violations

### V1 — `cl-card__title` is Inter sans, not Instrument Serif Italic [USER BRIEF, P0]

`gateway/static/pages/changelog.css:300-307`

```css
.cl-card__title {
  font-family: var(--font-ui);
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  …
}
```

The renderer at `gateway/changelog_routes.py:388` emits `<h2 class="cl-card__title">{version}</h2>` — this is the per-release header on every changelog card (the literal "released" headline the user named). The CSS sets it to `--font-ui` (Inter sans, semibold). Per the brief and per narve-design ("Display / hero headlines: Instrument Serif Italic — used sparingly for hero copy and page-level 'feature' headings"), every release header on a changelog feed is exactly the page-level "feature" heading case and should be `var(--font-display)` with `font-style: italic; font-weight: 400`, matching the `.cl-page-title` block at lines 249–258. As written, the page has one italic hero ("What's new") and then drops back to Inter for every release card, which inverts the intended editorial rhythm.

### V2 — Fourth typeface in bullets via `--font-body` (Source Serif 4) [P0]

`gateway/static/pages/changelog.css:403-419` and `gateway/static/tokens.css`:

```css
.cl-bullets li { font-family: var(--font-body); … }
```

`--font-body` resolves to `"Source Serif 4", Georgia, "Times New Roman", serif`. narve-design is explicit: "**Never anything else.** No Helvetica fallback, no system-ui as 'temporary,' no decorative serif." Source Serif 4 is a fourth typeface, full stop, regardless of whether it's also declared on `body` site-wide. The bullets, `.cl-bullets strong`, `.cl-empty`, `.cl-footnote`, all hit `--font-body`. Either restrict bullets to `var(--font-ui)` (Inter) for body copy and reserve `var(--font-display)` purely for hero/release headers, or — if the team has decided editorially that body text is now serif — push that conversation up to `tokens.css` and update the narve-design skill, since this page is just one of dozens that now ship a fourth face. The skill currently treats this as a hard rule. (Note: this is a vault-wide token decision, not a changelog bug, but it manifests here.)

### V3 — Third-party Google Fonts fetch for Instrument Serif [P0]

`gateway/static/changelog.html:18-20`:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@1&display=swap" rel="stylesheet">
```

Every other narve face is self-hosted (`gateway/static/fonts/Inter-Variable-subset.woff2`, etc.) and `font-display: swap` is set locally. This page reaches out to Google Fonts on every visit, which (a) breaks the "fallback chain ends at Inter; if Inter hasn't loaded, the page waits with `font-display: swap`" guarantee, (b) leaks user IPs to Google on a privacy-marketed product, (c) trips Cloudflare's render-blocking-request budget, and (d) introduces a render path that doesn't honour the cache-bust `?v=mtime` convention. Self-host Instrument Serif Italic the same way Inter is hosted, or remove these three lines entirely and rely on the `--font-display` chain's Georgia fallback that tokens.css already provides. There is no other narve page that pulls fonts from Google.

### V4 — Raw px in `.nv-changelog-item__title` / `__body` [P1]

`gateway/static/changelog_widget.css:115` and `:118`:

```css
.nv-changelog-item__title { … font-size: 13.5px; }
.nv-changelog-item__body  { font-size: 12.5px; … }
```

narve-design: "Sizes use the `--text-*` token scale (xs through 5xl). No raw px values for type." Use `var(--text-sm)` / `var(--text-xs)` (or add a `--text-2xs` token if 12.5 px is load-bearing).

### V5 — Raw px in widget unseen dot, badge geometry, and date row [P1]

`changelog_widget.css:128, 132, 38-42, 60, 107`:

- `font-size: 9px;` and `vertical-align: 2px;` on the unseen dot.
- `min-width: 18px; height: 18px; padding: 0 6px; border-radius: 9px;` on the badge.
- `height: 24px;` and `padding: 0 var(--space-2);` on the collapse button.
- `font-family: ui-monospace, SFMono-Regular, Menlo, monospace;` on the date row — bypasses `--font-mono` (Geist Mono).

The date row in particular is the user's explicit "dates in monospace" requirement; today it falls through to the OS mono stack, not Geist Mono, so the date glyphs differ between this widget and `.cl-card__date` on the main page (which correctly uses `var(--font-mono)`). Swap to `var(--font-mono)`; replace pixel geometry with `--space-*` and a small new radius token if 9 px is intentional.

### V6 — `font-family: ui-monospace, …` instead of `var(--font-mono)` on widget date row [P0, narve-design hard rule violation]

`gateway/static/changelog_widget.css:107`:

```css
.nv-changelog-item__date {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  …
}
```

This is the second of the three typefaces — must be Geist Mono via `var(--font-mono)` to match `.cl-card__date` at `pages/changelog.css:312`. Splitting the mono face into two definitions (`var(--font-mono)` on the page card, raw `ui-monospace` in the widget) means on systems without Geist Mono installed the OS stacks diverge and the widget's dates don't read as part of the same family. (Called out separately from V5 because narve-design treats the typeface ceiling as a hard rule, not a tokenisation nit.)

### V7 — Inline `style="…"` attributes in `changelog_widget.js` skeleton + error states [P1]

`gateway/static/changelog_widget.js:61, 63, 65, 76`:

```js
'<span class="skeleton skeleton-text-sm" style="width:90px;display:inline-block">'
…
'<button … data-changelog-retry style="background:none;border:none;color:var(--text-secondary);text-decoration:underline;cursor:pointer;padding:0;font:inherit">'
```

narve-design anti-pattern: "Write `style="…"` inline in HTML for anything other than the rare per-card colour variable." Promote the retry button to a `.cl-changelog-retry` class in `changelog_widget.css` (or reuse `narveSkel.error` per the skill's "Reach for the existing component" table). Skeleton width inline styles can be replaced by a `.skeleton-w-90`/`.skeleton-w-70`/`.skeleton-w-55` set or expressed with the standard `--skeleton-w` custom prop.

### V8 — Widget renders "Loading…" semantics via custom skeleton rather than `narveSkel.show` [P1]

`changelog_widget.js:56-69` hand-rolls a skeleton list. narve-design says: "If you need loading state → use `narveSkel.show(container, {shape, count})`. Don't write 'Loading…' string." The custom skeleton diverges from every other narve loading state and reimplements the same `.skeleton` / `.skeleton-text-sm` classes inline. Pull `narveSkel.show(listEl, {shape: 'list', count: 3})` instead.

### V9 — Hardcoded scroll-offset and bar-height magic numbers [P2]

`pages/changelog.css:285` `scroll-margin-top: 96px;` and `:131-134` form/inner paddings, plus the JS scroll threshold `y > 120` at `changelog.html:127`. The comment at `:284` even admits "Sticky bar is roughly 64–72px tall when expanded" — i.e. drifts the moment the bar's padding changes. Express as `calc(var(--space-12) + var(--space-3))` or expose a `--cl-bar-h` custom property the bar itself owns. Same for the JS threshold — read the bar's `getBoundingClientRect().height` or a CSS-driven scroll-driven animation.

### V10 — `<style>` reach for `backdrop-filter` blur(12px) on the sticky bar [P2]

`pages/changelog.css:40-46`:

```css
background: color-mix(in srgb, var(--bg-base) 92%, transparent);
backdrop-filter: saturate(180%) blur(12px);
```

narve-design rules out "No decorative chrome … No animations beyond opacity, transform-translate, and width/height transitions" and "Reach for `box-shadow` for non-modal elements" — backdrop blur is the visual cousin of those. Compare with how the dashboard sidebar and app shell handle stickiness: a flat `var(--bg-base)` with `var(--border-ghost)` underline, no blur. The blur reads more "consumer SaaS" than narve's restraint. Drop the `backdrop-filter` pair; the `--border-ghost` bottom and a non-transparent `--bg-base` background carry the affordance.

### V11 — Density: card padding + page padding bypass density tokens [P2]

`pages/changelog.css:282` `padding: var(--space-6);` and `:226` `padding: var(--space-8) var(--space-6);`. narve-design: "A density toggle (comfortable / compact) exists via `[data-density]`; design defaults to comfortable but ensures compact still reads. Density: `--row-pad-y`, `--row-pad-x`, `--card-pad`, `--section-gap`, `--page-pad` — change with `[data-density="compact"]`." This page uses raw `--space-*` rather than `--card-pad` / `--page-pad`, so flipping `data-density="compact"` has no effect on it. Swap `padding: var(--space-6)` → `padding: var(--card-pad)` and `padding: var(--space-8) var(--space-6)` → `padding: var(--page-pad)` (or whatever the density token resolves to).

### V12 — `<script>` block injected in the page template instead of an external file [P2]

`changelog.html:101-175` carries a ~75-line `<script>` that handles subscribe-bar collapse + newsletter form. narve-design anti-pattern: "Add a `<style>` block in a page template (extend `gateway.css` or component CSS instead). One template (`feedback.html`) currently has one — don't replicate the pattern." The same logic applies to inline `<script>` — move to `gateway/static/js/changelog_page.js` and reference via `<script src=… defer>` so `pwa_middleware._asset_version` cache-busts it. Bonus: keeps the HTML diff small for future copy edits.

---

## Things the page gets right (don't regress in fixes)

- `<link rel="alternate" type="application/rss+xml">` at line 23 is present and feed readers auto-detect. RSS feed button is visible in the subscribe bar at line 44, not hidden under a meta-only discovery. **User requirement met.**
- `.cl-card__date` (the per-release date on the main page) is correctly `var(--font-mono)` + `font-variant-numeric: tabular-nums;` + `text-transform: uppercase;` at `pages/changelog.css:311-318`. **User "dates in monospace" requirement met on the main page** (broken only in the widget — V6).
- The hero "What's new" headline at `.cl-page-title` (`pages/changelog.css:249-258`) is correctly Instrument Serif Italic via `var(--font-display)` + `font-style: italic`. Matches the user's brief for the page-level headline.
- All colour values resolve through tokens — no `#fff`, `#000`, or `rgba()` in either CSS file. Light/dark works for free.
- `cl-chip` legacy pill chrome has been deliberately neutralised at `:330-372` so the server can still emit BEM-tagged section labels without colour. Comments explain the why. Good defensive pattern.
- No emoji in chrome (titles, buttons, headers).
- Anchor `scroll-margin-top` exists so RSS deep-links don't land under the sticky bar (only the magic-number expression is the issue, V9).
- Responsive collapse at `@media (max-width: 700px)` at `:506-534` handles iPhone/Android widths and explicitly drops the date below the title so two baselines don't crush together. Mobile coverage rule honoured.

---

## Top 3

1. **V1 — `cl-card__title` is Inter, must be Instrument Serif Italic.** This is the user's explicit brief and the most visible regression on the page: every release header reads as plain UI chrome rather than editorial. One-line CSS fix in `pages/changelog.css:300-307`.
2. **V3 — Third-party Google Fonts call.** Privacy + performance + cache regression on a privacy-marketed product. Three lines to remove in `changelog.html:18-20`, plus self-host the Instrument Serif Italic woff2 next to `Inter-Variable-subset.woff2`.
3. **V2 — Fourth typeface (Source Serif 4) in bullets via `--font-body`.** Hard-rule violation per narve-design. Either restrict bullets to Inter or, if the editorial-body decision is intentional, surface it to the skill and tokens.css so it's a documented exception, not silent drift.

---

## Out of scope (flagged, not opened)

- The site-wide `--font-body: "Source Serif 4"` token decision in `tokens.css` is the root cause of V2 and affects more than just `/changelog`. It deserves its own design discussion ("is narve's body face now serif? if so, update the skill") rather than a per-page patch.
- The "What's new" widget reimplements `narveSkel` and `narveToast`-style affordances (V7, V8) rather than calling the shared utilities. Worth a small cleanup pass when the widget is next touched.

— Audit run 2026-05-15.
