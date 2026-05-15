# Commit Summary — 2026-05-15

## Snapshot: `git log --oneline -50`

```
0e7efbb test(extension): record run — no test files matched glob
707d6a6 test(social): record social test run — no telegram/discord tests found
ad2c463 test(markets): record markets test run — 29 passed
db6041d fix(gateway): enforce GATEWAY_SSO_SECRET at startup — close empty-empty compare_digest bypass
4a50a0d fix(exports): remove guessable signing-key fallback + bind download to session
e7ab369 fix(logging): H-2 — close redaction gaps for JWT/Stripe-Sig/HMAC + url hint
845fc24 revert(prerelease): roll back to 2026-04-29 state (e8eaa68) per user directive
7f351a6 fix(exports): surface schema-drift in _safe_query via log+manifest, re-raise other OperationalErrors
0be2a2d revert(prerelease): hard-checkout pre-release files to verified-clean 0421267
1804a77 audit(webhooks): outbound delivery — HMAC/retry/DLQ/SSRF/body-size — 3 HIGH/5 MED/6 LOW/4 INFO
18b4727 audit(design): gateway/static/pages/*.css — 82 files, 2028 findings
46708cd fix(tests): re-pin db.conn after sibling tests reload the db module
a5be109 audit(openapi): 105 duplicate operation IDs from app.openapi() warnings
925c854 audit(trading-addon): self-grant + period-end semantics + missing webhook branch — 1 CRIT/3 HIGH
88a3917 audit(design): dashboards.html hub cards — 7 violations
1a157ac audit(jobs-dir): gateway/jobs/*.py cron/atomicity sweep — 5 CRIT/26 HIGH
3a77d2e audit(design): billing+account+profile+referrals+settings*.html — 162 findings
97bfdfd audit(server-admin): _require_admin_user call sites in gateway/server.py — 3 HIGH, 7 MED
ad5cf7a audit(gdpr-export): generator.py vs auth.db — 13 PII tables missed
0fea52c audit(rate-limits): gateway mutation routes — 41/90 covered, 9 HIGH
d4d1d36 audit(subproduct-dashboards): HMAC across 14 subproducts — 8 FAIL, 5 with no auth at all
3bf5126 audit(design): static images — 0 broken refs, all PWA icons present, 4 cosmetic findings
4673475 audit(env-example): gateway/.env.example — 53 missing vars, 2 name-mismatch bugs
0627dc8 audit(server-auth): server.py auth routes — 14 findings, 2 HIGH
f9f68ff audit(design): error templates — 38 violations
45d3d47 audit(account-delete): two flows diverge — 14 findings, 1 CRIT, 4 HIGH
e1217d3 audit(design): portfolio/trade/markets surfaces — 24 violations
dc341e9 audit(tests): conftest + shared fixtures — 14 findings, 3 HIGH
23042d4 audit(architecture): drift check vs ARCHITECTURE.md — 16 findings, 5 HIGH
68a6904 test(browser_e2e): record run — 1 passed, 12 failed, 59 skipped, 4 deselected
d6c4a36 audit(design): subproduct landing — 8 violations
0a77f4e audit(design): collections templates — 56 violations
f75029e audit(design_api_docs): design-system review — 10 violations across 4 files
27ba5ec audit(sw): add service-worker audit (1H/3M/3L/3I, 10 total)
31f0abf audit(design): /changelog page — 12 violations
70edddc audit(design_onboarding): adversarial review — 36 findings, 5 advisory
1b73dc0 test(profile_referrals): record pytest run — 68 passed, 5 failed
090a675 audit(cache): adversarial review of gateway/cache/{service,ttl}.py — 0C 2H 5M 6L
824c22c test(conftest): record isolation sanity — no target tests
7113d69 test(trading_addon): record pytest run — 12 passed, 15 failed
6528729 test(push): record run — 14 passed, 8 failed, 29 skipped
22cccb6 audit(audit_log): coverage map — 24 untracked privileged actions
19b695e test(market_portfolio): record run — 136 passed, 0 failed
7309a1f audit(design_support): support/feedback/contact design-system pass — 18 findings
84d519a audit(design_legal): /privacy /terms /dpa — 14 findings
0254bed audit(state_reconciliation): drift check — 11 stale claims
3d90865 audit(design_leaderboard): static/leaderboard{.html,.js,pages/leaderboard.css} — 8 violations
44f2e58 audit(design_admin_shell): design conformance — 64 violations
d4fb6f4 test(email): record run — 50 passed, 3 failed
ede51db audit(manifest): PWA manifest.json review — all required fields present, 3 polish gaps
```

## Commits today (2026-05-15)

**Total: 114 commits**

## Files touched today

**Total unique files: 171**

### Breakdown by area

- `audits/` — 91 audit reports (+`_audit_run.py` runner)
- `test_results/` — 41 test-run snapshots
- `gateway/` (code) — 30 source/test files
- Repo root docs — 5 (CHANGELOG.md, CLOUDFLARE_CHANGES.md, ENV_DEFAULTS_AUDIT.md, SERVER_STASH_INVENTORY.md, STASH_INVENTORY.md)
- `gateway/RUNBOOK.md` — 1

### Code files touched (gateway/)

```
gateway/admin_routes.py
gateway/api_keys_routes.py
gateway/db.py
gateway/email_system/sanitizer.py
gateway/export_routes.py
gateway/exports/generator.py
gateway/logging_config.py
gateway/market_routes.py
gateway/migrations/188_fix_users_invite_token_fk.py
gateway/pwa_middleware.py
gateway/queries/subscriptions.py
gateway/security/audit.py
gateway/server.py
gateway/static/fonts/InstrumentSerif-Italic.woff2
gateway/static/fonts/SourceSerif4-Variable.woff2
gateway/static/pages/prerelease.css
gateway/static/prerelease.html
gateway/static/settings_integrations.js
gateway/static/tokens.css
gateway/tests/conftest.py
gateway/tests/test_admin_jobs.py
gateway/tests/test_api_keys_admin_auth.py
gateway/tests/test_credentials_key.py
gateway/tests/test_db_conn.py
gateway/tests/test_export_routes.py
gateway/tests/test_exports_generator.py
gateway/tests/test_fonts_selfhost.py
gateway/tests/test_ip_hash_salt.py
gateway/tests/test_log_redaction.py
gateway/tests/test_migration_188.py
gateway/tests/test_newsletter_sanitize.py
gateway/tests/test_polymarket_siwe.py
gateway/tests/test_settings_integrations.py
gateway/tests/test_sso_secret.py
gateway/tests/test_subscription_pause.py
```

### Theme

Day was dominated by adversarial audits (security, design-system, architecture-drift, GDPR, jobs/cron, rate-limits, webhooks, HMAC) plus broad test-suite snapshotting and a handful of targeted fixes — SSO secret hardening, exports signing-key fix, logging redaction gaps, schema-drift surfacing, FK migration #188, and a prerelease rollback to the 2026-04-29 verified-clean state.
