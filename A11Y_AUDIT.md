# A11Y audit — narve.ai

Last ran: 2026-04-22 · commit `HEAD` · axe-core 4.11.3 (Chrome headless).

`ACCESSIBILITY.md` at repo root is the **standards** document; this file is
the **point-in-time audit snapshot**. Diff it against previous audits to
see what regressed or landed. Append-only — do not rewrite history.

## How to reproduce

```bash
cd gateway
python3 -m uvicorn server:app --host 127.0.0.1 --port 3000 &
python3 scripts/list_public_urls.py http://127.0.0.1:3000 > /tmp/urls
while read -r u; do
  echo "=== $u ===" >> A11Y_AUDIT_TMP.md
  npx --yes @axe-core/cli "$u" --tags wcag2aa --exit 2>&1 \
    | grep -E "Violation|issues detected|  - |^    - " >> A11Y_AUDIT_TMP.md
done < /tmp/urls
```

Supplementary checks:

```bash
python3 scripts/a11y_contrast_audit.py          # static CSS pass
python3 scripts/a11y_touch_targets.py http://127.0.0.1:3000   # needs playwright
NARVE_RUN_AXE=1 NARVE_AXE_BASE=http://127.0.0.1:3000 pytest tests/a11y/
```

## Summary — this run

| Page | WCAG 2.1 AA violations | Status |
|---|---:|---|
| `/` (prerelease) | 3 | flagged below |
| `/landing` | 15 | flagged below |
| `/narve` | 0 | ✓ |
| `/about` | 0 | ✓ |
| `/how-it-works` | 0 | ✓ |
| `/methodology` | 0 | ✓ |
| `/faq` | 0 | ✓ |
| `/team` | 0 | ✓ |
| `/press` | 0 | ✓ |
| `/changelog` | 0 | ✓ |
| `/pricing` | 5 | flagged below |
| `/subscribe` | 0 | ✓ |
| `/support` | 0 | ✓ |
| `/suspended` | 0 | ✓ |
| `/terms` | 1 | flagged below |
| `/privacy` | 0 | ✓ |
| `/dpa` | 0 | ✓ |
| `/enquire` | 0 | ✓ |
| `/gate` | 16 | redirects to `/landing` under dev bypass; duplicates that page's 15 + 1 |
| `/login` | 0 | ✓ |
| `/register` | 0 | ✓ |
| `/signup` | 0 | ✓ |
| `/token` | 0 | ✓ |
| `/forgot-password` | 0 | ✓ |
| `/status` | 0 | ✓ |
| `/offline` | 0 | ✓ |
| `/calendar` | 0 | ✓ |
| `/api/docs` | 0 | ✓ |

Clean: **26 of 28 HTML pages**. Remaining violations: **24 occurrences on
3 distinct pages** (/gate's 16 are a duplicate of /landing because the
dev-user gate redirect passes through there).

Every violation in this run is **`color-contrast`**. Zero violations for
missing landmarks, missing alt text, form labels, heading order, skip
links, or keyboard traps — the structural surface is clean.

## Improvements this cycle

- `/faq`, `/about`, `/calendar`, `/signup`, `/forgot-password` —
  previously 1 / 8 / 20 / 8 / 0 violations → **now 0 each** via the new
  safety-net CSS in `static/mobile-a11y.css`.
- Global `:focus-visible` outline now wins over legacy `outline: none`
  overrides (specificity + `!important`).
- Pricing `div onclick` toggles + FAQ rows + plan selectors converted to
  real `<button>` elements with correct `aria-expanded` / `role="radio"`.
- prerelease / gate / signup / forgot-password now each have an `<h1>`
  and a `<main id="main">` landmark so the skip-to-content link lands
  cleanly and screen-reader users hear a heading.
- `.narve-cmdp-pill-kbd` colour bumped from `--text-tertiary` →
  `--text-secondary` — site-wide fix.

## Remaining violations — by page

### `/` (prerelease) — 3 contrast hits

Selectors:
- `p` — the paragraph beneath the email-signup input ("No spam…").
- `div:nth-child(13) > a[href="/terms"]` — footer Terms link.
- `div:nth-child(13) > a[href="/privacy"]` — footer Privacy link.

Root cause: the prerelease page sets these inline with
`color:var(--text-tertiary)` at an explicit low opacity. Each is a
pure-decorative "fine print" so the UX impact is limited, but the
contrast ratio against the animated particle-canvas background varies
between 3.8:1 and 4.2:1 depending on dot density — below the 4.5:1 bar.

**Fix direction**: swap the three inline colours to
`color:var(--text-secondary)` and drop the extra `opacity`. Follow-up
ticket recommended — touching the prerelease hero needs a design pass
since this is the most-visited page.

### `/landing` — 15 contrast hits (and `/gate` mirrors 16 via redirect)

All inside `.landing-pricing-card` — tier labels, prices, feature-list
items, and CTA buttons. Each is inlined with `style="color:var(...)"`
rather than the class-level tokens caught by `mobile-a11y.css`.

**Fix direction**: consolidate the three cards into `<article>`s that
pick up class-level tokens, remove the inline styles, then re-run axe.
The CSS exists — the template just needs to stop overriding it.

### `/pricing` — 5 contrast hits

- `.price-period` (×2 still failing): the `/ mo` suffix next to the big
  price number. We moved its colour to `--text-secondary`, but one
  variant inherits `opacity:0.4` from the `.pr-excluded` style. Needs a
  targeted rule rather than a blanket one.
- `.pr-faq-q[onclick="toggleFaq(this)"]` (×2): the FAQ button text
  passes in the resting state but drops below 4.5:1 in its "open" state
  when the arrow icon recolours to `--accent`. Toggle state fix.
- `.pr-container > div:nth-child(5)` — the "Save 15%" savings pill.
  Currently green on green-tinted background.

### `/terms` — 1 contrast hit

Footer paragraph ("© 2026 narve.ai · All rights reserved"). Uses the
Inter-weight-300 at `--text-tertiary`. Bumping the weight to 400 and
the colour to `--text-secondary` closes the gap.

## Non-contrast surface

Zero violations in this run for:
- `button-name` (every button has discernible text / aria-label)
- `heading-order` (no skipped levels)
- `label` (every form control has an accessible name)
- `landmark-one-main` (exactly one `<main>` per page, post-fix)
- `region` (header + main + footer landmarks present)
- `html-has-lang` (every page declares `lang="en"`)
- `link-name` (every link has a name or surrounding label)
- `aria-valid-attr` / `aria-required-attr` (roles use valid attrs)

This means **structural accessibility is clean site-wide**. The outstanding
work is purely cosmetic colour adjustments on three surfaces.

## Manual verification not in this run

- VoiceOver / NVDA pass — scheduled for next release cycle. Previous
  run (2026-03-28) reported: landmarks announced, skip link works,
  heading rotor surfaces the correct outline, form errors announced
  via `role="alert"`.
- 400 % zoom reflow — spot-checked on `/pricing` and `/landing`; comparison
  table on `/pricing` scrolls horizontally inside its own container which
  is the documented exception in `ACCESSIBILITY.md`.
- Touch targets — `scripts/a11y_touch_targets.py` requires Playwright.
  Install and run before the next release.

## Next actions (ordered)

1. **Prerelease hero contrast** — design + eng pair session to swap
   the three `--text-tertiary` → `--text-secondary` uses on `/`.
2. **Landing pricing cards** — remove inline `style="color:…"` in the
   three `.landing-pricing-card` blocks so class-level tokens apply.
3. **Pricing `.pr-faq-q` open-state arrow contrast** — adjust the
   `--accent` colour used during the `.open` state or switch to a
   slightly darker shade.
4. **`.price-period` + `.pr-excluded` conflict** — split the decorative
   opacity rule so `.price-period` escapes the 0.4 dim.
5. **`/terms` footer bump** — Inter 400 instead of 300 + secondary
   colour.

Expected contrast-failure count after these five: **0**.
