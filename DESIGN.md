# narve.ai Design Language — "Editorial Light"

Single source of truth for every dashboard UI. Derived from the landing page (`gateway/static/landing.html`); enforced fleet-wide by the gateway-injected `gateway/static/narve-theme.css`. The old dark theme is preserved at `gateway/static/narve-theme-dark.css` (opt-in only, e.g. full-bleed map/globe pages).

## Identity

Sleek editorial minimalism on a bright, light canvas — Claude-style warmth: near-white paper, near-black ink, serif display type, and a vivid accent system. Whitespace is a feature. Color is loud only where it carries meaning.

## Tokens (CSS custom properties — names are frozen, values may evolve)

| Token | Value | Role |
|---|---|---|
| `--hb-bg` | `#f5f5f5` | page canvas (landing `--bg-void`) |
| `--hb-bg-elevated` | `#fafafa` | table headers, subtle elevation |
| `--hb-surface` | `#ffffff` | cards, panels |
| `--hb-surface-hover` | `#f0f0ef` | hover fill |
| `--hb-surface-active` | `#e8e8e6` | pressed fill |
| `--hb-border` | `rgba(0,0,0,0.09)` | hairline borders |
| `--hb-border-subtle` | `rgba(0,0,0,0.05)` | row separators |
| `--hb-text` | `#0d0d0d` | primary ink |
| `--hb-text-secondary` | `#5a5a5a` | secondary ink |
| `--hb-text-muted` | `#9a9a9a` | tertiary/muted ink |
| `--hb-accent` | `#d97757` | primary accent — Claude coral |
| `--hb-accent-hover` | `#c96442` | coral hover |
| `--hb-accent-soft` | `rgba(217,119,87,0.12)` | coral wash |
| `--hb-accent-2` | `#7c3aed` | secondary accent (violet) |
| `--hb-green` / `--hb-green-soft` | `#15803d` / `rgba(21,128,61,0.10)` | positive / up |
| `--hb-red` / `--hb-red-soft` | `#b91c1c` / `rgba(185,28,28,0.10)` | negative / down |
| `--hb-amber` / `--hb-amber-soft` | `#b45309` / `rgba(180,83,9,0.10)` | caution |
| `--hb-blue` / `--hb-blue-soft` | `#2563eb` / `rgba(37,99,235,0.10)` | info |
| `--hb-font` | `'Jost', system-ui sans` | UI type |
| `--hb-font-display` | `'Lora', Georgia serif` | display headlines, hero numbers |
| `--hb-radius / -sm / -xs` | `12px / 8px / 6px` | cards / controls / chips |
| `--hb-shadow` | `0 1px 2px rgba(0,0,0,0.05), 0 4px 16px rgba(0,0,0,0.06)` | resting card |
| `--hb-shadow-lg` | `0 4px 12px rgba(0,0,0,0.08), 0 12px 40px rgba(0,0,0,0.10)` | modals |

## Type

- Display (`--hb-font-display`, Lora): page titles, section headings, hero stats. Weight 500–600, tight tracking (−0.025em).
- UI (`--hb-font`, Jost): everything else. Body 14px/1.6.
- Numbers that matter (probabilities, PnL) may use Lora at large sizes for the editorial feel; tabular figures for aligned columns (`font-variant-numeric: tabular-nums`).

## Components

- **Primary button**: black pill — `#0d0d0d` bg, `#f5f5f5` text, `border-radius: 100px`, hover lifts 1px with soft shadow. The landing's language.
- **CTA/highlight button**: coral `--hb-accent` bg, white text, same pill.
- **Ghost button**: transparent, hairline border, `rgba(0,0,0,0.05)` hover.
- **Cards**: white surface, hairline border, 12px radius, resting shadow; hover raises border-color, never scales.
- **Badges/chips**: pill, soft-tint bg (`*-soft`) with matching strong text color.
- **Tables**: uppercase 0.7rem muted headers on `--hb-bg-elevated`, hairline row separators, row hover `--hb-surface-hover`.
- **Motion**: `fadeUp` (16px, 0.4s ease-out) on page/panel entry; 0.15s cubic-bezier(0.4,0,0.2,1) for hovers. Nothing bounces.

## Data visualization (binding rules)

Categorical palette — **fixed assignment order, never cycled**, validated (CVD + contrast) for light surfaces:

1. `#d97757` coral 2. `#2563eb` blue 3. `#0d9488` teal 4. `#c026d3` magenta 5. `#b45309` amber 6. `#7c3aed` violet

Dark-surface companion set (only on opt-in dark pages): `#c96442, #5b8def, #0d9488, #d946ef, #d97706, #8b5cf6`.

- Sequential (magnitude, e.g. probability intensity): coral ramp, light→dark, one hue.
- Diverging (polarity): `--hb-blue` ↔ `--hb-accent` with neutral `#9a9a9a` midpoint.
- Movement/PnL: `--hb-green` up / `--hb-red` down, **always with an arrow or sign** (never color alone).
- Status colors are reserved for state (ok/warn/error) — never used as series colors.
- ≥2 series ⇒ legend always; ≤4 series also direct-labeled. Never a dual-axis chart. Thin marks: 2px lines, ≥8px markers, 2px surface gaps between fills, 4px rounded bar ends. Tooltips on hover for every plot. Text in charts wears ink tokens, never series colors.
- 7th+ category folds into "Other" — no generated hues.

## Do / Don't

- **Do** keep pages calm: one coral moment per view; bright colors belong to data and CTAs, not chrome.
- **Do** use `--hb-*` variables — never hardcode hex in dashboard CSS.
- **Don't** reintroduce dark backgrounds outside opt-in full-bleed visualizations.
- **Don't** use pure black/white shadows or heavy borders — hairlines and soft shadows only.
