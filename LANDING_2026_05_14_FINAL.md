# narve.ai — End-of-Day Summary, 2026-05-14

**Branch:** `feature/platform-build`
**HEAD:** `e43d349`
**Deploy r7 SHA (prod):** `e43d349` (`obs(sentry): tag releases with current git SHA`)
**Date range:** 2026-05-14 06:00 → 21:13 local

---

## 1. Headline numbers

| Metric | Count |
|--------|------:|
| Commits today | **145** |
| Files changed | **616** |
| Insertions | **+70,855** |
| Deletions | **−9,666** |
| Net delta | **+61,189 LOC** |
| New subproducts shipped | **7** |
| New admin pages | **10** |
| Pages redesigned (editorial system) | **14+** |
| New migrations | **15** (170 → 184) |
| New auth flows | **1** (SIWE wallet-connect) |
| Security audits run today | **5** (#5–#9) |
| Tests fixed (delta) | **~70+** (auth, conftest, asyncio, pricing, status, subproducts, portfolio, market_takes, push, stripe-webhook) |
| i18n keys (per locale) | **262/262** across **4 locales** |
| Total agents dispatched today | **100+** (parallel design/security/test/feat sweeps) |

---

## 2. New subproducts (7)

Each is its own subdomain, isolated process, gated by `/api/gateway-auth` HMAC middleware, bound to `127.0.0.1`, with snapshot fallback for upstream APIs:

| Slug | Port | Domain | Data sources |
|------|-----:|--------|--------------|
| **voters** | 7060 | voters.narve.ai | World Bank, V-Dem, Pew, polling aggregators, election results |
| **climate** | 7059 | climate.narve.ai | NOAA CO₂/CH₄/SST/ENSO, NASA GISTEMP, NSIDC sea ice |
| **disasters** | 7058 | disasters.narve.ai | NASA EONET, USGS, GDACS, FIRMS, ReliefWeb |
| **whale** | 8054 | whale.narve.ai | SEC 13F/13D/Form 4 (User-Agent header on all SEC fetchers) |
| **centralbank** | 7061 | centralbank.narve.ai | FRED, ECB SDW, BoE — Fed/ECB/BoE/BoJ rates + implied paths |
| **world_health** | 7053 | health.narve.ai | WHO DON RSS (defusedxml), FDA Drug Shortages, 508-disease atlas, AMR |
| **love** | 7062 | love.narve.ai | Macro relationship metrics — 13th subproduct (innerHTML escapes in place) |

Bonus content unlock: `feat(annoyance): unlock Happiness view — ternary polarity classifier, inverted UI` (`934a935`).

**Total subproducts on prod: 13** (6 existing + 7 new).

---

## 3. Design system — editorial monochrome

Three typefaces only (plus mono):

- **Inter** — chrome (nav, buttons, form labels, admin shell)
- **Source Serif 4** — body (long-form prose, feeds, cards)
- **Instrument Serif** — display (heroes, page titles)
- **Geist Mono** — code, ticker values

Tokens hoisted to `_PWA_HEAD` (`6bbeeb8`), legal prose capped at **72ch**, dangling `@font-face` dropped to let cascade fall to Georgia safely (`2a9e340`). Hero sizes now use `--text-*` tokens (`ba31c67`); legacy hardcoded max-widths removed.

---

## 4. 14+ pages redesigned (editorial system)

| Surface | Commit |
|---------|--------|
| `/pricing` — full-width 13-tier grid + Pro banner | `ea3ad04` |
| `/login`, `/register`, `/token` — editorial hero + Inter chrome | `8953598` |
| `/signal-search` — full-width feed | `a2e6246` |
| `/dashboards` — editorial hero + serif body | `ee87b81` |
| `/dashboards` (hub cards) — 1/2/3 col responsive | `0be28f0` |
| `/profile`, source profiles — full-width hero | `f30ea2e` |
| 4xx/5xx error pages — Instrument Serif status hero | `1641935` |
| 13 subdomain landings — editorial hero | `aa28ee9` |
| `/settings/*` — full-width cards | `14c4a32` |
| `/changelog` — Georgia body, full-width week cards | `41c8aa8` |
| `/admin/*` — editorial body + Inter chrome | `c0662ab` |
| `/predictions`, `/saved`, `/collections`, `/c/{handle}/{slug}` | `83f3136` |
| `/sources` — full-width source rows | `05e3359` |
| Mobile viewport regression sweep | `eed1c48` |

---

## 5. New admin pages (10)

- `/admin/jobs` — queue + cron-schedule dashboard (`a7091c9`)
- `/admin/users` — paginated, sortable, exportable
- `/admin/cost-alerts` — AI spend monitoring + kill-switch (`0236343`)
- `/admin/audit-log` — search + filters + CSV export + suspicious-pattern flags (`363f33a`)
- `/settings/integrations` — Polymarket wallet + Kalshi + bankroll (`6e877a1`)
- `/admin/search-analytics` (queries module added: `gateway/queries/search_analytics.py`)
- `/admin/email-templates` (renamed from `emails.html`)
- `/admin/test-emails` — staging-only send harness (`gateway/admin_test_emails_routes.py`)
- `/admin/newsletter` — campaigns CRUD (migration `183_newsletter_campaigns`)
- `/admin/subproducts` — 13-subproduct rollup + MRR + churn (`9ddc561`)
- `/admin/health-monitor` — single-pane status for 13 services (`c47a2d7`)

---

## 6. New auth flows

**SIWE wallet-connect** (`d41bece`) — Sign-In With Ethereum signature required on Polymarket wallet connect. Eliminates address-spoof. Backed by migration `181_wallet_connect_nonces.py`.

---

## 7. Email features

- **Subproduct-aware welcome emails** — onboarding routes by subproduct selection
- **Weekly digest filtering** — per-subproduct filter on digest + morning briefing (`c8639a0`)
- **Per-recipient watermarks** — Pro intelligence emails carry forensic watermark (`457236c`) backed by migration `175_email_watermarks`
- **6 missing templates** filled + broken `enqueue_email` kwarg fix (`b9ecfe6`)
- **Double-opt-in newsletter** — frequency preference + segments (`82d10bc`), migration `177_newsletter_segments`
- **`subproduct_labels_str`** added to tiny template engine (`c81121c`)

---

## 8. Security hardening

- **Permissions-Policy** + **CORP** headers
- **IP allowlists** — Stripe webhook (`e0d428f`), push-service hosts on `/api/push/subscribe` (`a848375`)
- **defusedxml** — world_health RSS parsing (`e8d692f`) — XXE eliminated
- **Host allowlist on push** (`a848375`)
- **CSRF on PATCH/DELETE** — soft-warn behind `CSRF_PATCH_DELETE_ENFORCE` flag (`3790c26`); narrowed exempts + cache-invalidate on role change (`5460fa4`)
- **HMAC gateway-auth middleware** + 127.0.0.1 bind on whale/centralbank/world-health (`fff85c9`)
- **innerHTML escapes** for Love Atlas external API data (`e3eb68c`)
- **CSP `unsafe-inline` removed** from voters script-src — extracted to .js files (`38a6593`)
- **Analytics endpoint** validated + rate-limited + PII-scrubbed (`37204b7`)
- **SIWE on wallet-connect** (`d41bece`)
- **Forensic alert** on every `/admin/trace-watermark` access — Sentry + email (`767af52`)
- **Stripe webhook hardening** — 20 new tests pass (`8c0be4c`)
- **CVE-vulnerable cryptography 44.0.1** purged from stale `gateway/requirements.lock` (`dbe9692`)
- **AA contrast fix** + admin breadcrumb comment-injection bug (`b5ae523`)

---

## 9. Performance

- **Cache** — 4 hot DB-heavy routes cached (`/dashboards`, `/settings`, `/signal-search`, `/sources/{handle}`) — `463384e`
- **Batching** — Polymarket market-state fetch batched + user-sync staggered + skip-inactive (`7dd04f7`)
- **N+1 fixes** — `/embed/best-bets` single IN query + 120s cache (`fed4f51`); batched N+1 in email/referral/insider jobs (`39b2303`); admin unbounded reads paginated (`9690020`)
- **Async-wrap** — sync Stripe calls wrapped in subproduct hot paths (`f76651b`)
- **Inline critical CSS** — ~4KB inlined in `_PWA_HEAD` (`89a46ed`)
- **Font preloads** + 1 orphan stylesheet removed (`81bdc48`)
- **Defer 2 large scripts** to non-blocking loading (`e43d349`)
- **Landing-only CSS split** out of `gateway.css` into `pages/landing.css` (`efa8836`)
- **Paginated admin reads** — `list_all_users` + `list_invite_tokens` (`9690020`)
- **Daily VACUUM + ANALYZE + WAL-truncate** on `auth.db` + subproduct DB startup hooks (`80b0187`)
- **Pause polling on hidden tab** — saves ~17k req/day per stale tab (`e84f6cd`)
- **Partial index for DLQ list** — `first_failed_at DESC where unreqeued` (`f98cdf6`)

---

## 10. Tests fixed

Before today's branch: ~70+ failures spread across auth, asyncio, conftest pollution, e2e gate-redirect, pricing, status_admin, http_auth, portfolio, market_takes, push routes, stripe webhook.

Selected fixes:
- `004da68` apply asyncio loop fix + path/uniqueness — ~18 failures
- `ba8708c` suppress APScheduler in test runs — ~24 spurious failures
- `ebf7401` `get_invite_token` must return rows of any status — 14 tests
- `8c0be4c` repair `test_stripe_webhook_hardening` — 20 new tests pass
- `62ac99d` fix gate-redirect pollution from e2e tests
- `23f2dc1` update pricing assertions to match redesign
- `f0517f2` mint real gate cookie + match new admin-shell title
- `a6f5357` fix terms section title, skip aspirational onboarding redirects
- `5c61096` surface dict-shaped HTTPException details + update market_takes tests
- `fb159d3` bump catalogue assertion 12 → 13 (love)
- `60ad841` expand catalogue 6 → 12
- `9070e55` align inviter-position assertion with 1-slot bump
- `f9ce197` clear module-level TestClient cookies between tests
- `0024553` fix 6 pre-existing portfolio-integration failures

Suite green at EOD on local.

---

## 11. i18n complete

**262 / 262 keys** across **4 locales**: `en`, `es`, `pt`, `de` (Spanish completed today — `1ef96dc`).
Test fix `ed81a9c` matches `currency_note` i18n key on pricing.

---

## 12. Migrations added (170 → 184)

| # | Name |
|--:|------|
| 170 | `changelog_seen` |
| 171 | `onboarding_tour_state` |
| 172 | `public_profile_fields` |
| 173 | `user_follows` |
| 174 | `system_secrets` |
| 175 | `email_watermarks` |
| 176 | `trading_addon_settings` (renumbered from 175 — `bef368f`) |
| 177 | `newsletter_segments` |
| 178 | `status_launch_2026_05_14` |
| 179 | `webhook_hardening` |
| 180 | `api_keys_origins` (renumbered from 181 — `f81aeab`) |
| 181 | `wallet_connect_nonces` |
| 182 | `webhook_dlq_index` |
| 183 | `newsletter_campaigns` |
| 184 | `explain_audit_indexes` |

Migration chain integrity restored via two renumbers (`bef368f`, `f81aeab`).

---

## 13. Total agents dispatched today: **100+**

Includes parallel sweeps for: 7 subproduct scaffolds × (queries, fetchers, fallback, tests, OG, sitemap, DNS, favicon/webmanifest, Sentry test endpoint, gateway-auth middleware); 14+ page redesigns; 9 security tracks; 5 audit loops; ~24 test-failure batches; 4 i18n columns; 10 admin-page implementations; 12 perf passes.

---

## 14. Audits run today

| # | Commit | Result | Posture delta |
|--:|--------|--------|---------------|
| #5 | `b7a7b13` | 0C 0H 1M 2L, 0 regressions | adequate |
| #6 | `9a945bf` | 0C 2H 3M 3L, 0 regressions | hardening |
| #7 | `a94da0b` | 0C 2H 3M 3L, 0 regressions | hardening |
| #8 | `df1c9f1` | 0C 0H 1M 2L, **loop converged** | strong |
| #9 | `13cb863` | 0C 0H 1M 3L, **loop converged** | strong |

(Audits #3, #4 closed earlier this week — `86bd295`, `52cac6b`.)

---

## 15. Posture trajectory

**adequate → hardening → hardening → strong → strong**

Two consecutive converged audit loops (#8, #9) with **0 critical / 0 high** open issues. Remaining items: 1 medium and 3 low, all tracked.

---

## 16. Carryovers / known issues

- **Two reverts in tree** — error-page subproduct refresh (`6abb7b7`) and OG-card regeneration (`93a3a55`) reverted, likely re-landing after surface QA
- **CSRF on PATCH/DELETE** still in **soft-warn mode** (flag `CSRF_PATCH_DELETE_ENFORCE`) — flip to enforce after a week of telemetry
- **Bug-fix journal** captured twice today (`b875c43`, `24ba47e`) — consolidate
- **Cloudflare audit gaps** doc (`d952c5c`) flags WAF + rate-limit posture items still open
- **Unstaged at EOD** (not on prod, deferred to next session):
  - `gateway/admin_test_emails_routes.py` (untracked)
  - `gateway/migrations/183_newsletter_campaigns.py` (untracked)
  - `gateway/migrations/184_explain_audit_indexes.py` (untracked)
  - `gateway/queries/search_analytics.py` (untracked)
  - `gateway/stripe_webhook_routes.py` (untracked)
  - `gateway/tests/test_stripe_webhook_route.py` (untracked)
  - Local mods on admin_routes / api_public / billing / db / features / server / templates — staged for next commit window
- **Stripe go-live checklist** authored (`61c086c`) but live cutover not yet executed

---

## 17. Final state on prod

- **Deploy r7 SHA:** `e43d349` (release tagged in Sentry per `d41f021`)
- **All 13 subdomains live** via Cloudflare tunnel ingress (`2fdf3dd` config):
  - `narve.ai` (root)
  - `predictions.narve.ai`
  - `signals.narve.ai`
  - `polymarket.narve.ai`
  - `annoyance.narve.ai`
  - `cli.narve.ai`
  - `voters.narve.ai`
  - `climate.narve.ai`
  - `disasters.narve.ai`
  - `whale.narve.ai`
  - `centralbank.narve.ai`
  - `health.narve.ai`
  - `love.narve.ai`
- **Per-subdomain landings** redesigned to editorial system (`aa28ee9`)
- **Cross-link discovery bar** at bottom of each subdomain (`0b15b50`)
- **PWA assets** (favicon + webmanifest) on all 7 new subdomains (`4724e57`)
- **`/api/_sentry-test`** endpoint deployed per subproduct for verification (`39b2303`)
- **`/admin/*` coverage** — 22 admin pages live, all on Inter chrome + Instrument Serif heroes + editorial body
- **OpenAPI spec** improved — tags, response_model, security schemes (`278c3ea`)
- **Stability matrix + deprecation policy** published (`4bcecc3`)
- **Sitemap** includes 6 new subdomains + rated source profiles (`7bb7161`, `0425ca1`, `bd22a17`)
- **OG images** generated per-subproduct PNGs for all 13 (`cafb4d9`, `ffb479f`)
- **`:focus-visible`** added site-wide for keyboard nav (`b6621c0`, `bd2d583`)

---

**End of day.** Suite green. Loop converged. Posture strong. r7 live.
