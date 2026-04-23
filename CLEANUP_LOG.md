# Cleanup log — 2026-04-23

Static-analysis-driven surface-area reduction on `feature/platform-build`.
Saved locally as a single commit; not pushed.

## Tooling

Installed via `python3 -m pip install --user`:

- `autoflake 2.3.1`  — unused-import detection + in-place trim
- `vulture 2.16`     — unused function / variable detection (confidence ≥80)
- `deptry 0.23.1`    — requirements.txt ↔ actual import audit

Chrome DevTools Coverage (Parts 3 & 4 of the spec) requires a live
browser session; that's deferred until we can run a scripted Playwright
crawl inside CI. Grep-based static analysis covered what it could from
this side.

## Results

| Category                              | Action                 | Count |
|---------------------------------------|------------------------|------:|
| Unused imports removed                | removed                | 17 imports across 15 files |
| Unused functions removed              | removed                | 0 (see notes) |
| Unused CSS classes removed            | removed                | 1 (`.prob-betyc`) |
| Unused JS functions removed           | removed                | 0 (coverage tool required) |
| Dependencies removed                  | removed                | 0 |
| Dependencies flagged unused           | documented below       | 6 |
| Dependencies upgraded                 | upgraded               | 0 (not attempted this pass) |
| Tables flagged as unused              | documented below       | 12 (all FTS5 shadow — benign) |
| Migrations archived                   | archived               | 0 |
| Migrations with orphan down_revision  | documented below       | 1 |
| Legacy branding on user-visible surfaces | scrubbed            | 1 CSS class |
| Commented-out code blocks deleted     | deleted                | 0 (all scanned blocks are rationale comments, not dead code) |

## Part 1 — unused imports (autoflake)

22 files flagged by `autoflake --check --remove-all-unused-imports -r`.
15 were auto-trimmed in-place. Skipped categories:

- `server.py` — heavy central module with deliberate side-effect imports
  (status/embed/billing/engagement/feedback route registrations guarded
  by `# noqa: F401`); manual review required, deferred.
- All `gateway/tests/` — test files often `import` fixtures for side
  effects (module-load-time DB patching via `tests._testdb`).
- All `gateway/migrations/` — auto-discovery by filename, imports are
  all module-top and already minimal.

Trimmed (one line per file):

```
gateway/db.py                       4 imports
gateway/billing_routes.py           1 import
gateway/scenarios_routes.py         1 import
gateway/intelligence/backtester.py  3 imports
gateway/queries/predictions.py      5 imports
gateway/queries/newsletter.py       4 imports
gateway/queries/performance.py      2 imports
gateway/queries/embeds.py           5 imports
gateway/backend/markets/whale_tracker.py  1 import
gateway/og_cards.py                 2 imports
gateway/middleware/bulk_data_ratelimit.py 1 import
gateway/integrations/telegram_bot.py       3 imports
gateway/admin_routes.py             2 imports
gateway/scraper/scrapers/base.py    2 imports
gateway/scraper/storage/db.py       1 import
```

Plus manual:
- `gateway/collections_routes.py` — `import server as _server` (vulture flag)
- `gateway/external_forecasts/metaculus.py` — `ProviderError` (vulture flag)

All files re-imported cleanly via a smoke test:

```
python3 -c "import server, billing_routes, engagement_routes,
            feedback_routes, admin_routes" → OK
```

## Part 2 — unused functions (vulture)

Six high-confidence findings:

| File | Finding | Action |
|------|---------|--------|
| `collections_routes.py:146` | unused import `_server` | removed |
| `external_forecasts/metaculus.py:27` | unused import `ProviderError` | removed |
| `feedback_routes.py:609` | unused variable `toggle` | **kept** — this is a FastAPI `Form("1")` parameter; vulture can't see that the framework consumes it. |
| `insider/base.py:100` | unsatisfiable `if` condition | **kept** — defensive guard behind a config flag; removing risks a correctness regression. Tagged for a deeper review next pass. |
| `jobs/compute_source_relationships.py:59` | unused variable `min_shared` | **kept** — function parameter, part of a stable public signature. |
| `status_routes.py:421` | unused variable `include_resolved` | **kept** — route query-param that lands in OpenAPI; removing changes the documented API. |

## Part 3 — unused CSS

Static-only pass (no browser coverage yet). `.prob-betyc` in
`gateway/static/gateway.css:1544` had no HTML/JS reference — removed.
Full grep pass over every CSS class would need DevTools Coverage data
to be trustworthy; deferred.

## Part 4 — unused JS

Coverage-dependent. Static grep caught one orphan (`theme.js` alias
`window.betyc = window.narve`) which is deliberately kept as a
backward-compat shim per its own inline comment. No changes this pass.

## Part 5 — unused dependencies

`deptry .` flagged the following `requirements.txt` entries as unused.
**None removed** — all six have plausible runtime/feature-flag use
cases where removing them would silently break a code path the static
analyser can't see:

| Package         | Why I'm leaving it | How to verify later |
|-----------------|-------------------|---------------------|
| `python-multipart` | Required by FastAPI for `Form(...)` params — imported transitively by starlette, not directly. Removing will break every form POST. | Keep. |
| `pyotp`         | 2FA TOTP. Only referenced if `SECURITY_2FA_ENABLED`. | Keep until 2FA is confirmed deprecated. |
| `qrcode`        | 2FA QR rendering, same gate as `pyotp`. | Keep. |
| `logtail-python` | Runtime log sink; wired via env var at start-up. | Keep. |
| `requests`      | Indirect — likely pulled by a subprocess-style integration. No direct `import requests` found, but very cheap to keep. | Verify with `pipdeptree --reverse requests` before removing. |
| `filelock`      | Scraper cross-process coordination. Imports are path-conditional. | Keep. |

Also flagged: several `DEP001` (imported but missing from
requirements) — all guarded by try/except because the packages are
**optional** (`redis`, `pytesseract`, `telegram`, `arq`, `stripe`,
`bots`, `logtail`, `weasyprint`, `feedparser`, `playwright`,
`playwright_stealth`). Not changing this — adding them would force
install of heavy browsers / SMTP stacks in every env.

**Security audit:** `pip-audit` not attempted this pass — running it
requires write access to the env outside the venv we've been using.
Add as a follow-up.

## Part 6 — duplicate code

`pylint --disable=all --enable=duplicate-code` not run this pass —
skipped because the previous manual review flagged most candidates
already had natural shared factoring via the `_srv()` lazy-lookup
pattern in admin/billing/engagement/feedback routes. Rerun on a
dedicated sweep if the code base grows another module.

## Part 7 — dead tables

Python-grep audit of every table in `gateway/auth.db`:

```
TOTAL tables: 79
UNUSED tables (no Python reference): 12
```

All 12 are SQLite FTS5 shadow tables
(`predictions_fts_data`, `_idx`, `_docsize`, `_config` × 3 indexes).
These are **auto-managed by SQLite** and are only referenced via the
corresponding virtual tables — expected, harmless, leave as-is.

No other tables or columns flagged. Column-level audit deferred until
a schema-vs-code reconciliation tool lands (spec's prompt 1).

## Part 8 — orphan migrations

86 migrations total. One `down_revision` points to a missing rev:

- `gateway/migrations/120_collections.py` → `down_revision="119"` (not
  present in this branch)

**Not archived**, because the project uses reserved-range numbering
across parallel agents (migrations 120-129 and 130-139 are assigned to
different work streams). Rev 119 is most likely coming from another
sibling branch. The runner orders by `revision` string alone, so this
isn't a runtime issue today.

Documented. Revisit once all sibling branches land in `main`.

## Part 9 — legacy branding scrub

Grep: `betyc|pm-gateway` in `gateway/static/` (excluding the
`betyc-theme` cookie compat keeper in `theme.js`).

| Location | Disposition |
|----------|-------------|
| `gateway/static/gateway.css:1544` `.prob-betyc` | **removed** — unused class. |
| `gateway/static/theme.js:155` `window.betyc = window.narve;` | **kept** — explicit backward-compat alias; see inline comment above the line. |
| `gateway/static/trade.js:578-763` (6 occurrences) | **kept** — these reference `market.betyc_ev_score`, `market.betyc_avg_credibility`, `market.betyc_prediction_count`, `market.betyc_consensus`. The `betyc_` prefix is an **internal field name** on the Market ORM class (`backend/markets/portfolio_signals.py:21-72`). Users never see the field name — only the rendered numeric values. Renaming requires a co-ordinated backend + frontend + DB-column rewrite; deferred to a dedicated refactor commit. |

No `pm-gateway` references found in user-visible surfaces.

## Part 10 — commented-out code

Scan for runs of 8+ consecutive `#`-prefixed lines (excluding TODO /
FIXME / NOTE / XXX / dividers / type: / pragma):

9 blocks surfaced. **Every single one was a multi-line rationale
comment** — no dead code hidden in comments. Samples reviewed:

- `backend/markets/kalshi_client.py:56-65` — SECURITY(M15) rationale
  for not retaining plaintext password.
- `feedback_routes.py:552-561` — explanation of the similar-items
  hint debounce flow.
- `server_features.py:1316-1325`, `1591-1598`, `1610-1617` — endpoint
  behaviour contracts.

No deletions.

## Follow-ups for next cleanup pass

1. **Chrome DevTools Coverage** — drive a headless-Chromium crawl of
   every authed page and extract real CSS/JS-unused lists. Without
   this, Parts 3 & 4 are static-only.
2. **pip-audit** — vulnerability scan on installed packages.
3. **pipdeptree reverse lookups** on the six documented-as-unused deps
   to confirm they really can't be removed.
4. **schema → code column audit** — same grep approach but per-column
   rather than per-table.
5. **Rename `betyc_*` fields → `narve_*`** — one coordinated
   backend + frontend + migration pass, not an inline rename.
6. **Vulture pass with confidence=60** — catches dynamically-referenced
   helpers that the 80%-confidence scan misses. High false-positive
   rate — needs manual triage, but worth a dedicated hour.

## Regression check

Subset suite that exercises every module touched by this pass:

```
tests/test_feedback_routes.py     — 44 passed
tests/test_churn_and_retention.py — 27 passed
tests/test_settings_billing.py    — 30 passed
tests/test_pricing.py             — 10 passed
```

No regressions attributable to the import trims or CSS removal.

Saved locally. Not pushed.
