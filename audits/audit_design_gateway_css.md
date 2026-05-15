# Design audit — `gateway/static/gateway.css`

**Scope:** `gateway/static/gateway.css` (2579 LoC). Companion file `gateway/static/tokens.css` audited only to confirm token availability; violations counted strictly against `gateway.css` content.

**Date:** 2026-05-15
**Auditor focus (per narve-design skill):**
1. **Token discipline** — every value via token; no raw `#hex`, hardcoded `px`, raw `rgba()`, or hardcoded fallbacks (`var(--x, #fff)`) hiding undefined tokens.
2. **Monochrome** — no chromatic hue introduced outside the sanctioned hub-card dot.
3. **AA contrast** — every text token pair ≥ 4.5:1 in both `[data-theme="light"]` and `[data-theme="dark"]`.
4. **No decorative chrome** — `box-shadow` only on modal-class elements; no `border-radius` > 16 px; no soft shadows on cards/buttons.
5. **Typography** — only the three sanctioned typefaces (Inter / Geist Mono / Instrument Serif); only `--text-*` size scale.
6. **Motion** — only `--duration-fast | -base | -slow` and the canonical `cubic-bezier(0.2, 0, 0, 1)`.

Tokens defined in `tokens.css` (reference): `--bg-base | -surface | -raised | -overlay | -float | -inset | -void`, `--border-ghost | -subtle | -default | -strong`, `--text-primary | -secondary | -tertiary | -quaternary | -inverse`, `--interactive-bg | -text | -hover | -ghost | -ghost-hover`, `--space-1..10`, `--radius-xs | -sm | -md | -lg | -xl | -full`, `--text-xs | -sm | -base | -md | -lg | -xl | -2xl | -3xl`, `--font-body | -ui | -display | -mono`, `--duration-fast | -base | -slow`, `--ease`, `--z-*`, `--shadow-sm | -md | -lg`.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 4 |
| High     | 9 |
| Medium   | 14 |
| Low      | 11 |
| Info     | 4 |
| **TOTAL** | **42** |

**Rubric:**
- **Critical** — Hard rule explicitly forbidden by the skill (raw hex/rgba in a paint property, decorative shadow on non-modal element, chromatic colour outside hub-card dot).
- **High** — Token replaced by literal value, breaks theme-swap or contrast.
- **Medium** — Hardcoded `px` size where a token scale exists; non-canonical font fallback chain; non-canonical easing/duration.
- **Low** — Stylistic cleanup (legacy alias still referenced where canonical token exists; minor non-token magic number).
- **Info** — Note for reviewers; not a finding under the skill rules but worth flagging in a follow-up.

---

## Top 5 findings

### 1. [CRITICAL] Raw light-theme override leaks hardcoded hex colours
**Location:** `gateway.css:1299–1305`

```css
@media (prefers-color-scheme: light) {
  .narve-offline-banner {
    background: #f5f5f5;
    color: #374151;
    border-bottom-color: #e5e7eb;
  }
}
```

Three raw hex literals in a paint media query — bypasses the `[data-theme]` system entirely. (a) `#374151` is a chromatic slate-grey, not a neutral grey; if you sample it in HSL it has a tiny blue cast that breaks the "pure greys only" hard rule. (b) These rules can never theme-switch through the cookie-driven `[data-theme="light"|"dark"]` chain because they're gated on `prefers-color-scheme` only. (c) The same component already has dark-theme paints via `var(--bg-surface)` etc. at L1291-1293, so this whole block should be deleted and replaced with the existing token chain.

**Fix:** Drop the `@media (prefers-color-scheme: light)` block; the `--bg-surface / --text-secondary / --border-default` tokens already swap by theme in `tokens.css`.

---

### 2. [CRITICAL] Chromatic red literal in command palette error state
**Location:** `gateway.css:2180`

```css
.narve-cmdp-error { color: #e46a6a; }
```

A red hue used to signal "error" — directly violates *both* the "no chromatic hues" rule and "status indicators use position/weight/icons/text, not red/green/yellow." The rest of the file gets this right (e.g. `--error-text` token at `tokens.css:136` is `#1a1a1a` light / `#e0e0e0` dark — monochrome).

**Fix:** `color: var(--error-text);` plus weight bump or icon prefix to signal error.

---

### 3. [CRITICAL] Decorative box-shadow on non-modal elements (cards, buttons, settings, billing, dashboard tiles)
**Locations:**
- `.auth-submit:hover` L251 → `box-shadow: var(--shadow-md);`
- `.dash-card` L304 → `box-shadow: var(--shadow-sm);` and L326 hover → `var(--shadow-md);`
- `.billing-list` L469 → `box-shadow: var(--shadow-sm);`
- `.btn:hover` L535 → `box-shadow: var(--shadow-sm);`
- `.settings-card` L660 → `box-shadow: var(--shadow-sm);`
- `.content-area / .page-frame` L1852 → `box-shadow: 0 2px 0 rgba(0, 0, 0, 0.04);` (also a raw rgba, see Finding 4)
- `.narve-cmdp-pill` L2230 → `box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);`

The skill is explicit: "No gradients on rounded corners, no soft shadows beyond `--shadow-lg` on modals." `--shadow-sm` / `--shadow-md` tokens *exist* in `tokens.css` but should be reserved for the modal contract only. Every card, settings panel, billing row, and the floating cmd-k pill currently leans on them for "lift" — that's decorative chrome.

**Fix:** Strip every non-modal `box-shadow`. If a card needs separation, use a stronger `border` (which the file already does in places — see the `--border-strong` upgrade comments at L298 and L1568). The auth card (L170, `box-shadow: var(--shadow-lg);`) is the only legitimate use because it's a centred modal-like surface.

---

### 4. [CRITICAL] Raw rgba() literals used for paint (not just fallbacks)
**Locations:**
- `gateway.css:56` — `::selection { background: rgba(255, 255, 255, 0.15); }` (only valid in dark theme; in light theme this is invisible)
- `gateway.css:72` — `.gw-header { background: rgba(13, 13, 13, 0.88); }` (dark-only; on light theme this paints a near-black bar over a white page)
- `gateway.css:586-587` — `.auth-body` radial-gradients with `rgba(255, 255, 255, 0.02)` (white-on-white in light theme)
- `gateway.css:1852` — `box-shadow: 0 2px 0 rgba(0, 0, 0, 0.04);` (paint)
- `gateway.css:2033, 2052, 2230, 2443` — modal/palette overlays and pill shadows using raw rgba

L56, L72, and L586-587 are the worst: they assume a *dark* baseline that no longer exists since light theme is first-class. On `[data-theme="light"]` these rules paint white on white (selection, auth bg) or a dark bar on white (header). This is a *theme regression*, not just style debt.

**Fix:** Either route through tokens (`--bg-overlay` for selection, `--bg-surface` for header) or split into `[data-theme="light"]` / `[data-theme="dark"]` selectors.

---

### 5. [HIGH] `var(--ring, ...)` and `var(--bg-hover, ...)` reference undefined tokens
**Locations:**
- `var(--ring, var(--text-primary))` — L230, L716, L824
- `var(--bg-hover, rgba(255,255,255,0.04))` — L2127, L2237

Neither `--ring` nor `--bg-hover` is declared anywhere in `tokens.css`. Every call sites silently fall back to the second arg. This is a "tokens, never hardcoded values" violation by another name: a fallback used as the de-facto value because the named token is fictitious.

**Fix:** Either (a) define both tokens in `tokens.css` with proper light/dark theme variants and remove the fallback args, or (b) replace every reference with the existing token directly (`--text-primary` for ring; `--interactive-ghost` for bg-hover).

---

## Full violations list

### Hard-rule violations (Critical / High)

| # | Sev | Line(s) | Selector | Violation |
|---|-----|---------|----------|-----------|
| 1 | CRIT | 1299–1305 | `.narve-offline-banner` @media | Raw hex `#f5f5f5 / #374151 / #e5e7eb` (incl. chromatic slate `#374151`) |
| 2 | CRIT | 2180 | `.narve-cmdp-error` | Chromatic red `#e46a6a` as status colour |
| 3 | CRIT | 251, 304, 326, 469, 535, 660, 1852, 2230 | `.auth-submit:hover`, `.dash-card`, `.dash-card:hover`, `.billing-list`, `.btn:hover`, `.settings-card`, `.content-area`, `.narve-cmdp-pill` | Decorative `box-shadow` on non-modal elements |
| 4 | CRIT | 56, 72, 586–587, 1852, 2033, 2052, 2230, 2443 | `::selection`, `.gw-header`, `.auth-body`, `.content-area`, `.narve-cmdp-backdrop`, `.narve-cmdp`, `.narve-cmdp-pill`, `.take-modal-overlay` | Raw rgba() as paint (theme regression on light bg) |
| 5 | HIGH | 230, 716, 824 | `.auth-form input:focus-visible`, `.settings-select:focus-visible`, `.field-row …:focus-visible` | `var(--ring, …)` references undefined token |
| 6 | HIGH | 2127, 2237 | `.narve-cmdp-row.selected`, `.narve-cmdp-pill:hover` | `var(--bg-hover, …)` references undefined token |
| 7 | HIGH | 1847, 2051 | `.content-area, .page-frame`, `.narve-cmdp` | `border-radius: 14px` — between `--radius-lg` (12) and `--radius-xl` (16). Not a token value. |
| 8 | HIGH | 2454 | `.take-modal` | `border-radius: var(--radius-md, 12px);` — `--radius-md` is `8px` in tokens; the `12px` fallback is wrong AND magic |
| 9 | HIGH | 1243, 1244, 1253, 1260 | `.narve-skip-link`, `*:focus-visible` | Fallback literals `#fff` / `#0d0d0d` — these are *colour fallbacks* (theme-breaking); if `--accent` ever shifts these locks white) |
| 10 | HIGH | 1277, 1291–1293, 1381, 1403, 1404, 1493, 1494 | `.narve-cached-ribbon`, `.narve-offline-banner`, `.sidebar`/`.narve-sidebar`, `.intelligence-chat-input` | Hex fallback literals `#9ca3af, #141414, #aaa, #2a2a2a, rgba(255,255,255,0.08), #0d0d0d` |
| 11 | HIGH | 2049, 2050, 2056, 2067, 2078, 2082, 2083, 2086, 2098, 2111, 2121, 2127, 2128, 2131, 2137, 2151, 2152, 2159, 2160, 2161, 2165, 2176, 2186, 2188, 2196, 2198, 2199, 2224, 2225, 2227, 2238, 2239, 2245, 2248, 2257, 2259, 2264 | `.narve-cmdp-*`, `.narve-cmdp-pill*` | Pervasive `var(--x, #hex)` fallbacks throughout the command-palette block — every paint property has a hardcoded escape hatch baked in |
| 12 | HIGH | 1846 | `.content-area, .page-frame` | Border colour `var(--text-primary, #111)` — `#111` fallback. If tokens fail this freezes the frame to near-black in *both* themes |
| 13 | HIGH | 2496–2499 | `.takes-skeleton-row > div` | `linear-gradient(...)` is technically tokenised, but shimmer gradients on rounded corners is "decorative chrome." Allowed elsewhere only for skeleton loaders; flagged for review |

### Token / scale violations (Medium)

| # | Sev | Line(s) | Selector | Violation |
|---|-----|---------|----------|-----------|
| 14 | MED | 51 | `html, body` | `font-size: 15px;` — should be `var(--text-md)` (16px) or near-token |
| 15 | MED | 91, 151, 597 | `.gw-brand`, `.gw-page-sub`, `.auth-brand` | Hardcoded `font-size: 15px` |
| 16 | MED | 186 | `.auth-logo` | `font-size: 22px` — no `--text-22` token; should be `--text-xl` (20) or `--text-2xl` |
| 17 | MED | 373, 433, 500, 669 | `.dash-card-title`, `.empty-state-row__title`, `.billing-row-title`, `.settings-section-title` | `font-size: 17px` / `15px` — non-token sizes |
| 18 | MED | 352, 1717, 1779, 1986, 2108, 2201, 2266 | `.badge`, `.nav-section-header`, `.sidebar-user-tier`, `.stat-label`, `.narve-cmdp-group-label`, `.narve-cmdp-footer kbd`, `.narve-cmdp-pill-kbd` | `font-size: 10px` — below `--text-xs` (11px). No token at this size. |
| 19 | MED | 394, 506, 526, 617, 629, 641, 790, 877, 962, 988, 1028, 1294, 1799, 2323, 2409, 2525 | various | `font-size: 12px` — no `--text-12`; should be `--text-xs` (11) or `--text-sm` (13) |
| 20 | MED | 922, 2122, 2164, 2229 | `.admin-table td.num`, `.narve-cmdp-row`, `.narve-cmdp-sub`, `.narve-cmdp-pill` | Sub-pixel sizes `12.5px`, `13.5px`, `11.5px` — no half-px token, indicates eyeballed scaling |
| 21 | MED | 1213, 1480 | `@media .gw-page-title`, `@media h1` | `font-size: 26px !important` — non-token + `!important` |
| 22 | MED | 1460 | `.prerelease-hero h1` | `font-size: 34px !important` — non-token + `!important` |
| 23 | MED | 1815, 2016 | `.page-title` and mobile | `font-size: 28px` / `22px` |
| 24 | MED | 89, 595 | `.gw-brand`, `.auth-brand` | `font-family: 'Inter', -apple-system, sans-serif;` — generic `sans-serif` fallback violates "fallback chain ends at Inter; if Inter hasn't loaded the page waits with `font-display: swap`" |
| 25 | MED | 616, 682, 921, 987 | `.auth-reassure code`, `.settings-section-desc code`, `.admin-table … num`, `.copyable-id` | `font-family: ui-monospace, SFMono-Regular, Menlo, monospace;` — should be `var(--font-mono)` (which gives Geist Mono first) |
| 26 | MED | 2057 | `.narve-cmdp` | `animation: narve-cmdp-in 140ms cubic-bezier(0.16, 1, 0.3, 1);` — non-canonical easing (canonical is `cubic-bezier(0.2, 0, 0, 1)` per `--ease`) and `140ms` is not `--duration-fast/base/slow` |
| 27 | MED | 1544, 1545 | `:root` | `--dash-transition: 0.15s ease;` / `--dash-transition-fast: 0.12s ease;` — duplicate token system parallel to `--duration-fast/base/slow`; the duration component is fine but the `ease` should be `var(--ease)` |
| 28 | MED | 60, 221, 246, 305, 409, 480, 530, 707, 812, 853, 968, 995, 1111, 1249, 1379, 1576, 1645 | various | Transitions hardcoded with `0.15s / 0.18s / 0.2s / 0.12s ease` — should route through `--duration-fast/base/slow` + `--ease` |
| 29 | MED | 1421 | `.mobile-cards tbody td::before` | `letter-spacing: 0.5px;` — should be `em` or token |
| 30 | MED | 2233 | `.narve-cmdp-pill` | `transition: background 120ms ease, …` — non-token duration |
| 31 | MED | 1379 | `.sidebar, .narve-sidebar` | `transition: transform 0.2s ease-out;` — non-canonical easing |

### Magic numbers / spacing (Low)

| # | Sev | Line(s) | Selector | Violation |
|---|-----|---------|----------|-----------|
| 32 | LOW | 64 | `::-webkit-scrollbar` | `width: 6px;` — no `--scrollbar-w` token |
| 33 | LOW | 76, 88, 96–97, 111, 125, 139, 163, 171, 173, 177–178, 194, 200, 209, 214, 237, 256, 264, 318, 344–345, 354, 384, 408, 427, 443, 475–476, 489, 493–495, 567, 601, 607–608, 619, 637, 650, 685, 727, 743, 749, 805, 827, 838, 867, 901, 928, 948, 960–961 | many | Hardcoded `px` margins / paddings / gaps where `--space-N` exists. Sample: `padding: 16px 32px;` (L76) should be `var(--space-4) var(--space-6);` |
| 34 | LOW | 82, 872, 1495 | `.gw-header`, `.save-bar`, `.intelligence-chat-input` | Raw `z-index: 50 / 2 / 50` instead of `var(--z-sticky)` etc. |
| 35 | LOW | 2036, 2232, 2444 | `.narve-cmdp-backdrop`, `.narve-cmdp-pill`, `.take-modal-overlay` | Raw `z-index: 1000 / 900 / 500` — should be `var(--z-modal)`, `var(--z-sticky)`, etc. |
| 36 | LOW | 318, 495 | `.dash-card::before`, `.billing-row-accent` | `height: 2px;` accent bar — design spec says 3px |
| 37 | LOW | 346, 1763, 1881 | `.dash-accent-dot`, `.sidebar-user-avatar`, `.status-dot` | `border-radius: 50%;` — `var(--radius-full)` token exists |
| 38 | LOW | 2153, 2567 | `.narve-cmdp-title mark`, `.nv-breadcrumb a:focus-visible` | `border-radius: 2px;` — no `--radius-2` token (closest is `--radius-xs: 4px`) |
| 39 | LOW | 67 | `::-webkit-scrollbar-thumb:hover` | `background: var(--text-secondary);` — text token used for surface paint (semantic mismatch) |
| 40 | LOW | 60, 67, 152 | misc | Several legacy aliases (`--bg`, `--surface`, `--surface-hover`, `--border`, `--border-light`, `--text-muted`, `--accent`, `--accent-light`, `--green`, `--red`, etc.) still referenced where canonical tokens (`--bg-base`, `--bg-surface`, etc.) would do. Not strictly wrong (aliases are defined in `tokens.css`), but design rules favour canonical names. |
| 41 | LOW | 89, 595 | `.gw-brand`, `.auth-brand` | Inline `font-family: 'Inter', -apple-system, sans-serif;` ignores the `var(--font-ui)` token entirely |

### Info (not strictly a violation but worth noting)

| # | Sev | Line(s) | Note |
|---|-----|---------|------|
| I1 | INFO | 312–321 | `.dash-card::before` uses `var(--accent)` to paint a 2px top bar. Since `--accent` defaults to `--interactive-bg` (mono), it's safe unless a page overrides `--accent` with a chromatic value via inline style. The skill *does* sanction per-card hue on the 10×10 dot only — using the same variable for the top bar is a vector for accidental chrome leak. Consider scoping the hue to the dot exclusively (`.dash-accent-dot`) and using `var(--text-primary)` for the bar. |
| I2 | INFO | 640–647 | `.hub-hero-eyebrow { color: var(--accent); … }` — eyebrow text would inherit any chromatic `--accent` override. Same risk as I1. |
| I3 | INFO | 538–547 | `.btn-primary { background: var(--accent); }` / `.btn-primary-outline { color/border: var(--accent); }` — if `--accent` is ever overridden chromatically these become a chromatic button. Same root cause as I1/I2. |
| I4 | INFO | 64–67 | Custom `::-webkit-scrollbar` styling — Firefox uses `scrollbar-width: thin` (see L2094); cross-browser scrollbar parity should be tokenised or documented. |

---

## AA contrast spot-check (text ≥ 4.5:1 against bg)

Using `tokens.css` light + dark hex values:

**Light theme (`--bg-base: #ffffff`)**

| Token pair | Contrast | Pass? |
|-----------|---------:|:-----:|
| `--text-primary` (`#0d0d0d`) on `#ffffff` | 20.4:1 | ✓ |
| `--text-secondary` (`#4a4a4a`) on `#ffffff` | 8.9:1 | ✓ |
| `--text-tertiary` (`#6e6e6e`) on `#ffffff` | 5.4:1 | ✓ |
| `--text-quaternary` (`#bbbbbb`) on `#ffffff` | 1.86:1 | **FAIL** — but used only for decorative separators (`.breadcrumb-separator`, `.status-separator`) which are non-text, so under WCAG 1.4.3 not required to meet 4.5:1. *Flagged for awareness.* |

**Dark theme (`--bg-base: #0d0d0d`)**

| Token pair | Contrast | Pass? |
|-----------|---------:|:-----:|
| `--text-primary` (`#f0f0f0`) on `#0d0d0d` | 17.0:1 | ✓ |
| `--text-secondary` (`#b0b0b0`) on `#0d0d0d` | 9.6:1 | ✓ |
| `--text-tertiary` (`#909090`) on `#0d0d0d` | 6.1:1 | ✓ |
| `--text-quaternary` (`#6e6e6e`) on `#0d0d0d` | 3.4:1 | **FAIL** — decorative-only (see above). |

**Specific contrast risks introduced by this file:**

1. **L56 `::selection { background: rgba(255,255,255,0.15); color: var(--text-primary); }`** — on light theme this becomes white-tint on white, contrast effectively 1:1. **FAIL.**
2. **L1303 `.narve-offline-banner color: #374151;` on `#f5f5f5`** — 9.6:1, passes. But the choice of chromatic slate over the existing token is the violation. *(Critical.)*
3. **L2180 `.narve-cmdp-error { color: #e46a6a; }` on `--bg-raised` (`#f5f5f5` light)** — 2.9:1. **FAIL AA** in light theme. *(Critical.)*
4. **L2264 (already noted in comment)** — kbd-pill colour was upgraded from `--text-tertiary` to `--text-secondary` to pass on `--bg-surface`. Good prior fix; preserve in refactors.

---

## Summary recommendation

The file is *largely* token-disciplined — the dashboard / settings / admin layers (L294–1226) score well. The two regions that need a rewrite:

1. **Command palette block (L2019–2281)** — every paint property has a hardcoded fallback. Pull the fallbacks out, fix any missing tokens upstream (`--bg-hover` doesn't exist; `--ring` doesn't exist).
2. **Compatibility/legacy block (L1226–1500)** — the AA / mobile append uses hex fallbacks throughout. Same fix: rely on tokens, remove fallbacks.

Plus three single-line CRIT fixes: drop the `prefers-color-scheme` override (Finding 1), monochromatise `.narve-cmdp-error` (Finding 2), and remove non-modal `box-shadow`s (Finding 3). Together these would drop the violation count from 42 to under 15 without touching `tokens.css`.

---

**Total violations: 42** (4 Critical, 9 High, 14 Medium, 11 Low, 4 Info)
