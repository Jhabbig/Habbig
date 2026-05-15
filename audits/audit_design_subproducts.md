# Subproduct landing pages — design audit

**Scope.** Audit the subproduct landing pages and `/dashboards` hub against the
narve-design skill spec. The spec requires: italic Instrument Serif hero
headline, monospace stat pills, accent dot colour pulled from `config.json`,
floating-numbers atmosphere disabled on `prefers-reduced-motion`, three stat
pills, pricing card with bundle math.

**Files in scope (actual filenames in the repo).**

- `/Users/shocakarel/Habbig/gateway/static/subproduct_landing.html` — the single
  template that renders every subdomain landing (whale, voters, climate, etc.).
- `/Users/shocakarel/Habbig/gateway/static/pages/subproduct_landing.css` —
  page-specific styling for the subproduct hero, pills, tabs, pricing.
- `/Users/shocakarel/Habbig/gateway/static/dashboards.html` — the `/dashboards`
  hub page (the six/thirteen subproduct cards). The brief asked for
  `dashboard.html`; that filename does **not** exist. The actual hub is
  `dashboards.html`.
- `/Users/shocakarel/Habbig/gateway/static/pages/dashboards.css` — page-
  specific styling for the hub grid + cards.
- `/Users/shocakarel/Habbig/gateway/subproduct.py` — catalogue + landing-
  context builder (stat-pill templates, animation-style mapping).
- `/Users/shocakarel/Habbig/gateway/server.py:3244–3415` — `_render_subproduct_landing`,
  `_format_stat_pills`, `_format_hero_headline` — server-side wiring.
- `/Users/shocakarel/Habbig/gateway/config.json` — per-dashboard accent hex.
- `/Users/shocakarel/Habbig/gateway/static/tokens.css` — `--font-display`,
  `--font-mono`, `--font-body` token definitions.
- `/Users/shocakarel/Habbig/gateway/static/narve-redesign.css:82–182` — hub
  card overrides (10×10 dot, full-width black CTA).
- `/Users/shocakarel/Habbig/gateway/pwa_middleware.py:107–143` — site-wide
  injected `<head>` block (preloads, narve-redesign.css, narve-polish.css).

The brief asked for `subproduct-*.html` variants. None exist — every
subdomain renders through the single `subproduct_landing.html` template
with server-side substitution of slug, accent, hero copy, pills, and
animation style. That is itself spec-compliant (single source of truth),
so the audit treats the one template as covering all 13 subproducts.

---

## Spec checklist

| # | Spec rule | Where checked | Pass? |
|---|---|---|---|
| 1 | Italic Instrument Serif hero headline (`--font-display`, `font-style:italic`) | `subproduct_landing.css:153–162`; `tokens.css:51–58, 252` | PASS |
| 2 | Hero headline size ~72–80 px desktop / 48–56 px mobile | `subproduct_landing.css:157` uses `--text-5xl` (72 px) desktop, `--text-4xl` (48 px) mobile via the 900 px breakpoint | PASS |
| 3 | Hero subtitle Inter 16 px, `--text-secondary`, max-width ~480 px | `subproduct_landing.css:174–181` — `--font-body` not `--font-ui`, max-width 540 px not 480 px | PARTIAL — uses body serif (Source Serif 4) not Inter as spec implies; 540 vs 480 max-width is within tolerance |
| 4 | Monospace stat pills (numbers in Geist Mono, labels in Inter) | `subproduct_landing.css:206–216`; `server.py:3347–3370` splits labels from `{placeholder}` values; `--font-mono` on `.sp-pill-num`, `--font-ui` on `.sp-pill-label` | PASS |
| 5 | Three stat pills | Every entry in `subproduct.py SUBPRODUCTS` has exactly 3 entries in `stat_pills` — verified for all 13 slugs | PASS |
| 6 | Accent dot colour comes from `config.json` | `server.py:3299` reads `DASHBOARDS.get(dashboard_key)["accent"]`; injected as `--sp-accent` on `<body>` (`subproduct_landing.html:24`); applied to the 10×10 dot at `subproduct_landing.css:136–143` | PASS |
| 7 | 10×10 accent dot (the **only** hue on the page) | `subproduct_landing.css:138–139` (10 px × 10 px); `narve-redesign.css:117–124` enforces 10 px globally on hub cards as well | PASS for the dot itself; see violation #1 about a secondary use of `--accent` on the dot's border on the hub cards |
| 8 | Floating-numbers atmosphere right of hero, 15 spans, scattered positions | `subproduct_landing.html:50–52`; `subproduct_landing.css:246–260` defines exactly 15 nth-child slots | PASS |
| 9 | Animation style per slug — six rhythms (flicker, drift, tectonic, frenetic, measured, pulse) | `subproduct.py` `animation_style` field; `subproduct_landing.css:262–323` defines the six keyframe sets | PASS — but 8/13 slugs share `measured`, so the "one per family" guidance is loosely held |
| 10 | Disabled on `prefers-reduced-motion` | `subproduct_landing.css:325–327` → `animation: none !important; opacity: 0.5;` | PASS |
| 11 | Hidden on small phone (the spec says ≤ 520 px for the apex landing; subproduct landing chose 480 px) | `subproduct_landing.css:230–232` → `display: none` at ≤ 480 px | PARTIAL — uses 480 px not 520 px. Below 480 px the numbers vanish entirely, which is fine; the band between 481–520 px shows the atmosphere on a narrow phone where the spec wanted it suppressed. Cosmetic. |
| 12 | Pricing card with both individual and Pro bundle math | `subproduct_landing.html:64–99`; `server.py:3291–3293` computes `bundle_sum_gbp` from the live catalogue and `bundle_save_gbp = max(0, sum − 180)` | PASS |
| 13 | `narve.ai Pro · £180/mo` bundle figure | `subproduct_landing.html:86` hardcodes `£180`; `server.py:3293` also hardcodes the literal `180.0` | PARTIAL — hardcoded as a number constant in two places, not in `config.json` alongside the per-product prices. If Pro price ever changes, two edits required. |
| 14 | Three-typeface rule (Inter + Geist Mono + Instrument Serif), nothing else | `tokens.css:249–253` declares **four** font-family tokens: `--font-body` (Source Serif 4), `--font-ui` (Inter), `--font-display` (Instrument Serif), `--font-mono` (Geist Mono). The site-wide `--font-body` is Source Serif 4. | PARTIAL — see violation #2 below. The Source Serif 4 body face has been deliberately added (PWA middleware preloads it; comment in tokens.css:65–82 documents the choice). It is a fourth typeface even if used only for body prose. The narve-design skill explicitly says "three typefaces total". |
| 15 | Wordmark per subdomain: `narve.ai` italic + `/` divider in default + slug in Geist Mono | `subproduct_landing.html:28–30`; CSS `subproduct_landing.css:60–88` — apex italic display, divider Inter `--text-tertiary`, slug mono | PASS |
| 16 | Tabs use Inter labels with monochrome underline-on-active | `subproduct_landing.css:345–373` — Inter, `--text-tertiary` default, `--text-primary` + 2 px underline on `aria-selected=true` | PASS |
| 17 | Tabs and pricing on the same base background, no inset shells | `subproduct_landing.css:329–443` — borders only on top via `--border-subtle`; pricing cards have one border + dashed for bundle | PASS |
| 18 | "Also on narve.ai" cross-link bar — five items, no accent reuse | `server.py:3308–3319` picks 5 random other slugs; `subproduct_landing.css:567–624` — borders + type only, no accent | PASS |
| 19 | Footer: `narve.ai · Terms · Privacy · DPA · Support`, 11 px, `--text-tertiary` | `subproduct_landing.html:108–118`; `subproduct_landing.css:628–657` — `--text-xs` (11 px), `--text-tertiary` | PASS |
| 20 | Theme cookie (`narve-theme`) read inline before paint to avoid FOUC | `subproduct_landing.html` has **no inline theme-boot `<script>`**. `dashboards.html:22` does have one. The middleware injects narve-redesign but not the cookie reader. | FAIL — see violation #3 |
| 21 | Hub card chrome: 3 px accent top-bar, 10×10 dot, "Active"/"Locked" badge, title + desc + price, black CTA | `dashboards.css:80–202`; `narve-redesign.css:82–182`; `server.py:4071–4084` matches the spec exactly | PASS for structure |
| 22 | Hub card description capped to 2 lines, body in serif | `narve-redesign.css:145–155` line-clamps to 2; `dashboards.css:173–179` uses `--font-body` | PASS |
| 23 | Hub card "Open ↗" / "Unlock →" full-width black CTA | `narve-redesign.css:163–177` — `background: var(--text-primary)`, `text-align: center`. Inter via `dashboards.css:188–190` | PASS |
| 24 | Hub grid collapses to single column at small viewports, 2 then 3 cols up | `dashboards.css:126–148` — 1 col default, 2 at ≥ 720 px, 3 at ≥ 1100 px; conflicting upstream `narve-redesign.css:82–87` sets `auto-fill min(100%, 280px)` | PARTIAL — `dashboards.css` is `body .page-frame .dash-grid` which beats `narve-redesign.css body .dash-grid` on specificity, so the explicit 1/2/3 wins on `/dashboards`. Anywhere else `.dash-grid` is used (rare), the auto-fill rule applies. Cosmetic. |
| 25 | Per-page hero `<title>`, OG card, twitter card, canonical link | `subproduct_landing.html:6–16` — all present, slug-templated | PASS |
| 26 | Subproduct count for "All {n} sub-products" text | `server.py:3335` uses `len(_SP)` (currently 13); `subproduct_landing.html:84` interpolates as `subproduct_count` | PASS — drifts from the user's memory note "12 active subproducts"; double-check whether one of the 13 in `subproduct.py` is meant to be hidden |

---

## Violations

Eight items did not pass cleanly: three failures or partial-fails serious
enough to call out, plus five minor partials documented inline in the table
above. Top three by severity:

### 1. Source Serif 4 introduced as a fourth typeface site-wide

**Spec rule.** "Three typefaces total. Never anything else." (narve-design
skill).

**Reality.** `tokens.css:249` defines `--font-body: "Source Serif 4", Georgia,
"Times New Roman", serif;`. `gateway.css:41` makes Source Serif the default
on `<body>` site-wide. `pwa_middleware.py:131–134` preloads it. The CSS for
the subproduct landing uses `--font-body` for the hero sub
(`subproduct_landing.css:177`), pricing taglines (`:481`), and tab card prose
(`:407`). The hub card description on `/dashboards` also uses `--font-body`
(`dashboards.css:175–176`). Three confirmed surfaces, plus most prose
across the site.

This is a real divergence from the narve-design skill's hard rule. Source
Serif's adoption is documented (`tokens.css:65–82` and `pwa_middleware.py:126–134`
both contain explanatory comments), so it is deliberate, not accidental.
Either the skill text needs updating to call body-Source-Serif a sanctioned
fourth face, or these references should be folded back to Inter. The
adoption appears deliberate enough that updating the skill is the likely
correct move; flagging here because the file-level audit cannot judge.

**Files.** `tokens.css:249–250`, `gateway.css:38–41`,
`pwa_middleware.py:126–134`, `subproduct_landing.css:177, 407, 481`,
`dashboards.css:175–176`.

### 2. No inline theme-cookie reader on the subproduct landing — FOUC on dark-mode visitors

**Spec rule.** "Anti-FOUC inline script in every page reads `narve-theme`
cookie before paint." (narve-design skill, "Light + dark themes are both
first-class").

**Reality.** `dashboards.html:22` has the inline boot block:
`<script>(function(){try{var m=document.cookie.match(/narve-theme=...) …;
document.documentElement.setAttribute("data-theme",t); …})();</script>`.

`subproduct_landing.html` has **no equivalent**. A dark-mode visitor lands
on `whale.narve.ai` and gets a flash of light theme (`tokens.css:95–96`
sets `:root` → light by default) until the deferred `theme.js` or
`narve-app.js` runs and applies `data-theme="dark"`. The PWA middleware
injects the redesign CSS but does not inject this inline cookie reader.

**Files.** `subproduct_landing.html` (between lines 22 and 23, where the
boot should sit), versus `dashboards.html:22`.

### 3. Pro-bundle price (`£180`) hardcoded in two places, not in `config.json`

**Spec rule.** "Pricing card with both individual and Pro bundle math."
This is not strictly a token rule, but narve-design's "Tokens, never
hardcoded values" plus the comment on `subproduct.py:33–37` ("the main
apex bundle (narve.ai Pro) continues to price from `config.json
monthly_cents`") imply Pro pricing should also be data-driven.

**Reality.** `subproduct_landing.html:86` hard-codes `£180`. `server.py:3293`
hard-codes the bundle threshold: `bundle_save_gbp = max(0.0, bundle_sum_gbp
- 180.0)`. If marketing decides Pro is £200 or £150, both spots need
editing in lock-step, and the per-page template can drift from the bundle-
math computation in `server.py`. There is no `pro_price_gbp` field in
`config.json` (`config.json:1–9` has `domain`, `gateway_port`,
`cookie_secret_env` — nothing about bundle pricing).

**Files.** `subproduct_landing.html:86`, `server.py:3293`, missing entry in
`config.json`.

---

## Other findings (not in top 3)

- The 480 px breakpoint at `subproduct_landing.css:230–232` hides floating
  numbers on phones smaller than that. The pre-release landing spec uses
  520 px. Minor inconsistency.
- `subproduct_landing.css:177` sets hero subtitle `max-width: 540px`; spec
  says ~480 px. Within tolerance, calling out for the record.
- 13 subproducts in `subproduct.py`, but user memory note says 12 are
  active. `subproduct_count` interpolates `len(_SP)`, so the pricing card
  reads "All 13 sub-products". If one is intended hidden,
  `subproduct.py` should mark it `hidden=True` and the renderer should
  filter.
- Animation styles: only 6 distinct keyframes; 8 of 13 slugs share
  `measured` (climate, voters, world_health, etc.). Spec says "six
  rhythms, one per subproduct family" — code comment matches but the
  effect is that most subdomains animate identically.
- Hub-card accent dot has a `1px solid color-mix(…, var(--accent) 70%, …)`
  border (`narve-redesign.css:123`). The dot is the sanctioned hue, but
  the spec wording "nothing else" arguably excludes a hue-derived border
  too. Cosmetic.

---

## Summary

**Violations counted (table rows that did not pass cleanly): 8 of 26
(3 FAIL/severe partials, 5 minor partials).**

**Top 3.**

1. Source Serif 4 is a deliberate fourth typeface, in tension with the
   skill's "three typefaces total" hard rule. Either the skill needs
   updating to sanction body-Source-Serif, or every `--font-body`
   reference on the subproduct landing + hub returns to `--font-ui`.
2. `subproduct_landing.html` has no inline theme-cookie reader, so dark-
   mode visitors land with a flash of light theme until deferred JS
   applies the saved theme. Copy the `<script>` block from
   `dashboards.html:22` into the `<head>` of `subproduct_landing.html`.
3. Pro bundle price `£180/mo` is hardcoded in `subproduct_landing.html:86`
   and `server.py:3293`. Move to `config.json` (e.g. `pro_monthly_gbp:
   180`) and pass through `_render_subproduct_landing` so both the
   display string and the `bundle_save_gbp` computation share one
   source.

No code was changed by this audit.
