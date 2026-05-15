# Design audit — `collections.html` + `collection_detail.html`

**Date**: 2026-05-15
**Files audited**:
- `/Users/shocakarel/Habbig/gateway/static/collections.html`
- `/Users/shocakarel/Habbig/gateway/static/collection_detail.html`
- `/Users/shocakarel/Habbig/gateway/static/pages/feeds.css` (shared)

**Standard**: narve-design skill — monochrome only, sanctioned typefaces, tokens not hardcoded values, AA contrast both themes, ≥ 44×44 px tap targets on mobile, ≥ 16 px input font on mobile, no inline `style=` (except per-card colour variables), no `confirm()` / `alert()`, no in-page `<style>` blocks, no decorative chrome, `prefers-reduced-motion` honoured.

Scope: only `collections.html` and `collection_detail.html` are explicit subjects; `pages/feeds.css` is the dedicated stylesheet they both pull and is reviewed for context (it is mostly compliant; findings against it are flagged separately). No `collections.css` / `collection_*.css` exists.

---

## Summary

| Category | Count |
|---|---|
| Monochrome violations | 0 |
| Typeface violations (vs skill's "three only" rule) | 2 |
| Inline `style="…"` violations | 33 |
| Token / hardcoded-value violations inside inline styles | 5 |
| Anti-pattern: `confirm()` / `window.prompt()` | 3 |
| Anti-pattern: page-local component reinvention | 3 |
| Mobile tap-target / input-font violations | 4 |
| Theme / contrast risk | 1 |
| Accessibility (dialog, aria, focus management) | 5 |
| **Total findings** | **56** |

Monochrome is intact — both templates use only token-driven greyscale (no red / green / amber anywhere). The catastrophic issue is **inline `style="…"` density**: 22 inline-style attributes in `collections.html` (lines 31, 47, 66–69, 71–73, 74, 75–78, 82–86, 88–91, 94–99, 101–104, 107–111, 118–119) and 11 in `collection_detail.html` (lines 36, 43–46, 53–54, 70–73, 74–77, 79, 80–84, 86–90, 91–92, plus four JS-generated inline styles in the search-result template literal at 201, 207–210, 213–223, 248, 256). All of this CSS belongs in `pages/feeds.css` — that file already owns the `feed-*` design vocabulary. A `narve-design` hard rule reads: *"Write `style='…'` inline in HTML for anything other than the rare per-card colour variable."*

The two templates additionally re-build a **dialog** (`collections.html` `<dialog>` lines 65–124), a **modal-like add panel** (`collection_detail.html` `#c-add-modal` lines 69–95), and **search-result rows** (JS at 201–225) — three component reinventions when `.nv-modal` / `.nv-modal__panel` already exist. The "comment of shame" on line 171 admits this: *"Confirmation via native dialog kept; replacing confirm() requires wiring the .nv-modal pattern which lives outside this template."* The fix is to wire it, not document the shortcut.

---

## Findings

### Critical (blocks design-system claims)

**1. Inline `style="…"` everywhere — 33 occurrences across 2 files** — `collections.html:31,47,66–124` + `collection_detail.html:36,43–46,53–54,70–95,201,207–223,248,256`

Hard rule: *"❌ Write `style='…'` inline in HTML for anything other than the rare per-card colour variable (`style='--accent: #ef4444'`)."* None of these inline styles is a per-card colour variable.

Worst offenders:
- The entire `<dialog id="c-new-dialog">` block in `collections.html` (lines 65–124) — every element (dialog, h3, three labels, three inputs, two buttons) is positioned and typed via inline `style="…"`. About 800 characters of CSS inlined per template render.
- The `#c-add-modal` block in `collection_detail.html` (lines 69–95) repeats the same input/label pattern inline.
- The search-result row HTML built by `renderResults()` (line 201–225) ships 9 inline-style attributes per row inside a `.innerHTML` string, including hardcoded `border-radius: var(--radius-md)` and `padding: var(--space-3)` — these would be one CSS class.

Fix: move all of this into `pages/feeds.css` under new selectors `.feed-dialog`, `.feed-dialog__title`, `.feed-field`, `.feed-field__label`, `.feed-field__input`, `.feed-field__select`, `.feed-field__textarea`, `.feed-search-row`, `.feed-search-row__kind`, `.feed-search-row__title`, `.feed-search-row__subtitle`. After this move, the HTML is just `<dialog class="feed-dialog">…</dialog>` and the rendered HTML in `renderResults` is a one-liner per row.

**2. Native `confirm()` for destructive action** — `collection_detail.html:172`
```js
if (!confirm('Delete this collection? This cannot be undone.')) return;
```
Hard rule: *"❌ Use `alert()` or `confirm()` — toast or modal."* Skill component table: *"Confirm before destructive action → modal with two buttons via `.nv-modal`."* The in-line comment at 170 acknowledges the violation. The `.nv-modal` pattern exists; wire it.

**3. Native `window.prompt()` as clipboard fallback** — `collection_detail.html:272, 276`
```js
} else {
  window.prompt('Copy this link:', url);
}
```
Two occurrences. Skill rule: never use the native dialogs. The fallback should be: surface the URL in a toast with a "Copy" button, or open a `.nv-modal` containing a selectable `<input value=url readonly>`. Alternatively, narve already exposes `[data-share]` — the comment on line 263 admits this template uses a "back-compat path"; replace it with the standard `<div data-share data-share-url="…" data-share-title="…">` per the skill component table.

**4. `Source Serif 4` is a 4th typeface — exceeds "three typefaces total"** — `collections.html:17` + `collection_detail.html:22`
```html
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
```
Skill hard rule: *"Typography — three typefaces total. Body / UI: Inter. Monospace: Geist Mono. Display / hero headlines: Instrument Serif Italic. **Never anything else.** No Helvetica fallback, no system-ui as 'temporary,' no decorative serif. The fallback chain ends at Inter."*

The repo has elected to introduce `--font-body: "Source Serif 4"` (tokens.css:249) as a 4th face, self-hosted at `gateway/static/fonts/SourceSerif4-Variable.woff2`. This is now used by `feeds.css` on `.feed-prose`, `.feed-lede`, `.feed-row-title` fallback, etc. **Either the skill is stale or the codebase is non-compliant** — both can't be true. Flag as a design-system intent decision the audit can't resolve unilaterally: pick one of (a) keep Source Serif 4, update the narve-design skill to say "four typefaces", remove the comment about three; or (b) revert `--font-body` to `var(--font-ui)` and delete the Google Fonts link + self-hosted woff2.

Even if path (a) is chosen, the **Google Fonts `<link>`** in these two templates is redundant — Source Serif 4 is already self-hosted via `tokens.css:70–82` with `local()` + `/_gateway_static/fonts/SourceSerif4-Variable.woff2`. The Google Fonts CDN duplicates the file over a third-party preconnect and triggers a privacy / GDPR concern (fonts.googleapis.com leaks visitor IPs to Google). Remove the `<link>` and the two `<link rel="preconnect">` tags above it in both files.

**5. `--font-body` referenced but file imports Source Serif 4 inconsistently** — `collection_detail.html:94, 216`
The inline-styled textarea uses `font-family: var(--font-body)` at line 94, but the inline-styled input at line 82 uses `font-family: var(--font-ui)`. There's no design reason for a textarea to render as Source Serif while a single-line input renders as Inter — visually they sit in the same form. Pick one. Inside the search-result row, the title uses `var(--font-body)` (line 216) while the kind label uses `var(--font-ui)` (line 213) — that pairing is at least intentional. Either commit to "form input = Inter" everywhere or "form input = body face" everywhere. Mixed today.

### High — page-level anti-patterns

**6. Page-local `<dialog>` reinvents `.nv-modal`** — `collections.html:65–124`
The new-collection dialog is a fully hand-built `<dialog>` element styled with 14 inline `style=` attributes. Component table: *"Modal → `.nv-modal` + `.nv-modal__panel`. Don't reinvent positioning."* The dialog also lacks:
- Backdrop click-to-close.
- ESC key handling (`<dialog>` ships ESC for free with `showModal()` — that part is fine; verify it works through the wrapper).
- Focus trap and focus return (native `<dialog>` does focus trap but only when `showModal()` is used — verify).
- An `aria-modal="true"` attribute (skip if the wrapper provides it).
- Open animation that honours `prefers-reduced-motion`.

Migrate to `.nv-modal`, then this section's other findings (#7, #16, #17, #18) collapse.

**7. Inline modal-like panel without modal semantics** — `collection_detail.html:69–95`
`#c-add-modal` is a `<div hidden>` styled to look like a modal but renders inline in the page flow (not absolutely positioned, no backdrop, no focus trap). On mobile this means the user must scroll to it after clicking "Add". Either:
- Make it a real `.nv-modal` (preferred — matches the rest of the system).
- Make it an explicit inline drawer with a named section heading and `<details>` semantics.
The current pattern is neither.

**8. Search-result rows hand-rolled via `innerHTML` string concatenation** — `collection_detail.html:199–225`
`renderResults()` builds 9 inline-styled `<div>` / `<button>` per result via template-literal concatenation. The result is unmaintainable styling (you can't see this CSS in the stylesheet), an XSS vector that has been correctly mitigated with `escHtml()` (good), and a divergence from how feed rows are styled elsewhere (the `.feed-row` selector exists, this should use it). Replace with a single `.feed-search-row` CSS class set and DOM-build the rows via `createElement` to avoid `innerHTML` entirely.

**9. Notification status only conveyed by text "Muted" / "Notify"** — `collections.html:191–192`
```js
btn.dataset.notifOn = current ? '0' : '1';
btn.textContent = current ? 'Muted' : 'Notify';
```
The button is the same button (no `aria-pressed`, no separate roles). Screen-reader users only get the new label. Add `aria-pressed="{0|1}"` on toggle and update it in lockstep with `dataset.notifOn`. While here, set `aria-label="Notifications for {{ title }}"` so it's not just "Muted" in isolation.

### High — hardcoded values inside inline styles

**10. Hardcoded `36px` `min-height` on form controls** — `collections.html:86, 111` + `collection_detail.html:84`
```css
min-height: 36px;
```
Three occurrences. The skill rule: tap targets ≥ 44 × 44 px on mobile. 36 px is the desktop minimum but fails the mobile tap-target floor for inputs and buttons that double as tap targets. The same 36 px appears in `feeds.css:107, 129, 289` for chips / search / actions — so this is a pattern across the feed family, not just collections. Tokenise as `--control-h: 36px;` (or split into `--control-h-mobile: 44px`) and use the token. Either bump the height or document an explicit exception with a fixed reason.

**11. Hardcoded `line-height: 1.55`** — `collections.html:99` + `collection_detail.html` not present
`line-height: 1.55` is a magic number for `.textarea`. No token exists. Either pick the same value used by `.feed-prose` (line 214: `line-height: 1.55`) — that consistency is at least intentional — and surface it as `--leading-prose: 1.55;`.

**12. Hardcoded `max-height: 280px` on results scroll area** — `collection_detail.html:87`
```css
max-height: 280px; overflow: auto;
```
Layout literal; no `--scroll-list-max` token. Acceptable as a one-off literal but flag for tokenisation if the pattern repeats (it does — modal-style result lists exist on `search.html` too, worth checking in a separate audit).

**13. Hardcoded `text-align: center` literal in error-state HTML** — `collection_detail.html:201, 248, 256`
Three identical placeholder-text inline-styled `<div>`s for the search results' empty / typing / failed states. These are exactly the `render_empty` / `narveSkel.error` patterns from the component table. Use the canonical helpers and drop the inline CSS.

**14. Hardcoded letter-spacing literals** — `collections.html:77, 90, 103` + `collection_detail.html:76, 215`
```css
letter-spacing: 0.08em; /* labels in dialog */
letter-spacing: 0.1em;  /* kind label in result row */
```
Two different tracking values for what looks like the same UI-meta pattern (uppercase, weight-600, tertiary). `feeds.css:362, 372` settles on `0.12em` and `0.1em` for analogous selectors. No `--tracking-*` token exists yet; flag as a future token candidate.

### High — accessibility / mobile

**15. Drop-in dialog has no labelled close button** — `collections.html:65–124`
`<dialog id="c-new-dialog" aria-labelledby="c-new-heading">` declares the heading but the only close paths are (a) ESC, (b) the Cancel button. No `<button aria-label="Close">×</button>` for users who don't know ESC dismisses dialogs. Mobile users especially expect a visible close. Add a close button or, if the design intent is strictly Cancel / Create, ensure Cancel is large enough on mobile and label it `aria-label="Close"` as well.

**16. Inline `<dialog>` border styled via inline `style=` skips light-on-dark theme verification** — `collections.html:66–69`
```html
style="border:1px solid var(--border-default);background:var(--bg-base);color:var(--text-primary)"
```
The values are tokens (good, see finding #1 for the format violation), but because they're inline, a theme regression won't be caught by linting `pages/feeds.css`. Move to CSS so theme audits cover it.

**17. Dialog open animation absent — `<dialog>` snaps in** — `collections.html:151`
```js
open.addEventListener('click', function(){ dlg.showModal(); });
```
Native `<dialog>` appears instantly. Skill rules sanction `var(--duration-base)` opacity / transform-translate fade. Add an enter transition; respect `prefers-reduced-motion` per skill ("`prefers-reduced-motion` MUST be respected").

**18. Dialog backdrop not styled** — `collections.html:65`
`<dialog>::backdrop` is unset, so it falls back to browser default (often plain black at 50 % alpha). In dark mode this may look fine, in light mode it can sit too heavy. Tokenise to `--bg-overlay` or `rgba(0,0,0,0.4)` with a token name. Same issue means the backdrop won't match the rest of the system's modal feel.

**19. Inline-rolled drag-and-drop reorder lacks keyboard support** — `collection_detail.html:282–322`
The owner-only reorder feature uses `pointerdown` / `pointermove` / `pointerup`. Mouse and touch users can reorder; keyboard users (and screen-reader users) cannot — there is no equivalent ↑ / ↓ button per row and no `aria-grabbed` toggling instructions surfaced to AT. The handler does set `aria-grabbed` on the dragged element (line 291, good — although `aria-grabbed` is deprecated in WAI-ARIA 1.1 in favour of `aria-dropeffect` / a roving listbox pattern), but no equivalent keyboard path exists. Add a roving-tabindex / arrow-key alternative or a "Move up" / "Move down" affordance per row.

**20. Dialog inputs have inline-styled labels that lose mobile font-size minimum** — `collections.html:75–87` (title), `93–99` (description), `106–116` (visibility) + `collection_detail.html:79–84` (search input)

The labels themselves declare `font-size: var(--text-xs)` (11 px) — that's intentional for label chrome, fine. But the inputs declare `font-size: var(--text-sm)` (13 px on `title` and `visibility`) and `font-size: var(--text-md)` (16 px on `description`). Hard rule: *"`<input>` / `<select>` / `<textarea>` ≥ 16 px font on mobile (defeats iOS auto-zoom)."* The two affected controls are:
- `collections.html:83` `<input name="title">` at `var(--text-sm)` (13 px).
- `collections.html:108` `<select name="visibility">` at `var(--text-sm)` (13 px).
- `collection_detail.html:81` `<input id="c-add-q">` at `var(--text-sm)` (13 px).

Bump all three to `var(--text-md)` (16 px) or wrap with a mobile `@media (max-width: 720px) { … font-size: 16px; }` override. The textarea at `collections.html:95` correctly uses `--text-md` — model the others on it.

**21. No mobile media query for the dialog / panel widths** — `collections.html:67`, `collection_detail.html:69`
```html
max-width:460px;width:90%   /* dialog */
```
On 360-wide Android the dialog is 324 px — fine. But the dialog padding `var(--space-6)` (32 px) plus 1-px borders eat 66 px, leaving 258 px of usable input width. The textarea wrapping at 3 rows + `min-height: 36px` inputs makes the bottom button row sometimes wrap. Test at 360 × 740; if wrapping happens, reduce padding to `var(--space-4)` on mobile and add `flex-wrap: wrap` on the button row.

### Medium — page-load and resource hygiene

**22. Google Fonts CSS link blocking render** — `collections.html:17` + `collection_detail.html:22`
Already mentioned in #4. To restate: the link is a render-blocking stylesheet from `fonts.googleapis.com`. Even with `display=swap`, the CSS file itself blocks render until network fetch. Source Serif 4 is **already** self-hosted in `tokens.css:65–82` — delete the Google `<link>` and the matching `<link rel="preconnect">` on lines 15–16 / 20–21. Net effect: one fewer render-blocking stylesheet, one fewer DNS resolution, one privacy concern removed.

**23. Anti-FOUC inline script lives inside `<head>` correctly** — `collections.html:18` + `collection_detail.html:23`
Matches the skill rule: *"Anti-FOUC inline script in every page reads it before paint."* Reads `narve-theme` first, falls back to `betyc-theme` legacy cookie. Compliant. No action.

**24. `cmdk.js` / `share_menu.js` loaded but probably unused** — `collections.html:206–207` + `collection_detail.html:330–331`
Same observation as the `gate.html` audit: the bottom-of-body deferred scripts include `cmdk.js` and `share_menu.js`. `collection_detail.html` has a hand-rolled share button (line 264–278); if it migrated to `[data-share]` (recommended) `share_menu.js` would be load-bearing. `cmdk.js` runtime cost is small but the load matters on mobile 3G. Flag for the same perf-sweep this audit family produces.

**25. Two stylesheets reference `Source Serif 4` font weights that the self-hosted variable file may or may not cover** — `collections.html:17`
The Google Fonts URL requests `wght@8..60,400;8..60,500;8..60,600` — weights 400, 500, 600. The self-hosted variable in `tokens.css:73` declares `font-weight: 200 900` — that covers everything. If finding #22 is fixed by deleting the Google link, no regression.

### Medium — JS / handler quality

**26. `location.reload()` after add / delete** — `collection_detail.html:133, 174, 235`
Three reloads after successful API calls. Skill doesn't ban full-page reloads but they're a bad UX on mobile (lose scroll position, repaint everything). Replace with optimistic DOM updates or refetch + replace `#c-items` HTML. Lowest-effort win: keep `location.reload()` for delete-collection (line 175 — page goes away anyway) but use targeted re-render for `add` and `remove-item`.

**27. CSRF token regex parsed once per call (not cached)** — `collections.html:184, 161` + `collection_detail.html:109`
Three independent helpers re-parse `document.cookie` for `_csrf`. Minor perf cost; bigger concern is the inconsistency across the file family — `predictions.html` uses one pattern, this file another. Refactor to a single `narve.csrf` helper exposed on `window`. Out of scope for design audit; flag for a frontend cleanup pass.

**28. `await api('GET', …)` violates the contract `api(method, path, body)` documents** — `collection_detail.html:253`
```js
var data = await api('GET', '/api/collections/search?q=' + encodeURIComponent(q));
```
The `api()` helper at lines 113–122 doesn't list a body for GET. Code path works (body is `undefined`, the `if (body !== undefined)` branch is skipped). Cosmetic — but worth noting the contract should be `api(method, path, body?)` with explicit type / JSDoc.

### Medium — semantics

**29. `<h2 class="feed-section-title">Items</h2>` competes with `<h1>` hero** — `collection_detail.html:61`
Heading order is `h1` then `h2` — correct ordering. But the `.feed-section-title` style at `feeds.css:356–364` makes it 11 px uppercase tertiary — visually it reads as eyebrow, not section heading. Screen-reader users get a heading-level-2 announcement that doesn't match the visual weight. Either downsize semantics to a `<div role="heading" aria-level="2">` (not great) or upsize the visual weight to actually look like a section heading. Prefer the latter.

**30. `aria-live="polite"` on lists that re-render via full reload** — `collections.html:52, 55` + `collection_detail.html:64`
`aria-live="polite"` is meant to announce DOM updates without a page reload. Here, all updates trigger `location.reload()` (see #26), so `aria-live` is announcing nothing the user can't already infer from the page-load. Keep the attribute for future when reloads disappear, but flag that current behaviour doesn't exercise it.

**31. `<dialog>` lacks `aria-modal="true"` and `aria-labelledby` is set but no `aria-describedby`** — `collections.html:65`
`aria-labelledby="c-new-heading"` is set (good). Native `<dialog>` with `showModal()` sets `aria-modal` implicitly per spec — verify the Safari implementation. Adding an explicit `aria-modal="true"` is defensive and harmless.

**32. `contenteditable` inline edit lacks aria-label and visual cue** — `collection_detail.html:41–42`
The hero title and description become contenteditable when `IS_OWNER && !IS_SYSTEM` (the `raw_title_editable` / `raw_desc_editable` interpolations inject `contenteditable="plaintext-only"` attributes on the server). The user only knows they're editable by trying. No edit icon, no hover affordance, no keyboard-discoverable indication. Add `aria-label="Edit title"` / `aria-label="Edit description"` to the editable elements when in owner mode, and surface a hover/focus visual cue (border-bottom on focus, perhaps).

**33. `<dialog>` form lacks ESC-to-close on iOS** — `collections.html:65–124`
`<dialog>` ESC works on Safari desktop but is unreliable on iOS Safari. Verify by hand or, defensively, add an explicit ESC keyboard handler.

### Low — observations & confirmed-correct patterns

**34. `feed-row` and `feed-list` from `feeds.css` are reused properly** — `collections.html:52, 55` + `collection_detail.html:64`
The shared editorial vocabulary (`.feed-shell`, `.feed-list`, `.feed-row`, `.feed-action`, `.feed-action--ghost`, `.feed-chip`, `.feed-eyebrow`, `.feed-title`, `.feed-lede`) is used consistently with `predictions.html`, `saved.html`, and `sources.html`. This is the right model. Compliant. The audit's recommendation is to extend the same model for the dialog / modal / search-row vocabulary (findings #1, #6, #7, #8).

**35. Wordmark / sidebar both injected via middleware (`{{ raw_sidebar }}`)** — `collections.html:25` + `collection_detail.html:30`
Correct — no re-implementation of the hamburger drawer.

**36. Skeletons and toast loaded but skeletons unused** — `collections.html:204, 205` + `collection_detail.html:328, 329`
`skeletons.js` is loaded but never invoked. `narveToast` is invoked. Replace the JS-generated "Type at least 2 characters…" / "Search failed." placeholders with `narveSkel.error(addResults, 'Search failed.', {retry: …})` to match the component table. The toast is correctly used for transient errors. Compliant on toast, incomplete on skeletons.

**37. Theme cookie matches skill spec** — `collections.html:18` + `collection_detail.html:23`
Reads `narve-theme` then `betyc-theme` legacy. Compliant.

**38. No emoji in chrome** — both files
Compliant with hard rule. The `<button>+</button>` "+" at line 222 of `collection_detail.html` is a typographic plus sign (not an emoji), used as an icon. Acceptable; consider replacing with a real icon or `aria-label="Add"` if the screen-reader experience matters here (the surrounding button has the kind/title/subtitle context, so a screen reader gets meaning regardless).

**39. `font-variant-numeric: tabular-nums` on every numeric line** — `collection_detail.html:46` + `feeds.css:83, 141, 207, 244, 267`
Compliant with the density / numeric-alignment rule.

**40. Monochrome confirmed** — both files
No `#ef4444` / `#22c55e` / `#fbbf24` / similar. No `color: red;`. No SVG path fills. Compliant with the hard monochrome rule.

**41. Logo `filter: invert(1)` on dark theme** — handled by the global `gateway.css` and `tokens.css` cascade; both templates correctly defer to it. Compliant.

**42. `narve-sr-only` not used** — both files
Neither template has any visually-hidden labels. The dialog labels are visible (good for sighted users). Verify the search input at `collection_detail.html:79–84` is sufficiently labelled — `<label>` wraps the input (correct nesting). Compliant.

**43. `data-explain` / `data-explain-title` not used on `<h1>`** — both files
Component table: *"Page title with explanation → `<h1 data-explain='…' data-explain-title='…'>`."* Neither hero `<h1>` has these. Both pages would benefit — "What is a Collection?" is exactly the data-explain use case. Flag as opportunity, not a violation.

**44. `breadcrumb` kwarg pattern not used; hand-rolled `feed-back` instead** — `collections.html:30–32` + `collection_detail.html:35–37`
Skill component table: *"Breadcrumb → `breadcrumb=[(label, url), …]` kwarg to `render_page`. Don't hardcode in the template."* Both templates hardcode a `← narve.ai` / `← Collections` back-link instead of using the breadcrumb helper. Flag as inconsistency vs the rest of the gateway pages.

**45. `feed-row` skeleton CSS exists but never used** — `feeds.css:337–353`
The skeleton row CSS in `feeds.css` lines 337–353 has no caller. `skeletons.js` is loaded but the JS calls `narveSkel.show()` with no `{shape: 'feed-row'}` — search the codebase to confirm. If unused, either wire it (the empty list at first load could use `narveSkel.show(document.getElementById('collections-body'), {shape: 'feed-row', count: 4})`) or remove the skeleton CSS.

**46. `prefers-reduced-motion` not honoured** — both files (and `feeds.css`)
No `@media (prefers-reduced-motion: reduce)` block in `feeds.css`. The transitions used (`background var(--duration-fast) cubic-bezier(0.2, 0, 0, 1)`) are within the sanctioned subset (background / opacity / colour transitions are arguably exempt from reduced-motion since they don't move pixels), but the inline `dragging.style.opacity = '0.6'` (drag handler at `collection_detail.html:292`) is technically a motion-adjacent state. Add a reduced-motion override in `feeds.css` for the dialog enter animation (when added per finding #17) and the drag opacity dim.

**47. `transition` shorthand missing `var(--ease)` in places** — `feeds.css:109–111, 169–170, 296–297`
Many transitions specify `cubic-bezier(0.2, 0, 0, 1)` as a literal instead of `var(--ease)`. Functionally identical, but the token is the canonical reference and a future ease-curve change must edit one place not many. Replace 6 literal cubic-beziers in `feeds.css` with `var(--ease)`.

**48. `transition: border-color var(--duration-fast)` etc. uses tokens** — `feeds.css:109–111, 169–170`
Duration token used (good). Easing literal — see #47.

**49. Density token coverage** — `feeds.css:404–405`
The compact density override exists and reduces padding correctly. Compliant.

**50. Mobile breakpoint at 720 px** — `feeds.css:377–401`
Skill anti-pattern note: *"CSS rule gated on `(pointer: coarse)` for sizing — Use `(max-width: 900px)` instead."* This file uses 720 px which is fine for the feed-row breakpoint but flag that the system's general mobile breakpoint is 900 px; intentional difference for the editorial layout, acceptable.

**51. `aria-live="polite"` correct usage** — `collections.html:52, 55` + `collection_detail.html:64`
Used on the `<ul>` lists. Correct ARIA pattern; depends on DOM mutation to fire (see #30).

**52. `<form method="dialog">` on the new-collection dialog** — `collections.html:70`
Form is intercepted with `ev.preventDefault()` so the `method="dialog"` doesn't actually close the dialog on submit — the explicit `dlg.close()` would need to be called on success. Currently the code does `location.href = '/collections/' + data.id;` which navigates away, so the dialog is never explicitly closed (it just disappears with the page). If a failure path is added that should keep the dialog open, ensure success closes it explicitly.

**53. Dialog `font-family: var(--font-ui)` inline declaration** — `collections.html:69`
This sets Inter as the dialog's default font — correct intent. But the heading at line 71–73 overrides to `var(--font-display)` italic Instrument Serif. That pairing matches the rest of the system (chrome on Inter, hero on Instrument Serif). Compliant in intent, non-compliant in implementation (inline style).

**54. Visibility radio-vs-select choice** — `collections.html:106–116`
The `<select>` ships three options (private / shared / public). On mobile, native selects show a wheel picker which is OK. Could be radios for desktop usability; pick a pattern and document. Subjective.

**55. `place="Start typing…"` placeholder** — `collection_detail.html:79`
Skill rule (mobile): inputs ≥ 16 px on mobile (see #20). The placeholder text is fine.

**56. Dialog content is form-heavy on a page meant to surface 6 fields at most** — `collections.html:65–124`
Three fields (title, description, visibility) is reasonable for a creation dialog. Compliant.

---

## Top 3 by severity

1. **33 inline `style="…"` attributes** (finding #1, `collections.html:31,47,66–124` + `collection_detail.html:36,43–46,53–95,201,207–223,248,256`) — hard rule violation, single biggest cleanup task. Includes a fully inline-styled `<dialog>`, an inline-styled add-items panel, and 9 inline styles per search-result row generated in JS. Fix by extending `pages/feeds.css` with `.feed-dialog`, `.feed-field*`, `.feed-search-row*` selectors and rewriting the HTML to use classes.
2. **Native `confirm()` for collection deletion + `window.prompt()` × 2 for share fallback** (findings #2, #3, `collection_detail.html:172, 272, 276`) — hard rule violation. The comment on line 170 admits the shortcut. Replace `confirm()` with `.nv-modal` two-button confirm; replace `prompt()` with toast + selectable input or `[data-share]` component.
3. **Source Serif 4 = 4th typeface, loaded redundantly from Google Fonts CDN** (finding #4, `collections.html:17` + `collection_detail.html:22`) — the templates duplicate the self-hosted variable font over a render-blocking third-party CDN with privacy implications (visitor IP leaks to Google). Drop the Google `<link>` and the two `<link rel="preconnect">` lines in both templates (Source Serif 4 is already self-hosted via `tokens.css:65–82`). Also raises a design-system question: is the skill stale at "three typefaces total", or is the codebase non-compliant?

---

## Suggested fix order (cheapest first)

1. Delete the Google Fonts `<link>` and matching `preconnect` lines in both templates — finding #4, #22, #25. Net: 3 lines removed × 2 files = 6 lines, immediate render-time + privacy win.
2. Replace `confirm()` and both `window.prompt()` calls in `collection_detail.html` with `.nv-modal` + toast — findings #2, #3. Lines 172, 272, 276.
3. Add `aria-pressed` to the bell toggle in `collections.html:177–199` — finding #9.
4. Bump input font-sizes to `var(--text-md)` (16 px) in inline-styled inputs and selects — finding #20. Lines `collections.html:83, 108` + `collection_detail.html:81`.
5. Move the `<dialog>` block in `collections.html:65–124` and the `#c-add-modal` block in `collection_detail.html:69–95` into `pages/feeds.css` under `.feed-dialog`, `.feed-field*`, `.feed-modal*` selectors — finding #1 (the bulk). Migrating to `.nv-modal` does this for free.
6. Replace the inline-styled search-result row builder at `collection_detail.html:199–225` with a class-based pattern — finding #1, #8.
7. Wire `narveSkel.show()` for the initial list load and `narveSkel.error()` for the search-failed state — finding #36, #46.
8. Add `prefers-reduced-motion` override and `var(--ease)` substitutions in `pages/feeds.css` — findings #46, #47.
9. Add keyboard reorder support for the drag-to-reorder feature — finding #19.
10. Consider whether the skill's "three typefaces" rule supersedes the in-repo `--font-body: Source Serif 4` — finding #4 (design-system intent decision; out of audit scope to resolve unilaterally).
