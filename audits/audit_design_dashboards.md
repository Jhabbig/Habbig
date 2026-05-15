# `/dashboards` hub — design audit

**Scope.** Audit the `/dashboards` hub landing page (HTML + page-scoped CSS
+ shared CSS that paints the same cards) against the narve-design skill's
"Hub card" pattern. The spec requires:

> Every card is structurally identical. Only variation: accent dot colour
> from `config.json` (red, blue, purple, amber, indigo, sky). Layout:
>
> ```
> [3px accent top-bar via ::before]
> [head: 10×10 accent dot] [badge "ACTIVE" or "LOCKED"]
> [title 18px / 600]
> [description 13px / 400 text-secondary]
> [price 12px / 400 text-secondary]
> [foot: black "Open →" CTA, 100% width]
> ```
>
> Don't add a 4th text row. Don't change the dot size. Don't add hover lift.

The skill also enforces the global rules: three typefaces, monochrome,
no decorative chrome, token-only values, AA contrast.

## Files in scope

- `/Users/shocakarel/Habbig/gateway/static/dashboards.html` — page template
  (138 lines). Renders sidebar, breadcrumb, hero, changelog widget, and the
  `{{ dashboard_cards }}` slot.
- `/Users/shocakarel/Habbig/gateway/static/pages/dashboards.css` — page-
  specific stylesheet (228 lines). Loaded last in the template `<head>` so
  its `body .page-frame …` selectors out-specificity `narve-redesign.css`.
- `/Users/shocakarel/Habbig/gateway/static/narve-redesign.css:80–183` —
  shared `.dash-card`, `.dash-accent-dot`, `.dash-card-action` rules
  (injected site-wide by `pwa_middleware`).
- `/Users/shocakarel/Habbig/gateway/static/gateway.css:270–461` — upstream
  `.dash-card` baseline (the page CSS + redesign override most of it; the
  hover-lift in here is the regression target).
- `/Users/shocakarel/Habbig/gateway/server.py:4046–4084` — server-side
  card render loop. The actual HTML for each card is built here, not in
  the template.
- `/Users/shocakarel/Habbig/gateway/config.json:13–135` — 13 dashboard
  entries with their `accent` hex.

The brief said "hub-cards landing per narve-design hub-card pattern". This
audit treats `dashboards.html` + `pages/dashboards.css` as the in-scope
surface, plus the shared CSS that paints the cards (otherwise the page
audit can't catch the hover-lift or dot-size questions).

---

## Spec checklist

| # | Spec rule | Where checked | Pass? |
|---|---|---|---|
| 1 | Every card structurally identical (same DOM tree) | `server.py:4071–4084` — single f-string loop, no branch that alters the element tree; locked vs active changes class names and the badge/action text only | PASS |
| 2 | Accent dot colour is the only varying brand hue | `server.py:4072` injects `style="--accent: {cfg['accent']}"` per card | PASS for structure; FAIL for "six brand colours" — see violation #1 |
| 3 | 3 px accent top-bar via `::before` | `narve-redesign.css:99–105` — `height: 3px; opacity: 1; border-radius: var(--radius-lg) var(--radius-lg) 0 0;` overrides the 2 px / 0.85 opacity rule in `gateway.css:312–321` | PASS |
| 4 | 10×10 accent dot, no halo | `narve-redesign.css:117–124` enforces `width: 10px; height: 10px; box-shadow: none;`. `dashboards.css:199–202` re-asserts the same values. `gateway.css:343–349` ships 8×8 + glow but loses on specificity. | PASS |
| 5 | Head row: dot + badge ("Active" or "Locked") | `server.py:4073–4076`; `gateway.css:336–341` flex row | PASS |
| 6 | Title 18 px / 600 | `dashboards.css:162–169` — `font-size: var(--text-lg, 18px); font-weight: 600;` | PASS |
| 7 | Description 13 px / 400 text-secondary | `dashboards.css:173–179` — `font-size: var(--text-sm, 14px)` and `color: var(--text-secondary)`. `narve-redesign.css:145–155` line-clamps to 2 lines. Spec says 13 px; both files use `--text-sm` (14 px per `tokens.css`). | PARTIAL — see violation #3 |
| 8 | Price 12 px / 400 text-secondary | `narve-redesign.css:156–162` — `font-size: var(--text-xs); color: var(--text-tertiary)`. `dashboards.css:183–185` confirms monospace. Spec says `text-secondary`; reality is `text-tertiary` (one rung dimmer). | PARTIAL — see violation #4 |
| 9 | Foot: black "Open ↗" / "Unlock →" CTA, full-width | `narve-redesign.css:163–177` — `background: var(--text-primary); color: var(--bg-base); text-align: center;`. **But** `gateway.css:387–410` keeps `.dash-card-foot` as a `display: flex; justify-content: space-between` row with the price on the left and the action on the right, so the CTA does NOT span 100 % of the card width. | FAIL — see violation #2 |
| 10 | No 4th text row | `server.py:4077–4082` renders exactly title + desc + price + action. The price + action share the foot flex row; no additional text node. | PASS structurally — although the foot composition (price beside action) is itself a deviation from the spec illustration which puts the CTA on its own row. Documented under violation #2. |
| 11 | No hover lift | `narve-redesign.css:106–116` explicitly kills `transform` and `box-shadow` and only widens the top bar to 4 px. `gateway.css:323–329` `translateY(-2px)` is overridden. | PASS |
| 12 | No hover lift fallback in page CSS | `dashboards.css` has no `:hover` rules of its own (good — no re-introduction) | PASS |
| 13 | Don't change the dot size | `dashboards.css:199–202` re-asserts 10×10; nothing scales it via media queries | PASS |
| 14 | Single template path: all cards rendered by the same loop | `server.py:4047–4084` — one f-string template, no per-key conditional that emits a different element subtree | PASS |
| 15 | Grid: 1 col mobile, 2 col ≥720 px, 3 col ≥1100 px | `dashboards.css:126–148` | PASS — beats the `auto-fill, minmax(280px, 1fr)` rule from `narve-redesign.css:82–87` on specificity |
| 16 | Hero: Instrument Serif Italic, 36–56 px clamp | `dashboards.css:93–105` — `font-family: var(--font-display); font-style: italic; font-size: clamp(36px, 5.5vw, 56px)` | PASS |
| 17 | Hero lede in body face, 16 px | `dashboards.css:106–113` — `--font-body` (Source Serif 4), `--text-md, 16px` | PASS (Source Serif 4 is a documented fourth face; see violation #5) |
| 18 | Breadcrumb chips Inter, small, uppercase | `dashboards.css:43–77` | PASS |
| 19 | Footer/status bar with Terms · Privacy · DPA · Support | `dashboards.html:98–105`; `dashboards.css:205–217` | PASS |
| 20 | Inline theme/density boot script before paint (anti-FOUC) | `dashboards.html:22` reads cookies + localStorage for theme + density | PASS |
| 21 | No `<style>` blocks in template | `dashboards.html` has only `<script>` and `<link rel="stylesheet">`; no inline `<style>` blocks | PASS |
| 22 | No raw hex / px outside tokens | All numeric values in `dashboards.css` reference `var(--space-*)`, `var(--text-*)`, `var(--radius-*)`, `var(--font-*)`. The only literal pixel values are the dot dimensions (`10px`, `10px`), which are spec-mandated, and a `4px 10px` breadcrumb chip padding that should be tokens. | PARTIAL — see violation #6 |
| 23 | Active/Locked badge in Inter (chrome, not prose) | `dashboards.css:193–195` — `font-family: var(--font-ui)`. `narve-redesign.css:125–136` styles the badge with `--font-mono` (uppercase mono pill). Two rules disagree; page CSS wins on specificity for `body .page-frame .dash-card .badge`, so the badge renders in Inter here even though everywhere else the same `.badge` class is Geist Mono. | PARTIAL — see violation #7 |
| 24 | Card body face is the editorial serif (`--font-body`) | `dashboards.css:152–158` and `:173–179` | PASS |
| 25 | Card title and CTA face is `--font-ui` (Inter) | `dashboards.css:162, 188–195` | PASS |
| 26 | Card price face is `--font-mono` (Geist Mono) | `dashboards.css:183–185` | PASS |
| 27 | Focus-visible ring | `dashboards.css:220–228` — `outline: 2px solid var(--focus-ring, var(--text-primary)); outline-offset: 2px;` | PASS |
| 28 | All cards open in a new tab (active) / route to billing (locked) | `server.py:4056–4069` — active cards get `target="_blank" rel="noopener"`; locked cards drop both attrs and link to `/billing?dashboard={key}` | PASS — semantic, structural HTML difference is the `target` attribute and `href`, not the element tree |
| 29 | Card grid wrapped in `.stagger` for the canonical reveal animation | `dashboards.html:90` — `<div class="dash-grid stagger">` | PASS — `scroll-animations.css:56–61` defines 6 delay slots; with 13 dashboards in `config.json`, cards 7-13 get no transition-delay (default 0 ms), so they reveal simultaneously after the first six stagger. Cosmetic. |
| 30 | "What's new" widget loads `hidden` and only un-hides after fetch (so the dash-grid never paints behind a flash of empty space) | `dashboards.html:70–88` — `hidden` attribute on the wrapper; `data-changelog` hook | PASS |

---

## Violations

Seven items did not pass cleanly. Top three by severity:

### 1. `config.json` ships **13** accent colours, spec sanctions **6**

**Spec rule.** "Accent dots on dashboard hub cards are the only sanctioned
hue use — six brand colours from `config.json`, only on a 10×10 dot,
nothing else."

**Reality.** `gateway/config.json` defines 13 dashboard entries with the
following `accent` hexes (line numbers in parentheses):

| Dashboard | Accent | Spec colour? |
|---|---|---|
| (15) | `#ef4444` | red — yes |
| (25) | `#3b82f6` | blue — yes |
| (35) | `#8b5cf6` | purple — yes |
| (45) | `#f59e0b` | amber — yes |
| (55) | `#6366f1` | indigo — yes |
| (65) | `#0ea5e9` | sky — yes |
| (75) | `#10b981` | emerald — **new** |
| (85) | `#a855f7` | violet — **new** |
| (95) | `#34d399` | mint — **new** |
| (105) | `#22c55e` | green — **new** |
| (115) | `#14b8a6` | teal — **new** |
| (125) | `#10b981` | emerald (dup) — **new** |
| (135) | `#ec4899` | pink — **new** |

Seven colours past the sanctioned palette. Two entries share `#10b981`, so
13 dashboards across 12 distinct hues. Either the skill text predates the
expansion and needs updating, or `config.json` needs its palette pulled back
to the six listed. The audit cannot judge intent; flagging as the largest
single divergence from the hub-card spec.

### 2. Foot composition is two columns (price + CTA on one row), not a 100 %-wide black CTA

**Spec rule.** Layout illustration ends with:

```
[price 12px / 400 text-secondary]
[foot: black "Open →" CTA, 100% width]
```

Two distinct rows: price above, then the CTA on its own line spanning the
full card width.

**Reality.** `server.py:4079–4082` emits both `.dash-card-price` and
`.dash-card-action` inside a single `.dash-card-foot` element:

```html
<div class="dash-card-foot">
  <span class="dash-card-price">£X/mo · £Y/yr</span>
  <span class="dash-card-action">Open ↗</span>
</div>
```

`gateway.css:387–395` styles the foot as
`display: flex; align-items: center; justify-content: space-between;` —
price hugs the left, action hugs the right. The action is an inline-flex
`<span>` (`gateway.css:402–410`), not a 100 %-wide button.

`narve-redesign.css:163–177` makes `.dash-card-action` look like a button
(`background: var(--text-primary); color: var(--bg-base); padding: 10px
14px; border-radius: var(--radius-md); text-align: center;`), but because
its parent is a flex row sharing a line with the price, the CTA only spans
its own intrinsic width — typically ~80 px for "Open ↗" / "Unlock →".

This is the load-bearing structural deviation: the spec literally shows
the CTA on its own row, full-width; the implementation puts it on a shared
line with the price. To fix, either:

1. Drop the price into its own row above the foot (the foot becomes just
   the CTA, the foot rule changes to `display: block; width: 100%;`), or
2. Stop styling the CTA as a button and ship a plain "Open ↗" affordance
   per the original `gateway.css` design — but the redesign comment at
   line 164 explicitly wants the black-bar CTA.

The audit notes a contradiction: `narve-redesign.css:163–177` says
"black bar CTA, full-width, confident" in the comment, but the rendered
HTML can never be full-width inside the existing foot flex row.

### 3. Card description uses `--text-sm` (14 px), spec says 13 px

**Spec rule.** `[description 13px / 400 text-secondary]`

**Reality.** `dashboards.css:175` and `narve-redesign.css:146` both set
`font-size: var(--text-sm)`, which `tokens.css` resolves to 14 px. There
is no `--text-13` token; the closest hop down is `--text-xs` (12 px,
already used by the price). The 1 px overshoot is cosmetic — desc reads
the same as the rest of the prose on the card.

---

### Other partial fails (documented for completeness)

**4. Price colour is `--text-tertiary`, spec says `text-secondary`.**
`narve-redesign.css:159` ships `color: var(--text-tertiary)`. The price
sits one rung dimmer than the description, which the spec did not ask
for. Cosmetic.

**5. Source Serif 4 is a documented fourth typeface site-wide.**
`tokens.css:249` defines `--font-body: "Source Serif 4"`. The hero lede
(`dashboards.css:106–113`) and the card description (`:173–179`) both
use it. The narve-design skill says "three typefaces total" — same
finding as in `audit_design_subproducts.md` violation #1; either fold
back to Inter for body prose or update the skill text. (Reference, not
new.)

**6. Two raw-pixel literals in `dashboards.css`.** Line 59 sets
`padding: 4px 10px;` on breadcrumb chips, and lines 200–201 set
`width: 10px; height: 10px;` on the accent dot. The dot pixels are
spec-mandated (the only place the skill cites a literal "10×10 px").
The breadcrumb chip padding should be tokens (`var(--space-1)` and
`var(--space-2)` are the natural fit), but it's cosmetic.

**7. Badge typeface conflict between page CSS and shared CSS.**
`dashboards.css:193–195` puts the badge in `var(--font-ui)` (Inter).
`narve-redesign.css:125–136` puts it in `var(--font-mono)` (Geist Mono)
with `text-transform: uppercase; letter-spacing: 0.06em`. The page CSS
wins on specificity, so the badge on `/dashboards` is Inter, while the
identically classed `.badge` element on every other page is mono. Two
files disagreeing about the same class is a maintenance smell; one
should win site-wide. Pick the mono version — it's the only place the
"ACTIVE" / "LOCKED" reading clearly distinguishes a chrome label from
the surrounding serif prose.

---

## Summary

- **Total spec items checked:** 30
- **Strict PASS:** 22
- **Violations (failures or partials worth fixing):** 7 + 1 cosmetic stagger
  observation that does not warrant a fix
- **Hard rules from skill specifically checked:**
  - "Every card structurally identical" — PASS
  - "Only variation: accent dot colour" — PASS structurally; FAIL on
    palette breadth (violation #1)
  - "Don't add a 4th text row" — PASS (foot is one row, not two)
  - "Don't change the dot size" — PASS
  - "Don't add hover lift" — PASS (redesign kills the gateway.css lift)
- **Most material structural deviation:** the foot row puts the CTA beside
  the price, so the "100 %-wide black CTA" spec illustration is not what
  ships (violation #2). This is the single best fix to land if only one
  change is in scope.

## Top three violations

1. **`config.json` ships 13 accent colours; spec sanctions 6.** The hub
   has expanded beyond what narve-design documents. Either widen the
   skill or trim the palette.
2. **CTA is not full-width.** The price and action live on the same
   `.dash-card-foot` flex row, so the "black bar, 100% width" the
   spec illustration shows never actually renders.
3. **Description is 14 px, spec says 13 px.** Cosmetic but a direct
   spec mismatch; either add a `--text-13` token or restate the spec.

## Files referenced

- `/Users/shocakarel/Habbig/gateway/static/dashboards.html`
- `/Users/shocakarel/Habbig/gateway/static/pages/dashboards.css`
- `/Users/shocakarel/Habbig/gateway/static/narve-redesign.css` (lines 80–183)
- `/Users/shocakarel/Habbig/gateway/static/gateway.css` (lines 270–461)
- `/Users/shocakarel/Habbig/gateway/static/scroll-animations.css` (lines 56–61)
- `/Users/shocakarel/Habbig/gateway/server.py` (lines 4046–4084)
- `/Users/shocakarel/Habbig/gateway/config.json` (lines 13–135)
- `/Users/shocakarel/.claude/skills/narve-design/SKILL.md` (lines 154–167 — hub-card pattern)
