# Design audit — feed + signals + search templates

Auditor: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
Date: 2026-05-15.

Scope (HTML, all under `gateway/static/`):
- `feedback.html`
- `feedback-detail.html`
- `admin-feedback.html`
- `admin/feedback.html` (admin-shell body fragment)
- `signal-search.html`
- `predictions.html` (canonical "feed" page — uses `pages/feeds.css`)
- `saved.html` (canonical "feed" page — uses `pages/feeds.css`)
- `collections.html` (uses `pages/feeds.css`)
- `collection_detail.html` (uses `pages/feeds.css`)

Scope (CSS, all under `gateway/static/`):
- `pages/feeds.css`
- `pages/feedback.css`
- `pages/feedback-detail.css`
- `pages/admin-feedback.css`
- `pages/signal-search.css`
- `sample-feed.css`

Server renderers in scope (because they emit the feed/signal HTML):
- `gateway/feedback_routes.py` (`_render_status_chip`, `_render_type_pill`,
  `_render_list_row`, admin row renderer, bulk bar)

Checks per file:
1. **Info-density** — feed rows dense (not airy); compact-density support
   via `[data-density="compact"]`.
2. **Numbers right-aligned + monospaced** (Geist Mono, `tabular-nums`).
3. **No-wrap in row cells** (long text truncates with ellipsis or
   `-webkit-line-clamp`; numeric / meta columns get `white-space: nowrap`).
4. **Density toggle wired** — `<html data-density>` set from cookie /
   localStorage on pre-paint script.
5. **Monochrome only** — no hex / rgb colour for categorisation, status,
   or branding. Charts / chips use weight + position + label.
6. **Three typefaces only** — Inter / Geist Mono / Instrument Serif. No
   `ui-monospace`, `system-ui`, `Helvetica`, `monospace` system fallback.
7. **Tokens, no hardcoded px / hex** — pads/sizes from CSS tokens.
8. **No `<style>` blocks** in HTML; page CSS lives in `pages/<slug>.css`.
9. **No `alert()` / `confirm()`** — `narveToast` / `.nv-modal` instead.
10. **No `"Loading…"` text** — `narveSkel.show` / `narveSkel.error`.
11. **Anti-FOUC theme pre-paint** present.

Notation: V = violation, OK = pass, N/A = not applicable.

Glyph note: `←` `→` `·` are typographic chevrons / bullets, not emoji.
`+ Add Topic` button has a literal `+` — counted as decoration, not emoji.

Density-toggle context: a global Comfortable / Compact toggle lives in
`/settings`, wired by `static/density.js`; pages that opt in must set
`data-density` on `<html>` in their anti-FOUC pre-paint script (see
`predictions.html` line 19 for the canonical pattern).

---

## `gateway/static/feedback.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | `.fb-row` (rendered by `feedback_routes.py` line 282) uses `padding:14px 18px` per row — comfortable. Page has no compact alternative; density attribute never set on `<html>` (see check 4). |
| 2. Right-aligned mono numbers | **V** | Upvote count rendered with `font-variant-numeric:tabular-nums` (good) and `text-align:right` (good), but **no `font-family:var(--font-mono)`** — falls through to Inter. Numbers should be Geist Mono per the rule "Numbers are right-aligned + monospaced". `feedback_routes.py` line 294. |
| 3. No-wrap | **V** | Title (line 288) and body preview (line 290–291) of `_render_list_row` truncate at 140 / 160 chars in Python but the resulting `<span>` has no `white-space:nowrap` + `text-overflow:ellipsis` — wraps to two lines on narrow viewports. The upvotes column similarly has no `white-space:nowrap`. |
| 4. Density toggle wired | **V** | Anti-FOUC script (line 13) reads `narve-theme` only — it does NOT set `data-density` from `nv-density` cookie. Contrast with `predictions.html` line 19 which reads both. `[data-density="compact"]` rules in `pages/feeds.css` therefore never apply on this page, even when the user has opted into compact in `/settings`. |
| 5. Monochrome | **V** | `feedback_routes.py` (which renders `{{ raw_rows }}`, the body of this page) introduces five colour-coded statuses (`STATUS_LABELS` lines 64–69): `#f59e0b` (amber, IN PROGRESS), `#10b981` (green, SHIPPED), `#ef4444` (red, DECLINED). Three type-pill backgrounds (`_render_type_pill` lines 261–263) use red / blue / purple `rgba()` washes for `bug` / `feature` / `question`. **Direct violation of "no colour for categorisation, branding, or semantic meaning".** Hierarchy must come from weight, label, position. |
| 6. Three typefaces | **V** | `_render_list_row` line 279: `font-family:ui-monospace,monospace` on the shipped-commit-sha badge. Must be `var(--font-mono)` (Geist Mono). |
| 7. Tokens | **V** | Page CSS (`pages/feedback.css`) hardcodes `padding:40px 24px 80px` (line 8), `padding:6px 12px` (line 15), `padding:10px 20px` (line 22), `border-radius:10px` (line 18), `font-size:28px` (line 10), `font-size:12px` (lines 15, 17). All should be `var(--space-*)`, `var(--radius-*)`, `var(--text-*)`. Bigger issue: row rendering in `feedback_routes.py` injects 11 separate `style="..."` strings per row with hardcoded `padding`, `width`, `gap`, `font-size`, `letter-spacing` — `_render_list_row` is a wall of inline-style rather than the prescribed "extend page CSS" pattern. |
| 8. No `<style>` blocks | OK | None in the HTML. |
| 9. No `alert/confirm` | OK | None. |
| 10. No `Loading…` text | OK | Server-rendered list — no loading state in this template. |
| 11. Anti-FOUC | OK (theme) / **V** (density) | Theme cookie read (line 13); density cookie not read. Counted once under check 4. |

**Violations: 7** — info-density (no compact path), mono-font on numbers,
no-wrap missing, density attr not set, monochrome (5 colour-coded
statuses + 3 colour-coded type pills), 4th typeface (`ui-monospace`),
hardcoded px / hex in both CSS and inline styles.

---

## `gateway/static/feedback-detail.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | N/A | Detail page (single item + comments thread) — density is for list / table surfaces. |
| 2. Right-aligned mono numbers | N/A | No numeric column. |
| 3. No-wrap | N/A | Single-column prose; wrap is fine. |
| 4. Density toggle wired | **V** | Anti-FOUC script (line 13) reads `narve-theme` only — no `nv-density` read. Inherits same gap as `/feedback`. |
| 5. Monochrome | **V** | `feedback_routes.py` line 471–474 (`_render_admin_reply_block`, rendered into `{{ raw_detail }}`) uses `background:rgba(16,185,129,0.08)`, `border:1px solid rgba(16,185,129,0.3)`, `color:#10b981` for the "Team response" callout. Green for "positive / official". Must be monochrome (border-weight, label, or inset background only). |
| 6. Three typefaces | OK in this file | The `feedback_routes.py` `ui-monospace` violation is counted under `/feedback`. |
| 7. Tokens | **V** | Inline `style="margin-top:16px"` on the comment form (line 48). `pages/feedback-detail.css` hardcodes `padding:40px 24px 80px` (line 8), `padding:28px` (line 12), `border-radius:10px` (line 12), `font-size:12px` (lines 9, 17), `margin-bottom:16px / 24px` (lines 9, 12). Replace with `var(--space-*)` / `var(--text-*)` / `var(--radius-*)`. |
| 8. No `<style>` blocks | OK | None. |
| 9. No `alert/confirm` | OK | None. |
| 10. No `Loading…` text | OK | Server-rendered. |
| 11. Anti-FOUC | OK (theme) | See check 4. |

**Violations: 3** — density attr not set, monochrome (green "Team
response" block in admin-reply renderer), hardcoded px in CSS + inline.

---

## `gateway/static/admin-feedback.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | Same as `/feedback` — comfortable padding only. Admin triage especially benefits from compact rows (power-user surface) and there is no opt-in. |
| 2. Right-aligned mono numbers | **V** | Row renderer in `feedback_routes.py` lines 747–777 shows upvote count (line 762) inline in the meta row — not right-aligned, not mono. Status dropdown sits at right but there is no numeric column. The 4-column grid (`grid-template-columns:28px 80px 1fr 160px`) is hardcoded inline rather than a class. |
| 3. No-wrap | **V** | Title (line 758) truncates at 120 chars in Python but no `white-space:nowrap` / `text-overflow:ellipsis` CSS — long titles wrap into the next column. Meta line 761 likewise. Admin-note preview (line 763) freely wraps. |
| 4. Density toggle wired | **V** | Anti-FOUC script absent entirely — this template has no theme pre-paint script at all (look at the `<head>` block; only `<link rel="stylesheet">` + `<link rel="icon">`). Dark-theme users see a light flash; compact-density users always render comfortable. |
| 5. Monochrome | **V** | `feedback_routes.py` line 744 — `pub_color = "#ef4444"` (red) for private items vs `var(--text-muted)` for public. Public/private status encoded by red colour. Status chips (`STATUS_LABELS`) inherit the same colour violations as `/feedback`. |
| 6. Three typefaces | OK in this file | `_render_list_row` violation (line 279) does not fire on admin rows; admin rows use the dedicated renderer block at lines 747–777. |
| 7. Tokens | **V** | Heavy inline `style="..."` on every row (lines 748–776): hardcoded `padding:14px 16px`, `grid-template-columns:28px 80px 1fr 160px`, `gap:14px`, `font-size:13px / 12px / 11px / 10px`, `letter-spacing:0.05em`, `border-radius:4px / 8px`. Bulk-bar similar (lines 794–812). Page CSS at `pages/admin-feedback.css` also hardcodes `padding:40px 24px 80px`, `font-size:26px`, `border-radius:10px`. |
| 8. No `<style>` blocks | OK | None. |
| 9. No `alert/confirm` | OK | None in this HTML; admin row delete path uses native form submit. |
| 10. No `Loading…` text | OK | Server-rendered. |
| 11. Anti-FOUC | **V** | Missing pre-paint cookie-read script entirely (theme **and** density). Dark-mode admins see a white flash. |

**Violations: 7** — no compact path, numbers not right-aligned + mono,
no-wrap missing, density (and theme) attrs not set, monochrome violations
(5 statuses + red private badge), hardcoded px in inline + CSS, anti-FOUC
script absent.

---

## `gateway/static/admin/feedback.html` (admin-shell fragment)

This is a body-only fragment included by `admin_shell.render_admin_page`.
Same `.adm-wrap` / `.af-list` layout as `admin-feedback.html`.

| Check | Result | Notes |
|---|---|---|
| 1–7, 10 | Same as `admin-feedback.html` | Server-rendered rows come from the same `feedback_routes.py` block. Violations 1, 2, 3, 5, 7 inherit. |
| 8 No `<style>` blocks | OK | None. |
| 9 No `alert/confirm` | OK | None. |
| 11 Anti-FOUC | N/A (in fragment) | Theme pre-paint lives in `admin_shell.py`; this fragment is wrapped. |

**Violations: 5** — counted distinct from `admin-feedback.html` because
this is a separate file (info-density, mono-font on numbers, no-wrap,
monochrome, tokens).

---

## `gateway/static/signal-search.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | `.ss-result` cards (`pages/signal-search.css` line 124–131) use `padding:var(--space-6)` (24 px) plus `gap:var(--space-5)` between cards — comfortable. No `[data-density="compact"]` override exists in `signal-search.css`; compare to `feeds.css` lines 404–405 which provide one. |
| 2. Right-aligned mono numbers | **V** | The `Posts` and `Predictions` counts are rendered as `<span class="ss-chip__value">` (signal-search.html lines 206–212; `pages/signal-search.css` line 200–204) — these are `font-family:var(--font-mono)` and `tabular-nums` (good) but laid out inline inside chips, not in a right-aligned numeric column. Per the "Numbers right-aligned" rule for feeds/tables, dense counts should be right-aligned in a fixed column similar to `feed-stats` in `feeds.css`. Source attribution + age (lines 187–195) are Geist Mono + tabular-nums (good) but the meta block is `display:flex; flex-wrap:wrap` (line 156) — wraps freely instead of fixed columns. |
| 3. No-wrap | **V** | `.ss-result__title` (line 141–148) has no `overflow:hidden`, no `text-overflow:ellipsis`, no `white-space:nowrap`. Long topic names wrap onto multiple lines, expanding the row vertically. `.ss-result__meta` (line 151–160) explicitly uses `flex-wrap:wrap` — chips wrap onto separate lines on narrow viewports. |
| 4. Density toggle wired | **V** | Anti-FOUC script (line 13) reads `narve-theme` only — does not set `data-density`. The CSS file has no `[data-density="compact"]` selectors at all, so even setting the attr would have no effect. |
| 5. Monochrome | OK | No hex / rgb categorisation. Confidence levels in the filter rail (High / Medium / Low — lines 67–69) use text labels only. |
| 6. Three typefaces | OK | All `font-family` references resolve to `var(--font-ui)`, `var(--font-mono)`, `var(--font-body)`, `var(--font-display)`. |
| 7. Tokens | **V** | CSS has minor hardcoded values: `background:rgba(0,0,0,0.6)` for modal overlay (line 508) — should be a token or `color-mix` against the token palette; `box-shadow: var(--shadow-lg, 0 8px 24px rgba(0,0,0,0.18))` (line 522) is OK as a fallback chain; `font-size:10.5px` (line 197, 415) is a magic value (should be `var(--text-xs)`); `padding:2px var(--space-2)` (line 420) and `padding:1px var(--space-2)` (line 449) hardcode the small dimension. Acceptable inline `font-size:16px` per the "mobile no-zoom" rule. |
| 8. No `<style>` blocks | OK | All page CSS in `pages/signal-search.css`. |
| 9. No `alert/confirm` | **V** | Line 123: `alert('Name and keywords required')`. Line 130: `alert(data.error)`. Line 137: `confirm('Delete this topic and all its data?')`. Must be `narveToast()` for ephemeral errors and `.nv-modal` confirm for the delete path. |
| 10. No `Loading…` text | **V** | Line 224: `body.textContent = 'Loading...'` for the per-topic AI analysis pane. The page already wires `narveSkel.show` for the topic-list load (line 235) — extend the same pattern to the inner pane. |
| 11. Anti-FOUC | OK (theme) / **V** (density) | Theme cookie read; density cookie not read. Counted under check 4. |

**Violations: 7** — info-density (no compact rules), numbers not in
right-aligned columns, no-wrap on title + meta, density attr not set,
hardcoded values in CSS, `alert/confirm` use, "Loading..." string.

---

## `gateway/static/predictions.html` (canonical feed)

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | OK | `pages/feeds.css` lines 404–405 ship a `[data-density="compact"]` rule (`padding: var(--space-5)`). |
| 2. Right-aligned mono numbers | OK | `.feed-stats` (`feeds.css` lines 252–268) is `align-items:flex-end; text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums`. Confidence percentage renders correctly. |
| 3. No-wrap | **V** (partial) | `.feed-prose` uses `-webkit-line-clamp:2` (good — clamps to 2 lines). `.feed-row-title` has `white-space:nowrap; overflow:hidden; text-overflow:ellipsis` (good — used on collection rows). `.feed-meta > span` is `white-space:nowrap` (good). However `.feed-handle` (line 201–208) and the meta line rendered at HTML line 109 have no wrap protection — long "@you · CORRECT · 5/12/2026" strings wrap. Minor. |
| 4. Density toggle wired | OK | Line 19 pre-paint reads both `narve-theme` and `nv-density`. |
| 5. Monochrome | OK | No hex; statuses (OPEN / CORRECT / MISS) are text + uppercase labels. |
| 6. Three typefaces | OK | `var(--font-mono)`, `var(--font-ui)`, `var(--font-display)`, `var(--font-body)` only. Note: `pages/feeds.css` defines `--font-body` as Source Serif 4 (loaded from Google Fonts at line 18) — Source Serif 4 is a fourth typeface beyond Inter / Geist Mono / Instrument Serif. The skill spec lists only three typefaces; this is a system-wide expansion that affects every `feeds.css` user. **System-level discussion, not per-page violation — flag once at end.** |
| 7. Tokens | OK | `feeds.css` is token-driven. |
| 8. No `<style>` blocks | OK | None. |
| 9. No `alert/confirm` | OK | Error path uses `error-state` div with retry button. |
| 10. No `Loading…` text | OK | Editorial skeleton rows shown via `showSkeleton()`; no string literal "Loading…". |
| 11. Anti-FOUC | OK | Theme + density both read. |

**Violations: 1** — handle / meta line lacks `white-space:nowrap` (minor;
on narrow viewports the meta line wraps).

---

## `gateway/static/saved.html` (canonical feed)

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | OK | Same as predictions — `feeds.css` density rule applies. |
| 2. Right-aligned mono numbers | OK | Credibility / YES % rendered in `.feed-stat-value` (mono, right-aligned). |
| 3. No-wrap | **V** (partial) | Same handle / meta wrap issue as predictions.html. |
| 4. Density toggle wired | OK | Line 19 reads both. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | OK in this file | Source-Serif-4 system note applies. |
| 7. Tokens | OK | All `var(--*)`. |
| 8. No `<style>` blocks | OK | None. |
| 9. No `alert/confirm` | OK | None. |
| 10. No `Loading…` text | OK | `showSkeleton()` placeholder rows. |
| 11. Anti-FOUC | OK | Theme + density. |

**Violations: 1** — handle line lacks `white-space:nowrap` on narrow vw.

---

## `gateway/static/collections.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | OK | Uses `feeds.css`. |
| 2. Right-aligned mono numbers | OK | Inherits `.feed-stats`. |
| 3. No-wrap | OK | Collection title uses `.feed-row-title` which has `white-space:nowrap; overflow:hidden; text-overflow:ellipsis`. |
| 4. Density toggle wired | OK | Line 18 reads both. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | OK | All token-driven. |
| 7. Tokens | **V** | The new-collection `<dialog>` (lines 65–124) is rendered with **huge inline `style="..."` blocks** instead of a class. Border, radius, padding, font-family, font-size, color, background, letter-spacing all inline. Should be `.feeds-new-coll-dialog` (or extend `.nv-modal`). |
| 8. No `<style>` blocks | OK | None — but the inline-style usage in the `<dialog>` is functionally the same violation. |
| 9. No `alert/confirm` | OK | Uses `narveToast`. |
| 10. No `Loading…` text | OK | Server-rendered list. |
| 11. Anti-FOUC | OK | Both. |

**Violations: 1** — inline-style wall in the new-collection dialog.

---

## `gateway/static/collection_detail.html`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | OK | Uses `feeds.css`. |
| 2. Right-aligned mono numbers | OK | Inherits `.feed-stats`. |
| 3. No-wrap | OK | `.feed-row-title` truncates. |
| 4. Density toggle wired | OK | Line 23 reads both. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | OK | Token-driven. |
| 7. Tokens | **V** | Same pattern as collections.html — large inline-style blocks on the visibility-pill row (lines 43–52), action row (lines 53–56), add-modal panel (lines 69–95), search-results rendering inside the JS (lines 200–225). |
| 8. No `<style>` blocks | OK | None. |
| 9. No `alert/confirm` | **V** | Line 172: `if (!confirm('Delete this collection? This cannot be undone.')) return;`. Comment in the same block acknowledges this should be replaced by `.nv-modal`. |
| 10. No `Loading…` text | OK | Server-rendered + `narveSkel`. |
| 11. Anti-FOUC | OK | Both. |

**Violations: 2** — inline-style walls, `confirm()` for destructive action.

---

## `gateway/static/pages/feeds.css` (shared)

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | OK | Lines 404–405 provide a compact override. |
| 2. Numbers mono + right-aligned | OK | `.feed-stats` is the canonical pattern. |
| 3. No-wrap | OK in stats / titles | `.feed-row-title` and `.feed-meta > span` use `white-space:nowrap`. **V** for `.feed-handle` (line 201): no wrap protection — long handle / status / date strings wrap on narrow viewports. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | **V (system-level)** | Line 70 declares `font-family: var(--font-body)` for `.feed-lede`; `--font-body` is wired to **Source Serif 4** (loaded from Google Fonts at the HTML level — see `predictions.html` line 18, `saved.html` line 18, `collections.html` line 17, `collection_detail.html` line 22). The narve spec lists exactly three typefaces: Inter, Geist Mono, Instrument Serif. Source Serif 4 is a fourth. This is a system-wide deviation — fix at the token level (`--font-body` → Inter) or escalate the skill, but do not perpetuate via more pages. |
| 7. Tokens | OK | All `var(--*)`. |
| 11. Anti-FOUC | N/A | CSS file. |

**Violations: 2** — `.feed-handle` wraps; Source Serif 4 is a fourth
typeface used by every feed page.

---

## `gateway/static/pages/signal-search.css`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | No `[data-density="compact"]` rules — page does not respond to the toggle. |
| 2. Numbers mono + right-aligned | **V** (partial) | `.ss-chip__value` is mono + tabular-nums (good) but values are inline in pill chips, not right-aligned in a column. Source meta uses `flex-wrap:wrap` rather than a fixed numeric column. |
| 3. No-wrap | **V** | `.ss-result__title` no wrap protection. `.ss-result__meta` is `flex-wrap:wrap`. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | OK | All `var(--font-*)`. (Source Serif 4 system note from `feeds.css` still applies if `--font-body` is used here — line 49, 165, 250, 256, 437 reference `--font-body`.) |
| 7. Tokens | **V** | `font-size:10.5px` (line 197, 415) is a magic value; `padding:1px var(--space-2)` (line 449), `padding:2px var(--space-2)` (line 420); `rgba(0,0,0,0.6)` modal scrim (line 508). |
| 11. Anti-FOUC | N/A | CSS. |

**Violations: 4** — info-density rules missing, numeric-column layout
missing, no-wrap missing, magic-value `10.5px` + raw `rgba` scrim.

---

## `gateway/static/pages/feedback.css`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | No `[data-density="compact"]` rules. |
| 2. Numbers mono + right-aligned | **V** | No `.fb-row` rules in this CSS — rows entirely styled via inline strings in `feedback_routes.py`. Upvote column not mono. |
| 3. No-wrap | **V** | No row-cell `white-space:nowrap`. |
| 5. Monochrome | OK | No hex (other than via `var(--bg-overlay, rgba(255,255,255,0.03))` fallback at line 19 — acceptable as a fallback chain). |
| 6. Three typefaces | OK | Token-driven. |
| 7. Tokens | **V** | `padding:40px 24px 80px` (line 8); `font-size:28px` (line 10); `padding:6px 12px` and `font-size:12px` (line 15); `padding:10px 20px` (line 22); `border-radius:10px` (line 18). Should be `var(--space-*)`, `var(--text-*)`, `var(--radius-*)`. |
| 8. `transition: all` | **V** | Line 15 — `transition:all 0.12s` is a shotgun rule. Spec wants only opacity / transform / width-height transitions on specific properties. |
| 11. Anti-FOUC | N/A | CSS. |

**Violations: 5** — no density override, no mono on numbers, no wrap
control, hardcoded px / radius, `transition:all`.

---

## `gateway/static/pages/feedback-detail.css`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | N/A | Detail page. |
| 2 / 3 | N/A | Prose, no list. |
| 6. Three typefaces | OK | Token-driven. |
| 7. Tokens | **V** | `padding:40px 24px 80px` (line 8); `font-size:12px` (line 9); `padding:28px` (line 12); `border-radius:10px` (line 12); `padding:10px 12px` (line 14); `font-size:12px` (line 17); `padding:8px 16px` (line 17, 18); `gap:6px` (line 18). |
| 11. Anti-FOUC | N/A | CSS. |

**Violations: 1** — hardcoded px values throughout.

---

## `gateway/static/pages/admin-feedback.css`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | No compact rules. |
| 2. Numbers mono + right-aligned | **V** | No `.af-row` numeric column styles — rows entirely inline-styled in Python. |
| 3. No-wrap | **V** | None. |
| 6. Three typefaces | OK | Token-driven. |
| 7. Tokens | **V** | `padding:40px 24px 80px` (line 8); `font-size:26px` (line 10); `padding:6px 12px` + `font-size:12px` (line 15); `border-radius:10px` (line 18); `padding:6px 12px` + `font-size:12px` (line 21). |
| 8. `transition: all` | **V** | Line 15 — same shotgun rule as `feedback.css`. |
| 11. Anti-FOUC | N/A | CSS. |

**Violations: 5** — same as `feedback.css`.

---

## `gateway/static/sample-feed.css`

| Check | Result | Notes |
|---|---|---|
| 1. Info-density | **V** | No compact rules. |
| 2. Numbers mono + right-aligned | OK (partial) | `.narve-sample-row__meta` uses `var(--font-mono)` and `white-space:nowrap` — good. No right-align rule though. |
| 3. No-wrap | OK | Meta uses `nowrap`. |
| 5. Monochrome | OK | No hex. |
| 6. Three typefaces | **V** | Line 86 — `font-family: var(--font-mono, monospace)` — the fallback chain ends at `monospace`, a system fallback. Same line 91, 100 ("font-family: var(--font-mono, monospace)"). Spec: "the fallback chain ends at Inter [for body]" — and for mono, the chain should end at `var(--font-mono)` only, not at generic `monospace`. The display fallback (line 29 — `var(--font-display, serif)`) similarly ends at generic `serif`. |
| 7. Tokens | **V** | `margin:16px 0` (line 9); `padding:12px 16px` (lines 23, 71); `gap:12px` (line 23, 71); `font-size:15px` (line 33); `font-size:12px` (lines 38, 88, 112); `padding:10px 16px 14px` (line 106); `padding:0 4px` (line 49). |
| 11. Anti-FOUC | N/A | CSS. |

**Violations: 3** — no compact rules, fallback chain ends at `monospace` / `serif` (system fallback faces), hardcoded px values throughout.

---

## `gateway/feedback_routes.py` (server renderer, in audit scope)

This file emits the HTML rendered into `feedback.html`, `feedback-detail.html`,
`admin-feedback.html`, and `admin/feedback.html`. Violations bleed into
all four pages.

| Check | Result | Notes |
|---|---|---|
| 5. Monochrome | **V (×3)** | (a) Lines 65–67: `STATUS_LABELS` colour-codes statuses with `#f59e0b` (amber), `#10b981` (green), `#ef4444` (red). (b) Lines 261–263: `_render_type_pill` colour-codes types with `rgba(239,68,68,0.12)` (red), `rgba(59,130,246,0.12)` (blue), `rgba(168,85,247,0.12)` (purple). (c) Line 423: `bg = "rgba(59,130,246,0.06)"` for admin comments. Lines 471–474: green "Team response" callout (`rgba(16,185,129,...)` + `#10b981`). Line 744: `pub_color = "#ef4444"` for private-flag badge. **All categorical / status colour. Direct violation of the monochrome rule.** |
| 6. Three typefaces | **V** | Line 279: `font-family:ui-monospace,monospace`. |
| 7. Tokens | **V** | Every row + chip + bulk-bar render is one long `style="..."` string with hardcoded `padding`, `font-size`, `letter-spacing`, `border-radius`, `gap`, grid-template-columns. ~25 inline-style blocks across the file. Move to `pages/feedback.css` / `pages/admin-feedback.css` with classes. |

**Violations: 5** (counted separately; do not double-count against the
HTML pages — the HTML page rows above marked these for traceability).

---

## Aggregate

| File | Violations |
|---|---|
| `feedback.html` | 7 |
| `feedback-detail.html` | 3 |
| `admin-feedback.html` | 7 |
| `admin/feedback.html` | 5 |
| `signal-search.html` | 7 |
| `predictions.html` | 1 |
| `saved.html` | 1 |
| `collections.html` | 1 |
| `collection_detail.html` | 2 |
| `pages/feeds.css` | 2 |
| `pages/signal-search.css` | 4 |
| `pages/feedback.css` | 5 |
| `pages/feedback-detail.css` | 1 |
| `pages/admin-feedback.css` | 5 |
| `sample-feed.css` | 3 |
| `feedback_routes.py` | 5 |
| **Total** | **59** |

---

## Top 3

### 1. Monochrome wholesale violated by feedback row renderers (8 sites)

`gateway/feedback_routes.py` encodes status + type + visibility with five
distinct hex / rgba colours: amber `#f59e0b` (IN PROGRESS), green
`#10b981` (SHIPPED + "Team response"), red `#ef4444` (DECLINED + private
badge), blue `rgba(59,130,246,…)` (Feature pill + admin-comment
background), purple `rgba(168,85,247,…)` (Question pill). This renders
into every feedback list view: `/feedback`, `/feedback/{id}`,
`/admin/feedback`, and the `admin/feedback.html` shell. Hierarchy must
come from typography weight, label, position, glyph — not colour. The
glyph + label pair is already in `STATUS_LABELS` (`○ ⚙ ✓ ✕ ↗`); drop the
hex and let the labels stand alone.

### 2. Density toggle dead on every feedback + signal-search page (4 sites)

`feedback.html`, `feedback-detail.html`, `admin-feedback.html`, and
`signal-search.html` all read `narve-theme` in their anti-FOUC pre-paint
script but never read `nv-density`. `admin-feedback.html` has no
pre-paint script at all (theme **or** density). The matching CSS files
(`pages/feedback.css`, `pages/admin-feedback.css`, `pages/signal-search.css`)
also ship zero `[data-density="compact"]` rules, so the global Compact
setting in `/settings` has no effect on these high-traffic surfaces. The
canonical pattern lives at `predictions.html` line 19 (reads both
cookies); copy that block into the four offenders and add compact
overrides to the CSS (mirror `feeds.css` lines 404–405).

### 3. Row rendering is wall-of-inline-style instead of CSS classes (25+ sites)

`feedback_routes.py` `_render_list_row` (line 280–297), the admin row
block (line 747–777), the bulk bar (line 794–812), the admin-reply
callout (line 471–476), and the type pill (line 265–270) each emit one
long `style="..."` blob per element — total ~25 inline-style blocks
covering `padding`, `grid-template-columns`, `font-size`,
`letter-spacing`, `border-radius`, `gap`, and color. The page CSS files
(`pages/feedback.css`, `pages/admin-feedback.css`) are then nearly empty
of row-specific rules — `.fb-row` only has a `:hover` and `:last-child`
selector; `.af-row` only has `:last-child` and one `select` rule. This
is the exact anti-pattern the skill calls out (`❌ Write style="..."
inline in HTML`). Move padding, typography, grid, and radius into
`.fb-row` / `.af-row` / `.fb-type-pill` / `.fb-status-chip` /
`.af-bulk-bar` in the page CSS, then drop the `style="..."` blobs and
let the class chain own layout. This also fixes the density toggle
(once the row uses `--row-pad-y / --row-pad-x` tokens, compact density
just works).
