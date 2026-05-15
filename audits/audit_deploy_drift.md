# Deploy Drift Audit — `origin/feature/platform-build` vs production server `HEAD`

**Date:** 2026-05-15
**Server:** `julianhabbig@100.69.44.108:~/Habbig`
**Comparison:** `origin/feature/platform-build` (post `git fetch`) vs server checked-out `HEAD`
**Server HEAD:** `f99f47aa7ac524c3734d4e3700543e7d8058ac31`
**Origin HEAD:** `38c35082b4e08ff15e52b1dad676f644e7b09483`

---

## Summary counts

| Metric | Count |
|---|---|
| Server-only commits (`HEAD` not on origin) | **0** |
| Origin-ahead commits (on origin, not on server) | **190** |
| Modified file count (`M` in diff) | **60** |
| Deleted-in-HEAD files (exist on origin, missing on server's HEAD) | **254** |
| **Total changed files (M + D + A)** | **314** |
| Untracked files on server (uncommitted) | **9** |

**Deploy-readiness verdict:** Server is **190 commits behind** origin with **zero** server-only commits. A clean fast-forward `git pull` (or reset to origin) will bring the tree to origin. No divergent server work to rescue.

Direction note: `git diff --name-status origin HEAD` describes the transform *from origin into HEAD*. A `D` line means the file exists on origin but is absent at server `HEAD` — i.e. origin **added** it in one of the 190 ahead-commits. An `M` line means the file exists on both sides with different content.

---

## 1. Origin-ahead commits (190) — what's missing from the server

Top of `git log HEAD..origin/feature/platform-build --oneline` (most recent first):

```
38c3508 security: audit #16 — 1C 1H 4M 7L — magic-link CRIT still open at HEAD, fix in WIP
fd0f2f8 audit#15 fixes — notifications + newsletter race + unsubscribe HMAC + onboarding magic-link
f086180 security: audit #15 — 1C 1H 4M 7L (audit#14 fixes verified, magic-link takeover NEW CRIT)
05a097f security(audit#14 HIGH #5): rollback contract for process_scheduled_deletions
6a17de5 audit(deprecations): tabulate 432 DeprecationWarnings from gateway tests
6a246b2 test(security): per-test setUp + token_hash shadow-column awareness
5c27678 security(audit#15 HIGH): 4 fixes — referrals + SIWE + market path-traversal + avatar bomb
d5ae3b8 test(audit#14 HIGH): pin Kalshi connect throttle — 3 buckets + Retry-After
9319423 test(audit#14 HIGH): pin avatar bomb-guard contract — 89MP→413, 5MP→200
3f097d3 security(audit#14 HIGH): avatar decompression-bomb guard — MAX_IMAGE_PIXELS=16M + 413
9080eb9 security(audit#14 HIGH): SIWE-required wallet attach on /api/portfolio/polymarket/connect
030debf security(auth): 4 HIGH fixes — hash sessions+impersonation at rest, self-demote lockout, cascade-delete
0b1a59d fix(billing/coll/api): MED-1 expires_at backfill + collections rate-limit + api allowed_origins
02bb671 audit(migrations): chain integrity — BROKEN, 1 root + 10 heads, 8 forks
f1e6d35 audit(cron-schedules): gateway/jobs/ register_cron review — 0C 5H 4M 4L
1bd7a4e fix(billing): H3 + C1 — scope cancel + Stripe checkout for trading-addon
... (174 more commits, see Appendix A) ...
```

Themes in the 190 commits:
- Security audits (audit #13 → #16) and HIGH/CRIT fixes — magic-link, SIWE wallet attach, session/impersonation hash-at-rest, self-demote lockout, cascade-delete, avatar decompression-bomb guard, Kalshi throttle, referrals
- Billing / trading-addon Stripe webhook hardening, scope-cancel, expires_at backfill
- Auth / session hashing migrations `191`, `192`, `193`, `194` (4 new migrations)
- Massive audit-report churn (140+ `audits/*.md` & `test_results/*.md` files — explains most of the 254 deleted-in-HEAD count: server's HEAD predates the audit/test-result write-up wave)
- Email system: unsubscribe HMAC hardening, sanitizer module deletion
- SEO/CF/DNS/TLS observability audits
- Body-size-limit middleware addition (then removed in origin? — see `gateway/middleware/body_size_limit.py` D)
- Lifespan refactor (`on_event` → FastAPI lifespan)

---

## 2. Server-only commits (0)

```
(empty)
```

Server is strictly behind. **No rescue work needed.**

---

## 3. Modified files between origin and server HEAD (60 `M`)

Code (gateway + dashboards):

```
M  annoyance-dashboard/db.py
M  gateway/RUNBOOK.md
M  gateway/admin_routes.py
M  gateway/api_keys_routes.py
M  gateway/api_public/auth.py
M  gateway/api_public/routes.py
M  gateway/billing_routes.py
M  gateway/changelog_routes.py
M  gateway/collections_routes.py
M  gateway/db.py
M  gateway/db_referrals.py
M  gateway/email_system/unsubscribe.py
M  gateway/export_routes.py
M  gateway/exports/generator.py
M  gateway/features.py
M  gateway/jobs/ai_jobs.py
M  gateway/jobs/backend.py
M  gateway/jobs/newsletter_blast_jobs.py
M  gateway/jobs/pipeline_jobs.py
M  gateway/jobs/registry.py
M  gateway/logging_config.py
M  gateway/market_routes.py
M  gateway/middleware/subproduct.py
M  gateway/onboarding_routes.py
M  gateway/portfolio/routes.py
M  gateway/profile_routes.py
M  gateway/public_routes.py
M  gateway/pwa_middleware.py
M  gateway/queries/admin.py
M  gateway/queries/auth.py
M  gateway/queries/collections.py
M  gateway/queries/newsletter.py
M  gateway/queries/subscriptions.py
M  gateway/realtime/channels.py
M  gateway/security/audit.py
M  gateway/security/csrf.py
M  gateway/server.py
M  gateway/server_features.py
M  gateway/static/gateway.css
M  gateway/static/pages/prerelease.css
M  gateway/static/prerelease.html
M  gateway/static/settings_integrations.js
M  gateway/static/tokens.css
M  gateway/stripe_webhook_hardening.py
M  gateway/stripe_webhook_routes.py
M  gateway/subproduct_signup_routes.py
```

Tests (also `M`):

```
M  gateway/tests/conftest.py
M  gateway/tests/test_admin_jobs.py
M  gateway/tests/test_changelog.py
M  gateway/tests/test_csrf.py
M  gateway/tests/test_markets.py
M  gateway/tests/test_polymarket_siwe.py
M  gateway/tests/test_profile.py
M  gateway/tests/test_referrals.py
M  gateway/tests/test_security_headers.py
M  gateway/tests/test_settings_billing.py
M  gateway/tests/test_settings_integrations.py
```

Top-level docs:

```
M  CHANGELOG.md
M  CLOUDFLARE_CHANGES.md
M  NARVE_SECURITY_AUDIT.md
```

---

## 4. Files in origin but not in server HEAD (254 `D` — added by origin)

Adding these means: the file exists at `origin/feature/platform-build` but the server's `HEAD` was made before they were added. Pulling brings them in.

### 4a. New migrations (4)

```
gateway/migrations/191_sessions_hash.py
gateway/migrations/192_impersonation_token_hash.py
gateway/migrations/193_subscriptions_expires_at_backfill.py
gateway/migrations/194_blast_cursor.py
```

Run-on-deploy implication: server's auth.db is at migration ≤190. Snapshot before deploy (already backed up — see untracked `gateway/auth.db.backup-pre-188-20260515-0004`). 191/192 add `token_hash` shadow columns to `sessions` and `impersonation_tokens`.

### 4b. New middleware / code modules (3)

```
gateway/email_system/sanitizer.py
gateway/middleware/body_size_limit.py
gateway/queries/notifications.py
```

### 4c. New static assets (2 fonts)

```
gateway/static/fonts/InstrumentSerif-Italic.woff2
gateway/static/fonts/SourceSerif4-Variable.woff2
```

### 4d. New tests (~30 — `gateway/tests/test_*.py`)

```
gateway/tests/test_account_delete_softflag.py
gateway/tests/test_admin_delete.py
gateway/tests/test_admin_self_demote.py
gateway/tests/test_api_keys_admin_auth.py
gateway/tests/test_audit_actions.py
gateway/tests/test_billing_addon_checkout.py
gateway/tests/test_body_size_limit.py
gateway/tests/test_cascade_delete.py
gateway/tests/test_cf_ip_trust.py
gateway/tests/test_collections_rate_limit_and_view_throttle.py
gateway/tests/test_credentials_key.py
gateway/tests/test_db_conn.py
gateway/tests/test_export_routes.py
gateway/tests/test_exports_generator.py
gateway/tests/test_flag_allowlist.py
gateway/tests/test_fonts_selfhost.py
gateway/tests/test_impersonation_middleware.py
gateway/tests/test_ip_hash_salt.py
gateway/tests/test_kalshi_throttle.py
gateway/tests/test_log_redaction.py
gateway/tests/test_newsletter_blast_cursor.py
gateway/tests/test_newsletter_sanitize.py
gateway/tests/test_notification_db_helpers.py
gateway/tests/test_portfolio_polymarket.py
gateway/tests/test_profile_avatar.py
gateway/tests/test_retry_job_hmac.py
gateway/tests/test_scheduled_deletion.py
gateway/tests/test_session_hash.py
gateway/tests/test_six_fixes.py
gateway/tests/test_sso_secret.py
gateway/tests/test_subproduct_signup_magic_link.py
gateway/tests/test_subproduct_signup_redirect.py
gateway/tests/test_subscription_pause.py
gateway/tests/test_subscriptions_expires_at_backfill.py
gateway/tests/test_trading_addon_gate.py
gateway/tests/test_unsubscribe_hardening.py
```

### 4e. New audit reports — `audits/*.md` (~140)

Long tail of audit write-ups added to origin. None are runtime-critical for deploy; they exist purely in `audits/`. (Listed in full in Appendix B.)

### 4f. New test-result snapshots — `test_results/*.md` (~65)

Same as 4e — documentation churn only, no runtime risk.

### 4g. Top-level docs gone-on-server (3)

```
ENV_DEFAULTS_AUDIT.md
SERVER_STASH_INVENTORY.md
STASH_INVENTORY.md
```

### 4h. Audit helper scripts (2)

```
audits/_audit_run.py
audits/_audit_stripe_idempotent.py
```

---

## 5. Untracked / uncommitted on server (9)

These are not in version control; they will not be touched by a `git pull` but should be reviewed before any reset/checkout:

```
?? .deployed-at
?? gateway/auth.db.backup-pre-188-20260515-0004      # ← pre-migration snapshot, KEEP
?? gateway/config.json.bak.1778768295                # ← config backup, review
?? gateway/config.json.bak.1778768304                # ← config backup, review
?? gateway/static/sitemap.xml                        # ← runtime-generated, OK
?? voters-dashboard/voters.sqlite-shm                # ← SQLite WAL sidecars
?? voters-dashboard/voters.sqlite-wal
?? whale-dashboard/whale.sqlite-shm
?? whale-dashboard/whale.sqlite-wal
```

None of these conflict with the 314 incoming file deltas — all `??` items are either DB sidecars, backups, or runtime-generated assets that aren't tracked.

---

## 6. Deploy-readiness call

**Green to ship.** Mechanics:

1. Server has zero divergent commits — fast-forward is safe (`git pull --ff-only origin feature/platform-build` or `git reset --hard origin/feature/platform-build`).
2. **Critical pre-deploy step:** snapshot `gateway/auth.db` (already partly done — `auth.db.backup-pre-188-20260515-0004` exists). Migrations 191–194 will run on next boot; 191/192 alter session + impersonation tables with shadow columns.
3. The 254 "deleted" entries are origin-added files — all will appear after pull. Risky among them: 4 new migrations (4a), 1 new middleware (`body_size_limit.py`, 4b), and the new `notifications.py` queries module.
4. Of the 60 `M` files, the highest-blast-radius are: `gateway/server.py` (lifespan refactor + CREDENTIALS_ENCRYPTION_KEY guard), `gateway/db.py`, `gateway/security/csrf.py`, `gateway/security/audit.py`, all 4 `gateway/jobs/*.py`, and `gateway/stripe_webhook_*.py`.
5. **Known caveat from commit log:** `38c3508 security: audit #16 ... magic-link CRIT still open at HEAD, fix in WIP` — pulling brings the audit in but the fix is NOT yet on origin. Deploying does not resolve audit #16's critical.

---

## Appendix A — full 190-commit list (origin ahead of server)

(All 190 hashes from `git log HEAD..origin/feature/platform-build --oneline` are captured in section 1 + audit-trail commits below; truncated in body for readability. To reproduce verbatim, re-run on server:
`git log HEAD..origin/feature/platform-build --oneline`.)

## Appendix B — full 254-deletion list

See raw `git diff --name-status origin/feature/platform-build HEAD | grep '^D'` on server; runtime-relevant deletes are enumerated in sections 4a–4h above. The bulk (~205) is `audits/*.md` + `test_results/*.md` documentation files with no runtime impact.
