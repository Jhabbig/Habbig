# Design audit — `gateway/static/admin-*.html`

Auditor: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
Date: 2026-05-15.
Scope: every `admin-*.html` template under `gateway/static/`.

Checks per template:
1. Monochrome only (no hex / rgb / colour names in markup).
2. Three typefaces only (Inter / Geist Mono / Instrument Serif). No
   `ui-monospace`, `SFMono-Regular`, `Menlo`, `Helvetica`, `system-ui`,
   `Arial` fallback chains.
3. Tokens (no inline raw px or hex; pads/sizes come from CSS classes).
4. AA contrast (text against bg uses tokenised pairs; no `color:#xxx`).
5. Mobile tap targets (≥ 44 px) and ≥ 16 px form-control font on mobile.
6. No `<style>` blocks (page CSS extracted to `pages/admin-<slug>.css`).
7. No emoji in chrome (titles, buttons, nav, errors, status).
8. Theme cookie pre-paint script present (anti-FOUC for dark mode).
9. Sidebar / breadcrumb / status-bar consistency with the canonical
   `app-shell` pattern used by the majority of admin pages.

Notation: V = violation, OK = pass, N/A = not applicable.

Glyph note: `←` (U+2190) and `→` (U+2192) used in `← Admin` back-links
and prose are typographic chevrons, not emoji. Not counted as
violations.

---

## `gateway/static/admin-churn.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour anywhere in markup. |
| 2. Three typefaces | OK | No font-family overrides. |
| 3. Tokens | OK | All layout via classes; no inline px / hex. |
| 4. AA contrast | OK | Uses `--text-primary` defaults via classes. |
| 5. Mobile tap targets | OK | No interactive form controls; back link only. |
| 6. No `<style>` blocks | OK | Page CSS lives at `pages/admin-churn.css`. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | **V** | Missing `<script>` cookie-read that sets `data-theme`. Dark-mode users will see a light-theme flash. |
| 9. Shell consistency | **V** | Uses bespoke `adm-wrap` / `adm-head` / `adm-nav-back` "back-link" pattern; the modern admin shell is `app-shell` + sidebar + breadcrumb + status-bar (used by 5 of the 9 templates). Two patterns coexist in the surface — pick one. |

**Violations: 2** — anti-FOUC missing, dual shell pattern.

---

## `gateway/static/admin-email-edit.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 188: JS sets `iframe.style.background = '#fff'` — hardcoded white instead of `var(--bg-base)` or omitting (the iframe `srcdoc` controls its own background). Breaks dark theme: the email-preview iframe stays bright white over a dark page. |
| 2. Three typefaces | **V** | Line 50: `<code style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.85em">`. Adds three system fallback faces — narve permits only Geist Mono via `var(--font-mono)` or `font-family:var(--font-mono)`. Should be a `.mono` class or rely on `<code>` defaults from `gateway.css`. |
| 3. Tokens | **V** | Heavy inline px throughout: `style="width:18px;height:18px"` (line 81), `style="font-size:13px"` (line 82), `style="margin-top:10px"` (line 141), `style="padding-top:10px"` (line 79), `style="max-width:1280px"` (line 60), `iframe.style.minHeight = '360px'` (line 186), `style="padding:20px"` in injected error HTML (line 205). All should reference tokens or extracted CSS classes. |
| 4. AA contrast | **V** | Line 189 `iframe.srcdoc = htmlStr || '<em style="opacity:.5">(empty body)</em>'` — `opacity:.5` over the iframe `#fff` background yields ~ 3.5 : 1 grey-on-white in some browsers, dropping below 4.5 : 1 AA. Use `--text-tertiary` instead. |
| 5. Mobile tap targets | **V** | The "Active" checkbox is `width:18px;height:18px` (line 81) — its tap area is ~18 × 18 px, not ≥ 44 × 44 px. Either wrap a 44 px clickable parent label, or rely on the `field-row` clickable area (the `<label>` here is wrapping the input but the label area itself is constrained by content on line 87). |
| 6. No `<style>` blocks | OK | None. Page CSS at `pages/admin-email-edit.css`. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | OK | Line 11. |
| 9. Shell consistency | OK | Uses the canonical `app-shell` + sidebar + breadcrumb + status-bar pattern. |

**Violations: 5**.

---

## `gateway/static/admin-emails.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | **V** | Line 56: `<div style="max-width:1100px">` — raw px, should be a content-width token or class. Same pattern appears across most admin pages. |
| 4. AA contrast | OK | Uses class defaults. |
| 5. Mobile tap targets | OK | Rows render via `raw_template_rows`; assumes server-side row HTML keeps ≥ 44 px hit-area — out of audit scope, but flagged for the server-side template. |
| 6. No `<style>` blocks | OK | None. No `pages/admin-emails.css` exists, but the page's only chrome is the shared `app-shell` + `admin-section` so a page-CSS is not strictly required. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | OK | Line 11. |
| 9. Shell consistency | OK | Canonical `app-shell`. |

**Violations: 1**.

---

## `gateway/static/admin-feedback.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | OK | No inline px / margins / paddings in markup. |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | OK | The rows are server-rendered; one inline anchor `style="text-decoration:none"` only. |
| 6. No `<style>` blocks | OK | Page CSS at `pages/admin-feedback.css`. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | **V** | Missing. Dark-mode users will see a light-theme FOUC. |
| 9. Shell consistency | **V** | Uses `gw-header` + `gw-main` + `adm-wrap` — a *third* admin shell pattern, neither `app-shell` nor `adm-wrap` alone. Three shells across 9 pages. |

**Violations: 2**.

---

## `gateway/static/admin-flag-edit.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | **V** | Line 46: same `font-family:ui-monospace,SFMono-Regular,Menlo,monospace` fallback chain on the `<code>` element in the page title. Three forbidden faces. |
| 3. Tokens | **V** | Inline px on rows: `style="width:18px;height:18px"` (81), `style="font-size:13px"` (82), `style="padding-top:10px"` (79, 90), `style="max-width:820px"` (54), `style="padding-top:4px"` (90). Should be classes / tokens. |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | **V** | "Enabled globally" checkbox is 18 × 18 px (line 81). Same issue as `admin-email-edit.html` — sub-44 px touch target. The wrapping `<label class="field-control">` does extend tap area; but tier checkboxes injected at line 91 via `raw_tier_checkboxes` are out-of-scope here — flagged for the server-side helper to verify. |
| 6. No `<style>` blocks | OK | None. No `pages/admin-flag-edit.css` exists — the inline `style=` here are exactly the symptoms that justify extracting one. |
| 7. No emoji in chrome | OK | The `→` in subtitle prose on lines 48 – 49 is a typographic chevron, not emoji. OK. |
| 8. Theme pre-paint script | OK | Line 9. |
| 9. Shell consistency | OK | Canonical `app-shell`. |

**Violations: 3**.

---

## `gateway/static/admin-flags.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | **V** | Line 52: `<div style="max-width:1100px">`. Raw px. |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | OK | Inputs use canonical `.field-row` styling — `var(--text-base)` resolves to 15 px and `mobile-a11y.css` line 327 / 737 bumps form controls to `max(16px, …)` on mobile. Defeats iOS auto-zoom. |
| 6. No `<style>` blocks | OK | None. No `pages/admin-flags.css` exists; could be added if inline px get worse. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | OK | Line 9. |
| 9. Shell consistency | OK | Canonical `app-shell`. |

**Violations: 1**.

---

## `gateway/static/admin-impersonation-detail.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | **V** | Line 72: `<code style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px">{{ ip_address }}</code>`. Three forbidden faces; should be `var(--font-mono)` (Geist Mono) via a `.mono` class. |
| 3. Tokens | **V** | Inline px: `style="max-width:1100px"` (57), `style="margin-bottom:24px"` (59), `style="margin-bottom:14px"` (66), `style="padding-top:10px"` (71), `style="padding-top:0"` (69), `font-size:13px` (72). |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | OK | No form inputs; back-link nav only. |
| 6. No `<style>` blocks | OK | None. No `pages/admin-impersonation-detail.css` exists; the inline `style=` count suggests one is warranted. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | OK | Line 11. |
| 9. Shell consistency | OK | Canonical `app-shell`. |

**Violations: 2**.

---

## `gateway/static/admin-impersonations.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | **V** | Line 55: `<div style="max-width:1100px">`. Raw px. |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | OK | Row HTML injected from server — assumed ≥ 44 px hit-area; flagged for server-side helper. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | OK | Line 11. |
| 9. Shell consistency | OK | Canonical `app-shell`. |

**Violations: 1**.

---

## `gateway/static/admin-sharing.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No raw colour. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | OK | All layout via classes. |
| 4. AA contrast | OK | Class-driven. |
| 5. Mobile tap targets | OK | Back-link + window-tab nav; tabs render via `raw_window_tabs` — flagged for server-side helper. |
| 6. No `<style>` blocks | OK | Page CSS at `pages/admin-sharing.css`. |
| 7. No emoji in chrome | OK | None. The `← Admin` back-link uses a typographic chevron, not emoji. |
| 8. Theme pre-paint script | **V** | Missing. Dark-mode users will see a light-theme FOUC. |
| 9. Shell consistency | **V** | Uses `adm-wrap` pattern (matches `admin-churn.html`), not the canonical `app-shell` of the other six. Also: duplicate `<meta name="robots" content="noindex, nofollow">` on lines 8 and 13. |

**Violations: 2** (+ 1 minor: duplicate meta-robots).

---

## Summary

| Template | Violations |
|---|---|
| `admin-churn.html` | 2 |
| `admin-email-edit.html` | 5 |
| `admin-emails.html` | 1 |
| `admin-feedback.html` | 2 |
| `admin-flag-edit.html` | 3 |
| `admin-flags.html` | 1 |
| `admin-impersonation-detail.html` | 2 |
| `admin-impersonations.html` | 1 |
| `admin-sharing.html` | 2 |
| **Total** | **19** |

### Top 5 violations to fix first

1. **Forbidden font-family chains on `<code>`** (`admin-email-edit.html`
   line 50, `admin-flag-edit.html` line 46,
   `admin-impersonation-detail.html` line 72). The
   `ui-monospace,SFMono-Regular,Menlo,monospace` chain adds three faces
   beyond the sanctioned trio. Fix: replace with a `.mono` utility class
   driven by `var(--font-mono)` (Geist Mono).

2. **Hardcoded `#fff` iframe background** in `admin-email-edit.html`
   line 188 (`iframe.style.background = '#fff'`). Breaks dark mode for
   the email-preview iframe. Fix: drop the assignment (the email
   `srcdoc` controls its own bg) or use `var(--bg-base)` via CSS class.

3. **Sub-44 px tap targets on checkboxes** (`admin-email-edit.html`
   line 81, `admin-flag-edit.html` line 81). Inputs set to
   `width:18px;height:18px` violate the mobile-tap-target rule. Fix:
   keep the visual 18 px box but expand the wrapping `<label>`
   clickable area to ≥ 44 px (padded `field-row`).

4. **Missing theme pre-paint script** on three templates
   (`admin-churn.html`, `admin-feedback.html`, `admin-sharing.html`).
   Dark-mode users see a light-theme FOUC. Fix: paste the same
   `narve-theme` cookie-reading IIFE the other six templates ship with.

5. **Inline raw px scattered across 7 of 9 templates** — most often
   `style="max-width:NNNNpx"` (1100, 1280, 820), `padding-top:10px`,
   `margin-bottom:24px`, `font-size:13px`. Extract to per-page CSS in
   `pages/admin-<slug>.css` (the convention already used by 4 of 9).
   Pages currently *without* a page CSS that need one:
   `admin-flag-edit.html`, `admin-impersonation-detail.html`,
   `admin-flags.html` (light), `admin-emails.html` (light),
   `admin-impersonations.html` (light).

### Cross-cutting notes (not counted in the 19)

- Three shell patterns coexist across 9 templates: canonical
  `app-shell` + sidebar + breadcrumb + status-bar (6 pages),
  bespoke `adm-wrap` + `adm-nav-back` (2 pages: churn, sharing), and a
  hybrid `gw-header` + `gw-main` + `adm-wrap` (1 page: feedback).
  Consolidating to the canonical shell would remove a class of drift
  bugs and bring the three FOUC-prone pages onto the standard theme
  pre-paint footer.
- `.btn-danger` (used in `admin-email-edit.html` and
  `admin-flag-edit.html` Save bars) is monochrome at the markup layer
  but resolves to `color: var(--red)` in `gateway.css:551`. That is a
  CSS-layer monochrome violation outside this template-only audit; flag
  for a separate sweep of `gateway.css`.
- `admin-sharing.html` declares `<meta name="robots" content="noindex,
  nofollow">` twice (lines 8 and 13). Minor; not counted.
