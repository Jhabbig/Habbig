# narve.ai Design System

Last updated: 2026-05-14

Canonical source: [`gateway/static/tokens.css`](gateway/static/tokens.css)
Repo-wide enforcement: see **Usage rules** at the bottom.

## Philosophy

Monochrome. Typography-forward. Information-dense. Professional.

Colour is never used to carry information. Hierarchy is expressed
through **weight**, **size**, and **position** — not hue. A
"high-credibility" badge and a "low-credibility" badge render in the
same greyscale family; the difference is the darkness of the fill and
the text label, not a green/red split.

The palette is exactly two lightness axes:

1. A **background scale** (`--bg-base → --bg-inset`) of near-white /
   near-black steps.
2. A **text scale** (`--text-primary → --text-quaternary`) of greyscale
   tones tuned to hit WCAG 2.1 AA on the theme's base background.

Everything else (interactive surfaces, semantic states, shadows, error
banners) composes from those two axes plus alpha.

---

## Tokens

### Colour — light theme (default)

| Token | Hex | Role | Contrast on bg-base |
|---|---|---|---|
| `--bg-base` | `#ffffff` | Page root |  |
| `--bg-surface` | `#fafafa` | Panel / card |  |
| `--bg-raised` | `#f5f5f5` | Raised panel / hover |  |
| `--bg-overlay` | `#efefef` | Sticky header behind blur |  |
| `--bg-float` | `#ffffff` | Popovers, modals |  |
| `--bg-inset` | `#f2f2f2` | Input fields |  |
| `--border-ghost` | `#ebebeb` | Hairline divider |  |
| `--border-subtle` | `#e0e0e0` | Standard 1-px border |  |
| `--border-default` | `#cccccc` | Input border |  |
| `--border-strong` | `#b0b0b0` | Active / focused border |  |
| `--text-primary` | `#0d0d0d` | Body, headings | 18.88:1 |
| `--text-secondary` | `#4a4a4a` | Secondary body | 8.59:1 |
| `--text-tertiary` | `#858585` | Meta, captions | 4.54:1 (AA) |
| `--text-quaternary` | `#bbbbbb` | Decorative only — separators, dots | 1.74:1 (fail; never body) |
| `--interactive-bg` | `#0d0d0d` | Primary button background |  |
| `--interactive-text` | `#ffffff` | Primary button text | 18.88:1 on `--interactive-bg` |
| `--interactive-hover` | `#1f1f1f` | Primary button hover |  |
| `--interactive-ghost` | `rgba(0,0,0,0.04)` | Ghost button background |  |
| `--interactive-ghost-hover` | `rgba(0,0,0,0.08)` | Ghost button hover |  |

### Colour — dark theme (`[data-theme="dark"]`)

| Token | Hex | Contrast on bg-base |
|---|---|---|
| `--bg-base` | `#0d0d0d` |  |
| `--bg-surface` | `#111111` |  |
| `--bg-raised` | `#161616` |  |
| `--bg-overlay` | `#1a1a1a` |  |
| `--bg-float` | `#1f1f1f` |  |
| `--bg-inset` | `#242424` |  |
| `--border-ghost` | `#141414` |  |
| `--border-subtle` | `#1f1f1f` |  |
| `--border-default` | `#2a2a2a` |  |
| `--border-strong` | `#383838` |  |
| `--text-primary` | `#f0f0f0` | 16.07:1 |
| `--text-secondary` | `#b0b0b0` | 8.42:1 |
| `--text-tertiary` | `#909090` | 5.65:1 (AA body — also 4.84:1 on `--bg-float` so popovers stay AA) |
| `--text-quaternary` | `#6e6e6e` | 3.24:1 (AA large only — decorative use) |
| `--interactive-bg` | `#f0f0f0` |  |
| `--interactive-text` | `#0d0d0d` |  |
| `--interactive-hover` | `#d4d4d4` |  |

### Rank (monochrome tier scale)

Four greyscale tiers for tier / prominence — credibility badges,
signal-strength pills, status markers. Named **rank** not
"semantic" or "high/low" because those words imply green-is-good /
red-is-bad hues; the palette explicitly rejects hue as a signal
carrier. Darker = more emphasis.

| Token | Light | Dark | Intended role |
|---|---|---|---|
| `--rank-1` | `#0d0d0d` | `#e0e0e0` | Strongest emphasis — active badge |
| `--rank-2` | `#555555` | `#888888` | Medium — pending / partial |
| `--rank-3` | `#aaaaaa` | `#444444` | Weak — inactive |
| `--rank-4` | `#cccccc` | `#2a2a2a` | Minimal — unmeasured |

Each has matching `-bg` and `-border` companions as rgba fractions of
black (light) or white (dark) for tinted surfaces.

The legacy names `--semantic-high / -mid / -low / -none` are kept as
back-compat aliases forwarding to `--rank-1..4`. New code must use
`--rank-*`.

### Error

One error tier, monochrome. Differentiated from a normal surface by
**border weight** (`--error-border` is darker than `--border-default`),
not hue. Icons carry the semantic.

| Token | Light | Dark |
|---|---|---|
| `--error-text` | `#1a1a1a` | `#e0e0e0` |
| `--error-bg` | `rgba(0,0,0,0.05)` | `rgba(255,255,255,0.06)` |
| `--error-border` | `rgba(0,0,0,0.18)` | `rgba(255,255,255,0.15)` |

### Spacing

Base unit is 4 px. Step sizes are not pure 4-px multiples past
`--space-4`: the scale was chosen for the pixel values the existing
layout relies on. Treat it as the authoritative scale; do not
substitute arbitrary pixel values.

| Token | px | Typical use |
|---|---|---|
| `--space-1` | 4 | Icon-adjacent gap |
| `--space-2` | 8 | Tight item gap |
| `--space-3` | 12 | Form field vertical gap |
| `--space-4` | 16 | Card interior padding |
| `--space-5` | 24 | Section gap inside a card |
| `--space-6` | 32 | Section gap between cards |
| `--space-7` | 48 | Block spacing |
| `--space-8` | 64 | Hero / major section |
| `--space-9` | 96 | Landing hero spacing |
| `--space-10` | 128 | Full-page section breaks |

### Radii

| Token | px | Use |
|---|---|---|
| `--radius-xs` | 4 | Tag, pill chrome |
| `--radius-sm` | 6 | Button, input |
| `--radius-md` | 8 | Card |
| `--radius-lg` | 12 | Modal |
| `--radius-xl` | 16 | Marketing hero card |
| `--radius-full` | 9999 | Circular avatar / dot |

### Typography — size

| Token | px | Use |
|---|---|---|
| `--text-xs` | 11 | Micro-label |
| `--text-sm` | 13 | Body meta |
| `--text-base` | 14 | Body |
| `--text-md` | 16 | Body emphasised |
| `--text-lg` | 18 | Sub-heading |
| `--text-xl` | 20 | Card title |
| `--text-2xl` | 24 | Section heading |
| `--text-3xl` | 32 | Page heading |
| `--text-4xl` | 48 | Hero heading |
| `--text-5xl` | 72 | Landing display |

### Typography — family

Three typefaces, no exceptions. The platform is monochrome and
typography carries hierarchy, so adding a fourth face is a
visual-identity regression — push back, don't reach for one.

| Token | Stack | Use |
|---|---|---|
| `--font-ui` | `Inter` var, sys fallback | Everything by default |
| `--font-display` | `Instrument Serif` | Landing heroes, wordmark |
| `--font-mono` | `Geist Mono` var, `SF Mono`, `Menlo`, `Consolas`, `ui-monospace` | Code, tabular numbers, hashes, market IDs |

**`Geist Mono` is the canonical monospace face** (since 2026-05-14).
Loaded via `@font-face` in `tokens.css` from
`/_gateway_static/fonts/GeistMono-Variable.woff2` (~71 KB variable
woff2). The fallback chain — `SF Mono → Menlo → Consolas →
ui-monospace` — keeps tabular surfaces legible if the woff2 fails to
load, but the deploy treats the woff2 as a required asset (see
[RUNBOOK.md → Deploy tarball](RUNBOOK.md#deploy-tarball--required-paths)).

Never use `font-family: "Geist Mono"` directly in component CSS —
always go through `var(--font-mono)` so the fallback chain and any
future swap stay in one place.

### Weights

| Token | Value |
|---|---|
| `--weight-normal` | 400 |
| `--weight-medium` | 500 |
| `--weight-semibold` | 600 |
| `--weight-bold` | 700 |

### Animation

| Token | Value |
|---|---|
| `--ease` | `cubic-bezier(0.2, 0, 0, 1)` |
| `--duration-fast` | 0.12s |
| `--duration-base` | 0.20s |
| `--duration-slow` | 0.40s |

### Z-index

One authoritative stack. Never hardcode a z-index — pick the layer
that fits.

| Token | Value | Use |
|---|---|---|
| `--z-dropdown` | 100 | Dropdown menus |
| `--z-sticky` | 200 | Sticky header |
| `--z-modal` | 1000 | Modal dialog |
| `--z-toast` | 2000 | Toast notifications |
| `--z-watermark` | 9998 | Forensic watermark canvas |
| `--z-overlay` | 9999 | Capture-detection overlay |

### Layout dimensions

Three repeated magic-numbers lifted into tokens so changing one
doesn't require grepping a dozen files.

| Token | Value | Use |
|---|---|---|
| `--max-content-width` | 1200px | Outer page wrapper |
| `--header-height` | 64px | Sticky top bar |
| `--sidebar-width` | 240px | Persistent dashboard nav |

### Focus ring

One token, one source of truth. **Always use `:focus-visible`, never
`:focus`** — the platform suppresses focus rings on mouse activation
and shows them only on keyboard navigation. Site-wide migration
landed 2026-05-14; new code that ships a bare `:focus` rule is a
visual-regression bug.

```css
:focus-visible {
  outline: var(--focus-ring);
  outline-offset: 2px;
}
```

`--focus-ring` = `2px solid var(--border-strong)`. The 2-px offset
keeps the ring off the element's own rounded border.

If a component genuinely needs a styled state on mouse focus
(e.g. a search input that stays "active-looking" while the cursor
is in it), express that via `:focus-within` on a wrapper, not by
reaching back to `:focus`.

### Shadows

| Token | Light | Dark |
|---|---|---|
| `--shadow-sm` | `0 1px 3px rgba(0,0,0,0.07)` + 2nd layer | deeper alpha |
| `--shadow-md` | `0 4px 12px rgba(0,0,0,0.09)` | deeper alpha |
| `--shadow-lg` | `0 12px 32px rgba(0,0,0,0.11)` | deeper alpha |

### Back-compat aliases

Legacy tokens that still resolve to canonical equivalents. Prefer the
canonical name in new code.

| Alias | Resolves to |
|---|---|
| `--bg` | `--bg-base` |
| `--surface` | `--bg-surface` |
| `--surface-hover` / `--surface-raised` | `--bg-raised` |
| `--border` | `--border-default` |
| `--border-light` | `--border-subtle` |
| `--text-muted` | `--text-tertiary` |
| `--accent` | `--interactive-bg` |
| `--accent-light` | `--interactive-ghost` |
| `--cta-bg` | `--interactive-bg` |
| `--cta-text` | `--interactive-text` |
| `--radius` | `--radius-xl` |
| `--green` | `--semantic-high` |
| `--green-bg` | `--semantic-high-bg` |
| `--red` | `--semantic-low` |
| `--red-bg` | `--semantic-low-bg` |

---

## Components

Every component below composes entirely from tokens. Copy-paste into
a scratch page, wrap in `[data-theme="light"]` or `="dark"` on the
`<html>` element to verify.

### Button — primary

```html
<button class="btn-primary">Save changes</button>
```

```css
.btn-primary {
  background: var(--interactive-bg);
  color: var(--interactive-text);
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-semibold);
  font-size: var(--text-sm);
  border: 1px solid var(--interactive-bg);
  transition: background var(--duration-fast) var(--ease);
}
.btn-primary:hover { background: var(--interactive-hover); }
```

### Button — secondary / ghost

```css
.btn-ghost {
  background: var(--interactive-ghost);
  color: var(--text-primary);
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-medium);
  font-size: var(--text-sm);
  border: 1px solid var(--border-subtle);
}
.btn-ghost:hover { background: var(--interactive-ghost-hover); }
```

### Button — destructive

**Monochrome**. Differentiated by a confirmation dialog, not by colour.

```css
.btn-destructive {
  background: var(--bg-raised);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-sm);
}
```

### Input

```html
<label for="email" class="input-label">Email</label>
<input id="email" type="email" class="input">
```

```css
.input-label {
  display: block;
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--text-secondary);
  margin-bottom: var(--space-1);
}
.input {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--bg-inset);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-family: var(--font-ui);
  font-size: var(--text-base);
}
.input:focus {
  outline: 2px solid var(--border-strong);
  outline-offset: -1px;
  border-color: transparent;
}
```

### Card

```css
.card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--space-5);
}
.card-title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  margin-bottom: var(--space-2);
}
.card-meta {
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-tertiary);
  margin-bottom: var(--space-1);
}
```

### Table

```css
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
.data-table th {
  text-align: left;
  font-weight: var(--weight-medium);
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: var(--text-xs);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border-subtle);
}
.data-table td {
  padding: var(--space-3);
  border-bottom: 1px solid var(--border-ghost);
  font-variant-numeric: tabular-nums;
}
```

### Badge — monochrome scale

```html
<span class="badge badge-high">HIGH</span>
<span class="badge badge-mid">MID</span>
<span class="badge badge-low">LOW</span>
```

```css
.badge {
  display: inline-block;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  padding: 2px var(--space-2);
  border-radius: var(--radius-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.badge-high  { background: var(--rank-1-bg);  color: var(--rank-1); }
.badge-mid   { background: var(--rank-2-bg);   color: var(--rank-2); }
.badge-low   { background: var(--rank-3-bg);   color: var(--rank-3); }
```

### Dropdown

```css
.dropdown-menu {
  background: var(--bg-float);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  padding: var(--space-1);
  z-index: var(--z-dropdown);
}
.dropdown-item {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-xs);
  font-size: var(--text-sm);
}
.dropdown-item:hover { background: var(--interactive-ghost); }
```

### Modal

```css
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: var(--z-modal);
  display: grid;
  place-items: center;
}
.modal {
  background: var(--bg-float);
  border-radius: var(--radius-lg);
  padding: var(--space-6);
  max-width: 480px;
  width: 90vw;
  box-shadow: var(--shadow-lg);
}
```

### Banner — info / warning / error (all monochrome)

```css
.banner {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  border: 1px solid;
}
.banner-info    { background: var(--rank-2-bg);   color: var(--rank-2);   border-color: var(--rank-2-border); }
.banner-warning { background: var(--rank-1-bg);  color: var(--rank-1);  border-color: var(--rank-1-border); }
.banner-error   { background: var(--error-bg);          color: var(--error-text);     border-color: var(--error-border); }
```

### Empty state

```css
.empty-state {
  text-align: center;
  padding: var(--space-8) var(--space-4);
  color: var(--text-tertiary);
}
.empty-state-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-medium);
  color: var(--text-secondary);
  margin-bottom: var(--space-2);
}
```

### Loading skeleton

See [`gateway/static/skeletons.css`](gateway/static/skeletons.css).

```css
.skeleton {
  background: var(--bg-raised);
  border-radius: var(--radius-xs);
  animation: skeleton-pulse 1.2s var(--ease) infinite;
}
@keyframes skeleton-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1.0; }
}
```

---

## Usage rules

These are load-bearing — every rule below has been broken in the past
and is the reason something drifted.

1. **Never hardcode hex colours.** Use a token. If one doesn't exist,
   add it to `tokens.css`.
2. **Never hardcode pixel spacing.** Use `--space-N`. If you need a
   value that isn't on the scale, that's a design signal — pick the
   nearest existing step.
3. **Never use `font-family: "Inter"` directly.** Use `var(--font-ui)`
   so the system-font fallback chain stays consistent.
4. **Dark mode parity.** Every component you ship must be verified in
   both themes. Text must hit 4.5:1 on the theme's `--bg-base`; run
   axe DevTools to confirm.
5. **No colour for hierarchy.** Differentiate by weight + size +
   position. If a reviewer asks "why is this green", the answer must
   be "it isn't" — it's a `--semantic-high` token that happens to
   render as near-black / near-white depending on theme.
6. **One z-index stack.** Never inline `z-index: 9999`. Use `--z-*`.
7. **Keep tokens.css small.** If you need a token that isn't an exact
   visual step in the existing scale, add the nearest multiple.
   Adding a one-off `--bg-my-special-surface` re-introduces drift.
8. **Prefer component classes over inline style.** Inline `style=`
   wins over stylesheet rules and ignores theme overrides; they are
   the reason `mobile-a11y.css` has a dozen `!important` patches.

### When adding a new component

1. Write it entirely in tokens.
2. Manually flip `<html data-theme="dark">` and verify.
3. Run axe on the page. No serious / critical findings.
4. Add the component's HTML + CSS example to this file under
   `## Components`.
5. If you added new tokens, add rows to the `## Tokens` tables.

---

## Accessibility

Load-bearing rules. Each has a corresponding token / pattern above —
this section is the prose explanation of why.

1. **`:focus-visible` is the only focus selector** (since
   2026-05-14). `:focus` fires for every mouse click and leaves a
   stale ring; `:focus-visible` only fires for keyboard /
   assistive-tech activation. Site-wide migration landed in commit
   `bd2d583`. If you need a permanent visual state on a focused
   element, use `:focus-within` on a wrapper.
2. **Contrast targets WCAG 2.1 AA on body text** — 4.5:1 minimum on
   the theme's `--bg-base`. The token tables above call out the
   ratio for every text token. `--text-quaternary` is decorative
   only — never body.
3. **No colour for hierarchy.** Differentiate by weight, size, and
   position. Re-stated here because it is the single largest
   accessibility win for colour-blind users: a green/red badge pair
   is a regression even if the green hits AA.
4. **Touch targets ≥ 44 × 44 px** on mobile surfaces. The
   `mobile-a11y.css` overrides exist specifically to bump narrow
   inputs / buttons up to this floor; do not "fix" them by shrinking
   the override.
5. **`prefers-reduced-motion`.** Skeletons and scroll-reveal
   animations check the media query; new animations must do the
   same.
6. **axe pass before merge.** Zero serious / critical findings on
   every new page. Run axe DevTools on both `[data-theme="light"]`
   and `[data-theme="dark"]`.

---

## File organisation

```
gateway/static/
├── tokens.css              — variables only (CANONICAL)
├── gateway.css             — imports tokens + all resets / layout / comp styles
├── states.css              — empty / error / loading patterns
├── skeletons.css           — skeleton placeholder animation
├── scroll-animations.css   — intersection-observer reveal helpers
├── mobile-a11y.css         — mobile breakpoint overrides + AA contrast
│                             fixes (loaded AFTER gateway.css so it can
│                             override)
└── watermark.css           — forensic overlay + utility pills
```

Import order for a page:

```html
<link rel="stylesheet" href="/_gateway_static/gateway.css">
<!-- gateway.css @imports tokens.css first — no separate link needed -->
<link rel="stylesheet" href="/_gateway_static/mobile-a11y.css">
<link rel="stylesheet" href="/_gateway_static/skeletons.css">
<link rel="stylesheet" href="/_gateway_static/scroll-animations.css">
<!-- authenticated pages only: -->
<link rel="stylesheet" href="/_gateway_static/watermark.css">
```

`pwa_middleware.py` already injects `mobile-a11y.css` on every
response; page-specific templates only need to add the `gateway.css`
link and any per-page sheet.

---

## Auditing drift

Run periodically to catch regressions:

```bash
cd gateway/static

# Every hex outside @font-face / data URIs / comments should be zero
# outside tokens.css:
grep -nE "#[0-9a-fA-F]{3,8}" *.css \
  | grep -v "^tokens.css" \
  | grep -vE "url\(data:|/\*|@font-face" \
  | wc -l

# Every var(--X) used must exist in tokens.css:
grep -hoE "var\(--[a-zA-Z0-9_-]+" *.css | sed 's/var(//' | sort -u > /tmp/used.txt
grep -hoE "\-\-[a-zA-Z0-9_-]+\s*:" tokens.css | sed 's/\s*:$//' | sort -u > /tmp/defined.txt
comm -23 /tmp/used.txt /tmp/defined.txt   # ← should be empty
```

If either returns lines, open a PR that either adds the missing token
to `tokens.css` or replaces the hex with an existing token.
