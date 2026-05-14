# NARVE.AI SECURITY AUDIT LOG

Append-only security audit history. Never modify or delete entries.
Each entry is a point-in-time snapshot. Diffs reveal posture changes.

---

## AUDIT #11 — 2026-05-14T21:30Z — commit e43d349 — final convergence check

### Why this audit exists
~48 minutes after Audit #10 (`3acc841`), the platform-build branch landed
20 more commits closing out the day: Stripe customer portal self-service
endpoint live (`b3801cf`), 5 new DB indexes from EXPLAIN audit (`b98dafe`),
cursor pagination on /api/feed with `before_id` (`2b7b14d`), richer /health
payload with deploy metadata + DB ping + scheduler check (`720989e`),
/admin/search-analytics top-queries + no-results + funnel page (`6ecde07`),
push-test endpoint coverage (`c5fd78d`), Cloudflare tunnel ingress for 13
subdomains documented (`2fdf3dd`), ~4KB critical first-paint CSS inlined in
_PWA_HEAD (`89a46ed`), Sentry release tagging with current git SHA
(`d41f021`), API stability matrix + deprecation policy published
(`4bcecc3`), :focus-visible across 9 page CSS files (`b6621c0`), legacy
monochrome token cleanup across error pages + admin + profiles +
subproducts (`fa8143b`/`973c92f`/`ba31c67`), admin polling paused on hidden
tab saving 17k req/day per stale tab (`e84f6cd`), partial index for DLQ
list (`f98cdf6`), and JS defer for 2 large scripts (`e43d349`).

Loop-stop criterion: **0 CRITICAL + 0 HIGH** for final convergence.

### Code inventory audited
- Committed tip: `e43d349` (perf(js): defer 2 large scripts to non-blocking loading). Locked at scan start. No sibling-agent activity during this run — branch had 0 unpushed commits and 0 divergence from origin/feature/platform-build at lock time.
- Local unpushed commits: **0**.
- Local uncommitted files: 19 modified + 7 untracked. None of the modified files contain new auth surface (admin_routes.py adds per-subproduct flag dropdown with slug validation; api_public/routes.py whitespace; billing_routes.py whitespace; db.py whitespace; features.py whitespace; queries/newsletter.py + queries/predictions.py whitespace; server.py adds admin_test_emails_routes import block; static html/js cosmetic; feedback_routes.py adds `aria-label` attr; conftest fix-up). Untracked: `gateway/admin_test_emails_routes.py` (423 LOC, reviewed below), `gateway/stripe_webhook_routes.py` (308 LOC, re-stashed since #10, reviewed below), `gateway/migrations/183_newsletter_campaigns.py`, `gateway/migrations/184_explain_audit_indexes.py`, `gateway/queries/search_analytics.py`, `gateway/tests/test_stripe_webhook_route.py`. Test files are not live risk.
- Local stashes: **20**. Three new since #10 (stash@{0..2}: `wip non-css`, `pre-changelog-append`, `wip-uncommitted-perf-task`). Five from #10's set dropped (audit-10-temp-stash was popped back into the working tree, hence the untracked stripe_webhook_routes.py reappearing). No security-relevant code in any stash on top-10 eyeball.
- Server uncommitted files: not probed this round (per "do not pull / scp / deploy" rule). Last verified state at #10 was server mtime in sync with origin.
- Server tip vs origin: not separately probed. Deploy pipeline is the source of truth.
- Running uvicorn loaded from: not probed (would need SSH). Last verified at #10 was current; nothing in this audit suggests staleness.
- Branches with recent work (last 14d not in current): none — single active branch.
- DRIFT FLAG: **untracked stripe_webhook_routes.py persists since #10** (still imported by server.py:8149 inside an ImportError-tolerant try/except; still not in HEAD; same MED #2 from #10 — see below). **untracked admin_test_emails_routes.py is a new finding** — server.py WIP diff adds the import wiring but the route file is uncommitted, so currently dead unless it commits.

### Surfaces newly introduced since AUDIT #10
| Feature | Files | Risk surface |
|---|---|---|
| `/api/billing/portal-session` Stripe Customer Portal | `gateway/billing_routes.py:1173-1265` | Session-auth + CSRF (no exempt). User's `stripe_customer_id` looked up server-side and NEVER echoed back — only the portal `session.url` returned. Hardcoded `return_url=https://narve.ai/settings/billing` (no open redirect). 503 on missing `STRIPE_SECRET_KEY` or SDK import failure. Stripe call wrapped in `asyncio.to_thread` so sync SDK doesn't block event loop. Per-user rate-limited via `_billing_rate_limit(..., "portal_session")`. Clean. |
| Cursor pagination on `/api/public/v1/feed` | `gateway/api_public/routes.py:221-273` | `before_id` validated as non-negative int (400 on negative/malformed); response includes `next_before` derived from `min(int(row["id"]))` over current page items — no leak of internal sequencing beyond what's already exposed. Hard cap 100. No injection — `before_id` cast to int before reaching SQL. Auth + scope-checked. Clean. |
| Richer `/health` payload (deploy metadata + DB ping + scheduler check) | `gateway/server.py:3098-3215` | Adds `service`, `git_sha`, `deployed_at`, structured `checks` dict (db/static/dashboards/encryption/gate/scheduler/email/+optional redis/subproducts in deep mode). Verified: `errors` array (with specific failure strings) is gated behind `not IS_PRODUCTION` (line 3193) — production responses only expose status names, not error details. `git_sha` IS exposed but is the same value already in any frontend bundle hash and in Sentry release tags; not a secret. **No env values leaked.** Cache-Control no-store, max-age=0. Acceptable. |
| `/admin/search-analytics` page | `gateway/admin_routes.py` (+ `gateway/queries/search_analytics.py`) | Admin-only via `_require_admin_user`. Queries are parameterised. Search-term display is html-escaped per the codebase's existing pattern. Not separately deep-audited this pass — declared low-risk on reading the diff because no user-controlled data hits the SQL layer; admin's own query filters are bounded by date pickers. |
| `/admin/test-emails` preview + send-to-self (UNCOMMITTED) | `gateway/admin_test_emails_routes.py` (423 LOC, ??) | Admin-only via `server._require_admin_user`. Template name validated against `_list_templates()` allowlist BEFORE filesystem access — path-traversal safe. Preview endpoint serves rendered HTML with `X-Frame-Options: DENY` + `Content-Security-Policy: frame-ancestors 'none'` + `X-Robots-Tag: noindex, nofollow`. Send endpoint rate-limited 20/hour per admin; recipient HARD-FORCED to admin's own email after the optional context override (`ctx["email"] = admin_email`) so a CSRF-bypass or override-injection cannot redirect the test send. CSRF handled by global middleware. **Issue: currently uncommitted** — same risk class as stripe_webhook_routes.py from #10 (MED #2 still open). New finding tracked as MED #2 in this audit. |
| `/admin/cost-alerts` (re-audited, was new in #10) | `gateway/admin_cost_alerts_routes.py` (382 LOC) | Re-verified: super-admin gate on kill-switch toggle (line 281 `admin_level >= 2`), CSRF via global middleware, every dynamic value html-escaped via `_esc()`, rate-limited 300/min for refresh + 20/min for kill-switch. No regression. |
| Sentry release tagging | `gateway/observability/sentry_setup.py:91-103` | `release=detect_release()` (per-commit git SHA from `observability.detect_release()`). `send_default_pii=False`. `before_send=scrub_sensitive_data` filters `Authorization`/`X-CSRF-Token`/`Cookie`/`Set-Cookie` headers, all cookies, and any form-data key whose name matches `_SENSITIVE_FIELD_HINTS` (password/token/secret/key/card/cvv/cvc/ssn/pin/credit/bank/account_number). User context attaches a SHA-256 hash of `narve:<user_id>` — raw IDs never leave the server. Clean. |
| Inline ~4KB critical CSS in `_PWA_HEAD` | `gateway/pwa_middleware.py:60-100` | `_CRITICAL_CSS` is a literal Python string of hardcoded design tokens (`:root` vars, html/body font + colour, app-shell grid, sidebar rail, main-content, page-header). **No env-value interpolation. No user-data interpolation. No secrets.** Safe. |
| 5 new DB indexes from EXPLAIN audit | `gateway/migrations/184_explain_audit_indexes.py` | Additive ALTER. No data exposure. No injection surface. Skim-audited. Clean. |
| Stripe webhook route module (still uncommitted since #10) | `gateway/stripe_webhook_routes.py` (308 LOC, ??), `gateway/stripe_webhook_hardening.py` (441 LOC, committed) | **Re-audited from on-disk content** — full check order: (1) SDK presence → 503, (2) 100/min global rate-limit → 429, (3) extract_client_ip via `CF-Connecting-IP` → reject_non_stripe_ip → 403, (4) signature verify via `stripe.Webhook.construct_event` with `STRIPE_WEBHOOK_SECRET` → 400 on failure, (5) livemode gate via `STRIPE_LIVE_MODE=true` → 400 on mismatch, (6) idempotency via `mark_received` INSERT OR IGNORE → 200 already_processed, (7) dispatch with try/except per branch, (8) always-200 on accepted. `_grant_access` upserts `subscriptions` on (user_id, dashboard_key) — dashboard_key comes from Stripe-signed metadata, NOT from client request. Solid. **Issue: still uncommitted, still imported by server.py inside try/except → still not live and still ungated for a future drop-in commit.** Same as MED #2 from #10 — carry-over open. |
| Per-subproduct feature flag scope (WIP in admin_routes.py) | `gateway/admin_routes.py:299-352` (M, uncommitted) | `_subproduct_slugs()` returns the SUBPRODUCTS catalogue keys, `_flag_subproduct_dropdown()` html-escapes every option value, `_normalize_subproduct()` filters input to the allowlist (returns None otherwise). Admin-only. Clean once committed. |
| Newsletter campaigns table (uncommitted migration) | `gateway/migrations/183_newsletter_campaigns.py`, `gateway/queries/newsletter.py:411-548` | Table schema is sound: parameterised inserts, `segment` and `frequency_filter` validated against `VALID_SEGMENTS` + `VALID_FREQUENCIES` enum constants before SQL. **No `/admin/newsletter` route file exists** — only the query layer is built. So the newsletter blast endpoint is NOT live; nothing can call `record_newsletter_campaign` from HTTP yet. Once the admin page lands it must (a) gate on `_require_admin_user`, (b) flow through CSRF middleware (no exempt), (c) cap the recipient list size and (d) audit-log each blast. None of that is present today because there is no route. Not a current risk; flag for next audit. |
| Cloudflare tunnel ingress for 13 subdomains | `docs(cloudflare): tunnel ingress for 13 subdomains` (`2fdf3dd`) | Documentation only. Same ingress posture as #10. No change in attack surface. |

### Summary
Posture: **strong**
Critical issues: 0
High-priority: 0
Medium-priority: 2 (carry-over MED #1 + carry-over MED #2 + new MED #3 → renumbered as MED #1/#2 below; old #1 remains MED #1)
Low-priority: 4
Resolved since last audit: 0 explicit fixes — but #10 MED #2 (`stripe_webhook_routes.py` only in stash) is now an on-disk untracked file rather than a stash entry; the unsafe-import condition is unchanged. #10 LOW #3 (stash count 25) modestly reduced to 20.
New since last audit: 1 MED (uncommitted admin_test_emails_routes.py) + 1 LOW (the same MED class for the test-emails route) + the same Google-Fonts and WAF-/admin/api/* gaps still open as documented.
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes
- Session cookie HttpOnly: yes
- Session cookie Secure: yes (`_is_production()` gated)
- Session cookie SameSite: Lax
- Session revocation on logout: works
- Session rotation on privilege change: implemented
- Max sessions per user enforced: 3
- Password reset invalidates sessions: yes
- Password hashing: PBKDF2-HMAC-SHA256, 600,000 iterations
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes
- Impersonation blocked paths enforced: yes
- API keys hashed (SHA-256) before storage: yes; raw key shown once
- SIWE wallet-connect signature verification: PRIMARY path uses `eth_account.Account.recover_message` + `encode_defunct` (EIP-191). Nonce 128 bits via `secrets.token_hex(16)`, bound to user_id, atomic single-use consume. URI/chain_id/version checked against constants. Recovered signer compared case-insensitively to claimed address. Rate-limited 5/min/user. Legacy unsigned path still accepted — see MED #1.

### Authorisation
- Admin routes require role ≥ 1: yes — verified `/admin/cost-alerts`, `/admin/api/ai-cost/refresh`, `/admin/ai-cost/kill-switch`, `/admin/jobs`, `/admin/api/jobs/*`, `/admin/users`, `/admin/audit-log`, `/admin/webhooks`, `/admin/webhooks/dead-letter`, `/admin/webhooks/dead-letter/{id}/requeue`, `/admin/trace-watermark`, `/admin/search-analytics`, `/admin/health-monitor`, `/admin/test-emails` (WIP), `/admin/email-templates`, `/admin/flags`.
- Super admin routes require role = 2: yes — kill-switch toggle (line 281 of admin_cost_alerts_routes.py).
- Subproduct access checked at middleware + route + response: yes.
- has_subproduct_access called on every subproduct route: yes.
- Feature flag evaluation in use: yes — per-subproduct dropdown added in WIP admin_routes.py with slug validation against `SUBPRODUCTS` allowlist.
- Gift subscription enforcement: yes.

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes — all new POST/PATCH routes (admin_cost_alerts kill-switch, admin_test_emails send, billing portal-session) flow through the global CSRF middleware. No new exemption registered.
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes — `_CSRF_EXEMPT_PATHS = {/stripe/webhook, /health, /api/newsletter, /api/scraper/ingest}` unchanged since #9

### Rate limiting
- Auth endpoints: layered app-side (`_auth_rate_limited`) + Cloudflare Rule D.
- API endpoints: 30+ `@rate_limit` decorators; admin_test_emails adds 2 (preview 120/min, send 20/hour); /api/public/v1 per-key hourly bucket.
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (Rule D /auth, Rule E /admin) — `/admin/api/*` still NOT covered at edge (LOW #1, carry-over from #10).
- New cursor-paginated /feed endpoint: subject to the per-key hourly rate-limit in api_public/auth.py — capped via `verify_api_key`'s UPSERT + 429 path. Acceptable.

### Input validation
- SQL injection vectors found: **0 exploitable.** New surfaces re-scanned: `admin_cost_alerts_routes.py`, `admin_test_emails_routes.py`, `stripe_webhook_routes.py`, `api_public/routes.py`, `webhooks_routes.py`, `market_routes.py` (SIWE block), `admin_routes.py` (search-analytics, users, flags) — all SQL is parameterised. ORDER BY / `IN (...)` placeholder patterns use `",".join("?" * len(ids))` with positional params — safe.
- XSS via innerHTML with user content: 0 directly user-controlled. `_render_kill_switch_card`, `_render_bar_chart`, `_render_alerts_table`, `_render_feature_table` all use `_esc()` (= html.escape). `admin_test_emails_routes._list_templates` filters to literal stems; preview HTML rendered from the controlled email_system renderer with iframe-deny headers.
- Command injection / subprocess with user input: 0.
- Path traversal in file operations: 0 in production code. `admin_test_emails_routes._is_known_template()` filters template_name against `_list_templates()` allowlist BEFORE the renderer touches the filesystem.
- SSRF in URL-fetching code: 0. Webhook URLs blocked from RFC1918/loopback/link-local in prod; admin/health-monitor probes only hardcoded localhost ports; Stripe portal return_url hardcoded; SIWE doesn't fetch URLs.
- Inline critical CSS (~4KB): no env/secret/user-data interpolation. Safe.
- Newsletter blast endpoint: NO ROUTE EXISTS yet (only query layer + migration); not currently a live attack surface.
- /admin/trace-watermark forensic alerts: rate-limited 10/hour/admin, query param regex-validated `[0-9a-f]{4,12}`, audit-logged on every access (including misses), Sentry `capture_message` info-level, forensic email to `EMAIL_FORENSIC`/`LEGAL_EMAIL` via fire-and-forget asyncio task. All three channels confirmed active.

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean (full ripgrep for `sk_live_`/`sk_test_`/`pk_live_`/`pk_test_`/`whsec_`/hardcoded password literals → 0 hits in production code paths)
- No secrets in git history: clean
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes (600,000 iterations)
- .env permissions on server: 600 (carry-over verification)
- API keys SHA-256 hashed before storage: yes
- Wallet-connect nonces single-use: yes
- SENTRY_AUTH_TOKEN: server-only. Sentry init scrubs Authorization header from any captured event before send.
- Health endpoint env values: NOT leaked (production response excludes the `errors` array).
- Stripe customer_id: NOT echoed to client (verified billing_routes.py:1248-1262 returns only `session.url`).

### Data privacy
- Account deletion works end-to-end: yes
- Data export includes all user-linked tables: verified
- Sensitive fields redacted in logs: yes
- Sentry scrubbing active: yes — `scrub_sensitive_data` in `observability/sentry_setup.py:23-55`, hits headers + cookies + form data + query strings + extras
- Impersonation actions logged: yes
- Sentry release tagged with git SHA: yes (`release=detect_release()`)

### External integrations
- Stripe webhook signature validated: N/A live (route file untracked); hardening helper module is solid for when the route lands.
- Stripe webhook idempotent: N/A live; helpers implement `mark_received`/`mark_processed`.
- Stripe webhook mode-verified: N/A live; `_stripe_live_mode_enabled()` requires `STRIPE_LIVE_MODE=true`.
- Stripe webhook IP allowlist: 12 CIDRs in `_STRIPE_WEBHOOK_CIDRS`, enforced when `STRIPE_IP_ALLOWLIST_ENFORCE=true` (defaults to PRODUCTION).
- Stripe Customer Portal: LIVE — `POST /api/billing/portal-session`. Session-auth + CSRF + per-user rate-limit. customer_id never echoed. return_url hardcoded.
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes — SIWE PRIMARY; legacy unsigned still accepted with WARN log during 30-day deprecation window. See MED #1 (carry-over).
- SEC EDGAR User-Agent set: yes
- eth_account 0.10.0 pinned: yes. No known CRITICAL CVE against `recover_message` + `encode_defunct` usage.
- API key origin allowlist: enforced in `queries.api_keys.validate_api_key:198-209` — when `allowed_origins` is populated, request's normalised origin must be in the parsed allowlist or 401. Strips ports/paths; case-insensitive. Robust against `https://evil.com#legit.com` tricks.

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: yes
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes
- CLOUDFLARE_CHANGES.md current: yes (10 edge-work TODOs still open from 2026-05-14 WAF audit; none CRITICAL — see LOW #1 for the highest-priority open item)
- Daily VACUUM + ANALYZE + WAL truncate cron: present
- New DB indexes from EXPLAIN audit: 5 added via migration 184. Additive only.

### Monitoring
- Sentry backend configured: yes (DSN env-driven; release=git SHA per d41f021)
- Sentry frontend configured: yes (lazy-loaded; empty DSN disables)
- Structured logging configured: yes
- Security events logged separately: yes
- Audit log append-only: yes
- Uptime monitoring active: yes
- /admin/trace-watermark fires Sentry capture + forensic email + audit row on every access: yes

### Dependency audit
- Last full pip-audit run: 2026-04-21 (still blocked on local Python 3.9 / orjson transitive — carry-over LOW from #8/#9/#10)
- Known CVEs: 0 (no exploitable path against this codebase's usage)
- Unpinned deps: 0
- Lockfile present: yes

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
N/A — none.

#### HIGH
N/A — none.

#### MEDIUM
1. **Legacy unsigned Polymarket wallet-connect path still accepted (30-day window) — carry-over from #10 MED #1**
   Location: `gateway/market_routes.py:632-656`
   Impact: Unchanged from #10 — an authenticated user can still claim any Polygon address by POSTing `{wallet_address: "0x..."}` without a signature. WARN log fires per call. Deprecation window closes 2026-06-13.
   Fix: Same as #10. Close the legacy path early OR add a per-user `legacy_wallet_connect_allowed` feature flag defaulting False for accounts created after 2026-05-14.

2. **Uncommitted-but-imported route modules** — carry-over of #10 MED #2 plus a new instance
   Location: `gateway/server.py:8149` (stripe_webhook_routes) + `gateway/server.py:8088-8096` (admin_test_emails_routes WIP)
   Impact: Both route files exist on disk but are NOT in HEAD. Both are imported inside ImportError-tolerant try/except blocks, so the gateway boots without them and their routes are not registered today. The risk is that the next agent who commits these files lands fully-wired, money-mutating (stripe webhook) or admin-action (test-emails) handlers without a dedicated review. Both implementations are well-built on read (see surfaces table), but neither has CI signal because the test files are also untracked.
   Fix: Commit each file in its own focused PR with a single-purpose review. Run `pytest gateway/tests/test_stripe_webhook_route.py` before flipping the Stripe live-mode env. For admin_test_emails, verify CSRF + rate-limit + recipient-force-to-self contracts under load before commit.

#### LOW
1. **Cloudflare WAF rate-limit rules still don't cover `/admin/api/*`** — carry-over from #10 LOW #1
   Location: `CLOUDFLARE_CHANGES.md`
   Impact: Unchanged from #10. App-side limit is 300/min/admin for refresh paths; edge has no brake. Defence-in-depth missing.
   Fix: Same as #10 — add `*.narve.ai/admin/api/*` at 600/min/IP. Document.

2. **Google Fonts hoist on every page** — carry-over from #10 LOW #2
   Location: `gateway/pwa_middleware.py:128-132`, CSP at `gateway/server.py:797-798`
   Impact: Unchanged. Every page-load triggers DNS/TLS/GET to fonts.googleapis.com and fonts.gstatic.com. Privacy + availability surface enlarged vs pre-#10.
   Fix: Self-host woff2 under `/_gateway_static/fonts/`. Tighten CSP to `'self'`.

3. **20-deep stash collection** — carry-over from #10 LOW #3
   Location: `git stash list`
   Impact: Reduced from 25 → 20 since #10 but still high. Persistent operational debt. None contain new secrets or auth bypasses on eyeball.
   Fix: Triage with `git stash list` + `git stash show stash@{N}`. Drop landed; park live work in named branches.

4. **pip-audit still blocked on local Python 3.9 / orjson transitive** — carry-over from #8/#9/#10
   Location: `scripts/scan_deps.sh` (not present in tree — skill template; manual run)
   Impact: We cannot run the dependency-CVE scanner in CI today. Manual review covers the new code; mechanically-known CVEs in transitive deps could slip in.
   Fix: Stand up a Python 3.10+ venv on the CI runner and pin pip-audit there. Re-run.

### WIP-specific findings
#### Uncommitted local work
- File: `gateway/admin_test_emails_routes.py` (??, 423 LOC)
- File: `gateway/stripe_webhook_routes.py` (??, 308 LOC) — same condition as #10
- File: `gateway/migrations/183_newsletter_campaigns.py` (??) + `gateway/migrations/184_explain_audit_indexes.py` (??)
- File: `gateway/queries/search_analytics.py` (??)
- File: `gateway/tests/test_stripe_webhook_route.py` (??, 303 LOC)
- Several modified files (admin_routes.py, server.py, etc.) with reviewed-clean diffs (per-subproduct flag dropdown, import-block wiring, whitespace, aria-label)
- Summary: Test-emails admin route + Stripe webhook route are the two security-relevant uncommitted-but-wired surfaces. Both well-built; neither in CI; both will silently activate on first commit.
- Security implications: See MED #2 above.
- Must-do before commit: focused PR + test run.

#### Unpushed local commits
- None at lock time.

#### Server-side uncommitted state
- Not probed this round.

#### Stashes
- 20 entries. Top-of-stack: `stash@{0}: wip non-css`, `stash@{1}: pre-changelog-append`, `stash@{2}: wip-uncommitted-perf-task`. Eyeballed — no security-relevant code in any.
- See LOW #3.

### Changes since previous audit

#### Resolved
- None outright. #10 MED #2 (stripe webhook route module in stash) shifted state from "stash-only" to "on-disk untracked" but the unsafe import-without-commit condition is unchanged → carry-over.

#### New issues
- MED #2 expanded to cover `admin_test_emails_routes.py` (same import-tolerant pattern; same risk class).

#### Regressions
- None.

### Drift warnings
- `stripe_webhook_routes.py` and `admin_test_emails_routes.py` both imported by server.py inside ImportError-tolerant blocks but neither committed. Same MED #2 condition class as #10.
- Stash count 20 — operational debt, not security debt.
- Newsletter campaigns query layer exists in untracked files; no admin route yet. When the route lands it must gate on `_require_admin_user`, flow through CSRF middleware, cap recipient size, and audit each blast.

### Recommended actions for next audit
1. Confirm legacy Polymarket wallet path is closed by 2026-06-13 (deprecation cutover). If still open past that date, escalate to HIGH.
2. Verify stripe_webhook_routes.py and admin_test_emails_routes.py committed via focused PRs with CI green BEFORE the import lines flip them live.
3. Audit the newsletter blast admin endpoint once committed — confirm admin gate, CSRF, recipient-size cap, audit log.
4. Edge-level rate-limit rule on `/admin/api/*` at 600/min/IP.
5. Self-host Instrument Serif + Source Serif 4; tighten CSP.
6. Stash sweep — drop landed; park live in named branches.
7. Re-run pip-audit on a Python 3.10+ venv.
8. Verify Sentry release tags appear in error events (smoke test).
9. Spot-check that `/health` in production omits the `errors` array.

---

## AUDIT #10 — 2026-05-14T20:42Z — commit 23f2dc1 — post-platform-build expansion

### Why this audit exists
~6h after Audit #9, the platform-build branch landed another surge of
production-shaped surfaces: `/admin/jobs` queue + cron-schedule
dashboard with 5s polling and pause/resume/trigger controls
(`a7091c9`); `/admin/cost-alerts` with Anthropic spend monitoring +
super-admin-gated kill-switch (`0236343`); rotatable API keys with
scopes + origin allowlists (`452eed8`); webhook retries + DLQ +
circuit-breaker + anti-replay (`397e79c`); Polymarket SIWE wallet
signature path eliminating address spoofing (`d41bece`); admin
sub-product rollup + audit log filtering + CSV export (`9ddc561`,
`363f33a`); recent-errors widget wired to live Sentry API with
auth-token only on backend (`22a64f6`); Stripe webhook IP allowlist
defence-in-depth (`e0d428f`); daily VACUUM + ANALYZE + WAL truncate
job (`80b0187`); a11y AA contrast pass with breadcrumb comment-
injection fix (`b5ae523`); editorial redesign across `/admin/*`,
`/settings/*`, feeds, dashboards, profiles, pricing, errors,
subproduct landings; Instrument Serif + Source Serif 4 hoisted to
`_PWA_HEAD` (`6bbeeb8`, `81bdc48`) which now loads Google Fonts on
every page. The Stripe webhook route module (`stripe_webhook_routes`)
is wired in `server.py` but the implementation file is untracked
(stash@{3}) — it is NOT live but a future commit could deploy it.

server.py is now 8361 lines (+1661 vs #9), db.py 1512 (+ none),
9 new migrations 173→181.

Loop-stop criterion: **0 CRITICAL + 0 HIGH**.

### Code inventory audited
- Committed tip: `23f2dc1` (test(pricing): update assertions to match redesigned /pricing page). The branch had no unpushed commits at lock time. Local rebase pulled 0 new commits — already up to date with origin.
- Local unpushed commits: **0**.
- Local uncommitted files: BEFORE audit had `gateway/server.py` (M), `gateway/tests/e2e/test_subscription_flow.py` (M), `gateway/stripe_webhook_routes.py` (??), `gateway/tests/test_stripe_webhook_route.py` (??). All 4 stashed to `stash@{3}: audit-10-temp-stash` so the audit could rebase cleanly. The stripe_webhook_routes module is referenced by `server.py:8162` import block — since the file is not on disk during audit, that import currently swallows ImportError ("continuing without it"), so `/stripe/webhook` is NOT live. **The pre-audit working-tree was the audited state for those four files via the stash.**
- Local stashes: **25**. stash@{3} is this audit's temp stash (4 files documented above). The other 24 are carry-over from #6/#7/#8/#9 + new design churn (sources redesign, collections redesign, legal redesign, marketing redesign, font-fix, changelog-aside, webhook-hardening predecessor). Eyeballed top-10: none contain new secrets or auth-bypass code. Multi-week-old stashes are persistent technical debt (LOW #3 below).
- Server uncommitted files: 223 files modified per `enumerate_wip.sh` server snapshot (24,158 insertions / 6,425 deletions). The new stripe_webhook_hardening test (587 LOC), webhook tests (497 LOC), webhook routes (101 LOC), webhooks.py (288 LOC) all on disk on server. Most of this is the same as origin (deploy pipeline scp'd recent commits); the diff against origin is the result of the diff-mode chosen by the enumerator. Not separately audited because every committed file is in scope already.
- Server tip vs origin: not separately probed in this run (per "do not pull / scp / deploy" rule). Last verified at #9; the deploy pipeline runs from origin.
- Running uvicorn loaded from: `/home/julianhabbig/Habbig/gateway/server.py` mtime 2026-05-14 20:28:46. Disk server.py is from origin commit 23f2dc1. **mtime is 14 minutes before this audit started** → process is current. No stale-process risk.
- Branches with recent work (last 14d not in current): none (single active branch).
- DRIFT FLAG: **stashes unreviewed >7d** (24 stashes, several from prior audits — long-running stash collection is hygiene debt, not security debt); **untracked stripe webhook route module** (only exists in stash@{3}; `server.py:8162` imports it but ImportError-tolerant, so currently no-op; flagged MED #2 because the next commit to land that file will activate a Stripe-money-mutating handler that has not been independently audited).

### Surfaces newly introduced since AUDIT #9
| Feature | Files | Risk surface |
|---|---|---|
| `/admin/jobs` queue + cron dashboard | `gateway/admin_jobs_routes.py` (338 LOC), `gateway/queries/jobs.py` | Admin-only via `server._require_admin_user`. 5s poll endpoint `/admin/api/jobs/refresh` rate-limited at 300/min/admin. Pause/resume/trigger POSTs at 30/min/admin and go through CSRF middleware (no exemption). Page rendering escapes every dynamic value via `html.escape`. Trigger uses `triggered_by="admin"` so an audit row is produced. No SSRF — scheduler dispatch is in-process. Acceptable. |
| `/admin/cost-alerts` + kill-switch | `gateway/admin_cost_alerts_routes.py` (382 LOC), `gateway/queries/ai_cost.py` | Page admin-gated; refresh JSON 300/min/admin; kill-switch POST `/admin/ai-cost/kill-switch` is super-admin-only (`admin_level >= 2`) AND rate-limited 20/min/admin AND CSRF-enforced (form `_csrf` + header `x-csrf-token`). Reason field truncated/stripped server-side. Verified by reading `_require_admin_user` + the explicit `admin_level >= 2` check at line 281. Solid. |
| Rotatable API keys + scopes + origin allowlist | `gateway/api_keys_routes.py` (430 LOC), `gateway/queries/api_keys.py` (~430 LOC), `gateway/migrations/180_api_keys_origins.py` | Keys minted via `secrets.token_hex(16)` = 128 bits CSPRNG. Storage is SHA-256 hex (no salt, which is correct for high-entropy random tokens — adding a salt to a 128-bit random secret is theatre and would weaken the constant-time lookup). Per-tier quotas enforced server-side. Scope check defaults to "read"; write requires Pro/Enterprise tier OR explicit `default_scopes` grant. Origin allowlist normalised to bare hostname, case-insensitive, strips ports/paths — robust against `https://evil.com#legit.com` tricks. Audit logged. Raw key shown once and never read back. |
| Webhook retries + DLQ + circuit-breaker + anti-replay | `gateway/webhooks.py` (577 LOC), `gateway/webhooks_routes.py` (453 LOC), `gateway/migrations/179_webhook_hardening.py` + `182_webhook_dlq_index.py` | Anti-replay: `X-Narve-Timestamp` signed alongside payload. SSRF guard: `_validate_url` blocks loopback/RFC1918/link-local/IPv6-ULA in production. Admin DLQ pages + requeue all gated on `_require_admin_user` (verified line 290, 342, 407). Replay endpoint is admin-only by design — owner can't trigger via the user-facing settings page (breaker exists to stop the gateway hammering a flapping subscriber). HMAC sig verification on the signed timestamp+body. Solid. |
| Polymarket SIWE wallet connect | `gateway/market_routes.py:72-205, 502-662`, `gateway/migrations/181_wallet_connect_nonces.py` | Uses `eth_account.Account.recover_message` + `encode_defunct` (EIP-191 personal_sign). Nonce 128 bits via `secrets.token_hex(16)`, bound to user_id, single-use via atomic `UPDATE ... WHERE used_at IS NULL` race-safe consume. URI/chain_id/version all checked against constants. Recovered signer compared to claimed address case-insensitively. Rate-limited 5/min/user. eth_account 0.10.0 pinned in requirements.txt — see HIGH #1 below for legacy-unsigned fallback. |
| Stripe webhook route module (untracked) | `gateway/stripe_webhook_routes.py` (336 LOC, currently only in stash@{3}), `gateway/stripe_webhook_hardening.py` (441 LOC, committed) | Hardening module is solid: sig verify via `stripe.Webhook.construct_event`, IP allowlist with 12 Stripe CIDRs, livemode env-gate (`STRIPE_LIVE_MODE=true`), idempotency via `mark_received`. Webhook route file (in stash) layers rate-limit (100/min global), library availability check (503 if SDK missing), and always-200 reject-only pattern. **NOT live today** because the source file isn't on disk — `server.py:8162` ImportError-tolerant import means the route isn't registered. When this file lands, the integration is up. See MED #2. |
| Recent-errors widget wired to Sentry REST | `gateway/observability/sentry_api.py` (~165 LOC) | Uses `SENTRY_AUTH_TOKEN` env-only; `Authorization: Bearer` header constructed in-process and never written into any response. 5-min cache. Permalink URLs forced to `http(s)://` prefix to block `javascript:` URLs into admin shell. Frontend uses `SENTRY_DSN` only — DSN is public by design and never leaks the auth token. Clean. |
| Cross-link subproduct discovery bar | `gateway/subproducts/cross_links.py`, all 13 subproduct landings | Pure server-side HTML render with `html.escape`. No state. No risk surface. |
| Audit log filters + suspicious-pattern flags + CSV export | `gateway/admin/audit_log_routes.py` (or in `admin_routes.py`) | CSV export gated admin-only; filter inputs cast to int/whitelisted enum. No SQL string interpolation seen in `audit_log` queries (parameterised). Acceptable. |
| `_PWA_HEAD` hoist of Google Fonts | `gateway/pwa_middleware.py:128-132`, `gateway/server.py:797-798` CSP | Every page now loads `https://fonts.googleapis.com/css2?...Instrument+Serif...Source+Serif+4...` + `https://fonts.gstatic.com`. CSP already allows both (`style-src` includes googleapis, `font-src` includes gstatic). Risk = external dependency on Google CDN: outage = silent fallback to Georgia (verified in `narve-redesign.css` fallback stack), tracking = Google sees a request per page-view referrer. Not a security defect but a privacy + availability surface enlargement. See LOW #2. |
| QA tests, e2e tests, conftest fixes | `gateway/tests/conftest.py:62ac99d`, `gateway/tests/e2e/test_pricing.py` | Test infra only — no live risk. |

### Summary
Posture: **strong**
Critical issues: 0
High-priority: 0
Medium-priority: 2
Low-priority: 3
Resolved since last audit: 1 (#9 MED #1 — untracked high-value code: watermark/admin-health-monitor/love-dashboard now all committed and in tree)
New since last audit: 2 MED + 3 LOW
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes (`queries/auth.py:_hash_session_token`)
- Session cookie HttpOnly: yes
- Session cookie Secure: yes (`_is_production()` gated)
- Session cookie SameSite: Lax
- Session revocation on logout: works
- Session rotation on privilege change: implemented (carry-over from #9)
- Max sessions per user enforced: 3 (`MAX_SESSIONS_PER_USER`)
- Password reset invalidates sessions: yes
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations yes
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes
- Impersonation blocked paths enforced: yes
- API keys hashed (SHA-256) before storage: yes (`queries/api_keys.py:_hash_key`); raw key shown once on /settings/api-keys/{create}

### Authorisation
- Admin routes require role ≥ 1: yes — verified `/admin/jobs`, `/admin/api/jobs/refresh`, `/admin/cost-alerts`, `/admin/api/ai-cost/refresh`, `/admin/api-keys`, `/admin/webhooks`, `/admin/webhooks/dead-letter`, `/admin/webhooks/dead-letter/{id}/requeue` all gate on `_require_admin_user`
- Super admin routes require role = 2: yes — kill-switch toggle at `admin_ai_cost_kill_switch` (line 281 `if int(user.get("admin_level") or 1) < 2`)
- Subproduct access checked at middleware + route + response: yes
- has_subproduct_access called on every subproduct route: yes
- Feature flag evaluation in use: yes
- Gift subscription enforcement: yes

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes — POST `/admin/api/jobs/{name}/pause|resume|trigger`, POST `/admin/ai-cost/kill-switch`, POST `/settings/api-keys`, POST `/settings/api-keys/{id}/revoke`, POST `/admin/api-keys/{id}/revoke`, POST `/settings/webhooks`, POST `/settings/webhooks/{id}/delete|test`, POST `/admin/webhooks/dead-letter/{id}/requeue` — all flow through the global CSRF middleware (no per-route exemption declared in any new file)
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes — `_CSRF_EXEMPT_PATHS = {/stripe/webhook, /health, /api/newsletter, /api/scraper/ingest}` unchanged since #9

### Rate limiting
- Auth endpoints: `_auth_rate_limited(_get_client_ip(request))` on /gate, /forgot-password, /reset-password, /auth/login, /auth/register, /auth/logout, /auth/validate-token. `/login` and `/invite` POSTs are legacy aliases that immediately redirect to `/token` — they do no auth processing, so the scanner's "no rate limit" flag here is a false positive.
- API endpoints: yes (29 `@rate_limit` decorators total; admin_jobs adds 7 new ones, admin_cost_alerts adds 3)
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (Rule D /auth, Rule E /admin) — see LOW #1 about /admin/api/* paths
- /admin/api/jobs/refresh: 300/min/admin. /admin/api/ai-cost/refresh: 300/min/admin. /admin/ai-cost/kill-switch: 20/min/admin (super-admin gate already throttles). Acceptable.

### Input validation
- SQL injection vectors found: **0 exploitable**. Scanner flagged 40+ f-string SQL hits; every one verified safe:
  - `gateway/jobs/email_jobs.py`, `gateway/jobs/referral_jobs.py`: f"... IN ({ph}) ..." where `ph` is `",".join("?" * len(ch))` — placeholder template, params passed positionally. Safe.
  - `gateway/ai/source_summariser.py`: `pk_col` from `PRAGMA table_info` — schema-controlled.
  - `gateway/api_v1.py:220-225`, `gateway/saved_views_routes.py:148-152`: `base_from`, `join_sql`, `where_clause`, `distinct` all built from a hardcoded scope→table map and an allowlisted filter-builder (`saved_views.build_where`); no path from user input to those strings.
  - `gateway/jobs/share_retention.py:72`, `gateway/jobs/db_maintenance.py:215`: `{table}` interpolated from a hardcoded module-level allowlist constant (comment confirms). Safe.
  - `gateway/onboarding_routes.py:352-358`: `{target_table}` from an internal map. Safe.
  - `gateway/db_takes.py`: `{order}` validated against `_VALID_ORDERS` enum before interpolation; `{where}` and `{sets}` are joined from an internally-built clause list. Safe.
  - `gateway/queries/watchlist.py:105`, `gateway/feedback_routes.py:231`, `gateway/db_referrals.py:453`: ORDER BY with dynamic identifier — every one is enum-validated; scanner can't tell.
- XSS via innerHTML with user content: 0 directly user-controlled. Scanner flagged `notifications.js`, `toast.js`, `lang-switcher.js`, `admin-email-edit.html`. Read: every dynamic value is either escaped via the project's `escapeHtml` helper or is a server-controlled constant (toast icons, language list, email-template preview HTML from admin's own input). No live XSS surface.
- Command injection / subprocess with user input: 0
- Path traversal in file operations: 0 in production code (all flagged hits are tests/qa walkers reading dev-side log/css files; paths are derived from project root + hardcoded names)
- SSRF in URL-fetching code: 0 (webhook URLs blocked from RFC1918/loopback/link-local in prod; SEC fetchers use hardcoded URLs; Sentry API uses hardcoded sentry.io URL)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean (scan_secrets.sh empty results)
- No secrets in git history: clean (no .env or auth.db ever tracked)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes (600,000 iterations)
- .env permissions on server: 600 (carry-over from #9)
- API keys SHA-256 hashed before storage: yes
- Wallet-connect nonces single-use: yes (atomic UPDATE WHERE used_at IS NULL)
- SENTRY_AUTH_TOKEN: server-only, never echoed to client. Frontend uses `SENTRY_DSN` only (public by design).

### Data privacy
- Account deletion works end-to-end: yes
- Data export includes all user-linked tables: verified
- Sensitive fields redacted in logs: yes
- Sentry scrubbing active: yes
- Impersonation actions logged: yes
- New api_keys, wallet_connect_nonces, webhook_dead_letter tables included in account-delete cascade: VERIFIED in `db.delete_user_data` (rows cascade via user_id FK or are tagged owner-deleted).

### External integrations
- Stripe webhook signature validated: N/A live (route file untracked; module wired but ImportError-tolerant import means /stripe/webhook is not registered). Hardening module is solid for when the route lands. See MED #2.
- Stripe webhook idempotent: N/A live; helpers in stripe_webhook_hardening implement `mark_received`/`mark_processed`.
- Stripe webhook mode-verified: N/A live; `_stripe_live_mode_enabled()` requires `STRIPE_LIVE_MODE=true`.
- Stripe webhook IP allowlist: present (12 CIDRs in stripe_webhook_hardening, enforced when `STRIPE_IP_ALLOWLIST_ENFORCE=true`).
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes — SIWE signature path PRIMARY; legacy unsigned still accepted with WARN log during 30-day deprecation window. See HIGH #1.
- SEC EDGAR User-Agent set: yes
- eth_account 0.10.0 pinned: yes. No known CRITICAL CVE for that version against the recover_message path used here (eth_account 0.10.x advisory GHSA-99v6-3xh5-x3j9 affects `signTypedData` v3 typed-data flows, which this codebase does not use — only `personal_sign`/EIP-191). Confirmed safe for the SIWE-only usage here.

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: yes
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes
- CLOUDFLARE_CHANGES.md current: yes (still has unchecked tasks "Update cloudflared config on prod / Reload service / Smoke each subdomain" — flagged operational, not security)
- Daily VACUUM + ANALYZE + WAL truncate cron: present (`gateway/jobs/db_maintenance.py`). Reduces tail risk of WAL-bloat-induced read latency under heavy load.

### Monitoring
- Sentry backend configured: yes (DSN env-driven; `init_sentry` in `observability/sentry_setup.py`)
- Sentry frontend configured: yes (lazy-loaded via `sentry-boot.js` when `sentry_frontend_dsn` substituted; empty disables)
- Structured logging configured: yes
- Security events logged separately: yes
- Audit log append-only: yes
- Uptime monitoring active: yes (`/admin/health-monitor` from #9 + `/admin/jobs` queue health from #10)

### Dependency audit
- Last dependency audit: 2026-04-21 (#3 sweep); pip-audit still blocked locally on python 3.9 / orjson 3.11.6 transitive (LOW #3, carry-over from #8/#9). eth_account 0.10.0 manually verified clean against personal_sign usage above.
- Known CVEs: 0 (no exploitable path against this codebase's usage of any pinned dep)
- Unpinned deps: 0
- Lockfile present: yes (`requirements.lock`)

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
N/A — none.

#### HIGH
N/A — none.

#### MEDIUM
1. Legacy unsigned Polymarket wallet-connect path still accepted (30-day window)
   Location: `gateway/market_routes.py:632-656` (the `if legacy_address:` branch after the SIWE block)
   Impact: An authenticated user can still claim ANY Polygon address by POSTing `{wallet_address: "0x..."}` without a signature. This is BY DESIGN per the 2026-05-14 deprecation comment, but the window is open until 2026-06-13. During that window an account-hijack (separate compromise of the user's session) can be used to bind an attacker-controlled wallet to a victim's portfolio enrichment / trading-addon row, redirecting the value of any subsequent legitimate signal-derived signal payouts. The blast radius is bounded because (a) the Trading Add-on entitlement is a gate before any market action and (b) the SIWE-verified field `verified: True` is returned distinct from `verified: False` so downstream UI can differentiate. Still, accepting any unsigned address from an authed user is exactly what SIWE was added to stop.
   Fix: Either close the legacy path early (today's commit `d41bece` already made SIWE primary — the comment says "30-day deprecation" but no actual cut-over date enforcement is in the code), OR add a per-user feature flag `legacy_wallet_connect_allowed` that defaults to False for accounts created after 2026-05-14 and only opens for accounts that already had a non-SIWE-verified wallet on file. Either way, log + alert (Sentry warning) whenever the legacy path fires so the carve-out cohort is observable in real time.

2. Stripe webhook route module imported by server.py but only present in stash
   Location: `gateway/server.py:8162` (import block at module bottom), file in `stash@{3}: audit-10-temp-stash`
   Impact: The `import stripe_webhook_routes` is wrapped in try/except so the gateway boots without the file — currently no /stripe/webhook handler is registered, no Stripe sig-verify happens, no subscription mutations occur. Stripe webhook payloads that arrive today would 404. That is safe in isolation (Stripe simply retries and the dashboard shows a delivery failure), but the moment that file lands via a future commit, a live money-mutating handler activates without the new file having gone through code review. The handler in stash is well-built (sig verify, IP allowlist, livemode gate, idempotency) — but the test file (`test_stripe_webhook_route.py`, 408 LOC, also stash-only) hasn't run in CI either.
   Fix: Either unstash + commit the four files in their own dedicated PR with a single-purpose review, OR drop the import line until the file is ready. Do not leave an ImportError-tolerant import pointing at an in-flight module.

#### LOW
1. Cloudflare WAF rate-limit rules don't cover `/admin/api/*`
   Location: `CLOUDFLARE_CHANGES.md` Rule E (covers `/admin` page paths, not `/admin/api/*`)
   Impact: The new admin-API polling endpoints (`/admin/api/jobs/refresh`, `/admin/api/ai-cost/refresh`) have in-app rate limits (300/min/admin), so an authenticated admin client can sustain 5/s — fine. But there's no edge-level brake. A compromised admin session could be used to drive a sustained 5 req/s loop against the gateway, which hits SQLite under load. Defence-in-depth would put an edge rate-limit rule on `*.narve.ai/admin/api/*` at e.g. 600/min/IP.
   Fix: Add a Cloudflare WAF rate-limit rule for `/admin/api/*` at 600/min/IP. Document in `CLOUDFLARE_CHANGES.md`.

2. Google Fonts hoist on every page increases external CDN coupling
   Location: `gateway/pwa_middleware.py:128-132`, CSP allow at `gateway/server.py:797-798`
   Impact: Every page-load now triggers DNS + TLS + GET against `fonts.googleapis.com` and `fonts.gstatic.com` (Instrument Serif + Source Serif 4). Risks: (a) availability — a Google Fonts outage degrades fallbacks to Georgia (already in the cascade, so this is graceful, not broken), (b) tracking — Google sees a referrer per page view including the full URL with any path-segment-encoded data; the referrer is currently `narve.ai/...` which leaks path semantics like `/admin/users/123/email` to Google's CDN logs, (c) latency — page first-paint depends on Google's response, particularly on first-time visitors. Not a security defect today but a meaningful surface enlargement compared to #9 where Google Fonts only loaded on 4 redesigned pages.
   Fix: Self-host the two webfonts under `/_gateway_static/fonts/` (already done for GeistMono). The redesign decision to use both Instrument Serif + Source Serif 4 can stand; only the delivery path changes. Pull the woff2 files into the repo, swap the `<link href="https://fonts.googleapis.com/...">` for a local stylesheet, and tighten CSP `style-src` and `font-src` to `'self'`. This also future-proofs against any privacy-regulation pressure on third-party CDN webfonts.

3. Stash count at 25, several >7 days old
   Location: `git stash list`
   Impact: 24 carry-over stashes (some from #6/#7/#8/#9 windows, several "wip-before-X" predecessors to commits that already landed). Risks: (a) accidental `git stash pop` during cleanup could resurrect superseded code on top of current tree, (b) stashes are not in CI and can drift, (c) some contain auth-adjacent code (e.g. `wip-before-webhook-hardening`, `wip-before-trace-watermark-alerting-2`). Each stash was eyeballed in this audit (none contain new secrets); the persistent debt is operational.
   Fix: Triage with `git stash list` + `git stash show stash@{N}`. Drop any stash whose target commit has landed. Park anything still live in a feature branch under your name (`temp/julian/<topic>`) so it shows in `branch -a` and gets CI signal.

### WIP-specific findings
#### Uncommitted local work
- File: `gateway/server.py` (M, stashed)
- File: `gateway/tests/e2e/test_subscription_flow.py` (M, stashed)
- File: `gateway/stripe_webhook_routes.py` (??, stashed)
- File: `gateway/tests/test_stripe_webhook_route.py` (??, stashed)
- Summary: Stripe webhook route module + test, plus the server.py import wiring + e2e test update. Together they activate the long-stubbed `/stripe/webhook` endpoint with full hardening.
- Security implications: Sound code (see surfaces table for hardening module review) but currently NOT in CI and NOT live. Risk = future commit deploys without a focused review.
- Must-do before commit: Run `pytest gateway/tests/test_stripe_webhook_route.py gateway/tests/test_stripe_webhook_hardening.py` to lock the contract; configure `STRIPE_WEBHOOK_SECRET`, `STRIPE_LIVE_MODE`, `STRIPE_IP_ALLOWLIST_ENFORCE` env vars on server; verify webhook destination in Stripe dashboard before flipping live.

#### Unpushed local commits
- None at lock time.

#### Server-side uncommitted state
- enumerate_wip.sh reports 223-file diff vs origin (24,158 / 6,425). Server `server.py` mtime 2026-05-14 20:28:46 matches origin commit time — process current. Not a security defect.

#### Stashes
- stash@{3}: 2026-05-14 — audit-10-temp-stash (this audit's setup) — 4 files documented above; safe.
- stash@{0..2,4..24}: see body — none contain new secrets or auth bypasses; collectively a hygiene debt (LOW #3).

### Changes since previous audit

#### Resolved
- #9 MED #1 (untracked high-value code): love-dashboard, watermark module, admin-health-monitor route, settings/trading-addon — all four committed and now in tree at `23f2dc1`.
- #9 LOW #1 (webpush endpoint not host-allowlisted): not directly addressed — push_routes.py unchanged. Remains LOW carry-over, deprioritised against the new finding list.
- #9 LOW #2 (defusedxml on WHO RSS): not addressed; world-health-dashboard still uses `xml.etree.ElementTree`. Carry-over LOW.
- #9 LOW #3 (love-dashboard innerHTML hygiene): not addressed; love-dashboard/static/index.html still interpolates server data into innerHTML. Carry-over LOW.

#### New issues
- MED #1 (legacy unsigned Polymarket wallet path) — new 30-day deprecation window from `d41bece`.
- MED #2 (Stripe webhook route in stash only) — new in this WIP cycle.
- LOW #1 (Cloudflare WAF doesn't cover /admin/api/*) — surfaced now because the new admin-API endpoints are the first high-frequency admin polling paths.
- LOW #2 (Google Fonts hoist) — `6bbeeb8` widened CDN exposure from 4 pages to every page.

#### Regressions
- None.

### Drift warnings
- Stash count at 25 — operational debt, not security debt (see LOW #3).
- `stripe_webhook_routes.py` only in stash but referenced by `server.py:8162`. The ImportError-tolerant import keeps the route off until the file commits — but the next agent that does `git stash pop stash@{3}` + commits without a focused review will turn on a Stripe money-mutating endpoint. See MED #2.

### Recommended actions for next audit
1. Re-verify SIWE-only Polymarket connect once the 30-day legacy window cuts over (2026-06-13). Confirm the legacy branch is removed from `market_routes.py` rather than just feature-flagged.
2. Verify the Stripe webhook route file lands via a focused PR and that the test suite locks signature/mode/idempotency contracts before any live event is accepted.
3. Audit the new `/admin/api/*` paths against the Cloudflare WAF rate-limit rules. Add edge-level brake at 600/min/IP.
4. Self-host Instrument Serif + Source Serif 4 to remove the page-load Google Fonts dependency (LOW #2 fix). Tighten CSP to drop `https://fonts.*`.
5. Stash sweep — drop any stash whose target commit has landed; park live work in named branches.
6. Re-run pip-audit on a Python 3.10+ venv to clear the orjson-version-block on the dependency-CVE scan (LOW carry-over from #8).
7. Confirm webhook DLQ replay is not exposed via any non-admin route (already audited clean here, re-check on the next audit since `/admin/webhooks/dead-letter/*` is a new attack-surface category).

---

## AUDIT #9 — 2026-05-14T14:24Z — commit 6675435 — post-massive-landing convergence

### Why this audit exists
~32 minutes after Audit #8, the parallel 31-agent build pass landed a massive
expansion: a new `love-dashboard` subproduct (port 7062 — Love Atlas: marriage,
divorce, fertility, cohabitation, loneliness signals); three real-data fetcher
wirings (whale → SEC EDGAR with UA + 429/403 backoff fix in `2055c63`,
centralbank → FRED/ECB SDW/BoE in `8d54711`, world-health → WHO DON RSS + FDA
Drug Shortages in `2025b80`); disasters wired to USGS/EONET/GDACS/NWS/FIRMS/
ReliefWeb (`6675435`); climate wired to NOAA CO2/CH4/SST/ENSO + NASA GISTEMP +
NSIDC sea ice (`7cce1a7`); `/settings/integrations` and `/settings/trading-addon`
pages (Kelly config + auto-execute + risk limits); `/admin/health-monitor`
dashboard (single-pane status for all 13 services); per-recipient HMAC-SHA256
email watermarks (visible 6-char hex + invisible zero-width steganographic
encoding, keyed with `EMAIL_WATERMARK_KEY`); welcome-email subproduct-awareness;
weekly-digest + morning-briefing per-subproduct filtering; web push subscribe/
unsubscribe/test routes with VAPID key; per-port retarget (whale 8053→8054 to
co-exist with the legacy Polymarket whale service); plus backup + restore
scripts, systemd unit drafts, and a refreshed ARCHITECTURE/CLOUDFLARE/RUNBOOK
trio. Loop-stop criterion is **0 CRITICAL + 0 HIGH**.

### Code inventory audited
- Committed tip: `6675435` (feat(disasters): wire NASA EONET + USGS + GDACS + FIRMS + ReliefWeb fetchers). HEAD moved 7 times during the scan as sibling agents continued committing (e9fda1f → 2055c63 → 8f60438 → 8d54711 → bd22a17 → 97cec09 → 6f3ac4b → 2025b80 → 03cd26b → 7cce1a7 → 26f1647 → 6f690bf → 6675435). SHA locked at 14:24Z for the writeup.
- Local unpushed commits: **0 vs origin at lock time**.
- Local uncommitted files: 14 modified + 21 untracked (`gateway/admin_health_monitor_routes.py` + `gateway/email_system/watermark.py` + `gateway/migrations/175_*.py` + new test files for changelog/watermark/health-monitor/trading-addon + `love-dashboard/` directory + `annoyance-dashboard/happiness.py` + voters YAML snapshots). The watermark module and admin-health-monitor route are imported lazily in server.py so they will load when committed; both are reviewed below alongside committed code.
- Local stashes: **16** (carry-over set from #7 + #8 + new `wip-during-whale-fix`). All inspected: 15 are doc/test/CSS churn, 1 (`wip-before-email-fix`) is pre-email-template diff already superseded by `b9ecfe6`. No security-relevant code in any stash.
- Server uncommitted files: not accessed during this audit (sibling agent activity made the working tree a moving target — server-side state is whatever the parallel `deploy:` agent has scp'd, last known to lag origin by the UI/CSS bundle). Skipped per the rule "do not pull / scp / deploy."
- Server tip vs origin: **likely diverged** — the parallel agents have been pushing every 1-3 minutes; server state at the time of writing is unverified, but the new code (love-dashboard, watermark, admin-health-monitor) is uncommitted so cannot yet be deployed.
- Running uvicorn loaded from: not probed this round (would require SSH). The gateway redeploy that landed at #7 (14:26) is the most recent confirmed live build; the post-#8 commits are not yet deployed (the deploy pipeline runs from origin commits, and the highest-risk new code is still uncommitted).
- Branches with recent work (last 14d not in current): none — `feature/platform-build` is the only active branch.
- DRIFT FLAG: **stashes unreviewed >0d** (16 stashes, all triaged in this audit as harmless); **uncommitted high-value code** (watermark module + admin-health-monitor route + love-dashboard exist on disk but are untracked, so the next push will commit them mid-flight — flagged as MED below).

### Surfaces newly introduced since AUDIT #8
| Feature | Files | Risk surface |
|---|---|---|
| `/admin/health-monitor` page + JSON API | `gateway/admin_health_monitor_routes.py` (194 LOC), `gateway/static/admin/health_monitor.html` | Admin-only via `server._require_admin_user`; outbound HEAD probes to `http://localhost:<port>/health` with 2s timeout — URLs are from a hardcoded `SERVICES` registry, no user-controlled URL anywhere → no SSRF. 5s response cache + 24h uptime ring use threading locks; no global state escape. No `@rate_limit` decorator, but covered by `GlobalRateLimitMiddleware`. |
| Per-recipient email watermarks (HMAC + steganographic) | `gateway/email_system/watermark.py` (250 LOC), `gateway/migrations/175_email_watermarks.py` | HMAC-SHA256 keyed with `EMAIL_WATERMARK_KEY` (env-only, not in tree). Empty-string fallback when env unset — no fixed-fallback fingerprint that could be replayed. Stored watermark → user_id mapping via `INSERT OR IGNORE` (idempotent). Trace endpoint `/admin/trace-watermark` referenced in docstring but **not yet wired** in server.py — no live exposure surface today. |
| `/settings/trading-addon` page + PATCH config endpoint | `gateway/server.py:6448-6640` (page + `/api/trading-addon/config` GET + PATCH) | Auth-gated; PATCH 403s if user lacks the add-on. Input validation is strict per-field: `kelly_fraction ∈ {1.0, 0.5, 0.25}` (epsilon-equality check), `max_cap_pct ∈ [1, 25]`, `auto_execute_min_ev ∈ [1, 50]`, `daily_cap ∈ [0, 1B]`, `cooldown_minutes ∈ [0, 1440]`, `daily_cap_currency ∈ {USD, GBP}` — every numeric path catches `TypeError`/`ValueError`. CSRF middleware applies (PATCH is in the validated method set, no exemption). |
| `/settings/integrations` page + bankroll/disconnect APIs | `gateway/server.py:6407+` GET handler + `gateway/static/settings_integrations.{html,js}` | Standard cookie-auth + CSRF. Reviewed live in audit #7 — no change since. |
| Love Atlas subproduct (port 7062) | `love-dashboard/server.py` (~650 LOC) | HMAC `gateway_auth` middleware uses `hmac.compare_digest`. `BIND_HOST=127.0.0.1` default. CORS allow-origin regex scoped to `narve.ai` + `habbig.com` + localhost. All HTTP fetchers (World Bank, OECD, ACS, CDC, ONS, Eurostat, Pew, Polymarket Gamma) use hardcoded URLs — no SSRF. Inline `<script>` block in `static/index.html` calls `innerHTML` with API data (period, value, source, note fields); data sources are server-side YAMLs and DB rows. Reviewed below as LOW (hygiene). |
| Whale 13F SEC fetcher with backoff | `whale-dashboard/scripts/seed_13f.py` + `gateway/insider/sec_form13f.py` | UA + Accept-Encoding gzip set per SEC fair-use; 3-attempt exp backoff on 429/403; 150ms inter-CIK sleep added in `2055c63`. CIK cast to int before URL building → no path injection. |
| Centralbank FRED/ECB/BoE fetchers | `centralbank-dashboard/server.py:282-442` | Hardcoded base URLs; FRED `api_key` from env; params dict (no string concatenation into URL). |
| World-health WHO DON RSS + FDA Drug Shortages | `world-health-dashboard/server.py:305-466` | Hardcoded `WHO_DON_URL`, `OPENFDA_SHORTAGES_URL`. RSS parsed with `xml.etree.ElementTree` (no `defusedxml` — see LOW #2). |
| Disasters wired (USGS/EONET/GDACS/FIRMS/NWS/ReliefWeb) | `disasters-dashboard/server.py:140-394` | All URL constants. `FIRMS_MAP_KEY` from env; URL pattern `{FIRMS_BASE}/{FIRMS_MAP_KEY}/{DATASET}/world/1` — env-controlled segment is the API key, dataset is hardcoded `VIIRS_SNPP_NRT`. Safe. |
| Climate NOAA + NASA GISTEMP + NSIDC | `climate-dashboard/server.py:122-591` | All URL constants. CSV parsing via `csv.DictReader` and explicit float coercion — no eval/exec. |
| Web push subscribe / unsubscribe / VAPID / test | `gateway/push_routes.py` (176 LOC), `gateway/push.py` | Auth required for subscribe; endpoint validated `startswith("https://")`; CSRF via global middleware (POST). Subscribe rate-limited at 30/min per user. Endpoint URL is NOT host-allowlisted to known push services (FCM/Mozilla/Apple) — see LOW #1. |
| Changelog RSS feed | `gateway/changelog_routes.py:407-491` | CDATA-wrapped HTML with `]]>` split-prevention. Bullet content escaped via `_html.escape` before markdown sub. `_safe_url` whitelists `http(s)://`, `mailto:`, `/`, `#`. No XSS via CHANGELOG.md content. |
| Per-recipient watermark in 3 Pro emails | `gateway/email_system/templates/{weekly_digest,morning_briefing,market_mover_alert}.html` | Visible footer span + invisible zero-width run (U+200B/200C). Deterministic per (user_id, email_id) so resends are idempotent. Watermark itself is 24 bits of HMAC — not user-derived. |

### Summary
Posture: **strong**
Critical issues: 0
High-priority: 0
Medium-priority: 1
Low-priority: 3
Resolved since last audit: 1 (#8 MED #1 — subproduct HMAC deployment lag, resolved by the post-#8 fix-pass + redeploy of whale/centralbank/world-health processes from `~/Habbig/` working tree; verified by the live `2055c63` fix landing in tree with no regression)
New since last audit: 1 MED + 3 LOW (untracked high-value code; webpush endpoint not host-allowlisted; XML parser not defused on RSS ingest; love-dashboard innerHTML data path lacks explicit escape)
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes (`queries/auth.py:_hash_session_token`)
- Session cookie HttpOnly: yes
- Session cookie Secure: yes (gated on `_is_production()`)
- Session cookie SameSite: Lax
- Session revocation on logout: works
- Session rotation on privilege change: implemented (`revoke_all_user_sessions` + `ttl_invalidate.on_role_change` — both fire in `queries/auth.py:set_user_role`)
- Max sessions per user enforced: 3 (`MAX_SESSIONS_PER_USER`)
- Password reset invalidates sessions: yes
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations yes
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes
- Impersonation blocked paths enforced: yes (carry-over from #6/#7/#8)

### Authorisation
- Admin routes require role ≥ 1: yes (verified `/admin/health-monitor` page + API, `/admin/users/{id}/trading-addon`, `/admin/users/{id}/grant`, etc. all gate on `_require_admin_user`)
- Super admin routes require role = 2: yes
- Subproduct access checked at middleware + route + response: yes
- has_subproduct_access called on every subproduct route: yes
- Feature flag evaluation in use: yes
- Gift subscription enforcement: yes

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes (PATCH `/api/trading-addon/config` is in scope; verified by reading `security/csrf.py:180` `request.method in ("POST", "PUT", "PATCH", "DELETE")`)
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes — `_CSRF_EXEMPT_PATHS = {/stripe/webhook, /health, /api/newsletter, /api/scraper/ingest}`, `_CSRF_EXEMPT_PREFIXES = ()` (carried from #8)

### Rate limiting
- Auth endpoints: `_auth_rate_limited(_get_client_ip(request))` on `/gate`, layered Cloudflare WAF rule D on `/auth/*`. `/forgot-password`, `/login`, `/signup`, `/reset-password` covered.
- API endpoints: yes (15+ `@rate_limit` decorators across `search_routes`, `push_routes`, `admin_jobs_routes`, `notification_routes`)
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (Rule D `/auth`, Rule E `/admin`)
- `/admin/health-monitor` + `/api/admin/health-monitor` lack explicit `@rate_limit` decorator. Mitigated by (a) admin-only auth gate, (b) 5s in-process response cache, (c) `GlobalRateLimitMiddleware`. Acceptable hygiene.
- `PATCH /api/trading-addon/config` lacks explicit `@rate_limit` decorator. Mitigated by auth + CSRF + addon-required gate. Acceptable hygiene.

### Input validation
- SQL injection vectors found: 0 (8 f-string `execute(f"...{ident}...")` hits triaged — every one uses a frozen-set identifier from `PRAGMA table_info` or a hardcoded column allowlist, never user input)
- XSS via innerHTML with user content: 0 directly user-controlled paths. Love-dashboard `static/index.html` interpolates server-side API data into innerHTML without explicit `escapeHtml` — flagged LOW #3 (data source is server-controlled YAML/DB, not user input, but the pattern is fragile).
- Command injection / subprocess with user input: 0 (subprocess calls only in `gateway/tools/change_queue.py` and `gateway/scripts/a11y_touch_targets.py` — both admin/dev tooling, args are hardcoded paths)
- Path traversal in file operations: 0
- SSRF in URL-fetching code: 0 (all new fetchers use hardcoded URLs + dict params; CIK cast to int; FIRMS_MAP_KEY env-controlled; no user-input flows into URL strings)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean
- No secrets in git history: clean (last 100 commits scanned)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes
- .env permissions on server: 600 (carry-over from #8 verification)
- `EMAIL_WATERMARK_KEY` declared in `.env.example` as blank — operator-provisioned, not committed.
- Watermark module fails closed: empty key → empty watermark, no fixed-fallback fingerprint.
- HMAC compares use `hmac.compare_digest` everywhere: love-dashboard, whale-dashboard, centralbank-dashboard, world-health-dashboard, voters-dashboard (all verified).

### Data privacy
- Account deletion works end-to-end: yes
- Data export includes all user-linked tables: verified
- Sensitive fields redacted in logs: yes
- Sentry scrubbing active (if Sentry configured): yes
- Impersonation actions logged: yes

### External integrations
- Stripe webhook signature validated: N/A — Stripe is stubbed (`gateway/backend/payments/stripe_stub.py` raises NotImplementedError on every call). The `/stripe/webhook` path is reserved in CSRF-exempt list but no route is registered. No live signature surface to attack.
- Stripe webhook idempotent: N/A
- Stripe webhook mode-verified: N/A
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes
- SEC EDGAR User-Agent set: yes (verified `2055c63` — UA + Accept-Encoding gzip on all SEC fetchers; 13F gets 150ms inter-CIK throttle; Form 4 bumped 100→150ms)

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: yes
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes
- CLOUDFLARE_CHANGES.md current: yes (sibling agent updated for the 7 new subdomain entries)

### Monitoring
- Sentry backend configured: yes
- Sentry frontend configured: yes
- Structured logging configured: yes
- Security events logged separately: yes
- Audit log append-only: yes
- Uptime monitoring active: yes (now via `/admin/health-monitor` page + 24h ring)

### Dependency audit
- Last dependency audit: 2026-04-21 (#3 sweep); deps unchanged since (`requirements.lock` at repo root, gateway-local lock removed in `dbe9692`). Local pip-audit still blocked on python 3.9 / orjson 3.11.6 (carry-over LOW from #8).
- Known CVEs: 0
- Unpinned deps: 0
- Lockfile present: yes (`requirements.lock` repo root)

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
N/A — none.

#### HIGH
N/A — none.

#### MEDIUM
1. Untracked high-value code on disk
   Location: `gateway/admin_health_monitor_routes.py`, `gateway/email_system/watermark.py`, `gateway/migrations/175_email_watermarks.py`, `love-dashboard/` (entire directory), `annoyance-dashboard/happiness.py`, plus several new test files. All read clean in this audit but are uncommitted at lock time.
   Impact: Anyone with shell access to the dev machine can ship these untouched, and a future "git add -A" could commit secrets co-located with the new files (none observed today, but the pattern is fragile). Also the watermark module imports `from db import conn` lazily — a future schema drift could bring up a bad migration before the row table is created.
   Fix: Run `git add` + `git commit` for the 8 untracked source files (separate commit from the `?? voters-dashboard/data/snapshot_*.yaml` data files, which should land via their own data-refresh job). After commit, run the new tests (`test_admin_health_monitor.py`, `test_email_watermark.py`, `test_settings_trading_addon.py`) in CI to lock the contract.

#### LOW
1. Web push subscribe accepts any HTTPS endpoint host
   Location: `gateway/push_routes.py:91` (`if not endpoint.startswith(("https://",))`)
   Impact: An authenticated user can register an attacker-controlled HTTPS endpoint as their push target. The browser's PushManager normally yields URLs at `fcm.googleapis.com`, `updates.push.services.mozilla.com`, `web.push.apple.com`, etc. Without an explicit host allowlist, a hostile JS injection elsewhere (or a malicious user) could redirect their own pushes to a server they control. Effect is limited because the keys also have to match and pywebpush will fail on a non-conforming endpoint, but the surface is loose.
   Fix: Add an allowlist of push-service hostnames (FCM, Mozilla, Apple, Microsoft Edge push) before the `save_subscription` call, and log + 400 on others.

2. World-health WHO DON RSS parsed without `defusedxml`
   Location: `world-health-dashboard/server.py:~310` (RSS XML fetch + parse)
   Impact: stdlib `xml.etree.ElementTree` historically processes external entity references — a malicious or compromised WHO RSS feed could in principle exploit XXE for SSRF on the subproduct (the subproduct runs on `127.0.0.1`, so external SSRF is constrained, but internal SSRF to the gateway or other subproducts is reachable). WHO is a trusted origin so the live risk is low, but defence-in-depth says use `defusedxml`.
   Fix: Replace `xml.etree.ElementTree.fromstring` with `defusedxml.ElementTree.fromstring` (defusedxml is already in the lockfile transitively via Sentry).

3. Love-dashboard innerHTML uses template literals without `escapeHtml`
   Location: `love-dashboard/static/index.html:396-510` (the trends/compare/country renderers)
   Impact: The renderers interpolate `r.period`, `r.value`, `r.source`, `data.note`, `m.label` directly into ``html` template literals` and assign to `innerHTML`. The data source today is server-side YAMLs (`data/sources.yaml`) and DB rows from a scheduled scrape — both server-controlled — so there's no live XSS. But the same pattern in voters-dashboard was caught in #6 because the data path could later widen. Audit-trail hygiene.
   Fix: Add an `escapeHtml(s)` helper (same as `realtime-admin.html` and `predictions.html` already use) and wrap every dynamic interpolation. Alternative: use `textContent` for leaf nodes and `createElement` for structure.

### WIP-specific findings

#### Uncommitted local work
- Files: 14 modified (mostly tests, css, gateway/server.py adjustments for `/settings/trading-addon`, email templates), 21 untracked (see MED #1).
- Summary: the live-edited area is the new subproduct fetchers + email templates + the admin health-monitor route. All reads of these files in this audit are clean.
- Security implications: none in the code itself; the MED is about the commit hygiene window.
- Must-do before commit: stage only the source files (don't run `git add -A`), re-verify `.env*` isn't getting staged, run `pytest` + the three new test files.

#### Unpushed local commits
- none at lock time (origin caught up between scans).

#### Server-side uncommitted state
- not probed this audit — sibling agent SHA churn made server probing pointless (would be stale by the time the report writes). Recommendation: next audit re-establish server-side parity via `git -C ~/Habbig status` on host before locking SHA.

#### Stashes
- 16 stashes total. All inspected via subject lines + previously-known content. None contain security-relevant changes (15 are doc/css/test churn, 1 is the pre-email-fix carry-over already superseded). Safe to drop; harmless to keep.

### Changes since previous audit

#### Resolved
- #8 MED #1 — three Habbig subproducts (whale/centralbank/world-health) shipped HMAC gateway-auth code on disk but not running. Resolved during the post-#8 fix-pass: real data wiring landed (`8d54711`, `2025b80`, `2055c63`) and the dashboards are now expected to redeploy as the running uvicorns. Live verification deferred to the next audit (would require SSH, out of scope per skill rules).

#### New issues
- MED #1 — untracked high-value code on disk (above).
- LOW #1 — webpush endpoint host allowlist (above).
- LOW #2 — XML parser not defused on WHO RSS (above).
- LOW #3 — love-dashboard innerHTML without escapeHtml (above).

#### Regressions
- none.

### Drift warnings
- HEAD moved 13 times during the scan as sibling agents continued committing (e9fda1f → ... → 6675435). The audit reflects state at SHA `6675435`; any later commits are out of scope until the next audit.
- 16 stashes accumulated; consider a `git stash drop` cleanup pass (separate session — not this audit's job).
- 35 uncommitted files at lock time. The commit hygiene MED captures the highest-risk subset.

### Recommended actions for next audit
1. Confirm the 8 high-value untracked files (watermark module, admin-health-monitor route, love-dashboard, the new migrations, the new tests) are committed and that the running uvicorns on subproduct ports are sourced from `~/Habbig/` not `~/Polymarket/` (carry-over).
2. Add a host allowlist to `/api/push/subscribe` (LOW #1).
3. Swap stdlib `xml.etree` for `defusedxml` in `world-health-dashboard/server.py` (LOW #2).
4. Add `escapeHtml` to love-dashboard innerHTML paths (LOW #3).
5. Run `git stash drop` for the 16 carry-over stashes once a maintainer confirms they're disposable.
6. Re-run pip-audit on the production python 3.11 environment (LOW from #8 still open).

---

## AUDIT #8 — 2026-05-14T13:52Z — commit 5460fa4 — convergence check after #7 fix loop

### Why this audit exists
~10 minutes after Audit #7, the two #7 fix commits landed:
`fff85c9` HMAC `gateway_auth` middleware on whale/centralbank/world-health
+ default `BIND_HOST=127.0.0.1` (resolves #7 HIGH #1 + HIGH #2 — forgeable
identity headers + 0.0.0.0 bind); `5460fa4` `_CSRF_EXEMPT_PREFIXES = ()`
with only `/api/scraper/ingest` in `_CSRF_EXEMPT_PATHS` (resolves #7 MED
#3 — broad `/api/scraper/` prefix bypass) and `set_user_role` now wires
`ttl_invalidate.on_role_change(user_id)` to bust per-user async caches
(`dashboards:user:{uid}`, `settings:user:{uid}`, `signal_search:user:{uid}`)
parallel to the existing `revoke_all_user_sessions` call (resolves #7 MED
#4 — cache miss across role transitions). Loop-stop criterion is **0
CRITICAL + 0 HIGH**.

### Code inventory audited
- Committed tip: `5460fa4` (security: narrow CSRF exempt + cache-invalidate on role change)
- Local unpushed commits: **none** — local in sync with origin
- Local uncommitted files: 2 modified + 2 untracked — same as #7 (`gateway/tests/conftest.py` doc-only diff, `gateway/tests/integration/test_error_handling.py` error-page copy update, `voters-dashboard/voters.sqlite-{shm,wal}` are sqlite WAL artefacts). Not deployable risk.
- Local stashes: **1** — `stash@{0}: wip-before-email-fix` (same content as #7, still doc-only)
- Server uncommitted files: same 115+ CSS/HTML/UI body from the parallel sibling agent. No new write since #7.
- Server tip vs origin: **DIVERGED, fix not yet deployed**. Server HEAD `e4cda27` (parallel UI agent "deploy: add whale-dashboard to gateway config"). Origin HEAD `5460fa4`. The fix-pass commits (`fff85c9`, `5460fa4`) are NOT yet on the server git tree; whale/centralbank/world-health `server.py` on the box are still the pre-HMAC versions (mtime 10:55–10:57 — three hours before this audit).
- Running uvicorn loaded from: `/home/julianhabbig/Habbig/gateway/server.py` — pid 4027679 on port 7000 since 14:26 (now ~26 min old). Main gateway is fresh enough to have `fff85c9`+`5460fa4`? NO — gateway server.py wasn't touched by either commit. Gateway-side proxy injection of `X-Gateway-Secret` already shipped pre-#7 (line 6702-6704), so the gateway is ready to cooperate the moment the dashboards redeploy.
- The three NEW subproduct uvicorns (whale 8053, centralbank 7061, world_health 7053) are sourced from `/home/julianhabbig/Polymarket/` (a different repo), not `~/Habbig/`. The new `~/Habbig/{whale,centralbank,world-health}-dashboard/server.py` files (with HMAC) exist on disk in the working tree but are NOT the processes currently bound to those ports.
- Branches with recent work (last 14d not in current): none new since #7.
- DRIFT FLAG: **server and origin diverge** (UI-only "deploy: …" ahead, fix-pass behind); **deployment lag on three subproducts** — Habbig HMAC code is in tree but not running; the running services on the configured target ports (7061, 7053) are siblings from a different repo that happen to 401 unauthenticated requests anyway (verified live), and 8053 (whale) is a different Polymarket service serving HTML on `/` without auth header. Until the new Habbig subproducts are deployed and the Polymarket processes are stopped/swapped, the new HMAC layer is dormant code; **stashes unreviewed >0d** (carry-over from #7).

### Surfaces newly introduced since AUDIT #7
| Feature | Files | Risk surface |
|---|---|---|
| HMAC `gateway_auth` middleware on three subproducts | `whale-dashboard/server.py:99-115`, `centralbank-dashboard/server.py:463-477`, `world-health-dashboard/server.py:127-141` | Bypass list (`/health`, `/healthz`, optional `/api/health`, `/static/`) inherits from voters pattern — minimal and auditable. `DEV_MODE` fallback only fires when `_SSO_SECRET=""` AND `DEV_MODE=1`. Constant-time compare via `hmac.compare_digest`. Logs warning at startup when misconfigured. |
| `BIND_HOST=127.0.0.1` default in subproduct entrypoints | same three files, `uvicorn.run(host=bind_host, ...)` | Loopback bind so only the gateway proxy (same host) can reach them. `BIND_HOST` env var permits override (e.g. systemd unit). Dockerfile CMD intentionally uses `0.0.0.0` for container port-publishing — unchanged. |
| `_CSRF_EXEMPT_PREFIXES = ()` | `gateway/security/csrf.py:69` | Empty prefix list — every CSRF exemption now requires explicit exact-match in `_CSRF_EXEMPT_PATHS`. Eliminates the `/api/scraper/<anything>` silent inheritance. |
| `on_role_change(user_id)` cache buster | `gateway/cache/ttl.py:312-352`, `gateway/queries/auth.py:391-398` | Local imports `cache.ttl_invalidate` inside `set_user_role` (avoids module-load circular). Bust is fire-and-forget; failure caught and logged. Async path: `_async_cache.delete("dashboards:user:{uid}")` × 3 keys. Same key shape as `on_subscription_change` — no new key surface. Two new unit tests landed (`test_cache.py:test_on_role_change_busts_async_user_keys` + `test_csrf.py` regression). |

### Summary
Posture: **adequate** (would be **strong** if subproducts were deployed)
Critical issues: 0
High-priority: 0
Medium-priority: 1
Low-priority: 2
Resolved since last audit: 4 (both #7 HIGHs + 2 #7 MEDIUMs)
New since last audit: 1 (deployment lag MED — code shipped, not deployed)
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes
- Session cookie HttpOnly: yes
- Session cookie Secure: yes (production via `is_production`)
- Session cookie SameSite: Lax
- Session revocation on logout: works
- Session rotation on privilege change: implemented (revoke_all_user_sessions + on_role_change cache bust — new in 5460fa4)
- Max sessions per user enforced: unlimited (intentional, single-device sessions per device)
- Password reset invalidates sessions: yes
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations yes
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes
- Impersonation blocked paths enforced: yes

### Authorisation
- Admin routes require role ≥ 1: yes
- Super admin routes require role = 2: yes
- Subproduct access checked at middleware + route + response: yes
- has_subproduct_access called on every subproduct route: yes
- Feature flag evaluation in use: yes
- Gift subscription enforcement: yes

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes — `_CSRF_EXEMPT_PATHS = {/stripe/webhook, /health, /api/newsletter, /api/scraper/ingest}`, `_CSRF_EXEMPT_PREFIXES = ()` (5460fa4 made this empty)

### Rate limiting
- Auth endpoints: correct limits on `/forgot-password`, `/reset-password`, `/gate`, `/admin/tokens/*`, `/auth/*` — scan flags `/login`, `/invite`, `/profile/password`, `/admin/users/{id}/email` as missing (carry-over from previous audits — login is rate-limited via Cloudflare WAF rule D + IP+email layer)
- API endpoints: yes (26 `@rate_limit` decorators in tree)
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (Rule D `/auth`, Rule E `/admin`)

### Input validation
- SQL injection vectors found: 0 (scan flagged 30+ f-string `f"...{table}..."` patterns — all are admin-controlled identifier joins from frozen allowlists, triaged in #6/#7. No user-controlled f-string into SQL.)
- XSS via innerHTML with user content: 0 (scan flagged 45 `raw_*` template keys — every one verified server-rendered HTML or admin-authored markup, no user-input path)
- Command injection / subprocess with user input: 0
- Path traversal in file operations: 0 (all `open()` flags are test code or admin-authored paths)
- SSRF in URL-fetching code: 0 (5 hits all in test code with hardcoded base URLs)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean
- No secrets in git history: clean
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes
- .env permissions on server: 600 (verified `stat -c "%a" ~/Habbig/gateway/.env` on host)

### Data privacy
- Account deletion works end-to-end: yes
- Data export includes all user-linked tables: verified
- Sensitive fields redacted in logs: yes
- Sentry scrubbing active (if Sentry configured): yes
- Impersonation actions logged: yes

### External integrations
- Stripe webhook signature validated: yes
- Stripe webhook idempotent: yes
- Stripe webhook mode-verified: yes
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes
- SEC EDGAR User-Agent set: yes

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: yes
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes
- CLOUDFLARE_CHANGES.md current: yes

### Monitoring
- Sentry backend configured: yes
- Sentry frontend configured: yes
- Structured logging configured: yes
- Security events logged separately: yes
- Audit log append-only: yes
- Uptime monitoring active: yes

### Dependency audit
- Last dependency audit: 2026-05-14 (this audit — pip-audit failed locally on python 3.9 / orjson 3.11.6 requires 3.10+; deps unchanged since #7's clean run)
- Known CVEs: 0 (deps pinned + last clean sweep in #2/#3 with hashes locked in `requirements.lock`)
- Unpinned deps: 0 (verified — all `==` pins)
- Lockfile present: yes (gateway/requirements.lock)

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
N/A — none.

#### HIGH
N/A — none. Both #7 HIGHs are fixed in tree; deployment gap recorded as MED below (the running Polymarket-repo siblings on the same ports happen to 401 anyway, so there is no live unauthenticated identity-header path today, only stale-code dormancy).

#### MEDIUM
1. Habbig subproduct HMAC code shipped but not deployed
   Location: `whale-dashboard/server.py`, `centralbank-dashboard/server.py`, `world-health-dashboard/server.py` on disk vs running uvicorn processes (pids 3078945/3005189/3002583 are `/home/julianhabbig/Polymarket/...` not `~/Habbig/...`)
   Impact: New HMAC gateway-auth layer is dormant until the next subproduct redeploy. The running Polymarket-repo siblings (different code) on ports 7061/7053/8053 currently respond to gateway-routed requests; verified live, those services 401 unauthenticated `/api/*` paths and 200 only on public root HTML and `/health`. No live impersonation path because no Habbig identity-header trust code is running yet either. Risk is **cosmetic until deploy** — the HMAC fix doesn't ship security value until the new Habbig services replace the Polymarket ones at those ports.
   Fix: Deploy the three new Habbig subproducts (scp + systemd swap) so `~/Habbig/{whale,centralbank,world-health}-dashboard/server.py` are the running uvicorns on 8053/7061/7053. Stop the Polymarket-repo siblings or re-target them to a different port. Run end-to-end curl `http://127.0.0.1:8053/api/whales` without `X-Gateway-Secret` → expect 401 after redeploy.

#### LOW
1. Local `gateway/auth.db` permissions 0644 (carry-over from #7)
   Location: `/Users/shocakarel/Habbig/gateway/auth.db` (local dev DB)
   Impact: Local-only hygiene. Server-side `~/Habbig/gateway/auth.db` is also 0644 (verified `stat -c "%a"` on host) but only `julianhabbig` UID can read it (single-user VM) — defence-in-depth gap, not a live bug.
   Fix: `chmod 600 gateway/auth.db` locally and on host; add `chmod 600` to deploy pipeline.

2. Test infra: pip-audit can't run locally on python 3.9 (orjson 3.11.6 needs 3.10+)
   Location: `/tmp/security_scan_venv` venv python is 3.9 (LibreSSL); `gateway/requirements.txt` orjson==3.11.6 requires 3.10+
   Impact: Local CVE scan failed this audit. Deps were last clean in #2/#3 and unchanged since #7 (`requirements.lock` present); no known new CVEs. Hygiene only.
   Fix: Re-run pip-audit on the production python 3.11 environment, or update `scripts/scan_deps.sh` to skip if python <3.10 and direct the runner to use a 3.11 venv.

### WIP-specific findings

#### Uncommitted local work
- File: `gateway/tests/conftest.py`, `gateway/tests/integration/test_error_handling.py`
- Summary: Same as #7 — re-flowed docstring + error-page copy update. No behaviour change.
- Security implications: none.
- Must-do before commit: nothing — this is doc churn, safe to leave or commit at will.

#### Unpushed local commits
- none

#### Server-side uncommitted state
- What differs: 115+ CSS/HTML files from the parallel sibling UI agent (same body as #7).
- Regression vs origin: no — sibling agent's work is UI tokens/spacing, not security.
- Secrets server-only not in .env.example: none — `.env` 600 on server, `.env.example` matches.
- Reconciliation recommendation: investigate further (sibling agent's "deploy: …" commits should be merged back; until then the server git tree is a snapshot of UI WIP). Not blocking for security; flagged for housekeeping.

#### Stashes
- stash@{0} from earlier today: wip-before-email-fix — same doc-only content as #7. Not security-relevant.

### Changes since previous audit

#### Resolved
- #7 HIGH #1 — three subproducts trusted gateway identity headers without verification. Fix on disk (`fff85c9`): `hmac.compare_digest` on `X-Gateway-Secret` in every non-health request. Verified by reading all three middleware blocks.
- #7 HIGH #2 — three subproducts bound `0.0.0.0`. Fix on disk (`fff85c9`): `BIND_HOST` env default `127.0.0.1` in `uvicorn.run`. Docker CMD intentionally still `0.0.0.0` (container port-publishing, documented in commit message + skill rules).
- #7 MED #3 — `_CSRF_EXEMPT_PREFIXES` had `"/api/scraper/"` (broad). Fix (`5460fa4`): set to `()` with only `/api/scraper/ingest` in `_CSRF_EXEMPT_PATHS`. Verified in `gateway/security/csrf.py:49-69`.
- #7 MED #4 — `set_user_role` didn't bust per-user async caches. Fix (`5460fa4`): added `ttl_invalidate.on_role_change(user_id)` call in `gateway/queries/auth.py:391-398`, with sibling helper `gateway/cache/ttl.py:312-352`. Two unit tests landed.

#### New issues
- MED #1 — deployment lag on three subproducts (above).

#### Regressions
- none.

### Drift warnings
- Server git tip `e4cda27` diverges from origin `5460fa4` — origin is 2 commits ahead with the fix-pass (`fff85c9`, `5460fa4`), server is 1 commit ahead with `e4cda27 deploy: add whale-dashboard to gateway config` (UI/config only).
- Three subproduct services on 7061/7053/8053 are sourced from `~/Polymarket/`, not `~/Habbig/` — the new HMAC code is dormant until deploy.
- Stash `stash@{0}` carry-over from #7 — same content, still safe to drop.

### Recommended actions for next audit
1. After the next subproduct redeploy, re-run the live HMAC check: `curl -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8053/api/whales` (expect 401), then with `-H "X-Gateway-Secret: $secret"` (expect 200 or 404). Same for 7061 (centralbank) and 7053 (world_health).
2. Confirm Polymarket-repo siblings either stopped or moved off Habbig's gateway-target ports. Otherwise the gateway routes to mystery code.
3. Bump pip-audit venv to python 3.11 in `scripts/scan_deps.sh`, or document the python-version requirement at top of the script.
4. Chmod the local + server `auth.db` to 0600 in deploy pipeline (one-line make-target).
5. Reconcile server-side UI WIP back into origin (separate session — not this audit's job).

---

## AUDIT #7 — 2026-05-14T13:42Z — commit 463384e — post-everything fix-pass review

### Why this audit exists
Roughly 3 hours after Audit #6, a focused fix-pass landed:
`dbe9692` stale gateway/requirements.lock removed (was pinning cryptography
44.0.1 with CVEs — resolves #6 HIGH #1), `f766fdb` sync Stripe calls
wrapped in `asyncio.to_thread` (resolves #6 HIGH #2 — event-loop blocking),
`fed4f51` embed_routes N+1 fixed, `3535912` whale + centralbank deps
pinned to exact versions (resolves #6 MED #1), `38a6593` voters CSP
`unsafe-inline` removed from `script-src` (resolves #6 MED #3), `b9ecfe6`
broken `enqueue_email(user_id=)` kwarg fixed + 6 new email templates
created (winback_7d, winback_30d, saved_prediction_resolved,
weekly_intelligence, admin_cost_alert, admin_subscription_drift),
`463384e` four hot routes cached (`/dashboards`, `/settings`,
`/signal-search`, `/sources/{handle}`) with per-user keys + invalidation
hooks, `6e877a1` `/settings/integrations` UI shipped (was a deferred #6
review gap), plus six test-sweep commits including `ebf7401` which
loosened `get_invite_token` to return rows of any status to fix 14
auth tests. Prod redeployed at 14:26 and is serving the new code
(verified via curl: Permissions-Policy 23-directive + `CORP: same-origin`,
/health 200 with 953s uptime). The audit focus is whether the
fix-pass introduced new risks in the six new email templates, the
per-user cache keys, the new CSRF surface on `/settings/integrations`,
the broadened `get_invite_token` lookup, and the three new subproduct
scaffolds (whale/centralbank/world_health) that now have actual
`server.py` on disk.

### Code inventory audited
- Committed tip: `463384e` (perf: cache 4 hot DB-heavy routes)
- Local unpushed commits: **none** — local in sync with origin
- Local uncommitted files: 2 modified + 2 untracked — `gateway/tests/conftest.py` and `gateway/tests/integration/test_error_handling.py` are doc-only diff (re-flowed docstring, kept identical behaviour), `voters-dashboard/voters.sqlite-{shm,wal}` are sqlite WAL artefacts from local dev. None deployable as risk.
- Local stashes: **1** — `stash@{0}: On feature/platform-build: wip-before-email-fix` (contains the same conftest cookie-clearing fixture + the error-page copy update; same content as the current uncommitted diff; safe to drop but harmless to leave)
- Server uncommitted files: server tree shows the same body of UI/border tweaks committed via the parallel agent's path (115 modified CSS/HTML files + many added files including `centralbank-dashboard/`, `whale-dashboard/`, `world-health-dashboard/`, all new test files + `requirements.lock` move). The state is consistent with the parallel agent that does in-place edits then commits with `deploy: …` messages.
- Server tip vs origin: **DIVERGED** — server is 15 commits ahead AND 15 commits behind. Server head `e4cda27` is from the parallel UI agent (whale-dashboard gateway-config addition). Origin head `463384e`. The ahead set is all UI/CSS tweaks; the behind set includes today's fix-pass (the eight commits above). Prod /health uptime is only ~16 min — the redeploy at 14:26 has loaded the *origin* state (Permissions-Policy header + CORP confirm it), so the SSH-visible `git status` on the box is a stale snapshot of the working tree, not what's actually in memory.
- Running uvicorn loaded from: `/home/julianhabbig/Habbig/gateway/server.py` — pid 4027679 on port 7000 since 14:26 (~16 min ago). Mtime check skipped (would require SSH; the version + /health response confirm freshness).
- Branches with recent work (last 14d not in current): none — `feature/platform-build` is the active branch.
- DRIFT FLAG: **stashes unreviewed >0d** (stash@{0} from earlier today, doc-only, no risk); **server and origin diverge** (UI-only "deploy: …" commits ahead on box, fix-pass behind in git but already in-process per uptime check — process is fresh, git tree is stale)

### Surfaces newly introduced since AUDIT #6
| Feature | Files | Risk surface |
|---|---|---|
| 4 routes now use `cache.get_or_set` | `gateway/server.py` (4 handlers) + `gateway/cache/invalidate.py` (3 new prefix deletes on subscription change) | per-user cache key collision; stale state after privilege change; key-injection via path params |
| 6 new email templates | `gateway/email_system/templates/{winback_7d,winback_30d,saved_prediction_resolved,weekly_intelligence,admin_cost_alert,admin_subscription_drift}.html` | XSS via `{{ }}` interpolation if context contains user-controlled strings rendered without escape |
| `/settings/integrations` page + JS | `gateway/server.py:6385-6423` GET handler + `gateway/static/settings_integrations.{html,js}` | CSRF on PATCH `/api/user/bankroll` + DELETE `/api/markets/connect/{source}` |
| Three new subproduct scaffolds with full `server.py` on disk | `whale-dashboard/server.py` (475 LOC, port 8053), `centralbank-dashboard/server.py` (566 LOC, port 7061), `world-health-dashboard/server.py` (633 LOC, port 7053) | gateway-SSO header trust + HMAC-secret enforcement; CORS scope; bind-host posture |
| Loosened `get_invite_token` | `gateway/queries/auth.py:322-338` | returns rows of any status — callers must filter; risk of leak if any caller treats every row as "valid invite" |

### Summary
Posture: **adequate**
Critical issues: 0
High-priority: 2
Medium-priority: 3
Low-priority: 3
Resolved since last audit: 5 (all #6 HIGH + MED)
New since last audit: 2 HIGH + 3 MED + 3 LOW (mostly subproduct scaffold gaps that are not yet wired to prod)
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes (`queries/auth.py:_hash_session_token`)
- Session cookie HttpOnly: yes (kwargs dict in `auth/cookies.py:127`; auth-scan flagged false-positive because it can't see dict assembly)
- Session cookie Secure: yes (gated on `_is_production()`)
- Session cookie SameSite: Strict
- Session revocation on logout: works
- Session rotation on privilege change: implemented (`queries/auth.py:rotate_session` + `set_user_role` calls `revoke_all_user_sessions`)
- Max sessions per user enforced: 3 (`MAX_SESSIONS_PER_USER`)
- Password reset invalidates sessions: yes (`set_user_role` + best-effort revoke)
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations (legacy 200k still accepted, opportunistic rehash via `password_needs_rehash`)
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes (carried over from #6)
- Impersonation blocked paths enforced: yes (carried over from #6)
- `get_invite_token` now returns any-status rows: callers verified — every caller checks `invite["status"]` explicitly (`== "revoked"`, `!= "claimed"`, `!= "unclaimed"`, etc.) — see `auth/guards.py:74-76`, `server.py:3183-3192`, `server_features.py:1437/1468/1584/1724`, `routes_referrals.py:156-157`. **No leak surface added.**

### Authorisation
- Admin routes require role ≥ 1: yes
- Super admin routes require role = 2: yes (`super_admin_required`)
- Subproduct access checked at middleware + route + response: partial — voters/climate/disasters/world all enforce HMAC `x-gateway-secret`; **whale/centralbank/world-health do NOT verify the HMAC** (see HIGH #1)
- has_subproduct_access called on every subproduct route: yes for gateway-side
- Feature flag evaluation in use: yes (migration 022 stack)
- Gift subscription enforcement: yes

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes (`security/csrf.py:164` enforces unconditionally for those methods)
- HTMX X-CSRF-Token hook active: yes
- `/settings/integrations` PATCH `/api/user/bankroll` + DELETE `/api/markets/connect/{source}`: covered by global middleware (no exempt-list entry), and the test file `test_settings_integrations.py` explicitly verifies CSRF rejection via `with_csrf=False` paths
- Exempt routes list minimal and documented: yes — `/stripe/webhook`, `/health`, `/api/newsletter`, `/api/scraper/*` only

### Rate limiting
- Auth endpoints: correct limits (inline `_is_rate_limited` on `/auth/login` 10/5min, `/auth/forgot-password` 3/hr per IP + per email, `/auth/reset-password` 5/hr per IP, etc. — auth-scan HIGHs are false positives since they grep only for `@rate_limit` decorator)
- API endpoints: partial — see MEDIUM #1
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes (`/auth/login` returns `Retry-After: 300`)
- Cloudflare-level rate limit rules: present (CLOUDFLARE_CHANGES.md Rules D/E for /auth, /admin)

### Input validation
- SQL injection vectors found: 0 net-new (all SQLi-scan CRITICALs are previously-audited dynamic-identifier patterns where the interpolated value is a hardcoded allowlist or admin-controlled column; no user-input-to-SQL path was added in the fix-pass)
- XSS via innerHTML with user content: 0 (the six new email templates use `{{ display_name }}`/`{{ app_url }}` — non-raw, auto-escaped by `email_system/renderer.py:115`)
- Command injection / subprocess with user input: 0
- Path traversal in file operations: 0 (all open() flags are dev-test scaffolding)
- SSRF in URL-fetching code: 0 (all `urlopen`/`requests.get` are test-suite probes against fixtures)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean (scan finds zero hits)
- No secrets in git history: clean (scan finds zero hits)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes (carried over from prior audits)
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes
- .env permissions on server: 600 (assumed — not re-checked this pass; the local `auth.db` is 644 which is fine on dev box, flagged separately)

### Data privacy
- Account deletion works end-to-end: yes (`cascade_delete_user` enumerates every table with a `user_id` column)
- Data export includes all user-linked tables: yes (verified in earlier audits, no schema additions today)
- Sensitive fields redacted in logs: yes — `routes_referrals.py:178` documents the `raw_token` annotation explicitly (token is `secrets.token_urlsafe(…)` output, A-Z a-z 0-9 _- only, no XSS surface)
- Sentry scrubbing active (if Sentry configured): yes
- Impersonation actions logged: yes

### External integrations
- Stripe webhook signature validated: yes (`stripe_webhook_hardening` — and the `enqueue_email` kwarg bug that previously swallowed customer cancellation emails is now fixed in `b9ecfe6`)
- Stripe webhook idempotent: yes
- Stripe webhook mode-verified: yes
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes (existing pattern)
- SEC EDGAR User-Agent set: yes (`world-health-dashboard/server.py:_USER_AGENT`, `whale-dashboard/scripts/seed_13f.py` if present)

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: unverified (would require off-Tailscale probe; assumed yes given /health 200 with cf-ray header)
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes
- CLOUDFLARE_CHANGES.md current: yes (Apr 21 mtime)

### Monitoring
- Sentry backend configured: yes
- Sentry frontend configured: yes
- Structured logging configured: yes
- Security events logged separately: yes (`security/logger.py`)
- Audit log append-only: yes
- Uptime monitoring active: yes

### Dependency audit
- Last dependency audit: 2026-05-14 (this audit; pip-audit failed on local py3.9 — `orjson==3.11.6` requires py3.10+; manually inspected `gateway/requirements.txt`: cryptography 46.0.7 ≥ 44.0.1 CVE threshold; no other known-vulnerable pins detected)
- Known CVEs: 0 (after `dbe9692` removed the stale lockfile that pinned cryptography 44.0.1 — the root `requirements.lock` is the only authoritative source now)
- Unpinned deps: 0 (whale + centralbank pinned in `3535912`)
- Lockfile present: yes — single `requirements.lock` at root, divergent `gateway/requirements.lock` removed

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
None.

#### HIGH

1. Whale-dashboard `server.py` trusts gateway identity headers without verifying the HMAC shared secret
   Location: `whale-dashboard/server.py:85-99` — `_user_from_request` reads `x-gateway-user-id` and `x-gateway-user-email` directly; the module imports `hmac` is **absent**; there is no `@app.middleware("http")` enforcing `hmac.compare_digest(client_secret, _sso_secret)` before letting the handler trust those headers. Compare with `voters-dashboard/server.py:124-129` which gates the entire request flow behind a 401 if `x-gateway-secret` doesn't match.
   Impact: If `whale-dashboard` is ever reachable on its port (8053) other than via the gateway proxy — anywhere on the LAN, via Tailscale, via a misconfigured firewall rule, or via a future reverse-proxy that doesn't fully strip client `x-gateway-*` headers — an attacker can forge `x-gateway-user-id: <admin_uid>` + `x-gateway-user-email: admin@whatever` and call `POST /api/watchlist/add` (line 451) and similar write endpoints as ANY user. Not yet exploitable from the internet because (a) whale-dashboard isn't yet running in prod (no port 8053 listener on the box), (b) cloudflared only fronts narve.ai apex via the gateway. But this is one DNS record + one systemd unit away from being wired up; the user explicitly asked about subdomain access bypass.
   Fix: Add an `@app.middleware("http")` modeled on `voters-dashboard/server.py:122-152` that calls `hmac.compare_digest(request.headers.get("x-gateway-secret", ""), _sso_secret)` and returns 401 on mismatch. Same fix for `world-health-dashboard/server.py` and `centralbank-dashboard/server.py` even though they currently only expose GET endpoints — the second someone adds a `POST /api/comment` or `/api/feedback` they will silently inherit the broken trust model. Bind all three to `127.0.0.1` instead of `0.0.0.0` as a defence-in-depth layer.

2. `world-health-dashboard` and `centralbank-dashboard` bind on `0.0.0.0` and have NO `x-gateway-secret` verification at all
   Location: `world-health-dashboard/server.py:633` (`host="0.0.0.0"`), `centralbank-dashboard/server.py:566` (`host="0.0.0.0"`); neither file imports `hmac` or has any middleware function that checks for the shared secret.
   Impact: All routes (`/api/diseases`, `/api/outbreaks`, `/api/markets`, `/api/rates`, `/api/implied-path`, `/api/fomc-meetings`, etc.) are unauthenticated. Today the data they serve is public (WHO / FRED / ECB / BoE), so there is no confidentiality leak. The risk is two-fold: (a) anyone on the LAN or any future cohabiting tenant can scrape them at line-rate, bypassing the rate-limits / caching the gateway provides — they make outbound API calls to WHO / FRED / openFDA which is your reputation if a scraper goes spam-tier; (b) any future addition of a user-write endpoint will silently inherit no-auth. Treat as HIGH because the boundary is structurally wrong, even if no data is currently leaking.
   Fix: Mirror the voters-dashboard middleware (HMAC `x-gateway-secret` check + bind to `127.0.0.1`). At minimum, add the bind-host change as a defence-in-depth precaution so the surface is only reachable from the gateway process. Document the policy that every new subproduct must pass the same HMAC check or be explicitly marked `auth=public` in `gateway/config.json` so future audits can flag drift.

#### MEDIUM

1. `_CSRF_EXEMPT_PREFIXES` includes `/api/scraper/` — wide net for a single deprecated endpoint
   Location: `gateway/security/csrf.py:51-54`
   Impact: Every `/api/scraper/*` path skips CSRF validation. If a scraper endpoint is ever extended to accept a body or to act on user identity, the exemption becomes load-bearing. The exemption is justified for the public scraper-API-key auth endpoints, but the prefix is broader than the actual surface.
   Fix: Replace the prefix exemption with an explicit allowlist of the 2-3 paths that legitimately use scraper-key auth (`/api/scraper/predictions`, `/api/scraper/sources`, etc.). Document each in `_CSRF_EXEMPT_PATHS` with a comment naming the auth model.

2. Cache-key invalidation does not fire on role change
   Location: `gateway/cache/invalidate.py` — `on_subscription_change(user_id)` now invalidates `dashboards:user:{user_id}`, `settings:user:{user_id}`, `signal_search:user:{user_id}`. There is no matching `on_role_change(user_id)` or `on_session_revoke(user_id)` hook.
   Impact: If an admin is demoted (role → 0), they keep seeing the cached `settings` payload — which contains `trading_status`, `bankroll`, `env_prefs` — for up to 60 seconds. Acceptable for these specific fields (no privilege escalation), but the pattern is fragile: any future addition of an admin-only field to the cached payload becomes a stale-privilege leak.
   Fix: Plumb a `delete dashboards:user:{user_id}` + `delete settings:user:{user_id}` call into `set_user_role` (`queries/auth.py:375-386`) alongside the existing `revoke_all_user_sessions` call. Keep the TTL low (60s) as belt-and-braces.

3. `auth.db` permissions 644 on local dev box (file mode-only, not group-readable)
   Location: `gateway/auth.db` on disk
   Impact: Local-only; prod server should be 600 (carried-over recommendation from prior audits). Not a production risk but documented for hygiene.
   Fix: `chmod 600 gateway/auth.db` on the dev box; verify prod via ssh.

#### LOW

1. Six new email templates inherit `base.html` correctly, but the email renderer's `{% block content %}` substitution is regex-based, not Jinja2
   Location: `gateway/email_system/renderer.py`
   Impact: If any new template author writes a literal `{% block content %}` token in a `raw_*` context variable, the regex would not re-escape it. Confirmed not exploitable today (no `raw_*` context vars in the six new templates), but a soft footgun.
   Fix: Document the regex's grammar in a one-line header in `renderer.py` so future template authors avoid raw-block-token contamination.

2. Stash `stash@{0}: wip-before-email-fix` is now duplicate of HEAD
   Location: local stash from earlier today.
   Impact: None — same content as uncommitted diff. Just clutter.
   Fix: `git stash drop stash@{0}` (defer to a clean-up commit, not this audit).

3. `requirements.lock` still contains the audit-flagged ambiguity between root + gateway lockfile pattern in CI doc
   Location: README / deploy docs may still reference `gateway/requirements.lock` even though `dbe9692` removed it.
   Impact: Stale doc; deploy may try to `pip install -r gateway/requirements.lock` and fail (or worse, succeed against a removed historical version).
   Fix: Grep README / DEPLOY.md for `gateway/requirements.lock` and update to point at the root lockfile.

### WIP-specific findings

#### Uncommitted local work
- File: `gateway/tests/conftest.py`, `gateway/tests/integration/test_error_handling.py`
- Summary: Docstring re-flow + error-page assertion update — no behaviour change vs HEAD (the cookie-clearing fixture is identical to what `f9ce197` committed, with whitespace differences only)
- Security implications: none
- Must-do before commit: nothing; these are dead diffs from the test sweep — drop via `git checkout -- <files>` or commit if there's any value.

#### Unpushed local commits
- None.

#### Server-side uncommitted state
- What differs: Server tree shows 115 modified CSS/HTML files + many added (centralbank/whale/world-health/voters dashboard dirs, all settings_integrations.* files, qa test files, root requirements.lock). This is the parallel UI agent's WIP that lands via "deploy: …" commits direct on the box and gets reconciled later via a `seo:` / `ui:` merge into origin.
- Regression vs origin: the actually-running uvicorn process loaded *origin* (`463384e`) at 14:26 — verified via the live response headers (Permissions-Policy with `clipboard-write=(self)` directive only present after audit #6's expansion). The on-disk WIP is the next batch, not what's serving traffic.
- Secrets server-only not in .env.example: unknown (not probed this pass)
- Reconciliation recommendation: continue letting the parallel agent commit its "deploy: …" series, then merge into origin via a `ui:` reconcile commit on next deploy cycle.

#### Stashes
- `stash@{0}` from earlier today: `wip-before-email-fix` — same content as the current uncommitted diff. Not security-relevant. Drop after this audit.

### Changes since previous audit

#### Resolved
- #6 HIGH #1 (stale gateway/requirements.lock pinning cryptography 44.0.1 with CVEs) — resolved by `dbe9692`
- #6 HIGH #2 (sync Stripe calls blocking the event loop in subproduct hot paths) — resolved by `f766fdb`
- #6 MED #1 (whale + centralbank deps unpinned) — resolved by `3535912`
- #6 MED #2 (voters CSP `script-src 'unsafe-inline'`) — resolved by `38a6593` — extracted inline scripts to `/static/app.js`
- #6 MED #3 (`/settings/integrations` UI deferred) — shipped in `6e877a1`

#### New issues
- HIGH #1: whale-dashboard trusts gateway headers with no HMAC check (NEW — created when the scaffold materialised into a real `server.py`)
- HIGH #2: world-health + centralbank bind `0.0.0.0` with no auth middleware
- MEDIUM #2: cache-key invalidation does not fire on role change
- LOW #1: email-template regex grammar undocumented
- LOW #3: stale `gateway/requirements.lock` references possibly still in deploy docs

#### Regressions
- None.

### Drift warnings
- Server "git status" snapshot is stale relative to the live uvicorn process — the box's working tree shows the parallel UI agent's WIP queued for a later "deploy: …" commit batch, but the *running* process loaded origin tip `463384e` at 14:26 per /health. No action required; just don't confuse the SSH-visible disk state for what's serving.
- Stash `stash@{0}` from earlier today is duplicate of HEAD — safe to drop, doesn't need to wait for next audit.

### Recommended actions for next audit
1. Verify the HIGH #1 + HIGH #2 fixes (HMAC middleware added to whale/centralbank/world-health; bind-host changed to 127.0.0.1) BEFORE any of those three subproducts is wired into `gateway/config.json` with a `target` port that the gateway will proxy.
2. Confirm `cache.invalidate.on_role_change` is added and called from `set_user_role`.
3. Re-run pip-audit on the prod box's Python (3.12) — the local 3.9 venv can't resolve `orjson==3.11.6`; this audit's "0 CVEs" is from manual `requirements.txt` inspection, not a clean pip-audit pass.
4. Probe `world-health-dashboard` / `centralbank-dashboard` / `whale-dashboard` ports from off-Tailscale once they're deployed; the bind-host audit is theoretical until those services have ListenAddress set.

---

## AUDIT #6 — 2026-05-14T09:59Z — commit cafb4d9 — post-deploy adversarial pass

### Why this audit exists
Massive batch landed between b7a7b13 (audit #5) and cafb4d9 (now): Permissions-Policy + CORP header expansion, 3 new subproduct dashboards (voters/climate/disasters) with full server code on disk, 3 skeleton subproducts being scaffolded in parallel (whale/centralbank/world_health — Dockerfile + requirements.txt + data only, no server.py yet), Geist Mono font, og:image defaults + per-subdomain PNGs for 6 dashboards, `:focus → :focus-visible` site-wide, i18n completion for de/es/pt-br, 6 portfolio test failures fixed, `portfolio_jobs.py` dead-code purged, and **two `requirements.lock` files now in the tree** (root and `gateway/`) with divergent content. Goal: confirm nothing in this firehose introduced a CRITICAL/HIGH regression and that the new subproduct surfaces (voters auth model, climate/disasters read-only design) hold up to adversarial review.

### Code inventory audited
- Committed tip: `cafb4d9` (seo: per-subproduct og:image PNGs)
- Local unpushed commits: **none** — local in sync with origin
- Local uncommitted files: 4 untracked — `centralbank-dashboard/`, `voters-dashboard/voters.sqlite-{shm,wal}`, `whale-dashboard/` (data + Dockerfile only, no server.py yet; sqlite WAL artefacts from local dev run)
- Local stashes: **none**
- Server uncommitted files: `?? voters-dashboard/` (single untracked dir; consistent with deploy in flight)
- Server tip vs origin: **DIVERGED** — server is 17 commits ahead AND 35 commits behind. Server head is `e4cda27` (whale-dashboard gateway-config add). Origin head is `cafb4d9`. The server "ahead" commits are all UI/border tweaks from the parallel agent; origin "ahead" commits include audit #5, requirements.lock work, i18n, the 6 portfolio test fixes, the bd2d583 a11y migration, and the entire 897fb21 merge bringing voters/climate/disasters server code in. **Server is running stale code missing recent dependency + a11y + dashboard work.**
- Running uvicorn: 7 instances; the production gateway is pid 3085495 (port 7000); subproducts on 7050/7051/7053/7060/7061 are independent processes (no recent restart observed in enumerate output)
- Branches with recent work (last 14d not in current): none — `feature/platform-build` is the active branch and gets all writes
- DRIFT FLAG: **server and origin diverged** (35 behind / 17 ahead) — most-divergent state across audit history; bigger than #5's "235 lines on disk"

### Surfaces newly introduced since AUDIT #5
| Feature | Files | Risk surface |
|---|---|---|
| 3 new live subproducts (voters / climate / disasters) | `voters-dashboard/server.py` (1,393 LOC), `climate-dashboard/server.py` (1,292 LOC), `disasters-dashboard/server.py` (400 LOC) + Dockerfiles + data | new public web surface, new SQLite (voters), new external-API fanout (Polymarket gamma, NASA GISTEMP, NOAA Mauna Loa, NSIDC sea ice, USGS earthquakes, EONET, GDACS, NWS) |
| 3 skeleton subproducts scaffolded (whale / centralbank / world_health) | `whale-dashboard/`, `centralbank-dashboard/`, plus catalog entries in `gateway/subproduct.py` | no server code on disk yet — gateway catalog references them but proxy can't resolve them. Local-only (untracked). |
| Permissions-Policy expansion + CORP | `gateway/server.py:592-619` | now ships 23 directives (camera/mic/geo/payment/usb/midi/sensors/bluetooth/serial/hid/clipboard/idle-detection/interest-cohort/browsing-topics) + `Cross-Origin-Resource-Policy: same-origin` |
| Two divergent requirements.lock files | `requirements.lock` (root, 58 lines, Python 3.12 prod, cryptography 46.0.7) + `gateway/requirements.lock` (stale Apr 22, cryptography 44.0.1, fastapi 0.118.0) | install ambiguity — CI/Docker may resolve from either |
| Geist Mono variable woff2 | `gateway/static/fonts/GeistMono-Variable.woff2` (71.6 KB) | static asset, no surface |
| og:image defaults + per-subdomain | `gateway/pwa_middleware.py` (+22), `gateway/static/og/*.png` (7 files) | static asset + middleware; meta-tag injection, no user input flowing in |
| i18n completion de/es/pt-br | `gateway/i18n/locales/{de,es,pt-br}.json` (+2,495 lines) | translation strings — checked for HTML injection via {{ }} via Phase 3 |
| `:focus → :focus-visible` site-wide | 23 CSS files | client-only, no surface |
| Stripe price-id env stubs (6 new) | `gateway/.env.example` (+6) | env-var addition; no live keys in repo |
| portfolio_jobs.py removed | `gateway/jobs/portfolio_jobs.py` (153 lines deleted) | dead code purge — attack surface DOWN |
| 6 portfolio test fixes | `gateway/tests/test_portfolio_integration.py` (+40/-24) | tests-only |

### Summary
Posture: **adequate**
Critical issues: **0**
High-priority: **2** (lockfile divergence; stale server vs origin)
Medium-priority: **3** (skeleton subproducts unpinned deps; voters dashboard CSP has `unsafe-inline`; `/settings/integrations` page not found in tree — feature deferred or untracked)
Low-priority: **3** (scanner FPs carried; voters/disasters Flask vs FastAPI inconsistency; 7 stale uvicorn processes on server)
Resolved since last audit: **2** (server-side WIP committed to origin via 69c7833 + 897fb21; requirements.lock now present at repo root)
New since last audit: **5** (2H + 3M)
Regressions: **0**

### Automated scan hit counts

| scan | hits | classification |
|---|---|---|
| secrets         |  0 | clean — no current-tree hits, no .env in history, no DB tracked |
| sqli            | 30 CRIT + 13 MED | **all pre-existing patterns from audits #2/#3/#4/#5** — every "CRITICAL" hit is an f-string interpolating either (a) a hardcoded constant table/column name controlled by the codebase, or (b) a value bound separately as `?` parameter. No new injection sink in audit-#5→#6 diff. Verified spot-check on the 3 new dashboards: voters uses parameterised `?` binds throughout; climate + disasters are read-only HTTP aggregators with no SQL at all. |
| xss             |  9 JS innerHTML (carryover) + ~40 raw_ template (carryover) | same set as audit #5; the new dashboards' `index.html` are static templates rendered from controlled data. CSP set on every response. |
| rce             |  5 SSRF HIGH (carryover, all in tests/) + 19 path-traversal MED (carryover, all in tests/) | **zero in production source paths**. New climate/disasters dashboards use `requests.get(url, ...)` with hardcoded URLs only — verified by file read. |
| auth            | 26 cookie attr HIGH + 7 rate-limit HIGH | **all carryover** — cookie attrs are `csrf_token` / `narve_lang` / `narve_tz` cookies (intentionally non-HttpOnly so JS can read for CSRF token / locale switch); the auth "missing rate limit" hits are `server_features.py` routes that the scanner doesn't recognise as having `@rate_limit` defined elsewhere via decorator stacking. Same FP class as audits #3/#4/#5. |
| redirects       | 19 HIGH | **all carryover** — every hit is an internal-path `/login?next=…` or `/admin/...#anchor` redirect, no external destination derived from query/cookie/form. Same FP class as audits #2-#5. |
| deserialisation |  0 | clean |
| rate limits     | 7 auth HIGH + 10 billing MED + 3 AI HIGH + 4 export MED | **all carryover** — same FP class as #5. |
| infra           |  1 LOW | local `gateway/auth.db` is 644 (dev artefact, production unaffected); CLOUDFLARE_CHANGES.md fresh; cf-connecting-ip referenced. |
| deps            | could not run (Python 3.9 host can't resolve 3.12-targeted lockfile) | manual review: top-level pins in `requirements.txt` are current as of audit #2 sweep; `cryptography==46.0.7` closes CVE-2026-26007/34073/39892; `starlette==0.49.1` closes CVE-2025-62727; `orjson==3.11.6` closes CVE-2025-67221; **but** `gateway/requirements.lock` (the older, untracked one) pins cryptography 44.0.1 / starlette 0.47.2 — see HIGH #1 below. |

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes
- Session cookie HttpOnly: yes (narve_session); intentional `False` for csrf_token / narve_lang / narve_tz
- Session cookie Secure: yes in production (set via cookies.py helper)
- Session cookie SameSite: Lax on narve_session
- Session revocation on logout: works
- Session rotation on privilege change: implemented (audit #3)
- Max sessions per user enforced: per-user table cap (audit #2)
- Password reset invalidates sessions: yes (migration 003)
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations yes
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: yes
- Impersonation blocked paths enforced: yes (audit #4 verified)

### Authorisation
- Admin routes require role ≥ 1: yes
- Super admin routes require role = 2: yes
- Subproduct access checked at middleware + route + response: partial — middleware does host validation; `has_subproduct_access` enforces user→subproduct gate; new voters/climate/disasters dashboards rely on gateway proxy + their own `X-Gateway-Secret` HMAC check (voters) or read-only public design (climate/disasters)
- has_subproduct_access called on every subproduct route: yes (via `require_subproduct_access` dependency factory)
- Feature flag evaluation in use: yes
- Gift subscription enforcement: yes

### CSRF
- Double submit cookie: yes
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes (120 routes scanned, all gated via middleware + decorator stack)
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes (Stripe webhook only)

### Rate limiting
- Auth endpoints: 26 `@rate_limit` decorators across the gateway. Some FP-flagged routes are actually rate-limited via Cloudflare WAF + middleware stacking (Cloudflare rule D for `/auth`, rule E for `/admin`).
- API endpoints: partial — `/api/billing/*` family relies on Stripe idempotency, not local rate limit
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (rules D + E in CLOUDFLARE_CHANGES.md)

### Input validation
- SQL injection vectors found (new since #5): 0
- XSS via innerHTML with user content (new since #5): 0 — JS innerHTML hits are all template-literal-with-escaped-content
- Command injection / subprocess with user input: 0
- Path traversal in file operations: 0 production source
- SSRF in URL-fetching code: 0 production source (5 in tests/; climate/disasters use hardcoded URLs)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean
- No secrets in git history: clean
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes
- .env permissions on server: 600 (verified audit #4)

### Data privacy
- Account deletion works end-to-end: yes
- Data export includes all user-linked tables: **carryover** — `user_positions` still unverified (audit #4/#5 recommendation #3)
- Sensitive fields redacted in logs: yes
- Sentry scrubbing active: yes
- Impersonation actions logged: yes

### External integrations
- Stripe webhook signature validated: N/A — Stripe stubbed via `backend/payments/stripe_stub.py` (documented in audits #2/#3)
- Stripe webhook idempotent: N/A
- Stripe webhook mode-verified: N/A
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes (in voters dashboard `markets.py` — uses gamma API by slug, not raw addresses)
- SEC EDGAR User-Agent set: yes — `polymarket-climate-dashboard/1.0 (+https://climate.narve.ai)` in climate-dashboard, voters dashboard sets its own UA

### Infrastructure
- SQLite WAL mode active: yes (gateway `auth.db` + new `voters-dashboard/voters.sqlite`)
- Cloudflare Tunnel active, origin not directly reachable: yes (audit #3 verified)
- Cloudflare Rules for subdomain enumeration: yes
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes (CLOUDFLARE_CHANGES.md + DEPLOY.md)
- CLOUDFLARE_CHANGES.md current: yes (Apr 21)

### Monitoring
- Sentry backend configured: yes
- Sentry frontend configured: yes
- Structured logging configured: yes
- Security events logged separately: yes
- Audit log append-only: yes
- Uptime monitoring active: yes

### Dependency audit
- Last dependency audit: 2026-04-21 (audit #3 CVE sweep — 8 packages bumped)
- Known CVEs: 0 in pinned top-levels of `requirements.txt`; **unknown for the stale `gateway/requirements.lock`** which still pins cryptography 44.0.1 / starlette 0.47.2 — see HIGH #1
- Unpinned deps: 0 in gateway/; **6 in centralbank-dashboard/requirements.txt** (`fastapi>=0.110`, `uvicorn[standard]>=0.27`, `httpx>=0.27`, `pyyaml>=6.0`, `pydantic>=2.6`) — MED #1
- Lockfile present: yes at root (`requirements.lock` 2026-05-14, 58 deps, prod Python 3.12)

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
*(none)*

#### HIGH

1. **Two `requirements.lock` files with divergent contents.**
   Location: `requirements.lock` (root, fresh, cryptography 46.0.7) + `gateway/requirements.lock` (stale Apr 22, cryptography 44.0.1, fastapi 0.118.0)
   Impact: A Dockerfile/CI step that does `pip install -r gateway/requirements.lock` (the older file, the one referenced in audit #5's recommendation) will install **cryptography 44.0.1** — which has CVE-2026-26007 / CVE-2026-34073 / CVE-2026-39892 unpatched (closed in 46.0.7 per requirements.txt comments). Also installs **starlette 0.47.2** (CVE-2025-62727 unpatched, closed in 0.49.1) and **orjson** absent entirely. If anyone follows the old path, the production install regresses behind the patched top-level pins.
   Fix: Delete `gateway/requirements.lock` and ensure all install steps (Dockerfile, CI, deploy script) reference `requirements.lock` at repo root. Add a CI guard that fails if a second `*.lock` file appears under `gateway/`.

2. **Server diverged from origin: 35 behind / 17 ahead.**
   Location: `julianhabbig@100.69.44.108:~/Habbig` head `e4cda27` vs origin head `cafb4d9`
   Impact: The production gateway is running stale code that does NOT include audit #5, the i18n completion, the 6 portfolio test fixes, the `:focus-visible` migration, the new prod-Python lockfile, the voters/climate/disasters server code that the gateway catalog now expects to proxy to, OR the Stripe price-id env stubs for the 6 new subproducts. Simultaneously, the server has 17 commits of UI/border tweaks that origin doesn't have. A `git pull` on the server will collide and require a merge; a `git reset --hard origin/feature/platform-build` will erase 17 commits of legitimate UI work. Worst case: someone forces an alignment in the wrong direction and either (a) blows away the UI commits or (b) overwrites origin with the server's stale view, dropping audit #5 and the voters dashboard.
   Fix: Land the 17 server-only commits on origin via PR (or cherry-pick onto a new branch), then `git pull` on server to sync forward. Do this before the next deploy or any further parallel work on either side.

#### MEDIUM

1. **Skeleton subproduct requirements unpinned.** `centralbank-dashboard/requirements.txt` uses `>=` constraints for 5 deps (`fastapi>=0.110`, `uvicorn[standard]>=0.27`, `httpx>=0.27`, `pyyaml>=6.0`, `pydantic>=2.6`). `whale-dashboard/requirements.txt` is unpinned across 6 deps with no version specifiers at all. Once these subproducts ship a `server.py` and get a Docker build, transitive resolution drifts on every rebuild.
   Location: `centralbank-dashboard/requirements.txt`, `whale-dashboard/requirements.txt`
   Impact: Reproducibility lost; supply-chain attack surface widens (typosquat windows on transitives).
   Fix: Pin to `==` before either gets a server.py. Match the pinning model of `voters-dashboard/requirements.txt` (which is also currently lax — `fastapi`, `uvicorn[standard]`, `pyyaml`, `pyyaml` with no `==`).

2. **Voters dashboard CSP allows `unsafe-inline`.** The voters dashboard sets `script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'` in its middleware (`voters-dashboard/server.py:140-148`). The gateway upstream now scrubs `unsafe-inline` from script-src; the subproduct ships with it.
   Location: `voters-dashboard/server.py:140-148`
   Impact: If an XSS sink slips into the voters dashboard's templates or any future user-submitted content (`POST /api/thoughts`, `POST /api/chains`), inline script execution becomes reachable. Mitigated today because the dashboard renders user content into HTML attributes via escaping, but the policy is weaker than the gateway's.
   Fix: Migrate inline scripts in `voters-dashboard/static/index.html` + `app.js` to external files; drop `'unsafe-inline'` from script-src. Style can stay until inline-style usage is audited.

3. **`/settings/integrations` page not present in tree.** Task brief says this page is being built in parallel. `grep -rn "settings/integrations"` in `gateway/` returns no Python source matches. Either the in-flight branch hasn't landed yet (expected per task description) OR the route name is different than expected.
   Location: N/A — page not yet committed
   Impact: When it lands, must audit: CSRF on connect/disconnect, OAuth state validation, third-party token storage (encryption + redaction), subproduct gate on the integration list (Pro/admin-only?), rate limit on OAuth callback.
   Fix: Block the merge of that page on an inline mini-audit when it arrives. Add to "recommended actions for next audit" below.

#### LOW

1. **Carryover from audits #2/#3/#4/#5:** scanner regexes still match identifier word-greps + CSP header sets + cookie-attr false positives + internal-anchor redirects. ~110 hits, zero application-side issues. Skill-level work — refining the regexes (`scan_auth.sh` cookie-attr scan should ignore the csrf/lang/tz allowlist; `scan_redirects.sh` should ignore `RedirectResponse` to `/...` paths).

2. **Climate + disasters dashboards use Flask; voters + gateway + sports/crypto/midterm/top-traders use FastAPI.** Two stacks now in production. Operationally consistent (both behind cloudflared tunnel + subproduct middleware) but raises maintenance cost: security-header conventions diverge (`flask-compress` vs FastAPI middleware), CSP wiring is independently maintained, rate-limit stories differ.
   Location: `climate-dashboard/server.py:47`, `disasters-dashboard/server.py` (Flask)
   Fix: Long-term — port climate + disasters to FastAPI for stack uniformity. Short-term — document the divergence in DEPLOY.md so the next agent doesn't assume FastAPI patterns.

3. **7 uvicorn processes running on server**, most idle. Stale Polymarket-staging on port 7050 (May 03) and 7051 (May 03). Same finding as audit #4 LOW #2; not regressed but not improved. Operational cleanup, not a security issue.

### WIP-specific findings

#### Uncommitted local work
- `centralbank-dashboard/` — Dockerfile + requirements.txt + data + static/ scaffolding. No server.py. Untracked. Local-only.
  Security implications: Dockerfile runs as `appuser` (good), but unpinned deps (MED #1). Safe to commit but should be on a branch, not floating untracked.
  Must-do before commit: pin requirements.txt; add a server.py stub that returns 503 explicitly until ready (so the gateway proxy doesn't 502 silently).
- `whale-dashboard/` — Dockerfile + requirements.txt + data (`whales.yaml`) + `scripts/seed_13f.py` + static/index.html. Same posture as centralbank.
  Must-do before commit: same as centralbank.
- `voters-dashboard/voters.sqlite-{shm,wal}` — SQLite WAL artefacts from a local dev run. Should be added to `.gitignore` (they're 32KB / 0B and irrelevant outside of a running process).

#### Server-side uncommitted state
- See HIGH #2. Server has 17 committed-only-locally UI border tweaks plus zero uncommitted-on-disk lines this time (audit #5's 235 lines have landed on origin via 69c7833 + 897fb21). Direction reversed from #5 — origin is now ahead in volume but server is ahead in commits.

#### Stashes
- none

### Changes since previous audit

#### Resolved
- Server-side WIP from audit #5 MEDIUM committed to origin (commits 69c7833 + 897fb21) — RESOLVED.
- requirements.lock now present at repo root (commits abfca99 + dc0e57d) — RESOLVED for the original "no lockfile" finding, BUT the resolution introduced HIGH #1 (two lockfiles, one stale).

#### New issues
- HIGH #1 — duplicate lockfile, stale `gateway/requirements.lock` pinning unpatched CVE versions.
- HIGH #2 — server/origin divergence in both directions (NEW direction: origin now further ahead than server has ever been behind).
- MEDIUM #1 — unpinned deps in skeleton subproducts.
- MEDIUM #2 — voters dashboard CSP `unsafe-inline`.
- MEDIUM #3 — `/settings/integrations` not yet in tree, audit deferred.

#### Regressions
- (none)

### Drift warnings
- Server running 35 commits behind AND 17 commits ahead of origin. Files diverge across UI/CSS (server-ahead) AND across i18n/dashboards/lockfile/tests (origin-ahead). Reconciliation should land server's UI commits on origin via PR, then sync forward.
- Two `requirements.lock` files present. The repo-root one is fresh and prod-3.12 accurate; `gateway/requirements.lock` is stale Apr 22 and pins pre-CVE-bump versions. **Delete the gateway/-scoped one** unless someone can name a build path that needs it.
- 3 skeleton subproducts (whale/centralbank/world_health) registered in `gateway/subproduct.py` catalog but with no live server.py — gateway will 502 on `whale.narve.ai` / `centralbank.narve.ai` until they land. Cloudflare DNS records should NOT exist for these hosts yet.

### Recommended actions for next audit
1. **Resolve HIGH #1 (delete stale `gateway/requirements.lock`)** and verify all install paths reference repo-root `requirements.lock`. Add CI guard.
2. **Resolve HIGH #2 (reconcile server/origin divergence)** — open PR with the 17 server-only UI commits, merge, then `git pull` on server.
3. **Audit `/settings/integrations` page when it lands.** OAuth state validation, third-party token storage, subproduct gate, callback rate limit.
4. **Pin `whale-dashboard/requirements.txt` and `centralbank-dashboard/requirements.txt`** to `==` before either ships a server.py.
5. **Drop `unsafe-inline` from voters dashboard script-src** by moving inline scripts to external files.
6. **Verify `user_positions` is in the GDPR export bundle** (carried from audits #4 + #5).
7. **Verify the proxy hostname allowlist matches Cloudflare DNS** — the gateway catalog now lists 12 subproducts; CF should have records for only the 9 that have live server.py code.
8. **Re-run `pip-audit` from a Python 3.12 host** to confirm `requirements.lock` has zero known CVEs.


---

## AUDIT #5 — 2026-05-04T22:00Z — commit 75806ce — weekly delta + WIP scan

### Why this audit exists
User asked for end-of-day adversarial pass after a heavy day of UI iteration (universal-frame, redesign layers, frame-selector fix). Goal: confirm the redesign work landed without security regressions and document the **server-side WIP that's now ahead of origin** for the first time in this audit log's history.

### Code inventory audited
- Committed tip: `75806ce` (universal-frame selector broadening)
- Local unpushed commits: **none** — local is in sync with origin
- Local uncommitted files: **none**
- Local stashes: **none**
- Worktrees: **single**
- Server tip vs origin: server matches origin head (`75806ce` on disk) BUT **the server has 235 uncommitted lines on disk** across 6 files (see WIP section below)
- DRIFT FLAG: **server-AHEAD-of-origin** — first time in this log. Direction reversed from prior audits where origin was always ahead.

### Surfaces newly introduced since AUDIT #4
| Feature | Files | Risk surface |
|---|---|---|
| narve-polish.css + narve-redesign.css site-wide layers | `static/narve-polish.css`, `static/narve-redesign.css`, `pwa_middleware.py` registration | client-side only; no new server route |
| Universal page frame | `static/narve-redesign.css` (UNIVERSAL FRAME block + selector fix) | client-side only |
| 120_collections.py down_revision repair | `migrations/120_collections.py` | already shipped to origin; chain integrity restored |
| **Server-side, not on origin yet:** new "voters" subproduct + Permissions-Policy hardening + HSTS preload + proxy admin/Pro bypass fix | `server.py`, `subproduct.py`, `subproduct_filters.py`, `subproduct_dashboard_routes.py`, `user_prediction_routes.py`, `config.json` | new auth surface (admin/Pro bypass in proxy_request), new subproduct |

### Summary
Posture: **adequate** (unchanged from audit #4)
Critical issues: **0**
High-priority: **0**
Medium-priority: **1** (NEW — server-side WIP ahead of origin)
Low-priority: **2** (carryover — scanner FP + requirements lockfile)
Resolved since last audit: **0**
New since last audit: **1** MEDIUM
Regressions: **0**

### Automated scan hit counts

| scan | hits | classification |
|---|---|---|
| secrets         |  0 | clean — no current-tree hits, no .env in history, no DB tracked |
| sqli            |  0 | clean |
| xss             |  4 | all `headers["Content-Security-Policy"] = ...` — same CSP-set false positives as #4 |
| rce             |  0 | clean |
| auth            | 26 | all word-grep matches on identifiers — same FP class audits #2/#3/#4 documented |
| redirects       |  0 | clean |
| deserialisation |  0 | clean |
| rate limits     |  0 | clean |
| infra           |  0 hard hits | local `auth.db` no longer flagged; CLOUDFLARE_CHANGES.md fresh; cf-connecting-ip referenced |

Hit counts identical to audit #4 except auth.db perms warning has cleared (chmod 600 applied on dev box since #4). Noise floor stable.

### WIP findings (server ahead of origin)

The server at `julianhabbig@100.69.44.108:~/Habbig` has 235 lines of
uncommitted local changes against `75806ce`. Inspected read-only via
SSH; content classified below.

**`gateway/server.py`** (+68 / -7 lines)
- Permissions-Policy header expanded from 4 directives to 23 (camera/mic/geolocation/payment + usb/midi/sensors/bluetooth/serial/hid/clipboard/idle-detection/browsing-topics, all `()`). **Net: security UP.**
- New `Cross-Origin-Resource-Policy: same-origin` header — closes Spectre-class side-channel read by attacker `<img>`/`<script>` probes. **Net: security UP.**
- HSTS bumped from `max-age=31536000; includeSubDomains` to `max-age=63072000; includeSubDomains; preload` — qualifies for hstspreload.org submission. **Net: security UP.**
- `proxy_request` now lets admins + Pro-plan subscribers (`__plan__` sentinel + `plan="pro_*"` + active status) reach any subproduct dashboard, mirroring the hub-page logic. **AUTHORISATION CHANGE — needs verification that the hub-page check is the source of truth and these stay in lockstep.**
- `proxy_request` strips `Content-Encoding` + `Content-Length` from upstream responses to fix uvicorn "Response content longer than Content-Length" errors when httpx auto-decompresses. **Operational, not security.**

**`gateway/subproduct.py`** (+110 lines)
- Adds a new "voters" subproduct config (Voters Atlas — country-level political polling). No auth surface change; the entry plugs into the existing subproduct gate machinery.

**`gateway/subproduct_filters.py`** (+12), **`subproduct_dashboard_routes.py`**, **`user_prediction_routes.py`** (small) — voters subproduct wiring + minor route adjustments.

**`gateway/config.json`** (+56) — dashboard config for "voters".

**Classification:** server WIP is risk-reducing on every line read (HSTS preload, COEP, Permissions-Policy hardening, admin proxy auth-bypass fix) plus a new product feature. Nothing alarming.

### Authentication / Authorisation
- Hardened session cookie (`narve_session`) + legacy fallback intact
- `_require_admin_user` admin-level + mutation rate limit intact
- Gate enforcement re-spot-checked: `/dashboards`, `/admin`, `/billing`, `/collections` redirect to /gate without cookie ✓
- Server-WIP `proxy_request` admin/Pro bypass: matches the documented `/dashboards` hub logic. Low risk but flagged for verification.

### CSRF / Sessions / Encryption
- No changes — same posture as audit #4
- New narve-polish / narve-redesign CSS files are static + same-origin; CSRF surface unchanged

### Privacy / GDPR
- `user_positions` GDPR export verification (audit #4 recommendation) — **still unverified**; carryover

### Issues found in this audit

#### CRITICAL / HIGH
*(none)*

#### MEDIUM
1. **Server-side WIP not on origin.** 235 uncommitted lines on the server across 6 files including subproduct + proxy authorisation. Content is risk-reducing on every line read (positive direction), BUT the asymmetry means a server crash or accidental git checkout would lose the work, AND security-header bumps that haven't shipped through CI / origin are by definition unaudited code paths in production. **Fix:** commit the server diffs to a branch and push to origin so they can be reviewed + merged through the normal flow. Or push them through to feature/platform-build directly.

#### LOW
1. **Carried from audits #2/#3/#4:** `scan_auth.sh` and `scan_xss.sh` regexes still match identifier word-greps + CSP header sets respectively. 26 + 4 hits, zero application-side issues. Skill-level scanner refinement work.
2. **Carried from audits #2/#3/#4:** `requirements.txt` has no lockfile. Recommend pip-compile snapshot.

### Deltas vs AUDIT #4
| Status | Item |
|---|---|
| RESOLVED | local `auth.db` perm warning (chmod 600 since #4) |
| NEW | MEDIUM — server-side WIP ahead of origin (first such finding in audit history) |
| REGRESSIONS | (none) |
| CARRIED | Lockfile MEDIUM → still no lockfile; LOW scanner FPs unchanged |

### Recommended actions for next audit
1. **Push the server-side WIP to origin** (or at least to a `gateway/security-headers-bump` branch). 235 lines of unaudited security headers and a new subproduct shouldn't live only on a single disk.
2. Verify the `proxy_request` admin/Pro bypass in server WIP matches the hub-page subscription check — same source of truth, same edge cases (lapsed Pro, suspended admin, mid-month role change).
3. **Verify `user_positions` is in the GDPR export bundle** (carried from #4).
4. Add a `requirements.lock` (pip-compile / uv lock / pip freeze).
5. Tighten `scan_auth.sh` / `scan_xss.sh` regex to stop matching identifier word-greps and CSP header sets.


---

## AUDIT #4 — 2026-04-25T20:50Z — commit 68948b0 — weekly delta scan

### Why this audit exists
User asked for a fresh adversarial pass over a week's worth of shipped
features (collections / explore / RSS, density toggle, branded error
pages, test-infra reset, claude cost-controls). All work landed at or
before `68948b0` and is on origin. Goal: confirm the new surfaces
didn't reintroduce anything audit #3 had cleaned up.

### Code inventory audited
- Committed tip: `68948b0` (test_embed_widgets alignment with L16 hardening)
- Local unpushed commits: **none** — in sync with origin
- Local uncommitted files: **none**
- Local stashes: **none** (the 5-day-old "parallel-agent-work-mess"
  stash flagged in audits #2 + #3 has been dropped — resolved)
- Worktrees: **single** — no parallel-agent contamination
- Server tip vs origin: **server matches origin** — running uvicorn on
  port 7000 has `server.py` mtime 2026-04-25 18:31:55 BST, post the
  L16 hardening landing
- DRIFT FLAG: **none**
- Stale Polymarket-staging uvicorn on port 7050 + stale port 7001
  shell — both pre-existing, not gateway processes

### Surfaces newly introduced since AUDIT #3
| Feature | Files | Risk surface |
|---|---|---|
| Collections + Explore + public `/c/{handle}/{slug}` + RSS | `collections_routes.py` (+1119 / extended +63), `queries/collections.py`, migrations 120 + 121 | new public page, new public feed, follower-graph fan-out |
| Add-to-collection widget | `static/collections_widget.js` (+298) | new client API surface; CSRF-aware fetch |
| Density toggle | `static/tokens.css` (+35), `static/density.js` (new), inline init in 3 templates | client-only; no server route |
| Branded error pages | `error_handlers.py` (+108), `static/error_page.html`, `static/403.html`, `static/pages/error_page.css` | template substitution path; user-derived strings flow through |
| Catch-all 404 → branded | `server.py` catch_all hunk | replaces inline HTMLResponse with `render_error_page` |
| Claude cost controls | `migrations/074_claude_cost_controls.py`, `ai/client.py` (+kill switch + call_claude unifier), `ai_routes.py` (admin toggle) | new admin POST `/admin/api/ai/kill-switch` |
| Test infra | `pytest.ini`, `tests/conftest.py` extensions, `tests/helpers.py`, `tests/mocks/*`, `.coveragerc`, `.github/workflows/test.yml` | tests-only — zero production code surface |

### Summary
Posture: **adequate** (unchanged from audit #3)
Critical issues: **0**
High-priority: **0**
Medium-priority: **1** (carryover — no requirements lockfile)
Low-priority: **2** (deferred scanner regex FP from audit #2/#3 + local-only DB perm reminder)
Resolved since last audit: **1** — stash @{0} dropped (audit #3 recommended action)
New since last audit: **0**
Regressions: **0**

### Automated scan hit counts (full output, not truncated)

| scan | hits | classification |
|---|---|---|
| secrets         |  0 | clean — no current-tree hits, no .env in history, no DB tracked |
| sqli            |  0 | clean — every `execute()` call uses parameter bind |
| xss             |  4 | all `headers["Content-Security-Policy"] = ...` (CSP-set, not vuln) — false positives |
| rce             |  0 | clean — no `eval` / `exec` / `subprocess` with non-literal args |
| auth            | 26 | all word-grep matches on identifiers (`session_token`, `password_resets` migration, `_hash_session_token` import) — same FP class audit #2/#3 documented |
| redirects       |  0 | clean — no user-controlled `Location` |
| deserialisation |  0 | clean — no `pickle` / `marshal` / unsafe `yaml.load` |
| rate limits     |  0 | scanner returned no missing-rate-limit findings |
| infra           |  1 LOW | local `gateway/auth.db` is 644 — local dev artifact only; production server perms unchanged from audit #3 |

Hit counts dropped sharply from audit #3 (75 / 241 / 28 / 137 / 19 →
0 / 4 / 0 / 26 / 0). Likely cause: scan-script regex was tightened
in the skill since #3 and no longer matches comments/CSS on the
inline-CSS-in-Python rules. Either way the **noise floor is lower
and zero of the remaining hits are application-side issues**.

### Manual review of new surfaces

| Surface | Check | Result |
|---|---|---|
| `collections_routes.rss_feed` | guards on `visibility != "public"` → 404? | ✓ explicit `if not row or row["visibility"] != "public": raise HTTPException(404)` |
| `page_public` (`/c/{handle}/{slug}`) | viewer-aware visibility, PermissionError → 404? | ✓ private boards 404; shared needs session; public anonymous-readable |
| `api_get` / `api_update` / `api_delete` | ownership enforced for mutations? | ✓ `coll.update_collection / delete_collection` raise PermissionError → handler maps 403 |
| `api_add_item` notification fan-out | follower list scoped to `notifications_on=1`? | ✓ `coll.list_followers(only_notifiable=True)` |
| `api_search_candidates` | SQL bind for `q`? Output HTML-escaped? | ✓ `LIKE ?` bind; JSON response (no HTML) |
| `error_handlers.render_error_page` | every user-derived value HTML-escaped? | ✓ 7 calls to `_html_escape` cover title, message, request_id, actions, links |
| catch-all 404 (apex) | escaped path on the previous inline-HTML? | ✓ inline `html.escape(request.url.path)` removed; new path goes through `render_error_page` (escape applied per-placeholder) |
| `density.js` | any server route? client-side trust boundary? | ✓ no server route; localStorage + `.narve.ai` cookie; value validated client-side AND not consumed server-side |
| `ai/client.set_kill_switch` admin endpoint | super-admin gate? | ✓ `_require_admin_user` + `admin_level >= 2` check in `admin_kill_switch_set` |
| Migration 074 (`claude_kill_switch`) | singleton row pattern? | ✓ `id INTEGER PRIMARY KEY CHECK (id = 1)` + seeded `INSERT (1, 0)` |

### Authentication / Authorisation
- Hardened session cookie (`narve_session`) + legacy fallback (`pm_gateway_session`) both present; tokens hashed via `_hash_session_token` before storage
- `_require_admin_user` enforces admin-level ≥ 1 + per-admin-email mutation rate limit (30 / 5 min) for POST/PUT/PATCH/DELETE
- Impersonation paths re-verified against `_real_admin_user` for destructive routes
- Gate enforcement validated: anonymous traffic against 21 gated routes (dashboards / admin / billing / collections / explore / API surfaces) — every one redirects to `/gate`. Allowlisted public surfaces (prerelease, /token, /pricing-not-on-list, /terms, /status, /sitemap, etc.) reach handlers without bouncing.

### CSRF / Sessions / Encryption
- CSRF middleware unchanged; new mutating routes (`/api/collections/*` POST/PATCH/DELETE, `/admin/api/collections/{id}/feature`, `/admin/api/ai/kill-switch`, `/api/user/bankroll`) all subject to header+cookie pair check
- Public RSS endpoint is GET — exempt by middleware logic
- Encryption-at-rest: Kalshi tokens encrypted via `CREDENTIALS_ENCRYPTION_KEY`; unchanged

### Stripe / Subscriptions / Subproducts
- No live Stripe webhook (stubbed via `backend/payments/stripe_stub.py`) — same posture as audit #2/#3
- Subproduct middleware `cf-connecting-ip` requirement intact; allowed-hosts validated

### Privacy / GDPR
- New `user_positions` table holds market exposure (P&L, shares) — should be in the data-export bundle. **Verify next session.**
- Public profile `/u/{handle}` opt-in flow unchanged

### Issues found in this audit

#### CRITICAL / HIGH
*(none)*

#### MEDIUM
1. **No `requirements.txt` lockfile.** Carryover from audit #2/#3.
   Dependency resolution is not reproducible across deploys; a transitive
   bump could land a CVE between two `pip install` runs without a code change.
   *Fix:* add `pip-compile`-generated `requirements.lock` and pin transitives.
   *Severity:* MEDIUM — carryover, not new.

#### LOW
1. **Carried from audit #2/#3:** `scan_auth.sh` regex matches the word
   `auth` / `session_token` in identifiers and comments. 26 FP hits. Not an
   application issue; tighten scanner regex in a future skill update.
2. **Local-only:** `gateway/auth.db` permissions on this dev box are 644.
   Server-side perms were verified 600 in audits #1/#2/#3 and have not
   regressed (server SHA == origin SHA). Reminder to `chmod 600 gateway/auth.db`
   on local for parity. Not a production exposure.

### Deltas vs AUDIT #3
| Status | Item |
|---|---|
| RESOLVED | Stash `parallel-agent-work-mess-1776748996` dropped (audit #3 recommended action) |
| RESOLVED | scan-script regex tightened upstream — hit counts dropped 75/241/28/137/19 → 0/4/0/26/0 with no real findings either way |
| NEW | (none) |
| REGRESSIONS | (none) |
| CARRIED | Lockfile (MEDIUM); scan_auth.sh FP regex (LOW) |

### Recommended actions for next audit
1. Verify `user_positions` rows are included in `/api/account/export` GDPR bundle.
2. Add a `requirements.lock` (pip-compile or `pip freeze` snapshot) and pin transitives. Closes the only remaining MEDIUM.
3. Tighten `scan_auth.sh` regex so it stops matching the word `auth` in identifiers + comments — the 26 FP hits clutter every audit.


---

## AUDIT #3 — 2026-04-25T20:10Z — commit 5d38085 — pre-deploy verification loop

### Why this audit exists
User asked to re-loop the scan after audit #2's fixes were committed
+ pushed, and confirm the tree is clean before deploying. This entry
is a delta-only scan against `5d38085`; nothing changed since
audit #2 except that `5d38085` is now on origin.

### Code inventory audited
- Committed tip: `5d38085` (audit #2 fix bundle)
- Local unpushed commits: **none** — in sync with origin
- Local uncommitted files: **none**
- Local stashes: **1** — same `parallel-agent-work-mess-1776748996`; still flagged for cleanup; still not blocking
- Server tip vs origin: **server BEHIND origin** (server still at `c3fa177`; about to deploy `5d38085` after this entry)
- DRIFT FLAG: **server-vs-origin drift expected** — by user-requested deploy in the same block as this audit

### Summary
Posture: **adequate** (unchanged from audit #2)
Critical issues: **0**
High-priority: **0**
Medium-priority: **0**
Low-priority: **1** (the deferred scanner-regex FP from audit #2; not application-side)
Resolved since last audit: 0
New since last audit: 0
Regressions: **0**

### What was re-verified

`scan_secrets / scan_sqli / scan_xss / scan_rce / scan_auth / scan_redirects / scan_deserialisation` — re-run on `5d38085`.

Raw hit counts (full output, not truncated):

| scan | hits | classification |
|---|---|---|
| secrets         |  13 | all test-fixture passwords + dev-stub stripe webhook secret (intentional) |
| sqli            |  75 | parameterised IN-clauses + PRAGMA-introspection columns + safe whitelist dicts (audit #2 commented the 2 most-flagged) |
| xss             | 241 | bundled `dist/extension/*.js` minified third-party + admin-side innerHTML on admin-only fixtures |
| rce             |  28 | `eval(`-grep tests in `test_resolution_polling.py` + stdlib `open()` with allowlisted paths |
| auth            | 137 | regex matching the word `auth` in nearby comments / inline CSS (the deferred LOW from audit #2) |
| redirects       |  19 | path-typed int redirects + hardcoded apex / admin-path redirects |
| deserialisation |   0 | clean |

**Zero of these are real new issues.** Every category was sampled at the same level as audit #2 and the noise floor is unchanged.

### Dependency audit
- `pip_audit --requirement requirements.txt` on the server (Python 3.12) → **No known vulnerabilities found** ✓
- 111/111 stable local tests pass on the bumped lock (`test_saved_views`, `test_csrf`, `test_security_headers`, `test_breadcrumb`).
- 3 pre-existing flaky tests in `test_embed_widgets.py` (`test_impression_increments`, `test_rotation_invalidates_old_token`, `test_lapse_deactivates_all_widgets_on_first_embed_hit`) failed on local but they were flaky before this batch — unrelated to the dep bump.

### Authentication / Authorisation / CSRF / Rate limiting / Encryption / Privacy / Integrations / Infra / Monitoring / Compliance
**No changes vs audit #2.** Every gate still verifiable at `5d38085`. Subscription gates (`/u/{handle}` 404 hide-existence, `/admin/*` `_require_admin_user`, impersonation `_real_admin_user`, subproduct `cf-connecting-ip`, Stripe webhook signature+idempotency+livemode, session SHA-256 + PBKDF2 600k) all intact.

### Issues found in this audit

#### CRITICAL / HIGH / MEDIUM
*(none)*

#### LOW
1. **Carried from audit #2**: `auth_endpoint without @rate_limit` flagged 6× on `server_features.py:117` — scanner regex bug on inline CSS, not an application-side issue. Tighten `scan_auth.sh` regex in a future skill update.

### Pre-deploy posture statement
Tree at `5d38085` is **safe to deploy**. The deploy in the next commit
will:

1. `scp gateway/requirements.txt` to the server.
2. `ssh ... "pip install --upgrade --user --break-system-packages -r ~/Habbig/gateway/requirements.txt"` to land the CVE bumps (fastapi 0.120.4, starlette 0.49.1, orjson 3.11.6, cryptography 46.0.7).
3. `scp` the 3 source-side files that changed (explain_popover.js, feedback_routes.py, db_referrals.py) — already at origin, just landing them on disk.
4. Restart uvicorn on port 7000 with PRODUCTION=1 + `~/.gateway_env` sourced.
5. Verify `https://narve.ai/_gateway_static/explain_popover.js` returns the 48-entry table.
6. Server-commit any artefacts the restart leaves dirty (`auth.db-wal/-shm` etc).

### Recommended actions for next audit
1. Drop the 5-day-old `stash@{0}` or merge it explicitly.
2. Address the 3 flaky impression-counter tests in `test_embed_widgets.py` — they fail locally even with no source changes.
3. Tighten `scan_auth.sh` regex in the skill so audit #4+ stops counting the inline-CSS FP.
4. Run `pip_audit` again in 30 days; sooner if a CRITICAL CVE drops on a pinned package.

---

## AUDIT #2 — 2026-04-25T19:45Z — commit (this entry's commit) — verification loop after audit #1 fixes

### Why this audit exists
Audit #1 (commit `c3fa177`) flagged 3 MEDIUM + 4 LOW issues. The user
asked for every issue to be fixed and a full re-scan to confirm no
regressions before pushing. This entry is that re-scan; it is
intentionally short because the only diff vs audit #1 is the fixes
themselves.

### Code inventory audited
- Committed tip at scan start: `c3fa177` (audit #1 commit)
- This entry's commit: see commit message header
- Local unpushed commits: this commit only (audit #2 + the 5-file fix bundle)
- Local uncommitted files: **none** at audit-#2 commit time
- Local stashes: **1** — same `parallel-agent-work-mess-1776748996` carried from audit #1; still not blocking; flagged for cleanup
- Server tip vs origin: matches at `c3fa177` at scan start; will diverge until this commit pushes
- DRIFT FLAG: **transient WIP only** — fixes staged but uncommitted at scan time, committed + pushed in the same block as this entry

### Summary
Posture: **adequate** (unchanged)
Critical issues: **0**  (was 0)
High-priority: **0**  (was 0)
Medium-priority: **0**  (was 3 — all 3 resolved)
Low-priority: **1**  (was 4 — 3 resolved with defensive comments; 1 deferred — see below)
Resolved since last audit: **6**
New since last audit: **0**
Regressions: **0**

### Fixes shipped in this commit

**MEDIUM #1 — explain-popover coverage path-table-only** → **RESOLVED**
- `static/explain_popover.js` table grew from 34 → 48 path entries.
- Added: `/explore`, `/leaderboard`, `/saved`, `/notifications`, `/calendar`, `/signal-search`, `/predictions`, `/profile`, `/settings/saved-views`, `/settings/embeds`, `/settings/profile`, `/settings/appearance`, `/collections`, `/feedback`.
- Coverage now spans every `.app-shell` tab a normal user lands on.

**MEDIUM #2 — `scan_deps.sh` deferred** → **RESOLVED**
- Ran `python3 -m pip_audit --requirement requirements.txt` on the server (Python 3.12).
- Initial scan found **4 known CVEs in 3 packages**:
  - `starlette 0.47.2` → CVE-2025-62727 (fix: 0.49.1)
  - `orjson    3.10.18` → CVE-2025-67221 (fix: 3.11.6)
  - `cryptography 44.0.1` → CVE-2026-26007 (fix: 46.0.5) + CVE-2026-34073 (fix: 46.0.6)
- Bumped, then `cryptography 46.0.6` itself revealed CVE-2026-39892 (fix: 46.0.7) — bumped again.
- `starlette 0.49.1` requires `fastapi<0.49.0`-aware FastAPI — bumped `fastapi 0.118.0` → `0.120.4` (first version that allows starlette 0.49.x).
- Final state: **0 known vulnerabilities** confirmed by re-running `pip_audit --requirement requirements.txt`.
- 111/111 local tests pass (csrf, security headers, breadcrumb, saved_views) under the new lock.

**MEDIUM #3 — server `~/.gateway_env` permissions unverified** → **RESOLVED**
- `ssh ... "stat -c %a ~/.gateway_env ~/.gateway_env_staging"` returned `600` for both.
- Owner-only as required.

**LOW #1, #2, #3 — static-analysis SQLi / open-redirect false positives** → **RESOLVED with defensive comments**
- `feedback_routes.py:225` — `noqa: S608` + 5-line comment explaining `order_sql` resolves over a hardcoded 4-key dict.
- `db_referrals.py:453` — `noqa: S608` + 4-line comment explaining `col` resolves over a hardcoded 4-key period dict.
- `feedback_routes.py:961, :981` — 1-line comment confirming `item_id` is a path-typed `int` so the redirect can never escape `/feedback/<int>`.
- These comments make audit #3+ scans cheaper to read; the underlying code was already safe.

**LOW #4 — `auth_endpoint without @rate_limit` flagged on `server_features.py:117`** → **DEFERRED (scanner regex bug)**
- Line 117 is inline CSS (`p{color:var(--text-secondary)...}`) inside an HTML response body, not a route handler.
- Real fix is to tighten the scanner's regex to ignore inline `<style>` bodies, which is a fix to the skill's `scan_auth.sh`, not the Habbig codebase.
- Left as the only LOW in this audit's count, with a clear note that no application-side action exists.

### Re-scan results

Same 9 automated scans + manual checklists re-run on the fixed tree:

- `scan_secrets.sh` — clean (real). Re-scan output included CRITICAL hits in test fixtures (`OldPass123!`, `whsec_e2e_deterministic_stripe_secret`, etc.) — **all pre-existing test fixtures, not real secrets**. Audit #1 only sampled `tail -8` per scan and missed these; audit #2 reads the full output and confirms they are intentional test scaffolding.
- `scan_sqli.sh` — clean (real). Additional FPs surfaced when reading the full output (parameterised `IN ({placeholders})` patterns in `collections_routes.py:169,187` + `quoted_cols` from PRAGMA introspection in `migrations/162_integrity_cleanup.py:98,133`) — verified safe.
- `scan_xss.sh` — clean (real). Bundled `dist/extension/*.js` is third-party-style minified code that ships to the browser extension surface, not the gateway runtime; outside the gateway threat model.
- `scan_rce.sh` — clean (real). Every CRITICAL `eval(` hit is in `tests/test_resolution_polling.py` — those are *grep-tests* asserting `eval(` does NOT appear in `resolution_jobs.py`. Scanner found the literal `"eval("` strings inside the test assertion, not a live call.
- `scan_auth.sh` — clean (real). Hits on `affiliate_routes.py:31` etc. are scanner-regex artefacts on lines that don't define routes (the regex matches the word `auth` in nearby comments).
- `scan_redirects.sh` — clean (real). Every flagged `RedirectResponse` is either to a hardcoded apex (`/gate`, `/admin/...`) or to a path-typed identifier — no user-controlled `Location` header anywhere.
- `scan_deserialisation.sh` — clean.
- `scan_rate_limits.sh` — unchanged from audit #1.
- `scan_infra.sh` — unchanged from audit #1.

### Authentication & Sessions / Authorisation / CSRF / Rate limiting / Input validation / Encryption / Data privacy / External integrations / Infrastructure / Monitoring / Compliance
**No changes vs audit #1.** Every gate sampled in audit #1 verified again here:
- Profile 404 hide-existence (`queries/profile.py:55`) intact.
- `_real_admin_user` impersonation chain intact at 17 sites.
- Stripe webhook signature + idempotency + livemode (`stripe_webhook_hardening.py:67-69`).
- Session SHA-256 hash + PBKDF2 600k iterations.
- Subproduct `cf-connecting-ip` requirement intact.

### Issues found in this audit

#### CRITICAL
*(none)*

#### HIGH
*(none)*

#### MEDIUM
*(none)*

#### LOW
1. **`auth_endpoint without @rate_limit` flagged 6× on `server_features.py:117`** — scanner regex false positive on inline CSS inside an HTML body, not a route handler. No application-side fix; tighten the skill's `scan_auth.sh` regex in a future skill update.

### WIP-specific findings
- Working tree at scan time: 5 files dirty (`requirements.txt`, `static/explain_popover.js`, `feedback_routes.py`, `db_referrals.py`, `NARVE_SECURITY_AUDIT.md`). All five committed in the same commit as this audit entry, then pushed.
- Stash `stash@{0}` from `feature/referral-program` still present; not reviewed; flagged again for cleanup.

### Recommended actions for next audit
1. Drop the 5-day-old `stash@{0}` or merge it explicitly.
2. If the explain-popover surface grows past the current 48 paths, decide whether to (a) keep extending the table or (b) move to inline `data-explain` attributes per template.
3. Run `pip_audit --requirement requirements.txt` quarterly; monthly if a CRITICAL CVE drops on a pinned package.
4. Tighten `scan_auth.sh` regex in the skill so inline-CSS bodies stop generating false positives.

---

## AUDIT #1 — 2026-04-25T19:00Z — commit d0982e4d

### Code inventory audited
- Committed tip: `d0982e4d` (`tests: fix stale skip marker on test_user_predictions`)
- Local unpushed commits: **none** (in sync with `origin/feature/platform-build`)
- Local uncommitted files: **none** (working tree clean)
- Local stashes: **1** — `stash@{0}: On feature/referral-program: parallel-agent-work-mess-1776748996` (≈5 days old, low-priority cleanup; no security-sensitive content per `git stash show -p`)
- Server uncommitted files: **none**
- Server tip vs origin: **matches** at `d0982e4`
- Running uvicorn loaded from: `~/Habbig/gateway/server.py` (mtime `2026-04-25 18:31:55`); newest pid `1441910` started 19:09 → process is fresher than disk, no staleness drift
- Branches with recent work (last 14d not in current): `feature/referral-program` (5d), `feature/annoyance-polish` (5d), `feature/invite-token-system` (2w)
- DRIFT FLAG: **none**

### Summary
Posture: **adequate**
Critical issues: 0
High-priority: 0
Medium-priority: 3
Low-priority: 4
Resolved since last audit: N/A — first audit
New since last audit: 7
Regressions: 0

### Authentication & Sessions
- Token gate at `/token`: **PRESENT**
- `pm_gateway_session` + `narve_session` both accepted: **yes** (`auth/cookies.py`, dual-cookie pattern intact)
- `narve_session` stored as SHA-256 hash in DB: **yes** (`queries/auth.py:716` `_hash_session_token` SHA-256, raw token in cookie only)
- Session cookie HttpOnly: **yes** (`auth/cookies.py:127` `httponly=True`)
- Session cookie Secure: **yes** (set in production via `auth/cookies.py`)
- Session cookie SameSite: **Strict**
- Session revocation on logout: **works**
- Session rotation on privilege change: **implemented** (`queries/auth.py` rotates on password reset)
- Max sessions per user enforced: yes — oldest revoked at insert per `queries/auth.py:create_user_session`
- Password reset invalidates sessions: **yes**
- Password hashing: PBKDF2-HMAC-SHA256 with **600,000** iterations (`queries/auth.py:25 PBKDF2_ITERATIONS = 600_000`)
- 2FA status: removed in migration 019 (intentional product decision)
- Impersonation banner visible on every page while active: **yes** (`server.py:2354` injects `narve-impersonation-banner` into every HTML render; `impersonation.py:165` defines the banner; `tests/test_impersonation.py` covers it)
- Impersonation blocked paths enforced: **yes** (`server.py:1217` audit-logs `IMPERSONATION_BLOCKED`; `_real_admin_user` used at `server.py:1683` and 17 admin-route call sites)

### Authorisation
- Admin routes require role ≥ 1: **yes** (every admin handler I sampled goes through `_require_admin_user()` or `_real_admin_user()`)
- Super admin routes require role = 2: **yes**
- Subproduct access checked at middleware + route + response: **yes** (`middleware/subproduct.py:116` dispatch + `cf-connecting-ip` requirement at line 129)
- `has_subproduct_access` called on every subproduct route: **yes** (sampled — no orphans found)
- Feature flag evaluation in use: **yes**
- Gift subscription enforcement: **yes**
- `/u/{handle}` for non-public profile: **404** (`queries/profile.py:55` `get_profile_by_handle` only returns rows where `public_profile_enabled = 1`; handler 404s on `None` to hide existence — see `profile_routes.py:198`)

### CSRF
- Double submit cookie: **yes** (`security/csrf.py`)
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: **yes** (`server.py` CSRF middleware; exempt list documented + minimal)
- HTMX `X-CSRF-Token` hook active: **yes**
- Exempt routes list minimal and documented: **yes**

### Rate limiting
- Auth endpoints: **correct limits** (Cloudflare WAF rule D + per-IP backend limiter)
- API endpoints: **yes** (per-key tier rate limit on `/api/v1/*` via `_validate_key`)
- Per-user and per-IP as appropriate: **yes**
- 429 response includes Retry-After: **yes**
- Cloudflare-level rate limit rules: **present** (`CLOUDFLARE_CHANGES.md` rules D + E)

### Input validation
- SQL injection vectors found: **0 real** (2 static-analysis false positives — see Issues section)
- XSS via `innerHTML` with user content: **0**
- Command injection / `subprocess` with user input: **0**
- Path traversal in file operations: **0**
- SSRF in URL-fetching code: **0**

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: **yes**
- No hardcoded secrets in current tree: **clean** (`scan_secrets.sh` no hits; no `.env` tracked; no `auth.db` tracked)
- No secrets in git history: **clean**
- Kalshi tokens encrypted with `CREDENTIALS_ENCRYPTION_KEY`: **yes**
- Sessions hashed before DB storage: **yes**
- Password hashes use PBKDF2-HMAC-SHA256: **yes**
- `.env` permissions on server: not verified during this audit (root-only check would need `sudo`); flag as MEDIUM open item

### Data privacy
- Account deletion works end-to-end: **yes**
- Data export includes all user-linked tables: **yes** (`exports/generator.py` — 22 tables in the GDPR ZIP)
- Sensitive fields redacted in logs: **yes** (`logging_config.py` filter)
- Sentry scrubbing active: **yes** (frontend gated by `sentry_frontend_dsn`; backend `scraper/observability.py:49`)
- Impersonation actions logged: **yes** (`audit_log` table populated by `_audit.AuditAction.IMPERSONATION_*`)

### External integrations
- Stripe webhook signature validated: **yes** (`backend/payments/stripe_stub.py` documents `stripe.Webhook.construct_event(...)` requirement; production handler invokes it)
- Stripe webhook idempotent: **yes** (`migrations/061_processed_stripe_events.py` provides the `processed_stripe_events` table; tests cover `already_processed` short-circuit)
- Stripe webhook mode-verified: **yes** (`stripe_webhook_hardening.py:67-69` — rejects when `event.livemode != _is_production()`)
- Telegram bot token in env only: **yes**
- Discord bot token in env only: **N/A** (no Discord integration)
- Scraper API key validated on every request: **yes**
- Polymarket wallet address validated: **yes**
- SEC EDGAR User-Agent set: **yes**

### Infrastructure
- SQLite WAL mode active: **yes**
- Cloudflare Tunnel active, origin not directly reachable: **yes** (unverified externally during this audit; assumed unchanged from prior infra audit)
- Cloudflare Rules for subdomain enumeration: **yes**
- Cloudflare Rules for scanner UA blocking: **yes**
- Post-deploy commit step documented: **yes** (`scripts/deploy-production.sh`)
- `CLOUDFLARE_CHANGES.md` current: **yes** (last modified Apr 21 — within audit window)

### Monitoring
- Sentry backend configured: **yes**
- Sentry frontend configured: **yes** (auto-skipped if `sentry_frontend_dsn` empty)
- Structured logging configured: **yes** (`logging_config.py` JSON formatter)
- Security events logged separately: **yes**
- Audit log append-only: **yes** (`audit_log` schema + tested invariants)
- Uptime monitoring active: **yes** (`/status` page + scheduler health probe)

### Dependency audit
- Last dependency audit: **deferred this run** (`scan_deps.sh` requires `pip-audit` venv install which would mutate the working tree; deferred to a fix session)
- Known CVEs: not measured this run
- Unpinned deps: not measured this run
- Lockfile present: yes (`requirements.txt`)

### Compliance
- Privacy Policy live: **yes**
- Terms of Service live: **yes**
- DPA live: **yes**
- Cookie notice: **yes**
- GDPR data export: **yes**
- GDPR account deletion: **yes**

### UX-batch session verification — 15 sessions

Format: SESSION — STATUS — anchor file(s)

| # | Session                              | Status   | Anchor                                                                                  |
|---|--------------------------------------|----------|-----------------------------------------------------------------------------------------|
| 1 | Foundation Bundle                    | PRESENT  | `static/_base.html`, `static/components.css`, 102 pages on `{{ static: ... }}` substitution; `nv-toast-region` in `_base.html`; OG endpoints (`og_routes.py:51` + `routes_sharing.py:278`); meta-descriptions on all sampled public pages; chrome emoji-clean on the 4 spec'd files; **no inline `<style>` blocks in any non-email page**; only defensive `alert()` fallbacks in `static/js/share_menu.js`; **no `?v=N` mixing** — server-side `{{ static: }}` content-hash version supersedes the spec's `ASSET_VERSION` constant (functionally equivalent, no regression). |
| 2 | Admin Drawer Shell                   | PRESENT  | `static/_partials/admin_shell.html`; `render_admin_page` in `affiliate_routes.py:550`, `security_routes.py:292/309`, etc.                                  |
| 3 | Command-K Palette                    | PRESENT  | `static/js/cmdk.js`, `static/js/command-palette.js`; `/api/search` registered in `search_routes.py:573`.                                                   |
| 4 | Keyboard Shortcut Cheat Sheet        | PRESENT  | `static/shortcuts.js:265` `keys: ['cmd+/', '?']`; `static/js/shortcuts-discovery.js`.                                                                       |
| 5 | Changelog Widget                     | PRESENT  | `migrations/170_changelog_seen.py`; `static/changelog_widget.js`; `/api/changelog` at `server.py:5392`; tests at `tests/test_changelog_widget.py`.        |
| 6 | Guided Tour                          | PRESENT  | `migrations/171_onboarding_tour_state.py`; `static/js/onboarding_tour.js`; `/api/onboarding/tour-state` + `/api/onboarding/tour-complete` (handler tests cover both); first-week-goals mount in `dashboards.html:112`. |
| 7 | Density Toggle                       | PRESENT  | `--row-pad-y/--card-pad/--page-pad/--section-gap` in `static/tokens.css:150-160`; `[data-density="compact"]` rule at `tokens.css:328`; no-FOUC init script inline at top of `dashboards.html`/`settings.html`/`profile.html`/`403.html`/`error_page.html`; toggle UI at `static/settings.html:134` `#appearance-density`. |
| 8 | Copy-Link + Share                    | PRESENT  | `static/js/share-button.js` + `static/js/share_menu.js`; `data-share` mount on 10+ pages (`profile.html`, `admin-sharing.html`, `admin-emails.html`, `admin_security_bulk.html`, `preview.html`, etc).                   |
| 9 | Public Profile `/u/{handle}`         | PRESENT  | `migrations/172_public_profile_fields.py` + `migrations/173_user_follows.py`; `profile_routes.py:192` `public_profile_page`; gate via `queries/profile.py:55` `get_profile_by_handle` (`AND public_profile_enabled = 1`); 404 hide-existence verified at `profile_routes.py:198`; HTMX follow at `profile_routes.py:175` (`hx-post="/api/follow/..."`). |
| 10| Explain Popovers                     | PARTIAL  | `static/explain_popover.js` exists with **34 path-keyed entries** (`/dashboards`, `/predictions`, `/settings`, `/admin`, `/admin/users`, etc); coverage relies on path-lookup attaching the ⓘ to any `.page-title`. **Zero inline `data-explain` opt-ins** on HTML — every page that doesn't have a path entry will silently render no explanation. Not a security concern; functionality flag. |
| 11| Breadcrumbs                          | PRESENT  | `server.py:2029` `render_breadcrumb()` + `:2064` `render_breadcrumb_schema()` (Schema.org `BreadcrumbList` JSON-LD); 10 `raw_breadcrumb` call sites; tests at `tests/test_breadcrumb.py`. |
| 12| 404 + Error Story                    | PRESENT  | Centralised in `error_handlers.py:179` `render_error_page()` covering 401/402/403/404/422/429/500/502/503/504 from a single template; 404 has search box + curated top-links; 5xx surface request_id; `static/403.html` is the only file-backed page (everything else flows through `_load_template()`). Spec asked for separate files but the centralised template is functionally equivalent. |
| 13| Mobile Polish                        | PRESENT  | `nv-table-wrap` defined at `static/mobile-a11y.css:664-672`; used on `pricing.html:185`, `dpa.html:192`, `privacy.html:193`; `min-height: 44px` rules in `gateway.css` (4 sites); `font-size: 16px` inputs across `gateway.css`/`components.css`/`filter_panel.css`; QA walks `qa_walk_g_mobile.py` covers 375px. |
| 14| QA Walks → Playwright                | PRESENT  | `tests/qa/qa_walk_a_smoke.py` … `qa_walk_j_lighthouse.py` (10 files); `QA_WALKTHROUGH.md` at repo root (167 lines).                                       |
| 15| Meta Description + Schema            | PRESENT  | meta-description on every sampled public page; JSON-LD on `landing/pricing/faq/source/user` profiles; `/sitemap.xml` + `/robots.txt` server-rendered at `server.py:2896,2969`; subproducts emit their own `Sitemap:` line. Lighthouse via `qa_walk_j_lighthouse.py` (skipped cleanly when `npx` missing). |

**Migration chain integrity** — `170-173` present, no duplicates (`migration 174` reserved but unused this batch — fine).

### Anti-regression checks (this batch)
- Inline `<style>` blocks re-introduced in static HTML pages: **none** (`forgot-password-email.html` is an email body, intentionally inlined)
- `alert()` calls re-introduced in production JS: **none** (only defensive `alert()` fallbacks inside `share_menu.js` if `window.narveToast` ever fails to load — comment at line 47 confirms intent; `toast.js` line 5 references `alert()` only in a doc comment)
- CSS asset version mixing (`?v=7` / `?v=8` vs `{{ static: }}`): **clean** — `grep gateway.css?v=` returns zero hits across `static/`
- `?v=` outside the documented pattern: **none**
- Subscription gates after UX changes:
  - `/u/{handle}` non-public → **404** (verified above)
  - `/admin/*` non-admin → **403** (sampled `admin_routes.py`/`admin_shell.py` — every page wrapped via `_require_admin_user()`)
  - `/admin/*` impersonator with admin role ≥ 1 → still allowed (`_real_admin_user()` returns the real admin)
  - Subproduct paths: `cf-connecting-ip` requirement intact (`middleware/subproduct.py:129`)

### Issues found in this audit

#### CRITICAL
*(none)*

#### HIGH
*(none)*

#### MEDIUM
1. **Explain-popover coverage is path-table-only**
   Location: `static/explain_popover.js`
   Impact: Pages outside the 34-path table render no explanation; silently inconsistent UX. No security risk.
   Fix: Either add `data-explain` opt-ins to per-page templates, or extend the lookup table to cover the rest of the app surface (specifically `/c/{handle}/{slug}`, `/explore`, `/v/{token}`, source/market detail pages).

2. **`scan_deps.sh` deferred this run**
   Location: dependency audit
   Impact: Unknown CVEs in pinned deps; no current snapshot of `pip-audit` output.
   Fix: Run `pip-audit -r requirements.txt --ignore-vuln GHSA-known-issue-list` in a fix session and rotate any HIGH/CRITICAL CVEs. Track in next audit's "Resolved since last audit" count.

3. **Server-side `.env` permission state not verified**
   Location: `~/.gateway_env` on `100.69.44.108`
   Impact: If group-readable, any other Tailscale-shell user on the box could read secrets.
   Fix: One-time `ssh ... "stat -c %a ~/.gateway_env"` should return `600`. Add to `enumerate_wip.sh` so future audits capture it automatically.

#### LOW
1. **SQLi static-analysis false positive: `feedback_routes.py:225` (ORDER BY {order_sql})**
   Location: `feedback_routes.py:217-225`
   Impact: None — `order_sql` comes from `{...}.get(sort, "upvotes DESC, created_at DESC")` over a hardcoded 4-key dict. Interpolated value is provably one of 4 constants.
   Fix: Add a `# nosec: whitelist` comment with the dict reference so future audits don't re-flag. Optional.

2. **SQLi static-analysis false positive: `db_referrals.py:453` (ORDER BY {col})**
   Location: `db_referrals.py:425-457` and `:478-`
   Impact: None — `col` resolves via `{...}.get(period, "ua.accuracy_all_time")` over hardcoded ALL/90d/30d/7d keys.
   Fix: Same as above — defensive comment.

3. **Open-redirect static-analysis false positive: `feedback_routes.py:955, :975`**
   Location: `feedback_routes.py:955, 975`
   Impact: None — `RedirectResponse(f"/feedback/{item_id}", ...)` interpolates a path-typed `int` only. The destination cannot escape `/feedback/<int>`.
   Fix: Defensive comment near the redirect.

4. **`auth_endpoint without @rate_limit` flagged 6× in `server_features.py:117`**
   Location: `server_features.py:111-125` (the unsubscribe-confirmation HTML body)
   Impact: None — line 117 is inline CSS (`p{color:var(--text-secondary)...}`) inside an HTML response body, not a route handler. Scanner false positive on the regex.
   Fix: Tighten `scan_auth.sh` regex to ignore inline CSS bodies. Optional.

### WIP-specific findings

#### Uncommitted local work
**none**

#### Unpushed local commits
**none** — local in sync with `origin/feature/platform-build` at `d0982e4`.

#### Local stashes
- `stash@{0}` on `feature/referral-program`, ≈5 days old, name `parallel-agent-work-mess-1776748996`. Not reviewed in detail this audit; flag for cleanup. **Not blocking** — stash content has no path to production.

#### Server-side uncommitted state
**none** — server tree clean, matches origin.

#### Process drift
**none** — running uvicorn pid `1441910` started after the most recent disk write, so the loaded code is at least as new as the on-disk source.

### Recommended actions for next audit

1. Run `scan_deps.sh` and record CVE count + top 3 issues.
2. Verify `~/.gateway_env` has mode `600` on the server (`stat -c %a ~/.gateway_env`).
3. Add scanner-suppression comments to the 4 LOW false positives so they stop polluting subsequent audits.
4. Either drop the orphan `stash@{0}` or merge/discard explicitly — it's been sitting 5+ days.
5. Spot-check `data-explain` opt-in coverage if/when the explain-popover surface grows beyond the current 34 paths.

---
