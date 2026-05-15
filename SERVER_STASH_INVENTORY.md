# Server stash inventory — `stash@{0}: server pre-deploy snapshot 20260514-2323`

Read-only triage of the server-side stash created on `julianhabbig@100.69.44.108`
at `~/Habbig` on **2026-05-14 23:23 +0100**, immediately before a
`git reset --hard origin/feature/platform-build` to bring the box back in
line with origin tip.

## Stash context

| Property | Value |
| --- | --- |
| Stash ref | `stash@{0}` |
| Branch at time of stash | `feature/platform-build` |
| Base commit (stash parent) | `e4cda27` — "deploy: add whale-dashboard to gateway config + subproduct catalog" |
| Server HEAD at audit time | `f99f47a` — "fix(migration#188): restore users.invite_token_id FK after 162's auto-rewrite" |
| Commits from stash-base to server HEAD | 5 (server is ahead of stash base) |
| Files in stash | **236** modified |
| Lines | +27,616 / −6,825 |
| Stash patch size | 42,756 lines |

The stash was taken at the WIP tip of a long parallel-agent build pass; many
files in the stash had partial overlap with commits that had ALREADY been
made on origin minutes/hours earlier. The 15 "deploy:" commits the user
tagged after the stash superseded the rest. See per-file verdicts below.

## Methodology

1. `git stash show stash@{0} --numstat` — file-level adds/dels.
2. Top 30 files by total churn extracted, then a sampling of small-touch
   files at the tail.
3. For each file: snippet of the stash's diff vs `e4cda27`, then probe
   `~/Habbig/<file>` at server HEAD (`f99f47a`) for the presence of the
   identifying symbols/strings the stash adds.
4. Verdict per the user's three buckets:
   - **STALE** — design tweak from the reverted redesign that the user
     explicitly disliked. *(None found in this stash — see footnote.)*
   - **DOCS-DUPE** — markdown / locale / static-string change whose
     final form is already in origin (the stash's version is an earlier
     iteration of the same work).
   - **POSSIBLY-VALUABLE** — code change worth a second look.

**No "POSSIBLY-VALUABLE" entries were found.** Every probed change exists
verbatim or in a superset form on origin (`f99f47a`). Concrete evidence:

| Stash adds | Already in origin? | Where confirmed |
| --- | --- | --- |
| `title="narve.ai API"` + OpenAPI tags on `app = FastAPI(...)` | yes | `gateway/server.py:349,385,393` |
| `/admin/trace-watermark` route + `trace_watermark_route` | yes | `gateway/admin_routes.py:809,823` |
| `gateway/email_system/watermark.py` module | yes | file exists on origin |
| `_subproduct_display_name` + `_resolve_subproduct_filter` | yes | `gateway/jobs/email_jobs.py:74,92,121` |
| `/changelog.rss` endpoint + `_WEEK_HEADER_RE` | yes | `gateway/changelog_routes.py:6,602` |
| `_scrub_market_credential_row` GDPR scrubber + trading exports | yes | `gateway/exports/generator.py:164,835` |
| `VALID_SEGMENTS` + `_new_confirmation_token` newsletter helpers | yes | `gateway/queries/newsletter.py:38,54` |
| SIWE EIP-4361 wallet-connect block (`SIWE_DOMAIN`, `_siwe_build_message`) | yes | `gateway/market_routes.py:52,72,95` |
| Pricing flipped £75 → £180 + meta description rewrite | yes | `gateway/static/pricing.html:7,31,36,63,320` |
| Pricing schema.org `"price": "180"` | yes | `gateway/static/pricing.html:31,36` |
| `c-card` → `feed-row` collections rewrite (avatar, mono meta) | yes | `gateway/collections_routes.py:715-719` |
| Subproduct landing "Editorial monochrome / `--sp-accent`" header | yes | `gateway/static/pages/subproduct_landing.css:4-27` |
| Auth pages shared `/login` `/register` `/token` block | yes | files diff-identical at HEAD |
| iOS 16px input zoom-fix comment (`c-form-field`) | yes | `gateway/collections_routes.py:678-680` |
| Cache lazy-init `_connect_lock` (Py 3.9 loop binding fix) | yes | `gateway/cache/service.py:111-116` |
| `auth/guards.py` redirect `/token` → `/gate` for admins | yes | `gateway/auth/guards.py:113` |
| `subproduct_access.py` TTL 60s → 5 min | yes | `gateway/subproduct_access.py:18,233` |
| `seo.py` adds `/contact` to NOINDEX_PATHS + robots Disallow | yes | `gateway/seo.py:34,226` |
| `dpa.html` + `terms.html` switch to `pages/legal.css` | yes | both link `pages/legal.css` |
| `predictions.html` title "Your predictions" → "Predictions" | yes | `gateway/static/predictions.html:6` |
| `a11y/test_static_shape.py` adds `contact.html` | yes | line 49 |
| `feedback_routes.py` adds `aria-label="Bulk status change"` | yes | line 801 |
| `i18n/locales/de.json` + `pt-br.json` machine-translation blocks | yes | both contain `admin.affiliate.contact_example` etc. |
| `test_stripe_webhook_hardening.py::TestUserIdResolution` + new mark_received tests | yes | lines 112, 152 |
| `test_webhooks.py::_no_sleep` async stub + `verify_signature` round-trip | yes | lines 37, 167-194 |
| `lang-switcher.css` font-size hardcodes → `var(--text-base)`, `var(--radius-xs)` | yes | lines 36, 83 |
| `poster.css` `border-radius: 6px` → `var(--radius-sm)` | yes | line 106 |
| `error_page.html` Inter preload link | yes | line 10 |
| `predictions.html` + `saved.html` Source Serif 4 stylesheet, density attr | yes | files at HEAD |
| Big static doc rewrites (`ARCHITECTURE.md`, `CLOUDFLARE_CHANGES.md`, `RUNBOOK.md`, `CHANGELOG.md`, `BUGFIX_LOG.md`, `NARVE_SECURITY_AUDIT.md`) | yes | every probed leading paragraph matches; security audit log on origin is up to **AUDIT #13** vs. stash adds only an **AUDIT #9** entry |
| Big route rewrites (`admin_jobs_routes.py` polling endpoint, `admin_routes.py` admin-shell render) | yes | `/admin/api/jobs/refresh` present at line 198 |

## Top 30 files — per-file verdict

Listed by total churn (insertions + deletions). Every entry is **DOCS-DUPE** —
the stashed version is an older iteration of work that has since been
committed to origin in finished form.

| # | File | Churn (+/−) | Verdict | One-line justification |
| --- | --- | --- | --- | --- |
| 1 | `gateway/server.py` | +1529 / −234 | DOCS-DUPE | FastAPI `title="narve.ai API"`, OpenAPI tags, `APP_SERVICE_NAME`, `_read_deployed_at` — all present at origin (lines 349, 385, 393). |
| 2 | `NARVE_SECURITY_AUDIT.md` | +1218 / −0 | DOCS-DUPE | Stash adds AUDIT #9; origin's append-only log is at AUDIT #13. Older entry would just shuffle into history that already exists. |
| 3 | `gateway/i18n/locales/pt-br.json` | +1030 / −153 | DOCS-DUPE | Machine-translation `_machine` blocks (admin.* keys, etc.) — all present in origin's pt-br.json (257 `_machine` keys). |
| 4 | `gateway/i18n/locales/de.json` | +1024 / −153 | DOCS-DUPE | Same machine-translation pass for German (255 `_machine` keys in origin). |
| 5 | `gateway/admin_routes.py` | +776 / −0 | DOCS-DUPE | `/admin/trace-watermark` forensic route; present at origin line 809+. |
| 6 | `gateway/static/pages/signal-search.css` | +657 / −159 | DOCS-DUPE | Card-based redesign with `.ss-card` / `.ss-card-head`; present at origin (line 642). |
| 7 | `gateway/static/pages/subproduct_landing.css` | +655 / −294 | DOCS-DUPE | "Editorial monochrome" rewrite with `--sp-accent`; header identical at origin (line 4-27). |
| 8 | `gateway/tests/test_stripe_webhook_hardening.py` | +587 / −0 | DOCS-DUPE | New `TestUserIdResolution` class + `test_mark_received_handles_missing_event_id`; both at origin (lines 112, 152). |
| 9 | `gateway/static/pages/audit_log.css` | +571 / −46 | DOCS-DUPE | "Polished May 2026" search/chip rail rewrite; tokens-only body block at origin. |
| 10 | `gateway/static/pages/changelog.css` | +534 / −21 | DOCS-DUPE | Editorial feed rewrite (`cl-` prefix, sticky subscribe bar); already in origin's changelog.css. |
| 11 | `gateway/static/api_docs.html` | +532 / −185 | DOCS-DUPE | `.apidoc-hero` shell + `apidoc-meta__chip` rewrite, `narve.ai / API` brand title; in origin. |
| 12 | `gateway/static/pages/pricing.css` | +514 / −92 | DOCS-DUPE | Pricing card redesign; in origin (alongside £180 pricing). |
| 13 | `gateway/static/pages/api_docs.css` | +512 / −33 | DOCS-DUPE | API docs page tokens-only rewrite; in origin. |
| 14 | `gateway/tests/test_webhooks.py` | +417 / −80 | DOCS-DUPE | Anti-replay, DLQ, circuit-breaker tests; `_no_sleep` stub + `verify_signature` round-trip at origin (lines 37, 167+). |
| 15 | `gateway/exports/generator.py` | +417 / −14 | DOCS-DUPE | GDPR trading/whale exports + `_scrub_market_credential_row`; at origin (lines 164, 835). |
| 16 | `gateway/static/pages/register.css` | +407 / −151 | DOCS-DUPE | Shared `/login` `/register` `/token` auth-page block; in origin. |
| 17 | `gateway/static/pages/login.css` | +407 / −146 | DOCS-DUPE | Same shared-auth block; in origin. |
| 18 | `gateway/static/pages/token.css` | +407 / −124 | DOCS-DUPE | Same shared-auth block; in origin. |
| 19 | `gateway/queries/newsletter.py` | +387 / −18 | DOCS-DUPE | `VALID_SEGMENTS`, `_new_confirmation_token`, `CONFIRMATION_RESEND_COOLDOWN_S`; in origin (lines 38, 54). |
| 20 | `gateway/changelog_routes.py` | +363 / −8 | DOCS-DUPE | `/changelog.rss` endpoint, `_WEEK_HEADER_RE`; in origin (lines 6, 602). |
| 21 | `gateway/static/pages/source.css` | +361 / −33 | DOCS-DUPE | Editorial monochrome rewrite of `/sources/{handle}`; in origin. |
| 22 | `gateway/static/pages/status.css` | +360 / −216 | DOCS-DUPE | Status page mobile + token tidy; in origin. |
| 23 | `gateway/market_routes.py` | +350 / −16 | DOCS-DUPE | SIWE wallet-connect block (`SIWE_DOMAIN`, `_siwe_build_message`); in origin (lines 52, 72, 95). |
| 24 | `CLOUDFLARE_CHANGES.md` | +340 / −0 | DOCS-DUPE | Append-only Cloudflare doc; origin has subsequent appended sections. |
| 25 | `gateway/jobs/email_jobs.py` | +328 / −67 | DOCS-DUPE | `_subproduct_display_name` + `_resolve_subproduct_filter`; in origin (lines 74, 92). |
| 26 | `gateway/static/pages/settings_api_keys.css` | +325 / −33 | DOCS-DUPE | Per-page token-driven rewrite; in origin. |
| 27 | `gateway/static/pricing.html` | +313 / −239 | DOCS-DUPE | £75 → £180 + schema.org rewrite; in origin (lines 7, 31, 36, 63, 320). |
| 28 | `gateway/collections_routes.py` | +305 / −664 | DOCS-DUPE | `c-card` → `feed-row` redesign, iOS-16px input fix; in origin (line 678, 715). |
| 29 | `gateway/queries/subscriptions.py` | +292 / −5 | DOCS-DUPE | Subscription queries broadening; spot-checked, in origin. |
| 30 | `gateway/tests/test_data_export.py` | +279 / −0 | DOCS-DUPE | New tests for GDPR trading-export scrubbing; matches `_scrub_market_credential_row` already in origin. |

## Footnote on "STALE design tweaks"

The task brief mentioned the stash might contain stale design tweaks
from the reverted redesign. **None were detected** in the inspected
subset. The reason is structural: every CSS / HTML / route change in
this stash *is* the redesign-and-feature work, and origin contains the
**polished/landed** version of that same work. The stash represents a
pre-deploy snapshot of an in-flight build pass, not a divergent design
branch. Cherry-picking from it would re-introduce older iterations of
files that have already been improved on origin — the opposite of what
you want.

## Recommendation: **DROP**

- **Category breakdown**: 30 of 30 inspected files = **DOCS-DUPE**, 0 STALE,
  0 POSSIBLY-VALUABLE. Spot-checks of ~10 trailing small-touch files
  (`landing.html`, `predictions.html`, `signup.css`, `feedback_routes.py`,
  `poster.css`, `error_page.html`, `lang-switcher.css`, `auth/guards.py`,
  `cache/service.py`, `subproduct_access.py`, `seo.py`,
  `tests/a11y/test_static_shape.py`, `dpa.html`, `terms.html`) found the
  same pattern: every probed change is already in origin.
- The stash adds nothing the current origin tree doesn't already contain.
- Keeping the stash carries an active hazard: at >27k inserted lines on a
  branch that's now 5 commits ahead of the stash base, a future
  `git stash pop` would produce a Conway's-Game-of-Life merge conflict
  with very high probability of clobbering newer fixes (the migration
  #188 FK fix in `f99f47a`, the AUDIT #13 entry, etc.).
- **Action**: drop the stash. The recommended one-liner (NOT executed by
  this audit) is:
  ```bash
  ssh julianhabbig@100.69.44.108 'cd ~/Habbig && git stash drop "stash@{0}"'
  ```
  This is a no-op for code integrity (everything in the stash is already
  on origin) and removes the foot-gun.

The remaining `stash@{1}` ("wip: add climate (and disasters) subproduct
— 2026-05-03 deploy") was not in scope for this audit.

---

*Audit performed read-only; no `git stash pop` or `git stash drop` executed
on the server. Patch was extracted to `/tmp/server_stash_full.patch` for
slicing and removed at end of audit.*
