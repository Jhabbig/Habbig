# Design audit — `gateway/static/leaderboard*`

Auditor: design-system pass against `~/.claude/skills/narve-design/SKILL.md`.
Date: 2026-05-15.
Scope: every file under `gateway/static/` whose name matches
`leaderboard*` — three files in total:

- `gateway/static/leaderboard.html`        (51 lines)
- `gateway/static/leaderboard.js`          (89 lines)
- `gateway/static/pages/leaderboard.css`   (69 lines)

The page is reached via `/leaderboard` (rendered by
`routes_referrals.py::leaderboard_page`) and pulls rows from
`/api/leaderboard` (same file, line 283).

Checks per file (subset adapted to a JS-rendered table page):

1.  **Monochrome only** — no hex / rgb / colour names in markup, CSS, or
    JS strings.
2.  **Three typefaces only** (Inter / Geist Mono / Instrument Serif). No
    `ui-monospace`, `SFMono-Regular`, `Menlo`, `Helvetica`, `system-ui`,
    `Arial` fallback chains.
3.  **Tokens** — no inline raw px or hex; pads / sizes / radii come from
    `--space-*`, `--text-*`, `--radius-*`.
4.  **AA contrast** — text against bg uses tokenised pairs; no
    `color:#xxx`, no `opacity:.5` text traps.
5.  **Mobile tap targets** (≥ 44 px) and ≥ 16 px form-control font on
    mobile; `nv-table-wrap` if the table can exceed viewport width.
6.  **No `<style>` blocks** — page CSS lives in
    `pages/leaderboard.css` (this part is already extracted; the audit
    verifies the extraction is clean and that the page does not
    re-introduce inline `style="…"` later).
7.  **No emoji in chrome** (titles, buttons, nav, errors, status).
8.  **Theme cookie pre-paint script** present (anti-FOUC for dark mode).
9.  **Shell consistency** — `page-header` + `page-body` + `page-title`
    class names (or the in-template equivalent), `raw_breadcrumb`
    passed from the server-side renderer, command-palette and share-menu
    JS at the bottom, no bespoke wrapper.

Task-specific verification claims (the caller asked these be checked
independently of the design-system 1–9):

- **A.** Only opt-in users shown.
- **B.** Handle-only — no email / no real-name / no other PII leak.
- **C.** Monospace ranks (numeric rank column uses Geist Mono).

Notation: V = violation, OK = pass, N/A = not applicable.

Glyph note: `←` (U+2190) and `→` (U+2192) are typographic chevrons, not
emoji. The horizontal-ellipsis `…` (U+2026) used in "Loading…" is a
single Unicode glyph, not three dots; not counted as a violation.

---

## `gateway/static/leaderboard.html`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No hex, rgb, or named colour anywhere in the markup. |
| 2. Three typefaces | OK | No `font-family` overrides; relies entirely on `gateway.css` defaults via the body element. |
| 3. Tokens | OK | No inline `style="…"` and no inline px / hex. Page chrome built from class names; all sizing lives in `pages/leaderboard.css` which itself uses tokens (see below). |
| 4. AA contrast | OK | All text inherits tokenised pairs (`--text-primary` / `--text-secondary` / `--text-tertiary`) via class defaults. Both themes covered. |
| 5. Mobile tap targets | **V** | Lines 22–25: the `.lb-tab` buttons render at `padding: 10px 16px` with `font-size: var(--text-sm)`. With 14 px text that's roughly a 38 × 38–40 px hit area — under the 44 × 44 px floor. The four tabs sit tight against each other (`gap: 4px`) so each one's effective target is narrow on a 360 px viewport. Bump padding to `12px 16px` or wrap each tab in a 44 px-min hit-area class. |
| 5b. Table viewport overflow | **V** | The five-column table (Rank / User / Predictions / Correct / Accuracy) is not wrapped in `.nv-table-wrap`. At 360 px the right-most "Accuracy" column will be cut off or force a horizontal scroll on the page body — the skill mandates `nv-table-wrap` for "tables that can exceed viewport width." |
| 6. No `<style>` blocks | OK | None. Page CSS lives at `pages/leaderboard.css`. The comment at the top of that file confirms the extraction was deliberate. |
| 7. No emoji in chrome | OK | None in the static template. (The JS template strings render `← you` — see the `.js` audit below.) |
| 8. Theme pre-paint script | OK | Line 11. Inline anti-FOUC reads `narve-theme` (and legacy `betyc-theme`) before paint and sets `data-theme` on `<html>`. |
| 9. Shell consistency | **V** | The page does NOT use the canonical `app-shell` + sidebar + breadcrumb + status-bar pattern used by the rest of the authenticated surface (admin pages, settings, dashboard tabs). It instead opens `<body>` with a bespoke `<main class="lb-main">` wrapper and uses bespoke `lb-h1` / `lb-sub` classes rather than the documented `page-header` / `page-title` / `page-body` triplet from the skill ("Dashboard tab structure"). The breadcrumb is rendered via `{{ raw_breadcrumb }}` (correct), but the surrounding chrome is one of two patterns coexisting on the surface — converge on the canonical shell. |

**Violations: 3** — tap-target on `.lb-tab` (5), missing `.nv-table-wrap` (5b), bespoke shell instead of `app-shell` (9).

---

## `gateway/static/leaderboard.js`

This file builds the rows client-side via template strings. It must
itself honour the design rules — emitted HTML inherits the design
contract from the page that owns it.

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No hex / rgb in strings. The only colour reference is `color:var(--text-tertiary)` (line 28) inside the "← you" hint — that is a token reference, not a raw value. |
| 2. Three typefaces | OK | No `font-family` set in any emitted string. |
| 3. Tokens | **V** | Line 28: `style="color:var(--text-tertiary);font-weight:400"` is an inline style on a `<span>`. The token reference itself is fine, but the rule "Never write `style="…"` inline in HTML for anything other than the rare per-card colour variable" applies — the `← you` hint should be a class (e.g. `.lb-you-tag`) in `pages/leaderboard.css`. Mechanically reusable, easier for the next CSS pass to retheme, and consistent with how every other component on the surface handles author intent. |
| 3b. Layout via class | OK | All other DOM (rows, cells, foot, my-rank) uses class names (`lb-rank`, `lb-handle`, `lb-numeric`, `lb-accuracy`, `is-you`, `lb-myrank`) with no inline px. |
| 4. AA contrast | OK | The "← you" hint resolves via `--text-tertiary`, which is the audited-AA tertiary token. No `opacity:.5` traps. |
| 5. Mobile tap targets | OK | No interactive elements rendered in rows; only the `lb-tabs` buttons exist as hit targets, audited above. |
| 6. No `<style>` blocks | OK | N/A — JS file. |
| 7. No emoji in chrome | OK | The arrow in `← you` (line 28) is U+2190, a typographic chevron — not an emoji. The body's "Loading…" / "Couldn't load leaderboard." / "No ranked users in this window yet." copy uses no emoji. |
| 8. Theme pre-paint script | N/A | JS file; page-level script lives in the template. |
| 9. Shell consistency | N/A | JS file. |
| Loading-state pattern | **V** | Lines 16, 41, 66, 77 use raw strings — `"Loading…"`, `"Couldn't load leaderboard."`, `"No ranked users in this window yet."` — rendered as `<td colspan="5" class="lb-empty">…</td>`. The skill says: "If you need… Loading state → use `narveSkel.show(container, {shape, count})`. Don't write 'Loading…' string." and "Error after fetch → `narveSkel.error(container, msg, {retry})`." and "List with no data → `render_empty(title, body, actions)`. Don't write 'No items'." Three components exist and are wired into the rest of the surface; the leaderboard short-circuits all three. |

**Violations: 2** — inline `style="…"` on the `← you` tag (3), skeleton / empty / error components not used (Loading + empty + error). The three string-instances roll up into one violation in the count.

---

## `gateway/static/pages/leaderboard.css`

| Check | Result | Notes |
|---|---|---|
| 1. Monochrome | OK | No hex / rgb anywhere. Every colour is a `var(--…)` reference. |
| 2. Three typefaces | OK | Line 11 uses `var(--font-display)` for the `.lb-h1` — that resolves to Instrument Serif Italic in `gateway.css`. Inter / Geist Mono inherited via body and `font-variant-numeric: tabular-nums` (lines 53, 55, 56). No fallback chains, no `monospace` / `system-ui`. |
| 2b. Monospace ranks | **V** (see verification claim C) | The rank cell (`.lb-rank`, line 53) sets `font-variant-numeric: tabular-nums` only — that is *tabular-figure spacing*, not the Geist Mono typeface. The cell still renders in Inter. The skill ("Information density") says "Numbers are right-aligned + monospaced (Geist Mono)." For the rank column specifically — and for `.lb-numeric` (line 55) and `.lb-accuracy` (line 56) — add `font-family: var(--font-mono)`. tabular-nums + Geist Mono is the correct pairing; the file has the former and is missing the latter. |
| 3. Tokens | **V** | Three raw-px values not tied to the `--space-*` / `--text-*` / `--radius-*` scale: |
|   |   | • Line 9: `max-width: 880px` — should be a content-width token (or a class like `.nv-page--narrow`). Same pattern flagged in `audit_design_admin_templates.md` for `<div style="max-width:1100px">`. |
|   |   | • Line 9: `padding: 48px 24px` — the 48 / 24 pair maps to `--space-12` / `--space-6` but is hardcoded instead of using the page-pad token (`--page-pad`) the rest of the surface uses. |
|   |   | • Line 12: `font-size: 28px` and Line 12: `letter-spacing: -0.02em` on `.lb-h1` — should be `var(--text-2xl)` or `var(--text-3xl)`. The skill: "Sizes use the `--text-*` token scale (xs through 5xl). No raw px values for type." |
|   |   | • Line 53: `width: 48px` on `.lb-rank` — maps to `--space-12` but is hardcoded. |
|   |   | • Line 59: `font-size: 12px` on `.lb-foot` — should be `var(--text-xs)`. |
|   |   | Five raw-px hits in a 69-line file. |
| 4. AA contrast | OK | All `color: var(--text-…)` references resolve to audited-AA tokens in both themes. The `.is-you` row uses `--bg-raised` against `--text-primary` (line 50) which is in the audited pair set. |
| 5. Mobile tap targets | OK | The CSS itself does not constrain tap-target sizing below 44 px — the violation is in the template's tab padding (audited above). |
| 5b. Mobile font ≥ 16 px | N/A | No `<input>` / `<select>` / `<textarea>` on the page. |
| 6. No `<style>` block | OK | This file IS the extracted CSS; the header comment confirms the migration. |
| 7. No emoji in chrome | OK | None. |
| 8. Theme pre-paint script | N/A | CSS file. |
| 9. Shell consistency | N/A | CSS scoped to bespoke `lb-*` classes — a downstream consequence of the bespoke shell violation in the `.html` file, not a separate violation. |
| Density tokens | **V** | `--row-pad-y` and `--row-pad-x` exist in the token system and respond to `[data-density="compact"]`. The table uses hardcoded `padding: 10px 12px` (line 39) and `padding: 12px` (line 46). The density toggle therefore has no effect on this table — compact mode users see the same row height as comfortable mode users. |

**Violations: 3** — `.lb-rank` / `.lb-numeric` / `.lb-accuracy` missing `var(--font-mono)` (2b), five raw-px hits (3), density tokens not used in table padding (density).

---

## Verification claims (caller-specified)

### A. Only opt-in users shown — OK

Verified at the data layer, not in the template. The template renders
whatever `/api/leaderboard` returns. Walked the chain:

- `routes_referrals.py:283` `api_leaderboard()` calls
  `dbr.get_leaderboard(period=…, limit=…)`.
- `db_referrals.py:417` `get_leaderboard()` SQL filters with
  `WHERE u.leaderboard_participation = 1 AND COALESCE(u.is_deleted, 0) = 0 AND COALESCE(u.suspended, 0) = 0 AND {col} IS NOT NULL AND ua.total_predictions > 0`.
- The opt-in flag is set/cleared only by `POST /api/leaderboard/participate`
  and `DELETE /api/leaderboard/participate` — both authenticated, both
  user-self-only (see `set_leaderboard_participation()` in
  `db_referrals.py:312`).
- Test coverage: `gateway/tests/test_referrals.py:596–642` and
  `gateway/tests/e2e/test_leaderboard_flow.py:24–48` exercise the
  participate / opt-out round-trip and assert a user appears only after
  participate and disappears after opt-out.

Empty-state copy in `leaderboard.js:18` ("No ranked users in this window
yet. Opt in at Settings → Privacy to appear here.") accurately reflects
this. No design violation.

### B. Handle-only (no email leak) — OK

The DTO emitted by `/api/leaderboard` is:

```
{rank, is_you, handle, total_predictions, correct_predictions, accuracy}
```

(See `routes_referrals.py:308–318`.) The SQL projection in
`get_leaderboard()` is:

```
SELECT u.id AS user_id, u.leaderboard_handle AS handle,
       ua.total_predictions, ua.correct_predictions, {col} AS accuracy,
       ua.last_computed_at
```

(`db_referrals.py:442–445`.)

- `u.email` is not in the projection.
- `u.id` is renamed to `user_id` and used **only** to mark `is_you`; the
  raw id is not emitted to the client. (Confirmed at
  `routes_referrals.py:312` — `"is_you": r["user_id"] == user["user_id"]`
  is computed server-side; the response carries the boolean, not the
  id.)
- `handle` comes from the dedicated `u.leaderboard_handle` column (a
  user-chosen display name, length-capped to 40, NFC-normalised, zero-
  width / bidi-stripped on the participate endpoint —
  `routes_referrals.py:339–351`), with a fallback to
  `f"user_{r['user_id']}"` if the handle is empty.

**Caveat (logged, not a violation):** the `user_N` fallback at
`routes_referrals.py:309` exposes the raw `users.id` integer for any
opted-in user whose `leaderboard_handle` is empty. This is currently
unreachable in practice because `set_leaderboard_participation()`
requires a non-empty `display_name`, but if a future code path sets
`leaderboard_participation = 1` without setting `leaderboard_handle`,
the fallback enumerates internal user ids. Worth a follow-up to either
(a) gate the SQL on `leaderboard_handle IS NOT NULL AND TRIM(leaderboard_handle) <> ''`
or (b) replace the fallback with a stable opaque token. Not in scope for
this design audit — flagging only.

The template renders `@{esc(r.handle)}` (`leaderboard.js:28`) — HTML-
escaped, prefixed with `@`. No email, no user id, no real name.

### C. Monospace ranks — **V**

`.lb-rank` (`pages/leaderboard.css:53`) sets
`font-variant-numeric: tabular-nums` but does **not** set
`font-family: var(--font-mono)`. The rank column renders in Inter with
tabular figures — same width per digit, but the same typeface as the
rest of the row.

The narve skill ("Information density"): *"Numbers are right-aligned +
monospaced (Geist Mono)."* tabular-nums is half of the answer.

Same gap on `.lb-numeric` (line 55) and `.lb-accuracy` (line 56). All
three need `font-family: var(--font-mono)`.

Fix is a one-line addition per selector. Already flagged as design
violation **2b** in the CSS audit above — listed here separately because
the caller asked it be verified independently.

---

## Summary

| File | Violations |
|---|---|
| `gateway/static/leaderboard.html` | 3 |
| `gateway/static/leaderboard.js` | 2 |
| `gateway/static/pages/leaderboard.css` | 3 |
| **Total** | **8** |

Verification claims A / B pass; claim C fails (monospace missing on the
three numeric columns).

### Top 3 (severity-ordered)

1.  **Numeric columns are not monospaced** — `.lb-rank`, `.lb-numeric`,
    `.lb-accuracy` in `pages/leaderboard.css:53–56` have tabular-nums
    but not `font-family: var(--font-mono)`. Breaks the core information-
    density contract for a numbers-heavy page; users scanning rank
    columns get inconsistent digit width across the typeface itself.
    The hard rule says monospaced. Direct user-visible regression vs.
    the rest of the surface (e.g. accuracy columns on the insider /
    portfolio tabs that do use Geist Mono).
2.  **Table not wrapped in `.nv-table-wrap` + tabs under 44 px tap
    target** — at 360 × 740 (small Android) the five-column table
    overflows the viewport and the four tabs sit tight at a ~38–40 px
    hit area. Both are explicit mobile contract items in the skill
    (`tests/test_mobile_viewport.py` is the running contract).
3.  **Skeleton / empty / error components not used** — `leaderboard.js`
    writes `"Loading…"`, a raw "No ranked users…" empty string, and a
    raw "Couldn't load leaderboard." error. Three first-class components
    (`narveSkel.show`, `render_empty`, `narveSkel.error`) exist
    specifically for these three states; the page short-circuits all
    three. Inconsistent with every other authenticated page on the
    surface and the reason these components exist in the first place.

Honourable mentions: bespoke `lb-main` / `lb-h1` shell instead of
`app-shell` + `page-header` + `page-title` (`leaderboard.html`); five
raw-px hits in 69 lines of CSS; inline `style="color:var(--text-tertiary);font-weight:400"`
on the `← you` tag in `leaderboard.js:28`; density tokens (`--row-pad-y` /
`--row-pad-x`) bypassed so the global compact-mode toggle has no effect
on this table.

No data-privacy violations: the API filters on opt-in
(`leaderboard_participation = 1`), excludes deleted / suspended users,
and the row DTO carries `{rank, is_you, handle, totals, accuracy}`
only — no email, no user id, no real name. Flagged for a follow-up
(non-design): the `f"user_{user_id}"` fallback at
`routes_referrals.py:309` could leak the internal id if the
participate-flag and handle ever go out of sync; gate the SQL on a
non-empty handle to close it.
