# Design Audit — Legal Pages

**Scope:** `gateway/static/privacy.html`, `gateway/static/terms.html`, `gateway/static/dpa.html`, `gateway/static/pages/legal.css`.
**Standard:** narve.ai design system (`narve-design` skill) — monochrome only, three typefaces (Inter / Geist Mono / Instrument Serif), tokens not raw values, ≤ 16 px radius, no decorative chrome, AA contrast both themes, no `<style>` block / no inline `style=`, no emoji in chrome, no `alert()`, mobile readable at 375 px, footer wordmark pattern.
**No `legal.html`** standalone page exists at `gateway/static/legal.html` — the legal pages are the three above plus the shared stylesheet at `gateway/static/pages/legal.css`. Audit covers what is present.
**No code changes made.** Audit only.

---

## Files reviewed

| File | LOC | Role |
|---|---|---|
| `gateway/static/privacy.html` | 657 | Privacy Policy (v3.0, 31 sections + TOC) |
| `gateway/static/terms.html` | 614 | Terms of Service (v3.0, 36 sections + TOC) |
| `gateway/static/dpa.html` | 339 | Data Processing Agreement (14 sections, **no TOC**) |
| `gateway/static/pages/legal.css` | 279 | Shared stylesheet (used by all three) |

Server routes confirmed in `gateway/server_features.py` lines 54, 67, 79 — all three are registered.

---

## Verdict

**14 design-system violations / inconsistencies** across the four files. The pages are mostly clean and on-brand (monochrome, tokens, Instrument Serif headlines, no decorative chrome, no emoji, no `alert()`). The bulk of the findings are: (1) **contact emails exposed via mailto** instead of routed through a form — the user's stated hard rule; (2) **one broken cross-link** (`/impressum`); (3) **DPA missing TOC** that the other two pages have; (4) one **invalid CSS token usage** in `legal.css`; (5) **theme cookie fallback** still reading the legacy `betyc-theme` name in the anti-FOUC inline script across all three pages.

No monochrome violations. No font-stack violations. No `<style>` blocks. No inline `style=` attributes. Mobile readable. Print stylesheet present. Focus rings present. Light + dark both ship via `data-theme`. Footer wordmark + cross-links match the public-landing rule (`narve.ai · Terms · Privacy · DPA · Support`) — close enough; "narve.ai" appears as the copyright label, not as a separate Instrument-Serif wordmark, which is acceptable for a footer micro-mark.

---

## Top 3

1. **Contact email exposed via 30+ `mailto:` links across all three pages — user's hard rule says "no contact email exposed (use form)".**
   Counts: `privacy.html` 25 mailto links, `terms.html` 5 mailto links, `dpa.html` 4 mailto links (and the `legal_email`/`privacy_email`/`support_email` template tokens render literal addresses). All Data-Subject-rights flows, DPO contact, EU Art. 27 rep, DSA notices, DMCA notices, and arbitration opt-outs are routed through `mailto:` rather than the existing `/contact` form (template exists at `gateway/static/contact.html`) or `/support` (route registered at `gateway/public_routes.py:605`). Spam-harvest surface + UX dead-end (`mailto:` does nothing on devices without a configured mail client). Fix: replace `mailto:{{ … }}` with `<a href="/contact?topic=…">` or `<a href="/support?topic=…">` and let those pages render the actual address (or a form). Keep a single textual "or write to legal@…" in `§31 Contact` if a fallback address must be visible, but route every body-paragraph CTA through the form.

2. **Broken cross-link `/impressum`** — `privacy.html` line 97 links to `<a href="/impressum">legal imprint</a>` and `landing.html` line 160 also links to `/impressum`, but no `/impressum` route is registered in any `*_routes.py` (search of `gateway/**/*.py` returns only the QA `pages.py` listing and the two callers). The route was added to QA's `tests/qa/pages.py` line 32 but never implemented. Hitting the link 404s. Fix options: (a) create `/impressum` template + route, (b) remove the line in §2 of privacy.html and the footer link in `landing.html` line 160, or (c) point the link to `/dpa#legal-entity` and add that anchor.

3. **`dpa.html` has no Table of Contents while `privacy.html` and `terms.html` do** — 14 `<h2>` sections in DPA but no `.legal-toc` block (the `legal.css` file even contains a comment, lines 7-8, noting this divergence: "Optional building blocks: .legal-toc (used by /terms, /privacy)"). Enterprise readers landing on DPA cannot jump to e.g. `#subprocessors` or `#eu-representative` without scrolling 14 sections. Privacy and Terms both render a two-column `.legal-toc` `<ol>` after `.legal-header`. Add the same pattern to DPA — markup already exists, CSS already supports it.

---

## All findings

### A. Cross-links and anchors

| # | Severity | File:line | Finding |
|---|---|---|---|
| A1 | **Hard rule** | `privacy.html` lines 85, 96, 334, 409, 428, 463, 473, 491, 510, 514, 524, 571, 607, 612, 635-637; `terms.html` lines 187, 218, 222, 292, 334, 380, 461, 480, 594-596; `dpa.html` lines 232, 281, 303, 315-316 | 34 `mailto:{{ privacy_email \| legal_email \| support_email }}` links. User's hard rule: "no contact email exposed (use form)". Route through `/contact` or `/support`. |
| A2 | High | `privacy.html` line 97 | `<a href="/impressum">` 404s — no route registered. Same broken link in `landing.html` line 160. |
| A3 | Medium | `privacy.html` line 411 | Body copy says "use the self-service controls in `/settings/privacy` … and `/settings → Delete account` (which calls `POST /account/delete`)". `POST /account/delete` is server-only (`server.py:4725`); user-facing copy referring to a POST endpoint by path is leaky implementation detail. Refer only to the visible button label. |
| A4 | Low | `privacy.html` line 617, 622 | Footnote anchors point to `/dpa#eu-representative` and `/dpa#uk-representative`. DPA has those `id`s at lines 271 and 292 — these resolve. **Not broken**, listed only to confirm the cross-anchor pair. |

### B. Page-structure consistency

| # | Severity | File:line | Finding |
|---|---|---|---|
| B1 | High | `dpa.html` (no line — absent) | No `.legal-toc` block. `privacy.html` and `terms.html` both render a TOC `<ol>` after `.legal-header`. DPA has 14 `<h2>` sections — same density as privacy/terms — but no jump-to anchors and no `id="s1"`-style numbered ids. (`<h2>` ids in DPA: only `#subprocessors`, `#eu-representative`, `#uk-representative` exist; sections 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 14 have no id.) Inconsistent with the other two pages. |
| B2 | Medium | `dpa.html` line 38 | `legal-meta` block is missing the `Version: …` line that `privacy.html` line 37 and `terms.html` line 37 both have. DPA is undated for version purposes. |
| B3 | Medium | `dpa.html` line 326 | Footer line: `© 2026 narve.ai. Prices in GBP. Not financial advice.` — `Prices in GBP` is irrelevant on a DPA page (DPA has no pricing), and `Not financial advice` appears on `terms.html` line 601 too, where it is on-brand. On DPA it is filler. Make DPA footer minimal: `© 2026 narve.ai. Version X.Y.` matching `privacy.html` line 644. |

### C. CSS / token integrity (legal.css)

| # | Severity | File:line | Finding |
|---|---|---|---|
| C1 | High | `legal.css` line 277 | `outline: 2px solid var(--focus-ring, var(--text-primary));` — `--focus-ring` is defined in `tokens.css` as a full shorthand `2px solid var(--border-strong)`, not a color. When substitution happens, the rule becomes `outline: 2px solid 2px solid var(--border-strong)` which is invalid and the browser drops the property. Either use `outline: var(--focus-ring);` (the tokens.css comment recommends this) or change `--focus-ring` to a color token. Result today: keyboard focus rings on legal pages fall back to UA default. |
| C2 | Low | `legal.css` lines 27, 87, 109, 223 | `.legal-shell { padding: var(--space-8) var(--space-7); }`, `.legal-toc ol { column-gap: var(--space-7); }`, `.legal-content h2 { margin: var(--space-7) … }`, `.legal-footer { margin-top: var(--space-9); }`. `--space-9` is non-canonical — narve-design enumerates `--space-1` through `--space-16`, so `--space-9` is in range if defined; **confirmed defined** in `tokens.css`. Listed only to note the upper end of usage. |
| C3 | Low | `legal.css` line 158, 194, 219 | `border-radius: var(--radius-xs)` used on `code` tags, blockquotes, and inline tags. `--radius-xs` is **defined** at `tokens.css` line `--radius-xs: 4px;` and is acceptable; narve-design's hard rule is "max 16 px", well within. **Not a violation** — listed to confirm. |

### D. Theme / FOUC script

| # | Severity | File:line | Finding |
|---|---|---|---|
| D1 | Low | `privacy.html` line 12, `terms.html` line 12, `dpa.html` line 12 | Anti-FOUC inline script still reads the legacy cookie name `betyc-theme` as a fallback: `document.cookie.match(/narve-theme=…/) \|\| document.cookie.match(/betyc-theme=…/)`. Acceptable as a transition fallback, but should have a sunset date. Search the repo for `betyc-theme` to see how many pages still carry the dual-name fallback. (Not unique to legal — same pattern in many pages.) |
| D2 | Low | `privacy.html` line 12, `terms.html` line 12, `dpa.html` line 12 | Inline `<script>` is acceptable here (anti-FOUC is the canonical exception to "no inline JS"). Listed to confirm pattern matches gateway-wide. |

### E. Footer wordmark / public-landing rule conformity

| # | Severity | File:line | Finding |
|---|---|---|---|
| E1 | Low | `privacy.html` lines 644-650, `terms.html` lines 600-608, `dpa.html` lines 326-332 | narve-design's public-landing rule says footer is `narve.ai · Terms · Privacy · DPA · Support` in `--text-tertiary`, 11 px. Legal-page footers split into two divs (copyright on left, links on right) using `.legal-footer` flex layout, font-size 12 px (`--text-xs`), color `--text-tertiary`. Close to spec but not identical; the canonical hub/landing footer uses the inline-with-bullets format. Either is defensible — flagged only for future unification. |

### F. Privacy / contact UX

| # | Severity | File:line | Finding |
|---|---|---|---|
| F1 | Medium | `privacy.html` line 411 | Refers to `/settings/privacy` and `/settings → Delete account` for data export and erasure. The route `/settings/privacy` is registered (`export_routes.py:439`). `/account/delete` exists as `POST` only. The copy should not name the HTTP verb + path — rewrite as "use the **Delete account** button in `/settings`." |
| F2 | Low | `dpa.html` line 157 | `<a href="/profile">Profile Settings</a>` — `/profile` resolves (`server.py:4714`), but the link is misleading because account deletion is initiated from `/settings`, not `/profile`. Either change copy to `/settings` (matches privacy.html line 411) or update the linked anchor target. |

### G. SEO / structured data

| # | Severity | File:line | Finding |
|---|---|---|---|
| G1 | Low | `privacy.html` lines 16-26, `terms.html` lines 16-26, `dpa.html` lines 16-27 | `application/ld+json` blocks are well-formed and consistent (`@type: WebPage`, `isPartOf` linking to `narve.ai`). DPA also adds `audience: BusinessAudience`. Good practice. **Pass.** |

---

## Things that are RIGHT (not violations — for the record)

- **Monochrome.** No hue anywhere in the markup or CSS outside the print stylesheet's `#000`/`#fff`/`#555`/`#ccc` (legitimate print overrides).
- **Three typefaces only.** `--font-display` (Instrument Serif Italic) on `.legal-title` and `.legal-content h2`. `--font-mono` (Geist Mono) on `.legal-eyebrow`, `.legal-meta`, `.legal-content table.pl th`, `.legal-content code`, `.sub-table th`. `--font-body` (Inter) everywhere else. No fourth face.
- **Tokens.** No raw `#fff`, no `padding: 16px`, no `box-shadow: 0 4px 12px rgba(...)`. Everything goes through `--space-*`, `--text-*`, `--border-*`, `--bg-*`, `--radius-*`. (Two `1px 6px` hardcodes on `code` padding at `legal.css` line 157 — acceptable inline-code micro-padding outside the 4 px scale.)
- **No `<style>` block, no inline `style=`** on any of the three pages.
- **No emoji.** No `alert()`/`confirm()`. No `Loading…` strings.
- **AA contrast preserved.** All text uses `--text-primary` / `--text-secondary` / `--text-tertiary` which the gateway tokens were already hardened for in AUDIT #4.
- **Print stylesheet** included (`legal.css` lines 241-268) — page-break-avoid on headings, URL expansion via `::after`, color overrides to true black. Strong.
- **Focus rings** present via `:focus-visible` (`legal.css` lines 271-279) — modulo the C1 bug.
- **Responsive.** TOC collapses to 1 column at ≤ 720 px (line 93-95); `.legal-title` drops from `--text-5xl` to `--text-4xl` at ≤ 640 px (line 65-67).
- **`<table>` always inside `<div class="nv-table-wrap" tabindex="0">`** for horizontal overflow on mobile — `privacy.html` lines 193, 263, 301, 338; `dpa.html` line 192. Matches the narve-design mobile rule.
- **Light + dark both ship** via `data-theme` from the anti-FOUC script.
- **Canonical URL + JSON-LD** consistent across all three pages.

---

## Recommended fix order

1. **A1 (hard rule):** route every `mailto:` through `/contact` or `/support`. Highest-impact, highest-visibility change.
2. **A2:** create `/impressum` route + template, or remove the two callers.
3. **C1:** fix `--focus-ring` substitution in `legal.css` (one-line edit).
4. **B1:** add TOC to `dpa.html` using the existing `.legal-toc` markup pattern.
5. **B2 / B3:** harmonise DPA footer + add `Version:` line.
6. **F1 / F2:** clean up the implementation-detail leaks in privacy.html copy and DPA's `/profile` link.
7. **D1, E1:** track for a future cleanup sweep; not blocking.

---

*Audit run 2026-05-15. Static review only. No deployed-page testing was performed in this pass.*
