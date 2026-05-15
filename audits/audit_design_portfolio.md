# Design audit — portfolio / trade / markets surfaces

**Date:** 2026-05-15
**Scope requested:** `gateway/static/portfolio*.html`, `gateway/static/trade*.html`, `gateway/static/markets*.html`
**Auditor rules:** narve-design (monochrome, three-typeface, density) + this audit's hard rules:

> Numbers right-aligned monospace, tables dense, no roomy padding, table rows highlight on hover via **opacity not bg-shift**.

---

## Files actually audited

The requested glob patterns do not match any HTML files literally. The closest existing market/trade/portfolio surfaces in `gateway/static/` are:

| Requested glob | Actual file(s) audited | Why |
|---|---|---|
| `portfolio*.html` | *(none exists in static)* | Portfolio surface is rendered client-side by `trade.js`, served as part of the dashboard switcher overlay. The Python module `gateway/portfolio/routes.py` is JSON-only API. |
| `trade*.html` | `gateway/static/trade.js` (1692 LOC), `gateway/static/settings_trading_addon.html` (223 LOC) | `trade.js` builds the full Markets / Portfolio / Orders overlay inline. The settings page is the only trade-prefixed HTML template that exists. |
| `markets*.html` | `gateway/static/market_detail.html` (528 LOC), `gateway/static/shared_market.html` (49 LOC) | Singular `market_detail.html` is the per-market page; `shared_market.html` is the public share card. No plural `markets.html` exists. |

`trade.js` is included because it is the **only** place a portfolio table, orders table, and markets table actually render — the audit's table rules would be untestable without it.

A canonical table-hover rule in `gateway.css` was also reviewed (it affects every page above via inheritance).

---

## Violations

**Total violations: 24** across 4 files + 1 canonical CSS rule.

### V1 — `trade.js` markets table: numbers are **not** right-aligned, **not** monospace

`trade.js:570-615` renders the Markets table. The numeric columns (`Yes`, `No`, `Volume`, `EV`, `Cred.`) are all rendered as plain `<td>` with no `text-align: right` and no `font-variant-numeric: tabular-nums` and no Geist Mono. They inherit `font-family: 'Inter', -apple-system, sans-serif` from `#hb-markets-overlay` (line 65). Direct rule break.

```js
<td style="font-weight:600">${pct(m.yes_price)}</td>
<td style="color:#a3a3a3">${pct(m.no_price)}</td>
<td>${usd(m.volume_usd)}</td>
<td style="font-size:12px;color:#666">${closeStr}</td>
<td class="${evClass}">${evStr}</td>
<td style="color:#a3a3a3">${credStr}</td>
```

None carry `class="num"` (which would right-align via `gateway.css:1924-1929`). The overlay CSS does not even import gateway.css's table rules — it lives in a `#hb-markets-overlay` z-10000 fixed div with its own scoped styles. Numbers render as Inter, left-aligned.

### V2 — `trade.js` portfolio table: same problem

`trade.js:991-1010` builds the Portfolio table. Columns `Shares`, `Entry`, `Current`, `P&L`, `Value` are all numeric. None right-aligned. None monospaced. `Side` is bold uppercase but in Inter.

```js
<td>${p.shares}</td>
<td>${p.avg_price ? pct(p.avg_price) : '—'}</td>
<td>${p.current_price ? pct(p.current_price) : '—'}</td>
<td class="${pnlClass}">${p.pnl ? usd(p.pnl) : '—'}</td>
<td>${p.value ? usd(p.value) : '—'}</td>
```

### V3 — `trade.js` orders table: same problem

`trade.js:1063-1078`. Columns `Amount`, `Price` are unstyled. No alignment, no mono.

### V4 — `trade.js` table row hover uses **background shift**, not opacity

`trade.js:132-133`:

```css
.hb-m-table tr { cursor: pointer; transition: background 0.1s; }
.hb-m-table tbody tr:hover { background: #141414; }
```

Plus the duplicate at line 225: `.hb-m-port-row:hover td { background: rgba(255,255,255,0.02); }`.

Hard-rule violation: the audit demands hover via opacity. The base body is `#0d0d0d`, hovered rows shift to `#141414` — a colour/bg jump, not an opacity ramp.

### V5 — Canonical table hover in `gateway.css` is also a bg-shift

`gateway.css:1930-1933`:

```css
.app-shell table tr:hover td,
.dash-table tr:hover td {
  background: var(--interactive-ghost);
}
```

This rule governs `market_detail.html`, `settings_trading_addon.html`, every admin table, and anything else that lands inside `.app-shell`. It does the bg-shift the audit rule forbids. Worth noting because changing `trade.js` alone would not bring the in-app tables into compliance.

### V6 — `trade.js` table padding is roomy, not dense

`trade.js:131`: `.hb-m-table td { padding: 14px 12px; font-size: 14px; ... }` — 14px vertical row padding is on the comfortable side. narve density tokens are `--row-pad-y` (defaults to ~6-8 px comfortable, ~3-4 px compact). The overlay ignores tokens entirely.

`trade.js:126`: header `padding: 10px 12px` also raw px. No density-token responsiveness — flipping `data-density="compact"` does nothing for this overlay's tables.

### V7 — `trade.js` uses hardcoded hex colours throughout (monochrome violation light)

The whole `trade.js` overlay is a separate CSS island: `#0d0d0d`, `#141414`, `#1f1f1f`, `#2a2a2a`, `#a3a3a3`, `#d4d4d4`, `#666`, `#444`, `#fff`. None are token-driven. Also two non-grey hexes that *are* explicit colour escapes:

- `.hb-m-btn-danger:hover { color: #ff6b6b; border-color: #ff6b6b; }` (line 122) — red.
- `.hb-m-error { color: #ff6b6b; ... background: rgba(255,107,107,0.08); ... }` (line 209) — red.

These directly break the "monochrome only" rule. Status meaning is being expressed in hue.

### V8 — `trade.js` PnL class encodes meaning via colour (or attempts to)

`trade.js:219-220`:

```css
.hb-m-pnl-pos { color: #fff; }
.hb-m-pnl-neg { color: #666; }
```

The class names imply red/green; in practice the file uses opacity-style monochrome (full white vs dim grey) which is acceptable monochrome. However the chart's `borderColor: 'var(--text-primary)'` in `market_detail.html:421` is good — the line dataset is monochrome.

### V9 — `trade.js` "active YES" vs "active NO" button uses tonal asymmetry (acceptable) but the YES bet button uses a colour-coded bet flow (border-color shift, not opacity)

`trade.js:183-184`:

```css
.hb-m-side-btn.active-yes { background: #fff; color: #0d0d0d; ... }
.hb-m-side-btn.active-no  { background: #333; color: #fff; ... }
```

`#333` vs `#fff` is monochrome; OK. But the inactive state has `color: #a3a3a3` and on activation it does a fill swap — that is bg-shift, not opacity. Same problem as table rows, just on toggle buttons.

### V10 — `market_detail.html` has 23 inline `style="..."` attributes

The narve-design rule "❌ Write `style="..."` inline" is broken throughout this template. Grep'd matches (lines): 24, 26, 28, 32, 33, 34, 35, 37, 48, 55, 56, 60, 63, 66, 68, 70, 73, 75, 77, 78, 81, 86, 88 — and many more inside the `<script>`-injected HTML strings (lines 389, 392-396, etc.). Per spec, only `style="--accent: #..."` is allowed as a per-card override.

### V11 — `market_detail.html` has raw px / em type sizing inline, breaking the token scale

- Line 32: `font-size:12px`
- Line 70, 149: `font-size:13px`
- Line 166: `font-size:11.5px` (not even on the px grid)
- Line 389: `font-size:13px`
- Line 391: `font-variant-numeric: tabular-nums` (good behaviour, but inlined instead of `class="num"`)

Sizes must use `--text-xs` … `--text-5xl` per narve-design.

### V12 — `market_detail.html` forecast bar inline-styles are doing what a class should do

Lines 389-396 inline-build a 3-column grid with hardcoded `130px 48px 1fr`, `gap:10px`, etc. This belongs in `gateway.css` or `components.css` as `.forecast-bar-row`. Inline `<style>`-string concatenation is the anti-pattern; the audit specifically calls out "❌ Add a `<style>` block in a page template (extend `gateway.css` or component CSS instead)".

### V13 — `market_detail.html`: emoji `✓` and `✗` in user-facing chrome

Line 275: `'in your favor ✓' : 'against you ✗'`. The narve rule: "No emoji in user-facing UI chrome". Resolution status is exactly the kind of chrome message that should be text + position, not glyph.

### V14 — `trade.js`: environmental-impact leaf emoji `&#127807;` in production UI

`trade.js:599-601` and `:677`. Emoji in chrome. Replace with a text indicator or border treatment.

### V15 — `trade.js` table cells use `style="max-width:320px"` and `style="color:#666"` inline

Lines 604, 607, 609, 611. Inline styles for typography/colour are explicitly forbidden.

### V16 — `market_detail.html` forecast chart grid uses raw rgba

Line 453, 458: `grid: { color: 'rgba(0,0,0,0.06)' }`. Hardcoded — should pull from `--border-ghost` or `--border-subtle` via a CSS variable read.

### V17 — `market_detail.html` filter/sort buttons in the Takes toolbar (`lines 108-122`) are token-good but the inline styles next to them (98, 99, 103-104) reset that progress

Mixed pattern. The component CSS lives elsewhere, but the heading + counter `style="color:var(--text-tertiary);font-weight:400"` is inline — should be a class.

### V18 — `shared_market.html` is mostly clean, but lacks tabular-nums on `.metric-value`

The file is 49 lines and reads tokens through `pages/shared_market.css`. Visually clean. One concern: `<div class="metric-value">{{ market_probability }}</div>` (line 30) — depending on what shared_market.css does, the probability could render in Inter without tabular-nums. Quick read of the file shows the data is a percentage; needs `font-variant-numeric: tabular-nums` and right-align if it ever appears in a row with siblings. Currently a single metric column, so right-align is moot — but tabular-nums is still required so digits don't jitter on refresh.

(Not loaded the linked CSS file in this audit — flagged as "verify".)

### V19 — `settings_trading_addon.html` numeric inputs are not styled as monospace

The page collects numbers (`#ta-max-cap`, `#ta-daily-cap`, `#ta-max-position`, `#ta-cooldown`, `#ta-auto-execute-min-ev`). All `<input type="number">`. None marked with `class="num"` or any monospace-applying class. As typed digits appear in Inter, ASCII-proportional — digits jitter.

This is a softer violation (inputs aren't tables) but the audit rule says **numbers** right-aligned monospace, full stop. Spinners on right-aligned numeric inputs are a known Safari issue; right-align is debatable for UX, but **monospace digits inside the input** are unambiguously correct.

### V20 — `settings_trading_addon.html` has 3 inline `style=` on the breadcrumb anchors

Lines 25, 27, 192: `style="text-decoration:none;color:inherit"`. This pattern is copy-pasted across 50+ templates. Should be a `.breadcrumb-item__plain` class.

### V21 — `market_detail.html` breadcrumb anchors have the same inline-style copy-paste

Lines 24, 26. Identical to V20.

### V22 — `trade.js` table rows DO carry pointer cursor (`cursor: pointer` line 132) but the affordance hint conflicts with narve's "drag/select for power users" tables

The Markets and Portfolio tables open a detail panel on **any** cell click (line 603 `onclick="window.__hbTrade.openDetail(...)"`, line 1017-1021). That makes the row click-greedy and prevents text selection of e.g. a market title — a density / power-user pattern problem. narve's density rule says "design for power users… open the app daily". They will want to copy a market title. Soft violation; flag for product, but it traces back to the same hover-treatment issue: visual treatment promises "click anywhere".

### V23 — `trade.js` button hover uses `transform: translateY(-1px)`

Line 116: `.hb-m-btn:hover { transform: translateY(-1px); }`. narve-design rule: "No animations beyond opacity, transform-translate, and width/height transitions" — translate IS allowed, so this is technically fine. **But** the spec for hover here is opacity (per this audit's hard rule on table rows). For visual consistency on the same overlay, button hover should also be opacity ramp, not a lift.

(Marginal — listing for completeness; could be dropped if the rule is strictly tables-only.)

### V24 — Service-worker / pwa_middleware injection note

`market_detail.html` and `settings_trading_addon.html` correctly let pwa_middleware inject sidebar / sitemap. Good. `trade.js` overlay, however, mounts as `position:fixed; z-index:10000` on top of everything — it bypasses the app-shell entirely, which is **why** none of the canonical `.app-shell table` rules apply. That is the architectural root cause of V1–V6.

---

## Top 3 (most damaging, fix first)

1. **`trade.js` is a 1692-line styling silo that bypasses gateway.css entirely.** Every table rendered by the Markets / Portfolio / Orders overlay uses raw hex colours, Inter-sans for numbers, roomy 14px padding, and a `background` shift on hover. Until this overlay is refactored to use `.dash-table` / `.num` and the design tokens, every other audit fix is cosmetic. This single file accounts for ~15 of the 24 violations (V1–V9, V14, V15, V22, V23).

2. **Canonical `gateway.css` table-hover (`gateway.css:1930-1933`) uses `background: var(--interactive-ghost)`, not opacity.** This violates the audit's hard rule across every in-app table site-wide, not just markets/trade. Fixing the rule once (swap to `opacity: 0.92` on hovered row, or a ::before overlay) propagates to admin, predictions, leaderboard, calendar, audit log, and the trading-addon settings cards.

3. **`market_detail.html` is dense with inline `style="..."` (23 attrs in static markup, more in JS-rendered strings) including raw px font sizes, raw rgba grid colours, and the `✓`/`✗` emoji in chrome.** This is the kind of file that drifts further on every commit because there is no class layer to add to. Promote the inline patterns to `components.css` classes (`.market-meta`, `.forecast-bar-row`, `.takes-heading-count`), strip emoji.

---

## Notes / gaps

- The user requested `portfolio*.html`, `trade*.html`, `markets*.html` — none of those file globs actually exist in `gateway/static/`. The closest surfaces (and only ones reasonably auditable for "portfolio / trade / markets") were used. If a real `portfolio.html` template is being added in a parallel session, this audit should be re-run.
- `shared_market.html` depends on `pages/shared_market.css` which was not opened — V18 is a "verify" not a confirmed break.
- `trade.js` is JS, not HTML, but it ships the only portfolio table in the product. Excluding it would make the audit answer "no portfolio tables to audit" — which is wrong.
