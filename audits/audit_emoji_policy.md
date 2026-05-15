# Emoji Policy Audit — UI Chrome

**Date:** 2026-05-15
**Auditor:** Claude (Opus 4.7, 1M context)
**Scope:** Emoji codepoints in UI chrome (titles, buttons, headers, error messages, nav) across `gateway/static/*.html` and Python error-response sites in `gateway/**/*.py`. **`prerelease.html` is explicitly off-limits and was excluded.**
**Policy under audit:** narve design system disallows emojis in UI chrome. User-generated content (feedback bodies, take text, prediction reasoning, etc.) is allowed to keep emojis.

---

## Method

Scanned every `.html` file in `/Users/shocakarel/Habbig/gateway/static/` (108 files after excluding `prerelease.html`) using a Python regex over standard Unicode emoji codepoint blocks:

```
U+1F300–U+1F5FF   symbols & pictographs
U+1F600–U+1F64F   emoticons
U+1F680–U+1F6FF   transport & map
U+1F700–U+1FAFF   supplemental symbols, chess, etc.
U+1F900–U+1F9FF   supplemental pictographs
U+2600–U+26FF    misc symbols (✓ ✗ ⚠ ⚙ ☀)
U+2700–U+27BF    dingbats
U+1F1E6–U+1F1FF  regional indicator flags
U+1F000–U+1F02F  mahjong
U+1F0A0–U+1F0FF  playing cards
U+FE00–U+FE0F    variation selectors
```

Then ran the same scan over all `.py` files under `gateway/` (650 files) and tagged hits whose surrounding context contained error-emission patterns (`raise HTTPException`, `detail=`, `flash(`, `.error(`, `toast_error`, etc.).

Arrows (`←` `→` `↔`), geometric shapes (`○`), and misc technical glyphs are **not** in the emoji blocks and were intentionally not flagged — these are common monochrome chrome glyphs in the narve design system.

---

## Headline numbers

| Surface | Emoji codepoints in chrome | Files |
|---|---|---|
| `gateway/static/*.html` | **5** | 3 |
| `gateway/**/*.py` error messages | **0** | 0 |
| `gateway/**/*.py` other chrome (rendered HTML in routes) | 8 | 2 |

**5 emoji codepoints in `static/*.html` chrome, in 3 files.**
**0 emoji codepoints in error messages anywhere in the codebase.**

---

## Findings — `gateway/static/*.html`

All five hits are status-indicator glyphs (`✓` U+2713 / `✗` U+2717) inside JavaScript that builds chrome text — paragraph status lines and a button label. Per the narve design system this is arguably the borderline case: `✓`/`✗` are dingbat block (U+2700–U+27BF) and live with emoji codepoints but render as monochrome text in most fonts. They are flagged here as a policy decision for the user — no automated remediation was applied.

| File | Line | Codepoint | Context | Chrome type |
|---|---|---|---|---|
| `gateway/static/market_detail.html` | 275 | U+2713 `✓` | `'Market resolved ' + (p.resolved_correct ? 'in your favor ✓' : 'against you ✗')` rendered into a `<p>` | Status text (body chrome) |
| `gateway/static/market_detail.html` | 275 | U+2717 `✗` | (same line) | Status text |
| `gateway/static/prediction_detail.html` | 80 | U+2713 `✓` | `'Resolved correct ✓'` inside `<strong>` | Status header |
| `gateway/static/prediction_detail.html` | 80 | U+2717 `✗` | `'Resolved incorrect ✗'` | Status header |
| `gateway/static/settings_embeds.html` | 276 | U+2713 `✓` | `btn.textContent = ok ? "Copied ✓" : "Copy failed";` | **Button label** |

### Classification

- **`settings_embeds.html:276` is a button label** — most clearly UI chrome under the policy. A `Copied` toast/inline confirmation can be reduced to plain text (`"Copied"` / `"Copy failed"`) without information loss.
- **`market_detail.html:275` and `prediction_detail.html:80`** are inline status glyphs in result/resolution chrome. They are decorative; the adjacent text already states the resolution ("Resolved correct" / "in your favor"). A pure-text version would not lose information.

No emoji were found in:

- HTML `<title>` tags
- `<h1>`–`<h6>` headers
- Top-nav anchors / breadcrumb / back-links (arrows excluded by design)
- `<button>` static text (only in JS-built button at `settings_embeds.html:276`)
- HTTP error templates (`403.html`, `error_page.html`, `offline.html`, `suspended.html`, `shared_invalid.html`)
- Form error / flash containers in HTML

---

## Findings — Error messages in Python source

**0 emoji codepoints found in any error-emission context** across 650 `.py` files. Pattern coverage included:

```
raise HTTPException(... detail=...)
HTTPException(...)
flash(..., 'error')
notify_error(...)
toast_error(...)
.error("...")
"message": "..."
```

`gateway/scripts/ci_check_input_hygiene.py:187` and `gateway/scripts/bench_large_data.py:373` use `❌` (U+274C) in `print()` output, but these are developer-facing CLI scripts, **not** user-facing error responses.

---

## Findings — Other chrome rendered by Python (server-side HTML)

These are server-rendered chrome strings emitted directly from Python route handlers (i.e. they reach the browser as HTML). They are out of strict scope (the user asked about `static/*.html`) but are listed for completeness because they are functionally identical to template chrome.

| File | Line | Codepoint | Chrome type |
|---|---|---|---|
| `gateway/feedback_routes.py` | 65 | U+2699 `⚙` | Status pill label (`STATUS_LABELS["in_progress"]`) — feedback admin UI chrome |
| `gateway/feedback_routes.py` | 66 | U+2713 `✓` | Status pill label (`shipped`) |
| `gateway/feedback_routes.py` | 67 | U+2715 `✕` | Status pill label (`declined`) |
| `gateway/feedback_routes.py` | 442 | U+2713 `✓` | `<button>` label — `"✓ Voted · Remove"` |
| `gateway/impersonation.py` | 172 | U+26A0 `⚠` | Admin impersonation banner — fixed chrome at top of every page when impersonating |
| `gateway/take_routes.py` | 522, 524, 743, 748 | U+2713 / U+2717 | Inline status badges (`correct ✓` / `✗ incorrect`) on take cards |
| `gateway/profile_routes.py` | 220 | U+2713 / U+2717 | Outcome glyph in profile feed |
| `gateway/scenarios_routes.py` | 634, 685 | U+2713 `✓` | Button success state (`Saved ✓` / `Tracked N ✓`) |
| `gateway/jobs/telegram_sends.py` | 105 | U+1F305 `🌅` | Telegram message header (outbound to user) — not web chrome |
| `gateway/pwa_middleware.py` | 203 | U+1F4AC `💬` | **Comment only** (the injected floating button is `<button>Feedback</button>` in the actual script; the emoji here is a Python comment, not chrome) |

**Highest-impact server-rendered chrome violation:** `gateway/impersonation.py:172` injects a persistent `⚠` warning banner via middleware on every admin-impersonated page. It satisfies the "warn loudly" UX goal but the chrome policy says no emoji in chrome.

---

## Recommendation (for the user, no action taken)

If the policy is enforced strictly, the inline `✓`/`✗` glyphs in chrome contexts should be replaced with:

1. **Pure text** — e.g. `"Copied"` / `"Copy failed"`, `"Market resolved (in your favor)"`, `"Resolved correct"`.
2. **CSS-styled badges** — e.g. `<span class="badge-correct">Correct</span>` styled via `gateway.css` semantic colour tokens (`--semantic-high` / `--semantic-low`), which is the pattern already established in `gateway/take_routes.py:522` (but currently with the glyph still appended).

Status-pill maps in `feedback_routes.py:63–69` should be redesigned to use the existing pill component (`sb-status-…` classes already exist in `gateway.css`) without the leading glyph.

The `⚠` impersonation banner at `gateway/impersonation.py:172` is the only "safety-critical" chrome violation — replacing it with a styled red-bordered banner ("Impersonating <user>") would meet the policy without losing the visual urgency.

---

## Files audited

- 108 HTML files in `gateway/static/` (excluded: `prerelease.html`)
- 650 `.py` files under `gateway/`
- No HTML in subproduct dashboard `static/` directories was in scope per the brief (`static/*.html` referred to the gateway only). Spot-checked `annoyance-dashboard/static/index.html`, `centralbank-dashboard/static/index.html`, `climate-dashboard/static/index.html`, `disasters-dashboard/static/index.html` — none contained emoji codepoints in chrome.

## Files containing emoji in `static/*.html` chrome

1. `/Users/shocakarel/Habbig/gateway/static/market_detail.html` (2)
2. `/Users/shocakarel/Habbig/gateway/static/prediction_detail.html` (2)
3. `/Users/shocakarel/Habbig/gateway/static/settings_embeds.html` (1)
