# Accessibility

narve.ai targets **WCAG 2.1 Level AA** conformance on every public page
and every authenticated page a subscriber can reach. This file is the
contract between engineering, design, and anyone auditing the product.

> **Reporting an issue**
> Email **a11y@narve.ai** with the page URL, the assistive technology
> you were using (e.g. VoiceOver 14.5, NVDA 2024.2, Windows High
> Contrast), and what happened. We treat a11y reports as security bugs
> — acknowledge within 3 business days, fix within 30.

---

## What is verified

Every item below is covered by either `tests/a11y/` (automated) or a
documented manual pass before release.

### Perceivable
- **Colour contrast ≥ 4.5 : 1** for body text, ≥ 3 : 1 for large
  (≥ 18 px / ≥ 14 px bold) text and UI components. Checked against both
  light and dark themes via `scripts/a11y_contrast_audit.py`.
- **Information never conveyed by colour alone.** Errors use an icon +
  a text label as well as red. Form validation adds `aria-invalid` and
  populates an error element referenced by `aria-describedby`.
- **Images have alt text.** Decorative images use `alt=""`.
- **Content reflows at 400 %** zoom without loss of functionality
  (WCAG 1.4.10). Horizontal scroll is removed except for tabular data.
- **Reduced motion respected.** `@media (prefers-reduced-motion: reduce)`
  disables auto-playing animations, floating-number tickers, and
  parallax. Page transitions still fire but run < 50 ms.

### Operable
- **Every interactive element reachable by keyboard.** Tab order
  matches visual order. Shift-Tab reverses.
- **Visible focus ring on every focusable element.** A global
  `:focus-visible { outline: 2px solid var(--text-primary) !important; }`
  rule in `static/mobile-a11y.css` acts as the safety net — per-element
  `outline: none` overrides are caught and the ring reinstated.
- **Skip-to-content link** (`.narve-skip-link`) auto-injected by
  `render_page()` on every HTML page.
- **Focus never trapped** outside intentional modals. Modals trap focus,
  return it to the trigger on close, and respond to Escape.
- **No keyboard-activated feature that is mouse-only or vice versa.**
- **Touch targets ≥ 44 × 44 px** on mobile viewports (WCAG 2.5.5).
  Measured via `scripts/a11y_touch_targets.py`.

### Understandable
- **`lang` on every `<html>`.** Narve templates all use `lang="en"`;
  future locale forks set the appropriate BCP-47 tag.
- **Landmark structure on every page** — `<header>`, `<nav>`, `<main>`
  (the skip link targets `#main`), `<aside>`, `<footer>`.
- **Heading hierarchy** — one `<h1>` per page, no skipped levels
  (h2 never follows h1 directly without an intervening level, etc.).
  Axe-core's `heading-order` rule gates this in CI.
- **Form inputs have programmatic labels.** Either a visible
  `<label for="…">` or an `aria-label` where a visible label would
  clutter the layout (e.g. search inputs with an adjacent icon).

### Robust
- **Live regions for dynamic content.** Notifications use
  `role="alert"` (assertive) or `aria-live="polite"`. The realtime feed
  pulses updates into a polite live region so screen readers announce
  new predictions without interrupting.
- **Icon-only buttons have `aria-label`.** `<button aria-label="Close">×</button>`
  pattern enforced by the template linter.
- **`<div>` + `onclick` disallowed** for interactive content — converted
  to `<button>` or `<a>`. The tree is cleaner, focus is keyboard-reachable,
  and screen readers announce the element's role correctly.

---

## Known limitations

- **PDF reports** (weekly intelligence / backtests) are not yet audited
  for AA. HTML view of the same data is the canonical accessible form;
  the PDF is an export-only secondary surface.
- **Chart.js line charts** in the trader dashboard rely on dash patterns
  + text labels for non-sighted users — sighted users see colour.
  Chart.js's own accessibility story is limited; we supplement with a
  `<figcaption>` summarising the trend in plain English. For detailed
  inspection, every chart has a "View as table" toggle.
- **Chrome extension overlay** renders on Polymarket's DOM; we cannot
  guarantee the contrast of the host page. The overlay itself meets AA
  when considered in isolation (dark text on a light card with 5 px
  border against the DOM).
- **Dashboards on sub-subdomains** (`sports.narve.ai`, etc.) are
  individually audited per-product; the apex site (narve.ai) is the
  only surface gated by tests in `tests/a11y/`.

---

## Testing

### Automated (runs in CI)

```bash
pytest tests/a11y/ -v
```

Covers:
- **Axe-core** — zero WCAG 2.1 AA violations on every public HTML page
  listed by `scripts/list_public_urls.py`.
- **Landmarks** — every page has `<main>`, at least one `<h1>`, and a
  skip link.
- **HTML lang** — `<html lang="…">` present on every rendered template.

### Manual (per release)

- **Keyboard-only pass.** Unplug the mouse. Work through every
  user journey:
  - pre-release signup → gate → token → login → dashboard
  - prediction → "Add to collection" → share card flow
  - settings → account deletion confirmation modal
- **Screen reader pass** on one of:
  - VoiceOver (macOS, ⌘ F5)
  - NVDA (Windows, free)
  - Orca (GNOME / Linux)
  Confirm landmark rotor lists every page's regions, headings form a
  sensible outline, and dynamic updates (feed, notifications) are
  announced.
- **High-contrast mode.** Toggle Windows High Contrast and macOS
  Increase Contrast. Icon buttons remain visible. Focus ring visible
  against both dark and light themes.
- **Zoom.** 200 % and 400 % in the browser. No horizontal scroll on
  anything except the markets comparison table (known, has an internal
  scroll container).

### Tool installation

```bash
npm install -g @axe-core/cli            # for ad-hoc CLI runs
pip install playwright                  # for touch-target measurement
playwright install chromium
```

---

## Policies

- **New interactive widgets must ship with a keyboard test.** `tests/a11y/`
  blocks PR merges if a new page is added without an axe case.
- **New images must ship with alt text.** `ci_check_alt_text.sh` greps
  the templates and fails the build on any `<img>` without `alt=`.
- **New colour tokens must pass contrast.** `scripts/a11y_contrast_audit.py`
  runs in pre-commit.
- **Auto-refreshing content must be pausable.** The realtime feed shows a
  "Pause updates" toggle that halts subscriptions until re-enabled.

---

## References

- [WCAG 2.1](https://www.w3.org/TR/WCAG21/) — normative standard.
- [Deque axe rules](https://dequeuniversity.com/rules/axe/) — every
  rule the automated suite runs with explanation + fix guidance.
- [Inclusive Components](https://inclusive-components.design/) by Heydon
  Pickering — the practical reference we lean on for dropdowns, modals,
  toasts, tabs, and other "rich" patterns.
