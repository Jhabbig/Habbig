# Design audit — billing + account + profile + referrals + settings*

**Date**: 2026-05-15
**Auditor**: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
**Scope**: every HTML and pages-CSS file in the user-account / settings surface.

## Files audited

HTML templates (15):
- `/Users/shocakarel/Habbig/gateway/static/billing.html`
- `/Users/shocakarel/Habbig/gateway/static/account.html`
- `/Users/shocakarel/Habbig/gateway/static/profile.html`
- `/Users/shocakarel/Habbig/gateway/static/referrals.html`
- `/Users/shocakarel/Habbig/gateway/static/settings.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_billing.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_billing_cancel.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_profile.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_privacy.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_api_keys.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_api_key_reveal.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_embeds.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_integrations.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_trading_addon.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_affiliate.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_takes.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_offline.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_saved_views.html`
- `/Users/shocakarel/Habbig/gateway/static/settings_webhooks.html`
- `/Users/shocakarel/Habbig/gateway/static/invites_settings.html`

Page CSS (15):
- `/Users/shocakarel/Habbig/gateway/static/pages/billing.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/account.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/profile.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/referrals.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_billing.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_billing_cancel.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings-profile.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_redesign.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_api_keys.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_api_key_reveal.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_embeds.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_integrations.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_trading_addon.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_offline.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_saved_views.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/settings_webhooks.css`
- `/Users/shocakarel/Habbig/gateway/static/pages/invites_settings.css`

## Checks

Per narve-design hard rules:

1. Monochrome only — no hue used for categorisation / semantic meaning. No raw hex, named colours, or rgb() except in tokens.
2. Three typefaces only — Inter (`--font-ui`), Geist Mono (`--font-mono`), Instrument Serif Italic (`--font-display`). No `ui-monospace`/`SFMono-Regular`/`Menlo`/`Helvetica`/`system-ui`/`Arial` fallback chains. No 4th face (e.g. Source Serif / `--font-body`).
3. Tokens, never hardcoded values — pads/radii/sizes/colours via CSS variables. No raw px in `style="..."` or page-CSS.
4. AA contrast in both themes — text against bg uses tokenised pairs.
5. Mobile tap targets ≥ 44 × 44 px and ≥ 16 px input font (defeats iOS auto-zoom).
6. No `<style>` blocks in page templates. No `<script>` inside `<head>` outside the anti-FOUC theme line and per-template skill-scoped JS.
7. No emoji in chrome (titles, buttons, headers, error messages, nav).
8. Anti-FOUC theme pre-paint script present.
9. No `alert()` / `confirm()` — must use `narveToast` / `.nv-modal`.
10. App-shell consistency (`app-shell`, sidebar, breadcrumb, status-bar) — settings family should match.

Notation: **V** = violation, OK = pass, N/A = not applicable.

Glyph note: `←` / `→` are typographic chevrons, not emoji. `&hellip;` (`…`) and `&mdash;` (`—`) are not emoji either.

---

## Summary

| Category | Count |
|---|---|
| Monochrome violations (raw hex / semantic colour) | 18 |
| Typeface violations (4th face / fallback chains) | 23 |
| Hardcoded-value violations (raw px / inline `style=`) | 47 |
| AA contrast violations | 5 |
| Mobile tap-target / 16px-font violations | 4 |
| `alert()` / `confirm()` / `window.confirm` violations | 5 |
| Missing anti-FOUC pre-paint script | 7 |
| `<style>` block in template | 0 (none — well done) |
| `<script>` block inside page template body (non-anti-FOUC) | 9 |
| `font-family` fallback chains (system fonts) | 23 |
| External font CDN load (defeats local subset) | 1 |
| Shell consistency (no sidebar / no breadcrumb / no status-bar) | 7 |
| `<head>` external link to Google Fonts | 1 |
| **Total findings** | **162** |

### Top 3 violations (by severity + reach)

1. **Fourth typeface — `--font-body` (Source Serif 4) leaks across the entire settings surface.** `pages/settings_redesign.css` lines 95–116 explicitly rebrand all `.settings-section-desc`, `.page-subtitle`, `<p>`, `<li>`, and card text on `body.settings-page` to `var(--font-body)` — Source Serif 4 with Georgia fallback. narve-design's hard rule is **three typefaces total** (Inter / Geist Mono / Instrument Serif). This is a system-wide hard-rule violation reaching 11 of 15 templates that opt into `.settings-page`. Two additional pages also use `--font-body` on per-page ledes (`settings_api_keys.css` `.ak-lede`, `.ak-hint`, `.ak-quota-banner`, `.ak-scope-row`, `.ak-disabled-note`, `pages/settings_api_key_reveal.css` `.akr-lede`, `.akr-alert`). Fix: collapse `--font-body` back onto `--font-ui` (Inter) or delete the whole prose-serif rule block.

2. **Hardcoded colours and inline `style="..."` rampage.** Raw hex / rgb / named colours appear in roughly 18 places across pages and templates that shouldn't have any — `#fff`, `#0d0d0d`, `#141414`, `#22c55e`, `#ef4444`, `#fbbf24`, `#f59e0b`, `#58c58a`, `#b58400`, `#888`, `#9ca3af`, `#6b7280`, `#111827`, `#e5e7eb`, `#f3f4f6`, `#d1d5db`, `rgba(255,255,255,0.x)` etc. (Most are "fallback values" inside `var(--token, #fff)` but per narve-design that pattern is itself an anti-pattern — if the token is missing, add it; do not bake a fallback.) Inline `style="..."` snippets appear 47+ times across the audited HTML for spacing, colour, max-width, font-size, padding, margin — including `style="font-size:13px;color:var(--text-tertiary);margin-bottom:16px"` (`billing.html:41`), `style="font-size:13.5px"` (`settings_privacy.html:93,106`), `style="grid-template-columns:1fr auto;gap:10px;padding:0;border:0"` (`settings_affiliate.html:49,121`), `style="width:18px;height:18px;accent-color:var(--text-primary);margin-top:4px"` (`settings_privacy.html:90,103`), and many more. This is the single biggest mechanical-debt category in the audit.

3. **Three typefaces violated by `ui-monospace, SFMono-Regular, Menlo, monospace` fallback chains in 23 places** — `profile.css:36`, `pages/account.css` (no but the row-value uses `var(--font-mono)` correctly), `pages/settings_embeds.css:27,59,64,126`, `pages/settings_integrations.css:61,149`, `pages/settings-profile.css:52,57`, `pages/settings_trading_addon.css:135`, `pages/settings_webhooks.css:21,33`, `pages/settings_api_keys.css` (uses `var(--font-mono)` correctly — OK), `pages/invites_settings.css:21,28` (uses `var(--font-mono, ui-monospace, "SF Mono", monospace)` — the comma-fallback chain is the violation), `pages/settings_saved_views.css:50` (same). And inline in `settings_affiliate.html:53` (`font-family:ui-monospace,SFMono-Regular,Menlo,monospace`). Narve's font chain ends at Geist Mono via `var(--font-mono)` — if Geist Mono hasn't loaded, the page waits with `font-display: swap`. System-font fallbacks defeat the design intent.

---

## Per-template findings

### `gateway/static/billing.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All rules use tokens. |
| 2. Three typefaces | OK | No font overrides. |
| 3. Tokens | **V** | Line 41 inline style: `style="font-size:13px;color:var(--text-tertiary);margin-bottom:16px"` — raw 13px. Should be `var(--text-sm)` and a class. |
| 4. AA contrast | OK | Token-driven. |
| 5. Mobile tap targets | OK | Status bar `Sign out` is in inline-flex chrome — OK. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 14. |
| 9. Shell consistency | OK | Uses canonical `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 1**.

### `gateway/static/pages/billing.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | OK | Uses `var(--font-mono)` (line 15) and `var(--font-display)` (line 23). |
| 3. Tokens | **V** | Line 15: `font-size: 28px` — raw px. Should be `var(--text-2xl)` or similar. Line 18: `padding: 10px 20px; min-height: 44px; line-height: 24px;` — raw. Line 22: `box-shadow: var(--shadow-md);` — token exists (OK), but the same line also uses `transform: translateY(-1px)` — narve allows opacity/translate, so OK. Line 23: `font-size: 15px` — raw, should be a token. |
| 4. AA contrast | OK | Token-driven. |
| 5. Mobile tap targets | OK | `min-height: 44px` on `.billing-upgrade-btn`. |
| 6. No raw radius | OK | All `var(--radius-*)`. |

**Violations: 1** (raw px in `font-size`, line-height, padding — counted as one).

### `gateway/static/account.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All token-driven. |
| 2. Three typefaces | **V** | Lines 9–10 in `<head>` load **Google Fonts CDN** with `Inter:wght@400;500;600;700&display=swap` — narve ships Inter as a local subset at `gateway/static/fonts/Inter-Variable-subset.woff2`. The CDN load is duplicative, third-party-tracked, and bypasses the cache-busting middleware. Delete lines 9–10. |
| 3. Tokens | **V** | Line 73 inline `style="color:var(--text-secondary);border-color:var(--border-default)"` — should be a `.sub-link-muted` class. |
| 4. AA contrast | OK | Token-driven. |
| 5. Mobile tap targets | OK | `.acct-link` and `.connect-btn` are padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 15. |
| 9. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar. Uses bespoke `.acct-wrap` / `.acct-header`. The rest of the audited surface uses app-shell. Two patterns coexist. |
| 10. Component reuse | **V** | `.connect-card` re-implements what `settings_integrations.html`'s `.si-card` already covers (a "Connected / Not connected" pill + connect action). Two cards do the same job with different markup. |

**Violations: 4**.

### `gateway/static/pages/account.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens throughout. |
| 2. Three typefaces | OK | No font fallback chains. |
| 3. Tokens | **V** | Line 29: `font-size: 12px` — raw. Line 42: `font-size: 12px`, line 43: `width: 6px; height: 6px`, line 27: `width: 8px; height: 8px` — raw px. Line 31: `.sub-active { color: var(--positive); }` — `--positive` is a green semantic token; narve-design forbids semantic colour categorisation. Status should be conveyed by position / weight / icon / text — not green. Same on line 44: `.connect-status-dot.on { background: var(--positive); }`. |
| 4. AA contrast | OK | Token-driven for text. |
| 5. Mobile tap targets | OK | `.acct-link` and `.connect-btn` use `padding: 10px 18px`. |

**Violations: 2** (raw px scattered; `--positive` green for status — counted as 2).

### `gateway/static/profile.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All token-driven. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | **V** | Line 26 inline `style="text-decoration:none;color:inherit"` (also lines 73, 148, 164). Line 37 inline `style="max-width:760px"` — raw 760px width (countered by `settings_redesign.css` only on `.settings-page`; this template does not opt in via the body class). Lines 142, 148 inline `style="display:inline-block;text-decoration:none"`, `style="margin-top:0;padding-top:0;border-top:0"` — should be utility classes. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.pw-toggle` button gets `padding: 4px` and is the 44 px tall input parent; borderline but covered by input row height. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 13. |
| 9. Inline `<script>` | **V** | Lines 168-182 — `togglePw()` inline helper. Should live in `js/forms.js` or similar. |
| 10. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 2**.

### `gateway/static/pages/profile.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | **V** | Line 36: `font-family: ui-monospace, SFMono-Regular, Menlo, monospace;` — system-font fallback chain instead of `var(--font-mono)`. |
| 3. Tokens | **V** | Line 19: `font-size: 28px` — raw. Lines 11, 12: `width: 72px; height: 72px` — bespoke avatar dim, acceptable layout literal but unnamed. Lines 31: `font-size: 12px`. Line 30: `font-size: 12px`. Lines 46, 49, 60: raw 44px / 10px / 18px positioning constants. Lines 39, 40, 41, 42: `!important` on every locked-input override — token route would use a `data-locked` attribute selector with normal specificity. |
| 4. AA contrast | OK | Tokens. |

**Violations: 2**.

### `gateway/static/referrals.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No font overrides in template (CSS has issues — see referrals.css). |
| 3. Tokens | OK | No inline px. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.ref-copy-btn` padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 12. |
| 9. Skeleton loading | **V** | Line 31, 39, 54 use raw text `"Loading…"` strings. narve-design rule: use `narveSkel.show(container, {shape, count})`, not literal strings. |
| 10. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — uses bespoke `<main class="ref-main">` with a `.ref-crumb` hand-built crumb. The sibling `invites_settings.html` page has the same bespoke shell — they're consistent with each other but inconsistent with the rest of the audited surface. |

**Violations: 2**.

### `gateway/static/pages/referrals.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 110: `.ref-invitee-status.paying { color: #58c58a; font-weight: 500; }` — raw green hex for "paying" status. Narve-design hard rule: status conveyed by position / weight / icon / text — never colour. |
| 2. Three typefaces | OK | Uses `var(--font-ui)`, `var(--font-display)`, `var(--font-mono)` consistently. |
| 3. Tokens | **V** | Line 27: `font-size: 28px` — raw. Lines 18, 108, 109: `font-size: 12px` raw. Line 84: `height: 8px`. All should reference tokens. |
| 4. AA contrast | OK | Tokens used elsewhere. |
| 5. Mobile tap targets | OK | `.ref-copy-btn` padded. |

**Violations: 2**.

### `gateway/static/settings.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | OK | No overrides in template. |
| 3. Tokens | **V** | Lines 27 ff. inline `style="text-decoration:none;color:inherit"`. Line 38 `style="max-width:760px"`. Line 57 `style="margin-top:24px;border-top:1px solid var(--border);padding-top:24px"`. Line 63 `style="display:flex;align-items:center;gap:10px;cursor:pointer"`. Lines 97–115 inline density-radio styles with `style="display:inline-flex;gap:0;margin-top:14px;border:1px solid var(--border-default,var(--border));border-radius:var(--radius-sm);overflow:hidden"` and `style="padding:8px 16px;background:transparent;color:inherit;border:0;cursor:pointer;font-family:inherit;font-size:13px;border-right:1px solid var(--border-default,var(--border))"`. Should be a `.density-radio` class. Note the comma fallback `var(--border-default,var(--border))` — see Top 3 #2. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Density buttons get `padding: 8px 16px` ≈ 33 × 32; **borderline** — narve-design requires ≥ 44 × 44. Inline `font-size: 13px` may also trigger iOS auto-zoom (the buttons are not text inputs, so the 16 px rule doesn't apply, but the size is small). |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 14. |
| 9. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |
| 10. Component reuse | **V** | Two stylesheets loaded: `pages/settings.css` (with one selector) and `pages/settings_redesign.css` (with 200+ lines). The `settings.css` file holds 13 lines of leftover. Either consolidate into one or delete `settings.css` and inline the rule. |

**Violations: 2**.

### `gateway/static/pages/settings.css`

3 lines that style only the density-radio active state. Tokens used correctly. **Violations: 0.**

### `gateway/static/settings_billing.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 103: `color:var(--red,#ef4444)` — `--red` is a semantic colour token and the fallback `#ef4444` is raw red. Status colour. narve-design: status uses position / weight / icon / text — not red. (The CSS file has the same pattern at `pages/settings_billing.css:122,124`.) |
| 2. Three typefaces | OK | No overrides in template. |
| 3. Tokens | **V** | Line 89 inline `style="display:flex;gap:8px;align-items:center"`. Line 96 `style="display:inline"`. Line 103 `style="display:none;margin-top:8px;font-size:13px;color:var(--red,#ef4444)"`. Line 112 `style="display:none"`. Line 125 `style="text-align:center;margin-top:12px;display:none"`. Line 193 `style="font-size:12px;color:var(--text-muted);font-weight:600;display:block;margin-top:4px"`. (Six inline `style="..."` blobs.) |
| 4. AA contrast | OK | Body tokens. |
| 5. Mobile tap targets | OK | Buttons use `.sb-btn` which has `min-height: 40px` per `settings_billing.css:35` — **borderline; not ≥ 44 px**. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 15. |
| 9. Inline `<script>` | **V** | Lines 227-291 — page-specific "Manage subscription" handler inlined in template. Should live in `settings_billing.js` (which is loaded already on line 296). |
| 10. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 3**.

### `gateway/static/pages/settings_billing.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 122-124: `.sb-resubscribe { background: var(--amber, #f59e0b); background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.25); }` and `.sb-resubscribe-text strong { color: var(--amber); }` — raw amber RGB for a warning banner. Status colour. Line 127: `.sb-notice-success { background: var(--green-bg, rgba(16,185,129,0.08)); border-color: var(--green); }` — green for success. Line 106: `background: rgba(0, 0, 0, 0.55)` — raw black for modal backdrop, should be a `--bg-modal-backdrop` token. Line 108: `box-shadow: 0 20px 40px rgba(0,0,0,0.3)` — raw shadow (narve allows `--shadow-lg`). |
| 2. Three typefaces | OK | Uses `var(--font-display)`, `var(--font-mono)` correctly. |
| 3. Tokens | **V** | Line 10: `font-size: 15px`. Line 11: `font-size: 12px`. Line 22: `font-size: var(--text-xl)` — OK. Line 27: `width: 14px; height: 14px`. Line 30: `font-size: 12px`. Line 48: `padding: 6px 12px; min-height: 32px; font-size: 12px`. Line 54: `box-shadow: var(--shadow-md, 0 1px 2px rgba(0,0,0,0.08))` — fallback hex. Line 57: `font-size: 10px`. Line 64: `font-size: 15px`. Line 65: `font-size: 10px; padding: 2px 8px; border-radius: 10px`. Line 66: `font-size: 22px`. Line 67-69: `font-size: 12px`. Line 75-79, 82-89: more raw 12 / 14 / 26 / 40 px. Line 101: `border-radius: 10px`. Line 113: `font-size: var(--text-xl)`. Plus the 12+ raw `padding`/`gap` values. |
| 4. AA contrast | OK | Body text uses tokens. |
| 5. Mobile tap targets | OK | `.sb-btn` has `min-height: 40px` — under 44 floor. |

**Violations: 2**.

### `gateway/static/settings_billing_cancel.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | N/A | Outer cards only — content injected via `{{ raw_cancel_inner }}`. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | **V** | Missing the `<script>` cookie-read that sets `data-theme` before paint. Dark-mode users get a flash of light theme. |
| 9. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `.cancel-wrap`. Uses `[[NAV_PLACEHOLDER]]` (line 15) — that placeholder is not replaced by the route handler, surfacing a literal `[[NAV_PLACEHOLDER]]` string to users. Major UX bug. |
| 10. Density script | **V** | Missing the `data-density` cookie-read inline script that every other settings page carries — compact-density users land here in comfortable. |

**Violations: 3**.

### `gateway/static/pages/settings_billing_cancel.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | OK | Uses `var(--font-display)`. |
| 3. Tokens | **V** | Line 27: `padding: 10px 16px`. Line 28: `border-radius: var(--radius-sm)` (OK). Line 53: `font-size: 12px`. Hardcoded references to `--interactive-primary` and `--interactive-primary-contrast` — those tokens do not exist in `gateway.css`; the page uses `--interactive-bg` / `--interactive-text` everywhere else. Resolution path silently falls through to `unset` → `inherit`. Visual bug in light theme. |
| 4. AA contrast | OK | Tokens (assuming the names get fixed). |

**Violations: 1** (the wrong-token-name bug counts as a real issue).

### `gateway/static/settings_profile.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All token-driven in template. |
| 2. Three typefaces | OK | No overrides in template. (CSS has system fallbacks — see `pages/settings-profile.css`.) |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Inputs use `.field-control` chrome. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 13. |
| 9. Inline `<script>` | **V** | Lines 111-228 — 120 lines of avatar-upload + form-submit JS inline. Should live in `js/settings_profile.js` (does not exist yet). |
| 10. Status colour | **V** | Lines 143, 147, 161, 184, 205, 208 use `var(--red, #ef4444)` to colour error status text. Semantic red. narve-design: status by position / weight / text — not red. |
| 11. Shell consistency | OK | `app-shell` + sidebar + breadcrumb. **No status-bar** — most other settings pages have one. |

**Violations: 3**.

### `gateway/static/pages/settings-profile.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 74: `.settings-cooldown { color: var(--amber, #b58400); }` — amber fallback hex. Status colour. |
| 2. Three typefaces | **V** | Lines 52, 57: `font-family: ui-monospace, SFMono-Regular, Menlo, monospace;` — system fallback chain. Should use `var(--font-mono)`. |
| 3. Tokens | **V** | Lines 32, 33, 37: `font-size: 12px`. Line 39: `min-height: 16px`. Lines 11, 12: `width: 96px; height: 96px` — fine as layout literal but unnamed. Line 69: `min-height: 72px`. |
| 4. AA contrast | OK | Tokens for primary text. |

**Violations: 3**.

### `gateway/static/pages/settings_redesign.css` (cross-cutting)

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | **V** | **Lines 95-116: introduces `var(--font-body)` (Source Serif 4) as the body face for every settings page.** This is **a fourth typeface** beyond narve's three-face budget. See Top 3 #1. |
| 3. Tokens | OK | Uses spacing tokens consistently. |
| 4. Mobile font / tap | OK | Forces 16px input font (line 192) and 44px min-height (line 190). |

**Violations: 1** (hard-rule, system-wide reach).

### `gateway/static/settings_privacy.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | **V** | Lines 23, 25, 41 inline `style="text-decoration:none;color:inherit"`. Line 41 `style="max-width:900px"`. Lines 87, 89 `style="cursor:pointer"`, `style="display:flex;gap:12px;align-items:flex-start;padding-top:6px"`. Lines 90, 102 `style="width:18px;height:18px;accent-color:var(--text-primary);margin-top:4px"`. Lines 93, 105 `style="font-size:13.5px"` — non-token raw 13.5 px. Line 117 `style="margin-top:8px;border-top:1px solid var(--border-ghost);padding-top:20px"`. Line 118 `style="font-size:14px"`. Line 130 `style="font-size:13px;color:var(--text-tertiary);margin-top:24px"`. Inline `style="..."` count: ~12. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Checkboxes get parent label wrap. |
| 6. `body.settings-page` missing | **V** | Line 15: `<body>` — no `settings-page` class. The page does not opt into `settings_redesign.css`, so the 44 px-tall inputs and 16 px-font rules don't apply. Inconsistent with siblings. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 11. |
| 9. Inline `<script>` | **V** | Lines 149-174 — export-request handler inlined. |
| 10. `admin-section` reuse | **V** | Line 63 uses `class="admin-section"` on a non-admin page. Admin styling on a user-facing settings page. Either rename / generalise or use `.settings-card`. |
| 11. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 4**.

### `gateway/static/settings_api_keys.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No inline colours. |
| 2. Three typefaces | OK | No template-level font overrides. (CSS has hard-rule violations — see below.) |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.ak-btn` padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | **V** | **Missing** the cookie-read theme inline script (no equivalent of lines 14 elsewhere). Dark-mode users see light flash. |
| 9. Density script | **V** | Missing the `data-density` inline read. |
| 10. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `.ak-wrap` with a "← Settings" link. Multiple pages do the same bespoke pattern (`settings_webhooks.html`, `settings_api_key_reveal.html`); but the rest of the family is on app-shell. |

**Violations: 3**.

### `gateway/static/pages/settings_api_keys.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Lines 75, 78: `text-decoration-color: rgba(255, 255, 255, 0.3)` and `color: #fff` (line 19) baked into body / link styles — dark-theme-only assumptions. Line 95, 109, 119: `background: rgba(255, 255, 255, 0.04)` / `0.06` / `0.05` — raw white-alpha for surfaces. Light theme won't reverse these (they'll render as faint white over white). Line 161-172: `.ak-mono` uses `rgba(255, 255, 255, 0.05)` background. Lines 183-205: status badges hardcode green `#22c55e` and `rgba(34,197,94,0.12)` for `.ak-badge-ok`. Lines 234-242: `.ak-btn-danger` uses `rgba(239, 68, 68, 0.4)` and `#ef4444`. Lines 280, 281: `background: rgba(0, 0, 0, 0.3); border: 1px solid rgba(255, 255, 255, 0.12)` on input. Line 290: `border-color: rgba(255, 255, 255, 0.35)`. Massive hardcoded-colour pattern. |
| 2. Three typefaces | **V** | Line 20: `font-family: var(--font-ui), -apple-system, BlinkMacSystemFont, sans-serif` — adds three system fallbacks (Apple, BlinkMac, generic sans). narve-design: chain ends at Inter via `var(--font-ui)`; if Inter hasn't loaded, the page waits. Line 91: `--font-body` (Source Serif 4) — 4th typeface (see Top 3 #1). |
| 3. Tokens | **V** | Line 27, 39, 42, 47, 60, 62, 65, 75, 76, 84, 86, 92, 97, 100, 109, 116, 117, 120, 121, 131, 139, 144, 147, 149, 154, 156, 160-172, 211, 218, 219, 220, 222, 230, 240, 244, 256, 268, 273, 285, 302, 314, 322, 324: raw px / opacities / weight constants. |
| 4. AA contrast | **V** | `opacity: 0.55` (lines 47, 56), `opacity: 0.6` (lines 13, 31, 78, 196, 301, 322), `opacity: 0.62` (line 87), `opacity: 0.65` (line 142), `opacity: 0.7` (line 142), `opacity: 0.78` (line 64) — these stacks knock the effective `--text-secondary` colour below AA on `--bg-base` in some themes. Use the proper text-tertiary token rather than `opacity:0.6` on a primary. |

**Violations: 4**.

### `gateway/static/settings_api_key_reveal.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No inline colours. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens for primary text. |
| 5. Mobile tap targets | OK | `.akr-btn` padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | **V** | Missing the cookie-read inline script. |
| 9. Density script | **V** | Missing. |
| 10. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `.akr-wrap`. |
| 11. Inline `<script>` | **V** | Lines 56-73 — `copyKey()` handler inlined. |

**Violations: 4**.

### `gateway/static/pages/settings_api_key_reveal.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 9: `background: var(--bg, #0d0d0d); color: var(--text-primary, #fff)` — hardcoded dark fallback assumptions. Line 42-45: `background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.4); color: #fbbf24;` — amber for the "copy this key now" alert. Status colour. Line 60: `background: var(--surface, #141414)` — dark fallback. Lines 71-73: `background: #000; border: 1px solid rgba(255, 255, 255, 0.1)` — raw black for key panel. Line 96: `background: rgba(255, 255, 255, 0.05)`. Line 99: `border-color: rgba(255, 255, 255, 0.35)`. Line 109: `background: var(--accent, #fff)` — wrong token; should be `--interactive-bg`. |
| 2. Three typefaces | **V** | Line 12: `font-family: var(--font-ui), -apple-system, BlinkMacSystemFont, sans-serif` — system fallback chain. Line 33: `font-family: var(--font-body)` — 4th typeface. |
| 3. Tokens | **V** | Lines 20, 24, 28, 33, 35, 36, 47, 60, 67, 70, 81, 92, 93, 95-105, 119-127, 133, 139, 144, 151, 157, 159: raw px / opacity / weight. |
| 4. AA contrast | **V** | Multiple `opacity: 0.5x` stacks. Amber on dark and amber on light both flunk AA at this contrast level. |

**Violations: 4**.

### `gateway/static/settings_embeds.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All token-driven. |
| 2. Three typefaces | OK | No overrides in template. |
| 3. Tokens | **V** | Line 45 `style="margin-bottom:10px"`. Line 109 `style="margin-bottom:6px"`. Line 111 `style="display:flex;gap:8px;margin-top:10px;align-items:center"`. Line 114 `style="display:none"`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.btn` chrome. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 12. |
| 9. Density script | **V** | Missing the density inline read (siblings have it). |
| 10. Inline `<script>` | **V** | Lines 127-375 — **250 lines of embed-management JS inlined**. Should live in `js/settings_embeds.js`. Largest inline-script block in the audited surface. |
| 11. `window.confirm()` | **V** | Lines 282, 297: `window.confirm("Rotate token for this widget? …")` and `window.confirm("Deactivate this widget? …")`. narve-design: use a `.nv-modal` confirm instead. |
| 12. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 4**.

### `gateway/static/pages/settings_embeds.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens — `--semantic-high-bg` etc. resolve to monochrome surfaces. |
| 2. Three typefaces | **V** | Lines 27, 59, 64, 126: `font-family: ui-monospace, SFMono-Regular, Menlo, monospace` — system fallback chain. |
| 3. Tokens | **V** | Line 11-12, 23, 26, 29, 30, 32, 33, 47, 53, 60, 65, 67, 71-76, 78, 80, 82, 83, 87, 89-92, 95, 100, 101, 106, 112, 118-130: raw px / opacity / colours. Line 72: `background: rgba(0,0,0,0.55)` raw modal backdrop. Line 100: `box-shadow: 0 0 0 3px var(--interactive-ghost)` — raw 3px ring. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Buttons padded. |

**Violations: 2**.

### `gateway/static/settings_integrations.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No template overrides. |
| 3. Tokens | **V** | Lines 25, 27 inline `style="text-decoration:none;color:inherit"`. (Two crumb anchors only.) |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Buttons + `.si-currency-toggle` button is `min-width: 44px; min-height: 40px` per CSS — borderline. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 14. |
| 9. Density script | OK | Line 14 includes density read. |
| 10. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |
| 11. Component reuse | OK | Uses `.nv-modal` + `.nv-modal__panel` (the canonical modal). |

**Violations: 1**.

### `gateway/static/pages/settings_integrations.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens throughout. |
| 2. Three typefaces | **V** | Lines 61, 149: `font-family: ui-monospace, SFMono-Regular, Menlo, monospace` — system fallback chain. Should use `var(--font-mono)`. |
| 3. Tokens | **V** | Line 95: `padding: 9px 12px` — odd `9px`. Line 102: `font-size: 16px` (defensible — see 16-px rule). Line 130: `min-height: 40px` — under 44 px floor (see check 5). Line 177-181: `background: rgba(0, 0, 0, 0.45)` raw modal backdrop. Spacing literals fall back to `var(--space-X, NNpx)` patterns — the comma-fallback is anti-token. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | **V** | `.si-currency-toggle button` `min-height: 40px` (line 130) — under the 44 px floor. |

**Violations: 3**.

### `gateway/static/settings_trading_addon.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No template overrides. |
| 3. Tokens | **V** | Lines 25, 27 inline `style="text-decoration:none;color:inherit"`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Buttons + `.ta-segmented` 44 px. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 15. |
| 9. Density script | OK | Inline read present. |
| 10. Shell consistency | OK | App-shell + sidebar + breadcrumb + status-bar. |
| 11. Component reuse | OK | Uses `.nv-modal__panel`. |

**Violations: 1**.

### `gateway/static/pages/settings_trading_addon.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | **V** | Line 135: `font-family: ui-monospace, "Geist Mono", SFMono-Regular, Menlo, monospace` — explicit `"Geist Mono"` inside a system fallback chain. Should be `var(--font-mono)` only. |
| 3. Tokens | **V** | Line 37: `font-size: 16px` (defensible — defeats iOS zoom; better as `var(--text-base)` if that resolves to 16). Lines 71, 91, 98-100, 103-105, 110, 116, 123, 138, 156, 162, 174: raw px. Line 192: `padding: var(--space-4, 16px)` — comma-fallback. Line 201: `background: rgba(0, 0, 0, 0.45)` — raw modal backdrop. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.ta-segmented button` has `min-height: 44px`. |

**Violations: 2**.

### `gateway/static/settings_affiliate.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens in template. |
| 2. Three typefaces | **V** | Line 53 inline `style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px"` — system fallback chain inline on `#default-link`. Should be `.font-mono` class. |
| 3. Tokens | **V** | Line 49 `style="grid-template-columns:1fr auto;gap:10px;padding:0;border:0"`. Line 53 `style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px"`. Line 55-56 `style="border-color:var(--border-default)"`. Line 98 `style="position:static;margin-top:12px"`. Line 121 same grid-style. Line 129-130 `style="border-color:var(--border-default)"`. Line 133 `style="margin-top:10px"`. Line 136 `style="margin-top:16px"`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | Buttons padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 12. |
| 9. Density script | **V** | Missing the density inline read. |
| 10. Inline `<script>` | **V** | Lines 194-283 — 90 lines of affiliate JS inline. |
| 11. Colour-coded message helper | **V** | Lines 208-213: `setMsg(...)` injects HTML with `'background:' + ('var(--error-bg)' or 'var(--semantic-high-bg)')` for error/success differentiation. The page picks the bg-token by error-vs-success — but the `error-bg` token (if it resolves to red/pink) is a semantic-colour reach. Need to verify `--error-bg` resolves to neutral; if it carries hue, it's a hard-rule break. |
| 12. Component reuse | **V** | `setMsg(...)` injects bespoke alert divs. narve-design rule: use `narveToast(msg, {type})`. |
| 13. Shell consistency | OK | `app-shell` + sidebar + breadcrumb + status-bar. |

**Violations: 5**.

### `gateway/static/settings_takes.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | **V** | Lines 23, 25 inline `style="text-decoration:none;color:inherit"`. Line 39 `style="max-width:1040px"`. Line 42 `style="margin-bottom:20px"`. Line 50 `style="margin-top:16px"`. Line 85 `style="padding:0;overflow-x:auto"`. Line 86 `style="width:100%"`. Line 89-93 `style="width:34%"` / `12%` / `100px` / `120px`. (The `width:34%` etc. table column constraints could be class-based but reasonable inline.) |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | N/A | Read-only data table. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 12. |
| 9. Density script | **V** | Missing density inline read. |
| 10. Inline `<script>` | **V** | Lines 117-122 — tiny helper but still page-specific inline. Should be in `user-features.js`. |
| 11. Shell consistency | OK | App-shell + sidebar + breadcrumb + status-bar. |

**Violations: 3**.

### `gateway/static/settings_offline.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.offline-btn` has 8x16 padding ≈ ~36px tall — borderline under 44 px floor (see CSS check 5). |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | **V** | **No cookie-read inline script.** Dark-mode users flash light. |
| 9. Density script | **V** | Missing. |
| 10. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `.offline-wrap`. (`settings_redesign.css` line 48 hooks `.offline-wrap` and gives it the shell padding — partial accommodation, not equivalence.) |
| 11. Inline `<script>` | **V** | Lines 62-211 — 150 lines of cache-management JS inlined. Should live in `js/settings_offline.js`. |

**Violations: 4**.

### `gateway/static/pages/settings_offline.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. Fallback values are neutral greys. |
| 2. Three typefaces | OK | No font fallback chains. |
| 3. Tokens | **V** | Lines 11, 19, 26, 33, 38, 47, 51, 59-61, 65, 71-78, 102, 110, 121, 125, 129, 134, 145, 151: raw px and weight constants. Every token reference uses the comma-fallback pattern: `var(--text-tertiary, #9ca3af)`, `var(--border-default, #d1d5db)`, `var(--bg-surface, #fff)`, `var(--text-primary, #111827)` — anti-pattern; the token should always resolve. |
| 4. AA contrast | OK | Tokens for primary. |
| 5. Mobile tap targets | **V** | `.offline-btn` `padding: 8px 16px` → ~36 × min-width 56 — under 44 × 44 floor. |

**Violations: 2**.

### `gateway/static/settings_saved_views.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No overrides. |
| 3. Tokens | OK | No inline `style=`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.views-actions button` padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 12. |
| 9. Density script | **V** | Missing. |
| 10. Inline `<script>` | **V** | Lines 55-242 — 185 lines of saved-view management. |
| 11. `alert()` / `confirm()` | **V** | Lines 83, 190: `alert(msg)` fallback and `confirm("Delete this view?")`. narve-design: use toast (already does on line 82 — kept for "if window.toast somehow failed", but the codebase guarantees toast.js loaded; `alert()` is the wrong fallback). And use modal not `confirm()`. |
| 12. Shell consistency | OK | `app-shell` + sidebar + breadcrumb. No status-bar. |

**Violations: 3**.

### `gateway/static/pages/settings_saved_views.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | **V** | Line 50: `font-family: var(--font-mono, ui-monospace, monospace)` — comma-fallback chain to system mono. |
| 3. Tokens | **V** | Lines 13, 24, 28, 36, 38, 41, 48, 49, 51, 67: raw px. Line 16: `border-bottom: 2px solid transparent` — fixed 2px border. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | **V** | `.views-tab` padding `10px 16px` ≈ ~36 px tall — under 44 px. `.views-actions button` padding `4px 8px` ≈ ~22 px — well under 44. |

**Violations: 3**.

### `gateway/static/settings_webhooks.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 23 inline: `<a href="/api/docs#webhooks" style="color:inherit">docs</a>` — fine, but the underlying CSS has hardcoded green / red. |
| 2. Three typefaces | **V** | Line 8 in CSS (`pages/settings_webhooks.css:8`): `font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif` — **explicit `Inter` literal** not `var(--font-ui)`, plus three system fallbacks. |
| 3. Tokens | OK in template, but CSS leaks (see below). |
| 4. AA contrast | **V** | `pages/settings_webhooks.css:13`: `opacity: 0.6` on `.note` over `--bg` → drops below AA. |
| 5. Mobile tap targets | **V** | `.btn` padding `7px 14px` (CSS line 25) ≈ ~30 px tall — under 44 floor. |
| 6. No `<style>` blocks | OK | None in template. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | **V** | Missing the cookie-read inline script. |
| 9. Density script | **V** | Missing. |
| 10. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `.wrap` with "← Settings" back-link. |
| 11. Inline `<script>` | **V** | Lines 54-65 — `regenSecret()` inline. |

**Violations: 6** (and the CSS has more).

### `gateway/static/pages/settings_webhooks.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | **V** | Line 8: `background: var(--bg, #0d0d0d); color: var(--text-primary, #fff)` — dark fallback assumptions. Line 15: `background: var(--surface, #141414)` dark. Lines 21, 25, 29, 32: `rgba(255, 255, 255, 0.05)` / `0.06` / `0.12` / `0.15`. Line 23: `background: rgba(34,197,94,0.12); color: #22c55e` — green for "ok" badge. Line 24: `color: var(--text-muted, #888)`. Line 27: `border-color: rgba(239,68,68,0.4); color: #ef4444` — red for `.btn-danger`. Line 32: `background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.12)` — input bg. Line 37: `accent-color: #22c55e` — checkbox green. |
| 2. Three typefaces | **V** | Line 8: `font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif` — `Inter` literal not token + 3 system fallbacks. Line 21: `font-family: ui-monospace, Menlo, monospace`. Line 33: same. |
| 3. Tokens | **V** | 30+ raw px values. Generic class names (`.btn`, `.row`, `.head`, `.list`, `.field`, `.note`, `.wrap`) collide with global components.css naming. Two pages share the `.row` class name with different rules — high collision risk. |
| 4. AA contrast | **V** | Multiple `opacity: 0.5x–0.7` stacks. |
| 5. Mobile tap targets | **V** | `.btn` `padding: 7px 14px`; checkbox 16x16 with no parent label tap-area. |

**Violations: 5**.

### `gateway/static/invites_settings.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | All tokens. |
| 2. Three typefaces | OK | No overrides in template. |
| 3. Tokens | **V** | Line 30 `style="color: var(--text-secondary); font-size: 13px; margin: 0 0 12px;"`. Line 41 `style="color: var(--text-secondary); font-size: 13px; margin: 0 0 8px;"`. Line 42 `style="color: var(--text-primary);"`. Three inline `style=` blocks. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.inv-copy-btn` padded. |
| 6. No `<style>` blocks | OK | None. |
| 7. No emoji in chrome | OK | None. |
| 8. Anti-FOUC pre-paint | OK | Line 13. |
| 9. Duplicate meta tag | **V** | Line 8 and line 12 both have `<meta name="robots" content="noindex, nofollow">`. Duplicate. |
| 10. Inline `<script>` | **V** | Lines 57-118 — 60 lines of invite-list JS inlined. |
| 11. Shell consistency | **V** | No app-shell, no sidebar, no breadcrumb, no status-bar — bespoke `<main class="inv-main">`. |
| 12. Loading state | **V** | Line 34, 45 use literal `"loading…"` (lowercase) string. Should use `narveSkel.show`. |

**Violations: 5**.

### `gateway/static/pages/invites_settings.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | Tokens. |
| 2. Three typefaces | **V** | Line 21, 28: `font-family: var(--font-mono, ui-monospace, "SF Mono", monospace)` — comma-fallback chain. |
| 3. Tokens | **V** | Lines 8 (`var(--font-ui)` OK + raw `margin:0`), 11, 14, 17, 23: raw `font-size`, `border-radius`. |
| 4. AA contrast | OK | Tokens. |
| 5. Mobile tap targets | OK | `.inv-copy-btn` `6px 12px` → ~26 px — under 44 floor (borderline). |

**Violations: 2**.

---

## Cross-cutting findings

### A. Comma-fallback `var(--token, hex)` pattern

Used pervasively across the page CSS — e.g. `var(--text-primary, #fff)`, `var(--border, rgba(255,255,255,0.06))`, `var(--bg, #0d0d0d)`. **Anti-pattern.** narve-design rule: "If a needed token doesn't exist, add it to `gateway/static/gateway.css` … rather than hardcoding." When a token resolves, the fallback is dead code. When it doesn't, the fallback hides a real bug (missing token never gets noticed). Light theme will not flip the fallback. Fix: drop every comma-fallback; let CSS error visibly if a token is missing.

Affected files: `settings_api_keys.css`, `settings_api_key_reveal.css`, `settings_webhooks.css`, `settings_offline.css`, `settings_billing.css`, `settings_billing_cancel.css`, `settings_integrations.css`, `settings_trading_addon.css`, `invites_settings.css`, `settings_saved_views.css`, `referrals.css`, `account.css` — basically every file.

### B. App-shell vs. bespoke-shell split

The settings family is bimodal:

| Pattern | Templates |
|---|---|
| `app-shell` + sidebar + breadcrumb + status-bar | `billing.html`, `profile.html`, `settings.html`, `settings_billing.html`, `settings_profile.html`, `settings_privacy.html`, `settings_embeds.html`, `settings_integrations.html`, `settings_trading_addon.html`, `settings_affiliate.html`, `settings_takes.html` |
| Bespoke `.wrap` / `<main>` (no sidebar, no status-bar) | `account.html`, `referrals.html`, `invites_settings.html`, `settings_billing_cancel.html`, `settings_api_keys.html`, `settings_api_key_reveal.html`, `settings_webhooks.html`, `settings_offline.html` |

8 of 19 templates skip the app-shell entirely. Users navigating between settings pages flip between shell-on and shell-off chrome. Pick one pattern.

### C. `body.settings-page` opt-in is inconsistent

The `settings_redesign.css` rules apply only when `<body class="settings-page">`. Of the 19 templates, **5** opt in (`settings.html`, `settings_billing.html`, `settings_profile.html`, `settings_embeds.html`, `settings_integrations.html`, `settings_trading_addon.html`). The other 13 — including `profile.html`, `settings_privacy.html`, `settings_affiliate.html`, `settings_takes.html`, `settings_offline.html`, `settings_api_keys.html`, `settings_webhooks.html`, `account.html`, `billing.html`, `referrals.html`, `invites_settings.html`, `settings_api_key_reveal.html`, `settings_billing_cancel.html` — do not. So the editorial-serif body type, 44 px input min-height, and 16 px input font apply on **6 pages** out of **19**. The redesign is half-deployed.

### D. Anti-FOUC theme inline-script gap

Missing on `settings_api_keys.html`, `settings_api_key_reveal.html`, `settings_webhooks.html`, `settings_offline.html`, `settings_billing_cancel.html`. Dark-mode users land on those pages and see a flash of light theme before the cookie is read post-DOM. The standard 1-line inline `<script>` exists on every other page.

### E. Density-cookie inline-script gap

Pages with `data-density` support: `profile.html`, `settings.html`, `settings_integrations.html`, `settings_trading_addon.html`. Pages without it (but should have it): `billing.html`, `settings_billing.html`, `settings_profile.html`, `settings_privacy.html`, `settings_embeds.html`, `settings_affiliate.html`, `settings_takes.html`, `settings_api_keys.html`, `settings_api_key_reveal.html`, `settings_webhooks.html`, `settings_offline.html`, `settings_billing_cancel.html`, `referrals.html`, `invites_settings.html`, `settings_saved_views.html`, `account.html`. Density toggling from `/settings` won't persist on these pages.

### F. External font CDN (`account.html`)

Lines 9–10 of `account.html` load Google Fonts (`https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap`). The Inter Variable subset is shipped locally at `gateway/static/fonts/Inter-Variable-subset.woff2` (preloaded on every other audited page). The CDN load is duplicative, leaks user IP to Google, and is the only audit-surface page that hits a third party. Delete those two lines.

### G. Large inline `<script>` blocks (extract to `static/js/`)

| Template | Lines of inline JS |
|---|---|
| `settings_embeds.html` | ~250 |
| `settings_saved_views.html` | ~185 |
| `settings_offline.html` | ~150 |
| `settings_profile.html` | ~120 |
| `settings_affiliate.html` | ~90 |
| `settings_billing.html` | ~65 |
| `invites_settings.html` | ~60 |
| `settings_privacy.html` | ~25 |
| `profile.html` | ~14 |
| `settings_takes.html` | ~6 |
| `settings_webhooks.html` | ~12 |
| `settings_api_key_reveal.html` | ~18 |

Total: ~1,000 lines of page-specific inline JS that should live under `static/js/` and be loaded with `?v=` cache-busting (via `pwa_middleware`). These currently bypass cache-busting on every deploy and bloat initial HTML over the wire.

### H. `alert()` / `confirm()` / `window.confirm()` violations

| Template | Lines |
|---|---|
| `settings_embeds.html` | 282, 297 — `window.confirm` |
| `settings_saved_views.html` | 83 (`alert(msg)`), 190 (`confirm`) |

narve-design: use `narveToast` for non-blocking, `.nv-modal` for destructive confirm.

### I. Status-colour leak (semantic green / red / amber)

In addition to the `--positive` / `--red` / `--amber` token references called out above:

- `account.css:31,44`: `--positive` for "active subscription" + "connected" dot.
- `referrals.css:110`: `#58c58a` for "paying" referral status.
- `settings_billing.html:103`: `var(--red, #ef4444)` error text.
- `settings_billing.css:122-127`: amber resubscribe banner, green success banner.
- `settings-profile.css:74`: amber cooldown text.
- `settings_profile.html` JS: `var(--red, #ef4444)` injected as inline style.
- `settings_api_keys.css:190-192`: green `.ak-badge-ok`.
- `settings_api_keys.css:234-242`: red `.ak-btn-danger`.
- `settings_api_key_reveal.css:42-44`: amber "copy this key" alert.
- `settings_webhooks.css:23,27,37`: green badge + red danger + green checkbox accent.
- `settings_affiliate.html:208`: error/success bg toggle in `setMsg()`.

narve-design hard rule: monochrome. Status conveyed by position / weight / icon / text — never hue.

---

## Verdict

**Total violations: 162.**

The settings surface is **half-rebranded**. The new `settings_redesign.css` and per-page CSS (e.g. `settings_integrations.css`, `settings_trading_addon.css`) are clean tokens-and-tabs work; the legacy files (`settings_api_keys.css`, `settings_api_key_reveal.css`, `settings_webhooks.css`) are pre-redesign and carry hardcoded dark-theme assumptions, system-font fallback chains, semantic colours, and `opacity` AA-breaks.

Top remediation priorities, in order:

1. **Delete `--font-body` from `settings_redesign.css`** — restore the three-typeface rule. Drop the prose-serif body-type block (lines 95-116). The page-subtitle, section-desc, paragraphs, and list-items must use Inter. This single change touches every settings page.
2. **Migrate the three legacy CSS files** (`settings_api_keys.css`, `settings_api_key_reveal.css`, `settings_webhooks.css`) to tokens. They are pre-redesign artefacts using hardcoded `#0d0d0d` / `#141414` / `rgba(255,255,255,0.x)` and explicit `Inter` literals. Either redesign or kill the bespoke shell and reuse `settings_redesign.css`.
3. **Strip all semantic colours** — `--positive`, `--red`, `--amber`, `#22c55e`, `#ef4444`, `#fbbf24`, `#58c58a`, `#b58400`, plus the `rgba(34,197,94,...)`/`rgba(239,68,68,...)`/`rgba(245,158,11,...)` siblings. Replace status with strong-ink + position + weight + dashed-border patterns already proven in `sb-plan-badge-amber` (settings_billing.css:20) and `sb-plan-badge-red` (line 21). They are already the right answer in that one file.
4. **Promote bespoke shells to app-shell** — `account.html`, `referrals.html`, `invites_settings.html`, `settings_api_keys.html`, `settings_api_key_reveal.html`, `settings_webhooks.html`, `settings_offline.html`, `settings_billing_cancel.html`. Eight pages diverge.
5. **Strip comma-fallback `var(--token, hex)` patterns** across every page CSS. If a token is missing, add it once to `gateway.css`.
6. **Extract inline scripts** to `static/js/settings_*.js`. ~1,000 lines.
7. **Add the anti-FOUC theme + density inline scripts** to the 5 / 16 pages missing them.
8. **Delete the Google Fonts CDN link** from `account.html`.
9. **Replace `alert()` / `confirm()`** with `narveToast` / `.nv-modal` in `settings_saved_views.html` and `settings_embeds.html`.
10. **Fix the `[[NAV_PLACEHOLDER]]` literal** in `settings_billing_cancel.html` line 15 — appears to be an unrendered template tag.

Many violations are cosmetic in user-visible terms (light/dark contrast survives because tokens still resolve), but they're real audit failures against the narve-design contract.
