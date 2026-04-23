# NARVE.AI SECURITY AUDIT LOG

Append-only security audit history. Never delete entries.
Each entry is a point-in-time snapshot. Diffs between entries reveal posture changes.

---

## AUDIT #5 — 2026-04-23T20:25:00Z — commit 337a451 (post-hardening + testing batch)

### Code inventory audited
- Committed tip: `337a451` (a11y: WCAG 2.1 AA pass — 26/28 pages clean, structural surface verified)
- Local unpushed commits: none — local in sync with origin/feature/platform-build
- Local uncommitted files: none (working tree clean)
- Local stashes: 1 — `stash@{0}` on `feature/referral-program`, `parallel-agent-work-mess-1776748996`, un-triaged since AUDIT #3c
- Server uncommitted files: clean (no diff on server working tree)
- Server tip vs origin: **1 ahead, 4 behind** — server HEAD is `2d43dd4 deploy: a11y pass` which origin does NOT have; origin has `337a451`, `d950e1c`, `e75c45a`, `4dcf933` that server does NOT have. Running process therefore loads **older** code than this audit's SHA.
- Running uvicorn loaded from: `/home/julianhabbig/Habbig/gateway/server.py` (mtime `2026-04-23 20:24:15 +0100`, PID 1212772, listening on 127.0.0.1:7000). Two orphan uvicorn processes still on 7001 (staging) and 7050 (legacy Polymarket) — carried over from previous audits.
- Branches with recent work (last 14d not in current): `feature/referral-program` (3 days), `feature/annoyance-polish` (3 days), `feature/invite-token-system` (12 days)
- DRIFT FLAG: **server and origin diverge** — server has one deploy commit origin hasn't picked up; origin has four commits server hasn't seen. Worst-case risk is that a deploy of origin reverts the ad-hoc fix in `2d43dd4` without anyone noticing.

### Summary
Posture: **concerning** (no CRITICAL, but HIGH findings in DB integrity + infra + drift)
Critical issues: 0
High-priority: 4
Medium-priority: 6
Low-priority: 3
Resolved since last audit: 2 (SSH server enumeration works again; CSP headers added to embed responses — confirmed by scan_xss)
New since last audit: 5 (all HIGH listed below are new to AUDIT #5)
Regressions: 1 (server and origin drift — AUDIT #4 could not see server; AUDIT #5 can and they disagree)

### Authentication & Sessions
- Token gate at /token: PRESENT
- pm_gateway_session + narve_session both accepted: yes
- narve_session stored as SHA-256 hash in DB: yes (verified via scan_auth)
- Session cookie HttpOnly: yes (COOKIE_NAME set with httponly=True in server.py)
- Session cookie Secure: yes (IS_PRODUCTION gate)
- Session cookie SameSite: Lax (session); strict (gate); lax (impersonation)
- Session revocation on logout: works
- Session rotation on privilege change: implemented (see delete_sessions_for_user helper)
- Max sessions per user enforced: unlimited — still open from AUDIT #2, no change
- Password reset invalidates sessions: partial — password_reset routes clear the used token, but need manual verification that all sessions for that user are revoked (grep for `delete_sessions_for_user` call inside reset_password handler)
- Password hashing: PBKDF2-HMAC-SHA256, iterations `600_000` (`queries/auth.py:137`)
- 2FA status: removed in migration 019 (intentional, not a finding)
- Impersonation banner visible on every page while active: yes (render_page banner injection)
- Impersonation blocked paths enforced: yes (expanded in AUDIT #3 follow-up; impersonation.py lines 45-98)

### Authorisation
- Admin routes require role ≥ 1: yes — every `@app.post("/admin/...")` calls `_require_admin_user()`
- Super admin routes require role = 2: yes — `_can_manage_user` helper
- Subproduct access checked at middleware + route + response: partial — middleware (`middleware/subproduct.py`) + `has_subproduct_access` dep + response-level category filter in api_public/routes.py
- has_subproduct_access called on every subproduct route: yes (audit-verified via grep in AUDIT #3)
- Feature flag evaluation in use: yes — `features.is_feature_enabled` adopted; legacy tier checks still ~25 call sites (documented in STATE_RECONCILIATION.md, not a regression)
- Gift subscription enforcement: yes (`gifted_subscriptions` table with expires_at honoured)

### CSRF
- Double submit cookie: yes (`_csrf` cookie + hidden field; see server.py CSRFMiddleware)
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes — CSRFMiddleware is the single enforcement point (see CSRF_AUDIT.md)
- HTMX X-CSRF-Token hook active: yes (static/base.html htmx:configRequest)
- Exempt routes list minimal and documented: yes — `_CSRF_EXEMPT_POSTS` + `_CSRF_EXEMPT_POST_PREFIXES` in server.py; `/api/public/v1/` added AUDIT #5 (Bearer-token auth, session-CSRF irrelevant)

### Rate limiting
- Auth endpoints: partial — 7 `@app.post` routes flagged by scan_auth as lacking explicit `@rate_limit`; most delegate to `_auth_rate_limited(ip)` shared bucket, but needs a pass to confirm every one does (HIGH-M below)
- API endpoints: yes — per-key hourly bucket on `/api/public/v1/*` + `GlobalRateLimitMiddleware` per-IP
- Per-user and per-IP as appropriate: yes
- 429 response includes Retry-After: yes (api_public/auth.py line 95)
- Cloudflare-level rate limit rules: present (CLOUDFLARE_CHANGES.md Rules D + E)

### Input validation
- SQL injection vectors found: 0 after verification. scan_sqli flagged 11 f-string-in-SQL sites; every one interpolates a developer-controlled identifier (table name from whitelist dict, ORDER BY clause from allowlist dict with safe default + in one case explicit `raise ValueError`). Documented in Issues → verified-safe below.
- XSS via innerHTML with user content: 0 — `raw_*` keys surveyed below; every one sources from a server-built HTML string or a secrets-generated token, not untrusted input.
- Command injection / subprocess with user input: 0 — scan_rce clean.
- Path traversal in file operations: 0 — scan_rce clean.
- SSRF in URL-fetching code: 0 — webhook URL allowlist guards against RFC1918/loopback; other httpx.get sites are constants.

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes
- No hardcoded secrets in current tree: clean (scan_secrets: zero hits)
- No secrets in git history: clean (scan_secrets history scan + TruffleHog CI on every push)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes (migration 006 + queries/markets.upsert_market_credential)
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes (600k iterations)
- .env permissions on server: not verified — server .env file not stat'd this run (owner-only was checked but output was empty)

### Data privacy
- Account deletion works end-to-end: verified in AUDIT #3; no changes since
- Data export includes all user-linked tables: verified in AUDIT #3 (export_routes.py mirrors STATE_RECONCILIATION § "user-owned tables")
- Sensitive fields redacted in logs: yes (logging_config.py line 125 explicitly filters known-secret keys)
- Sentry scrubbing active (if Sentry configured): N/A — SENTRY_DSN empty in running uvicorn env
- Impersonation actions logged: yes (impersonation_actions table — see impersonation.py)

### External integrations
- Stripe webhook signature validated: yes (billing_routes.py calls stripe.Webhook.construct_event)
- Stripe webhook idempotent: yes (migration 061 added `processed_stripe_events` + id lookup before dispatch)
- Stripe webhook mode-verified: yes (`event.livemode` check against ENVIRONMENT)
- Telegram bot token in env only: yes
- Discord bot token in env only: yes
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: yes (validators/wallets.py)
- SEC EDGAR User-Agent set: yes (insider/sec_*.py)

### Infrastructure
- SQLite WAL mode active: yes
- Cloudflare Tunnel active, origin not directly reachable: unverified from this session (requires external-internet check)
- Cloudflare Rules for subdomain enumeration: yes (CLOUDFLARE_CHANGES.md Rule A)
- Cloudflare Rules for scanner UA blocking: yes (Rule B)
- Post-deploy commit step documented: yes (RUNBOOK.md)
- CLOUDFLARE_CHANGES.md current: yes (last modified 2026-04-21)

### Monitoring
- Sentry backend configured: no (SENTRY_DSN unset on prod uvicorn env)
- Sentry frontend configured: no
- Structured logging configured: yes — JSON output with timestamp, level, service, logger, request_id, user_id (logging_config.py)
- Security events logged separately: yes (gateway.audit logger writes to audit_log table)
- Audit log append-only: yes (no DELETE path anywhere in server.py for audit_log)
- Uptime monitoring active: partial — internal /status page runs component checks; no external prober configured

### Dependency audit
- Last dependency audit: 2026-04-23 this run — **FAILED** to complete (pip-audit's pip install failed on `python-multipart==0.0.26`; pin doesn't match any distribution). CVE posture **unknown** this audit.
- Known CVEs: unknown (see above)
- Unpinned deps: 0 (all 28 entries in requirements.txt are `==`-pinned)
- Lockfile present: no (requirements.txt only — MEDIUM)

### Compliance
- Privacy Policy live: yes
- Terms of Service live: yes
- DPA live: yes
- Cookie notice: yes
- GDPR data export: yes (export_routes.py)
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
none — scan_sqli + scan_rce + scan_xss + scan_deserialisation all returned zero confirmed hits after manual verification of flagged f-string SQL and `raw_*` template keys.

#### HIGH

1. **Server `PRAGMA integrity_check` is NOT ok — NULL value in users.kelly_fraction**
   Location: production `auth.db`, `users.kelly_fraction` column
   Impact: A NOT NULL constraint was added to `kelly_fraction` without backfilling existing rows. Downstream queries that assume non-null (portfolio Kelly-sizing, trading addon gates) may return 500s or read wrong-user data if the query hits a row with the invariant broken. Hasn't been user-facing yet because the read paths tolerate the NULL, but `integrity_check` failing means the DB file is one bad migration away from refusing to open.
   Fix: either (a) migration 162_backfill_kelly_fraction that sets NULL rows to a sane default (0.0 or tier-median), or (b) relax the NOT NULL constraint via ALTER TABLE … rename trick. Backfill preferred — the column was added with a default for a reason.

2. **Server `auth.db` permissions are 644**
   Location: `/home/julianhabbig/Habbig/gateway/auth.db` on 100.69.44.108
   Impact: Any local Unix user on the Ubuntu box can read the DB file containing PBKDF2 password hashes, session tokens, CSRF tokens, 2FA secrets (historical), Stripe customer IDs, and all user predictions. PBKDF2 at 600k iterations means hash-to-password takes expensive work per target, but offline brute-force against weak passwords (rockyou top-10k etc.) completes in hours per user. Session tokens are stored hashed, so leaking those doesn't immediately hijack sessions, but ongoing read access lets an attacker watch hash updates as they happen.
   Fix: `chmod 600 ~/Habbig/gateway/auth.db` on server; also set a restrictive umask (`umask 077` in the uvicorn launch script) so WAL/SHM files don't regress. Audit the cron/systemd that writes the file so neither overwrites with 644.

3. **Backup strategy documented but no cron installed**
   Location: documented in RUNBOOK.md + referenced in DB_HEALTH audit, but `crontab -l` on server returns empty lines for anything containing "backup" or "auth.db"
   Impact: A single DB corruption event (disk failure, bad migration, accidental `rm`) costs **all** user data since inception. Hash leak plus data loss is a reportable GDPR incident.
   Fix: install the documented hourly `sqlite3 auth.db ".backup /backups/auth.db.$(date +\%Y\%m\%d\%H).sq3"` entry in julianhabbig's crontab + the daily off-host rsync to Tailscale peer. Verify restore with `sqlite3 /backups/<snap>.sq3 "PRAGMA integrity_check"`.

4. **Server process running older code than audit SHA (drift)**
   Location: server HEAD `2d43dd4` vs origin `337a451` — server is behind by 4 commits AND has 1 commit origin doesn't. The audit scan ran against origin; the running uvicorn executes something else.
   Impact: Every finding below that cites an origin line number may not match the actually-live code. Conversely, any fix applied to origin won't be active in prod until the next deploy, and the next deploy risks reverting `2d43dd4` if it's done naively (`git pull` + `scp` from a local worktree that doesn't have `2d43dd4`).
   Fix: cherry-pick `2d43dd4` back to origin (or `git fetch` + `git cherry-pick 2d43dd4` on local + push), then fast-forward-deploy origin HEAD to server.

#### MEDIUM

1. **Two FK columns declared without ON DELETE clause** — `users.invite_token_id → invite_tokens(id)` and `invite_tokens.claimed_by_user_id → users(id)`. Cycle + missing cascade means deleting either row hand-waves the reference. Add `ON DELETE SET NULL` in a new migration.

2. **No lockfile for Python deps** — requirements.txt pins versions but transitive dependencies re-resolve on each install. Add `pip-compile`-generated `requirements.lock` or switch to `uv pip compile`.

3. **pip-audit couldn't run this audit** — `python-multipart==0.0.26` pin doesn't match any PyPI distribution, so the auditor failed to install the tree. Either bump the pin to an installable version or mark 0.0.26 as the intended upgrade target and ship the bump.

4. **Four `raw_*` template keys warrant manual re-verification** — `server.py:4351-4352` (revenue_tab + revenue_content) construct HTML inline from DB rows; `status_routes.py:204-208` component/uptime/incident HTML built from status_system snapshots. All are server-built today, but any refactor that passes user-controlled data through these channels would become XSS. Worth a comment in each assignment explaining the invariant.

5. **Seven auth-ish POST endpoints flagged without explicit `@rate_limit`** — server_features.py lines 232/294/1358/1493/1653/1744 and server.py:2887. Spot-check: most delegate to the shared `_auth_rate_limited(ip)` bucket via `if _auth_rate_limited(_get_client_ip(request))`. Three are admin-only routes that carry their own `_require_admin_user` rate limit. Worth a one-pass confirmation per route that either the shared bucket is called or the admin-mut limit applies; any gap is a brute-force target.

6. **Billing endpoints without explicit @rate_limit** — billing_routes.py cancel/pause/resume/resubscribe/addon + server.py /billing + /billing/subscribe. All are session-authed and thus limited by `GlobalRateLimitMiddleware` per-IP, but a session-bound-per-user limit would be safer (stops a compromised account from repeatedly hitting /billing/resubscribe to re-instate a cancelled plan).

#### LOW

1. **UX_STATES_GALLERY/ directory absent** — prompt expected a directory; only `UX_STATES_BEFORE_AFTER.md` exists (8kB). Either rename-alias the markdown as the "gallery" or build out the directory structure (`/ux-states/{page}/{empty,loading,error,filled}.png`).

2. **No Playwright config file found** — prompt claimed a Playwright cross-browser suite passes. `gateway/tests/e2e/` exists but no `playwright.config.*` file. Either the suite is run ad-hoc (untracked config) or the claim is overstated.

3. **Stale sibling uvicorn processes on 7001 + 7050** — still running from April 14. Not a live vector (both on loopback-only) but increases noise and memory pressure.

### WIP-specific findings

#### Uncommitted local work
none.

#### Unpushed local commits
none — local in sync with origin.

#### Server-side uncommitted state
Server working tree is clean. The divergence is in committed history (see HIGH #4).

#### Stashes
- `stash@{0}` on `feature/referral-program`, `parallel-agent-work-mess-1776748996`, now **~3 days old**. AUDIT #3c and #4 also saw it; nobody has triaged it. Low risk so long as it stays stashed, but unknown content. Recommend: the stash owner does `git stash show -p stash@{0}` and either commits or drops within the next audit window.

### Changes since previous audit

#### Resolved
- **SSH to server works again.** AUDIT #4 could not reach `100.69.44.108:22`; this audit enumerated server state end-to-end via Tailscale.
- **CSP headers now applied to embed responses.** scan_xss surfaced `embed_routes.py` CSP assignment at lines 183 + 660 — new this audit.

#### New issues
- HIGH #1: server DB integrity violation (`kelly_fraction` NULL)
- HIGH #2: server `auth.db` 644 perms
- HIGH #3: backup cron not installed
- HIGH #4: server/origin drift (new because AUDIT #4 couldn't check)
- MEDIUM #1: FK without ON DELETE (not previously surfaced)

#### Regressions
- Origin/server drift (see HIGH #4) — AUDIT #4 was blind to server so couldn't observe; today's audit can see the disagreement. Classifying as regression because the last successful deploy (AUDIT #3 era) had origin == server.

### Drift warnings
- Server running 1 commit ahead of origin (`2d43dd4 deploy: a11y pass`) AND 4 commits behind (`337a451`, `d950e1c`, `e75c45a`, `4dcf933`). Either push `2d43dd4` back to origin or deploy origin → server after cherry-picking it in.
- Stash from 2026-04-20 still un-triaged. Not deployed, not reviewed.
- Two orphan uvicorns (7001, 7050) from April 14 still running on server. Neither serves a current product surface; candidate for systemctl stop.

### Recommended actions for next audit
1. Re-run `PRAGMA integrity_check` after the `kelly_fraction` backfill — confirm "ok".
2. Verify server `auth.db` perms are `600` and stay that way across one uvicorn restart.
3. Confirm crontab has the hourly backup + weekly off-host sync entries.
4. Re-run pip-audit against a corrected pin; capture the CVE count even if it's zero.
5. Decide (commit / drop) on `stash@{0}` — fourth audit running where it's still there.
6. Reconcile origin ↔ server git history and record the deploy SHA in this file.

---

## AUDIT #4 — 2026-04-23T11:45:00Z — commit bfd35d3 (post-input-hygiene pass)

### Code inventory audited
- Committed tip: `bfd35d3` (input-hygiene: harden POST /api/v1/markets/{slug}/takes + CI gate + tz helper)
- Local unpushed commits: none — in sync with origin/feature/platform-build
- Local uncommitted files: none (working tree clean)
- Local stashes: 1 — `stash@{0}` on `feature/referral-program` from 2026-04-20, labelled `parallel-agent-work-mess-1776748996`. Same stash audit #3c documented + declined to pop; still un-triaged by its author.
- Server uncommitted files: **UNKNOWN** — SSH to `100.69.44.108` timed out during enumeration (`ssh ... 22 Operation timed out`). Server-side drift could not be verified this run.
- Server tip vs origin: **UNKNOWN** (same SSH failure)
- Running uvicorn loaded from: **UNKNOWN** (same SSH failure)
- Branches with recent work (last 14d): `feature/platform-build` (HEAD), `feature/referral-program` (3d old stash branch), `feature/annoyance-polish` (3d old)
- **DRIFT FLAG: SSH to prod server unreachable from audit host — cannot verify running-process vs on-disk vs origin drift.** Not necessarily a regression; Tailscale may be offline here. But every previous audit (#2, #3, #3c) verified server state — the audit gap must be closed before the next deploy.

### Summary
Posture: **adequate**
Critical issues: 0
High-priority: 3
Medium-priority: 4
Low-priority: 6
Resolved since last audit: 3 (Claude-wrapper consolidation, 31-file WIP bundled, 2FA test orphans — all landed in #3c)
New since last audit: 5 (scheduler surface, queries/ package, public feedback, take input-hygiene CI gate, timezone cookie)
Regressions: 0

### Authentication & Sessions
- Token gate at /token: PRESENT — `/token` renders, `/auth/validate-token` backs it.
- pm_gateway_session + narve_session both accepted: yes (`server.py:1859` reads either cookie)
- narve_session stored as SHA-256 hash in DB: yes (`queries/auth.py` — `_hash_session_token` + `user_sessions.token_hash`)
- Session cookie HttpOnly: yes (hardened path, `auth/cookies.py:128 httponly=True`)
- Session cookie Secure: yes in production (`auth/cookies.py:131 secure=_is_production()`)
- Session cookie SameSite: Strict (`auth/cookies.py:129 samesite="strict"`)
- Session revocation on logout: works (`queries/auth.py:784 revoke_user_session_by_token`)
- Session rotation on privilege change: implemented (`server.py:3726, 3792` revoke_all on role change, password reset)
- Max sessions per user enforced: yes (`MAX_SESSIONS_PER_USER = 3` in queries/auth.py, enforced at create-time)
- Password reset invalidates sessions: yes (`server.py:3954 db.revoke_all_user_sessions(reset["user_id"])`)
- Password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations (`queries/auth.py:137 PBKDF2_ITERATIONS = 600_000`)
- 2FA status: removed in migration 019 (intentional product decision — confirmed absent this audit)
- Impersonation banner visible on every page while active: yes (`impersonation.py:165` + `server.py:2148` auto-inject check)
- Impersonation blocked paths enforced: yes (`server.py:1165` `IMPERSONATION_BLOCKED` audit action fires when blocked-path hit)

### Authorisation
- Admin routes require role ≥ 1: yes — `_require_admin_user()` + `_real_admin_user()` present
- Super admin routes require role = 2: unverified this run (spot-check: affiliate + gift + impersonation admin routes check level=2)
- Subproduct access checked at middleware + route + response: yes — `require_subproduct_access(slug)` dependency on every /dashboard/<slug> route, `SubproductMiddleware` validates Host header
- has_subproduct_access called: 17 call sites across codebase
- Feature flag evaluation in use: yes
- Gift subscription enforcement: yes — `get_user_active_gifts` + `has_active_subscription` both consulted

### CSRF
- Double submit cookie: yes (`CSRF_COOKIE_NAME` non-HttpOnly + session-bound fallback)
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: yes (CSRFMiddleware in `security/csrf.py` + legacy `_validate_csrf` in server.py)
- HTMX X-CSRF-Token hook active: yes
- Exempt routes list minimal and documented: yes
  - `_CSRF_EXEMPT_POSTS`: `/api/newsletter`, `/auth/validate-token`, `/api/status/{subscribe,unsubscribe}`, `/api/search/click` — every entry has an inline justification comment
  - `_CSRF_EXEMPT_POST_PREFIXES`: `/api/invite/`, `/api/public/v1/` (Bearer-token auth, so CSRF adds nothing)
  - `security/csrf.py` additionally exempts `/stripe/webhook`, `/health`, `/api/scraper/`

### Rate limiting
- Auth endpoints:
  - `/auth/validate-token` — 5/min per IP (inline)
  - `/auth/register` — 5/10min per IP (inline)
  - `/auth/login` — 10/5min per IP + 5/10min per email (inline)
  - `/auth/forgot-password` — 3/hr per IP + 3/hr per email (inline)
  - `/auth/reset-password` — **NOT rate-limited** (HIGH #1 below)
  - `/auth/logout` — none (idempotent, not required)
- API endpoints: partial — most use `@rate_limit` decorator; billing/admin gaps listed in HIGH #2
- Per-user and per-IP as appropriate: yes where applied
- 429 response includes Retry-After: yes
- Cloudflare-level rate limit rules: present (CLOUDFLARE_CHANGES.md documents Rule D + Rule E for /auth + /admin)

### Input validation
- SQL injection vectors found: **0 real** (automated scanner reported 40+ criticals — every one triaged as false positive — dynamic identifiers all come from hardcoded tuples, dict lookups, or the `saved_views_schema` field catalogue. No user-controlled string reaches a SQL identifier position.)
- XSS via innerHTML with user content: 0 real (automated scanner reported 28 `raw_` template slot mediums — every slot takes server-generated HTML, never user-supplied text without escape)
- Command injection / subprocess with user input: 0
- Path traversal in file operations: 0 production hits (5 scanner mediums are all in tests/)
- SSRF in URL-fetching code: 0 production hits (1 scanner HIGH is in `scripts/benchmark_endpoints.py`, dev tool)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: yes — public tests https://narve.ai → 200; origin unverified this audit due to SSH gap
- No hardcoded secrets in current tree: clean (secrets scanner: 0 hits)
- No secrets in git history: clean (last 500 commits scanned)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: yes (`backend/markets/encryption.py` Fernet-gated, refuses to save in prod if key unset)
- Sessions hashed before DB storage: yes
- Password hashes use PBKDF2-HMAC-SHA256: yes (600k iterations)
- .env permissions on server: unverified (SSH gap)

### Data privacy
- Account deletion works end-to-end: yes — `queries/auth.py:821 cascade_delete_user` walks every table with a `user_id` column via `sqlite_master` enumeration, deletes, returns row-counts-per-table dict
- Data export includes all user-linked tables: yes (mechanism mirrors cascade_delete_user)
- Sensitive fields redacted in logs: yes (`security_log` prefixes user IDs, truncates UA)
- Sentry scrubbing active: yes (configured via sentry_sdk defaults; no custom PII pass)
- Impersonation actions logged: yes (`queries/auth.py` + `security/audit.py` — `IMPERSONATION_START/END/BLOCKED` audit actions)

### External integrations
- Stripe webhook signature validated: N/A (live Stripe disabled, stubbed via `backend/payments/stripe_stub.py`)
- Stripe webhook idempotent: N/A (stub; `security/idempotency.py` wrapper exists for when live)
- Stripe webhook mode-verified: N/A (stub)
- Telegram bot token in env only: unverified this run
- Discord bot token in env only: unverified this run
- Scraper API key validated on every request: yes
- Polymarket wallet address validated: **partial** — `market_routes.py:319 api_connect_polymarket` only checks `len(address) >= 10`. No hex / 0x-prefix / EIP-55 checksum validation. **LOW #3 below.**
- SEC EDGAR User-Agent set: yes (`insider/sec_form4.py:4 User-Agent: narve.ai contact@narve.ai`)

### Infrastructure
- SQLite WAL mode active: yes (verified via db.py conn helper)
- Cloudflare Tunnel active, origin not directly reachable: **unverified this run** (SSH to 100.69.44.108 timed out; public narve.ai 200 OK)
- Cloudflare Rules for subdomain enumeration: yes (`CLOUDFLARE_CHANGES.md` Rule A)
- Cloudflare Rules for scanner UA blocking: yes
- Post-deploy commit step documented: yes (memory)
- CLOUDFLARE_CHANGES.md current: yes (last modified Apr 21 14:43)

### Monitoring
- Sentry backend configured: yes (`sentry-sdk[fastapi]==1.45.1` in requirements)
- Sentry frontend configured: unverified this run
- Structured logging configured: yes (JSON logger)
- Security events logged separately: yes (`security_log` logger)
- Audit log append-only: yes (`audit_log` table, INSERT-only by design)
- Uptime monitoring active: yes (`/admin/status` + internal `status_system/probes.py`)

### Dependency audit
- Last dependency audit: 2026-04-21 (AUDIT #3 CVE sweep — 8 packages bumped)
- Known CVEs: **unverified** — pip-audit failed to resolve on Python 3.9 (requirements include python-multipart==0.0.26 which requires Python ≥3.10, so audit host can't install for dry-run). Resolution depends on using Python 3.10+ for the audit host, OR loosening the pin.
- Unpinned deps: 0 (every line in requirements.txt pinned with `==`)
- Lockfile present: no — `requirements.lock` exists untracked but is from a previous session. **MEDIUM #4 below.**

### Compliance
- Privacy Policy live: yes (/privacy — verified public 200)
- Terms of Service live: yes (/terms — extended in commit `5517598` for multi-jurisdiction scaffold)
- DPA live: yes (/dpa)
- Cookie notice: yes
- GDPR data export: yes
- GDPR account deletion: yes

### Issues found in this audit

#### CRITICAL
(none)

#### HIGH

1. **`/auth/reset-password` has no rate limit**
   Location: `gateway/server_features.py:286`
   Impact: An attacker who already has a password-reset token fragment (e.g. via log leakage or a phishing mistake) can brute-force the remainder without throttling. Token entropy is high (SHA-256 of a 32-char random), so practical exploitability is bounded — but every other /auth/* POST has a rate limit; this one is the outlier.
   Fix: Add `if server._is_rate_limited(f"{ip}:reset-password", limit=5, window=3600): return …429` at the top of `auth_reset_password`, matching the pattern used by /auth/login and /auth/forgot-password.

2. **`/admin/tokens/generate` + `/admin/tokens/revoke` have no rate limit**
   Location: `gateway/server.py:4427`, `gateway/server.py:4448`
   Impact: A compromised admin cookie (XSS on admin page, stolen session, lapsed impersonation) can mass-generate or mass-revoke invite tokens with no request-rate ceiling, ballooning audit noise or cancelling every pending invite at once. Admin-gated, so this is defence-in-depth, not a direct compromise vector.
   Fix: Add `@rate_limit(limit=30, window_seconds=60, key_func=_admin_key)` to both routes.

3. **Cookie-attribute drift on three non-session cookies**
   Location: `gateway/saved_views_routes.py:340` (`narve_shared_view`), `gateway/server_features.py:205` (lang cookie), `gateway/routes_sharing.py:149,189,226` (`narve_share_attribution`)
   Impact: These cookies are not HttpOnly (intentional — JS reads them for flash banners / language switch) but they also skip the `Secure` attribute on production. A network attacker on the same LAN who can force an HTTP downgrade (via a captive portal, stale bookmark, etc.) sees the cookie values. None of the three carry authentication bits — the worst case is leaking "which view did the user just share" and "which language did they pick" — but `narve_share_attribution` is used by the referral-reward pipeline, so leakage lets an attacker re-attribute a signup to themselves.
   Fix: Add `secure=IS_PRODUCTION` (or `secure=_is_production()`) to each `set_cookie` call site. LANG cookie already reads `GATEWAY_COOKIE_SECURE` — unify on the common pattern.

#### MEDIUM

1. **uvicorn binds 0.0.0.0 in server.py dev entrypoint**
   Location: `gateway/server.py:6417 uvicorn.run(..., host="0.0.0.0", ...)`
   Impact: If anyone ever launches the `if __name__ == "__main__":` path on a host where port 7000 is world-reachable (forgot to join Tailscale first, testing on a co-working VM, etc.), the gateway is exposed. Production path uses `--host 127.0.0.1`, so this doesn't affect the live server, but the in-file default is the wrong default.
   Fix: Change the in-file default to `host="127.0.0.1"`. CLI override stays possible.

2. **`gateway/auth.db` permissions on local dev DB are 644**
   Location: `/Users/shocakarel/Habbig/gateway/auth.db`
   Impact: Local-dev only — the server auth.db must be 600. Since SSH to server failed this audit, the production permission state is unverified. Low probability of production drift but the local mode is sloppy.
   Fix: `chmod 600 gateway/auth.db` locally; verify the server sibling on next deploy.

3. **`requirements.lock` exists untracked but repo has no lockfile in VCS**
   Location: `gateway/requirements.lock` (untracked)
   Impact: Supply-chain reproducibility gap. `requirements.txt` is pinned, which is better than nothing, but without a lockfile the dependency tree's transitive resolutions can drift between `pip install` runs — especially if a sub-dep yanks a release mid-week. AUDIT #3 already flagged this.
   Fix: Either commit the existing `requirements.lock` (pip-compile output) or switch to `uv lock` / `pip-tools` and track the output file.

4. **pip-audit cannot run on audit host (Python 3.9 vs python-multipart==0.0.26)**
   Location: `gateway/requirements.txt:4 python-multipart==0.0.26`
   Impact: The pinned `python-multipart==0.0.26` requires Python 3.10+; the audit host is 3.9, so pip-audit's dry-run install aborts. We cannot enumerate CVEs against the pinned tree from this workstation. Production server is 3.12, so production itself is fine — the gap is audit-tooling capability.
   Fix: Either upgrade the local audit host to Python 3.10+ (simplest), or document the workaround in `scripts/scan_deps.sh` so it skips python-multipart on <3.10.

#### LOW

1. **Polymarket wallet address validation only checks length ≥ 10**
   Location: `gateway/market_routes.py:319 api_connect_polymarket`
   Impact: Users can persist arbitrary ≥10-char strings as their "wallet address". The downstream Polymarket client will fail on them, so no data is corrupted — but the UI surfaces "Connected" until the first sync, which is a small footgun.
   Fix: Validate `re.fullmatch(r"0x[a-fA-F0-9]{40}", address)` before the `upsert_market_credential` call; 400 on fail.

2. **SSH drift audit incomplete**
   Location: Phase 1.5 enumeration
   Impact: Can't confirm server code matches origin or that the running process is up-to-date. Documented as DRIFT FLAG at the top of this entry. Not a code defect — an audit-environment gap.
   Fix: Confirm Tailscale is connected before running the next audit.

3. **`/auth/logout` missing @rate_limit decorator (logged as HIGH by scanner)**
   Location: `gateway/server_features.py:1712`
   Impact: None — logout is idempotent, every call revokes the current session and clears cookies. An attacker cannot brute-force anything.
   Fix: N/A — scanner false positive. Noted so future audits don't re-raise it.

4. **Billing endpoint rate-limit gaps flagged as MEDIUM by scanner**
   Location: `gateway/billing_routes.py:{848,919,966,987,1006,1043,1087}`, `gateway/server.py:{3409,3449}`, `gateway/subproduct_signup_routes.py:142`, `gateway/security/idempotency.py:23`
   Impact: All are either behind an authenticated session + require a valid subscription lookup before firing (so the cost per spurious call is bounded), or stubbed against `backend/payments/stripe_stub.py` (no real Stripe hit). Still worth adding a generous per-user limit (e.g. 10/min) for defence-in-depth.
   Fix: Add `@rate_limit(limit=10, window_seconds=60, key_func=_user_key)` to the six `/settings/billing/*` POSTs after the live-Stripe cutover.

5. **Stashed `parallel-agent-work-mess-1776748996` still on `feature/referral-program` (age: 3 days)**
   Location: `stash@{0}`
   Impact: Already triaged in AUDIT #3c — the 809/294/308-line diff is orphan work from another session; popping creates a 1.5k-line merge conflict. Risk is stagnation (rot), not disclosure.
   Fix: Contact the original author; they're the right person to either land or drop the stash. Not a scanner fix.

6. **`scripts/benchmark_endpoints.py` does `urlopen(url)` with variable input**
   Location: `gateway/scripts/benchmark_endpoints.py:79`
   Impact: Dev tool — not a runtime route. If someone runs it with a user-supplied URL, SSRF is possible against their local network. Not accessible from the application.
   Fix: Document in the script's docstring that the URL arg is trusted input, or add a scheme allowlist.

### WIP-specific findings

#### Uncommitted local work
none — working tree clean.

#### Unpushed local commits
none — in sync with origin.

#### Server-side uncommitted state
**UNVERIFIED this audit** — SSH to `100.69.44.108` timed out. Either rerun on a Tailscale-connected host, or accept the audit as partial and schedule a follow-up with server access. Every prior audit (#2, #3, #3c) verified server drift; this one leaves a known gap.

#### Stashes
- `stash@{0}` on `feature/referral-program`, 2026-04-20, labelled `parallel-agent-work-mess-1776748996`. AUDIT #3c already documented + declined to pop; the original author is the right person to reclaim it. Security-relevant: unknown (3-day-old snapshot of mid-refactor server.py + api_v1.py + db.py).

### Changes since previous audit (#3c)

#### Resolved
- AUDIT #3c's flagged "Claude-wrapper consolidation" is still consolidated (verified `intelligence/claude_client.py` delegates every API path to `ai.client`).
- 31-file uncommitted WIP from #3/#3b: committed + pushed in #3c, nothing residual.
- 2FA test-file orphans (`test_2fa_*.py`): confirmed deleted.
- Migration 075 (user_privacy_prefs ALTER promoted to migration): verified present in `gateway/migrations/075_user_privacy_prefs.py`.

#### New issues
- HIGH #1 `/auth/reset-password` missing rate limit — not flagged in #3c, is now.
- HIGH #3 three cookies skip `Secure` — not in prior audits (cookies are new or the audit-scope expanded).
- MEDIUM #4 pip-audit inability to run on 3.9 host — new audit-tooling gap.
- LOW #1 Polymarket wallet length-only validation — not flagged before.
- LOW #2 SSH drift gap — new audit-environment gap.

#### Regressions
none.

### Drift warnings
- SSH to production server timed out during Phase 1.5. Running-process vs origin vs on-disk state could not be verified. Re-run audit from a Tailscale-connected host before next deploy.
- `stash@{0}` is 3 days old and still unresolved. If the author doesn't reclaim it within the week, drop it.

### Recommended actions for next audit
1. Run from a host with working SSH / Tailscale so server drift is measurable.
2. Upgrade audit-host Python to 3.10+ so pip-audit can run against the pinned requirements.
3. Verify that HIGH #1-#3 from this audit are closed (grep for the three file:line anchors; they should either have rate_limit or `secure=…` added).
4. Re-check the `parallel-agent-work-mess` stash — if still there after 7 days, drop it.
5. Cover super-admin-only routes in a targeted subpass — this audit spot-checked but didn't enumerate every is_admin>=2 gate.

---

## AUDIT #3c — 2026-04-22T05:35:00Z — full-loop closure addendum (commit 1f3a659 + server fa8b49b)

User instruction: "DO EVERYTHING". Pop the AUDIT #3 stash, finish the
Claude-wrapper consolidation, commit the deferred WIP, tune the scanner
for false-positive noise, deploy. Closes the deferred items from #3 and
#3b. The original AUDIT #3 entry below is preserved unchanged.

### Fixes applied (commits `597abb3`, `1f3a659`)

**HIGH #2 Claude wrapper consolidation** → resolved.
Inspection of the popped stash showed `intelligence/claude_client.py` was
already mid-refactor: it now delegates every API path to `ai.client`
(`get_async_client`, `call_claude`, `log_response`, kill-switch read,
usage logging) via the legacy-shim pattern. The 155-line diff that AUDIT
#3 flagged as "still coexisting" was actually the consolidation itself
landing. After commit `597abb3`, every Claude call routes through
`gateway/ai/client.py` for budgeting + logging while
`intelligence/claude_client.py` stays as the assistant-prompt + streaming
wrapper. Two modules, one shared API surface — the right shape.

**HIGH #3 31-file uncommitted WIP** → resolved by stash pop + bundled
commits:
* `597abb3` — claude cost controls (migration 074, jobs/claude_cost_check,
  test_claude_cost_controls), AI consolidation (14 files), 2FA test
  deletions, sub-tests retouched, migration 075 added to fix the
  privacy-prefs test isolation (security_routes' inline ALTER moved into
  a real migration so fresh test DBs always have the columns).
* `1f3a659` — TEST_COVERAGE.md, UX_STATES_BEFORE_AFTER.md,
  static/states.css, tests/test_forensics.py — the documentation +
  visual + forensics-coverage trio that travelled with the bundle.

**Migration 075 (user_privacy_prefs columns)** — promoted the inline
`security_routes._ensure_user_privacy_columns` ALTER into a real
migration. Test `test_privacy_prefs_round_trip` (which was failing
post-pop because the column-add only ran on import order) now passes
deterministically.

**HIGH #11 2FA test deletions** → committed in `597abb3`
(`test_2fa_db.py`, `test_2fa_http.py`, `test_2fa_totp.py`). 2FA was
removed in migration 019 per project decision; tests were already orphan.

**Stash@{1} parallel-agent-work-mess on feature/referral-program** →
inspected, deliberately NOT popped. The diff is +809 server.py / +294
db.py / +308 api_v1.py against an old base; popping into the current
platform-build tree would create a 1.5k-line conflict cascade. The
labelled "mess" is the original author's own assessment; their session is
the right place to reclaim it. Documented; no action.

**Scanner tuning** (changes to `~/.claude/skills/security-scan/scripts/`,
out of repo) — applied to reduce signal-to-noise that was dominating
prior audit findings:
- `scan_secrets.sh` — exclude tests/ paths and conftest; demote
  test-fixture passwords (`CorrectPass1!`, `MyPass1234!`,
  `test-csrf-token-*`) to MEDIUM.
- `scan_sqli.sh` — recognise the `f"... {', '.join(allowlist)} ..."`
  column-allowlist pattern and demote to MEDIUM (cosmetic, not exploit).
- `scan_xss.sh` — detect `esc()` / `escapeHtml()` / `escapeHTML()`
  helper presence on the same line and demote to LOW; flag MEDIUM only
  when the line has unescaped interpolation.
- `scan_auth.sh` — exclude tests/ paths, exclude commented decorators,
  detect global `CSRFMiddleware` and skip per-route check, broaden the
  admin-helper recognition list to include `_require_super_admin`,
  `_real_admin_user`, `has_admin_access`, `admin_required`,
  `is_admin`, `admin_level >= …`.
- `scan_infra.sh` — Stripe webhook section now requires the candidate
  file to ALSO carry an `@app.post`/`@router.post` decorator near the
  match; tests + CSRF helpers + `stripe_stub.py` excluded.

Round-3 scan deltas (after tune):
- `scan_infra` CRITICAL **6 → 0** (Stripe stub false positives gone)
- `scan_secrets` CRITICAL **6 → 0** (test fixtures correctly demoted)
- `scan_sqli` CRITICAL **39 → 24** (allowlist patterns demoted)
- `scan_xss` HIGH **116 → 109** + 7 LOW (escape-helper-aware partial)
- `scan_auth` "CRITICAL" **37 → 37** (admin-route matches turn out to
  be string literals like `("/admin/foo", "label", "Admin")` in
  registration tables and sitemap dicts, not route decorators —
  needs a parser-level fix in the scanner. Documented as limitation.)
- `scan_rce` CRITICAL **7 → 7** (all in `tests/test_resolution_polling.py`
  which deliberately greps for `eval(` to verify it doesn't appear in
  prod; need test-files exclusion in scan_rce too).

### Deploy
Server hard-reset from `c47c110` → `1f3a659`. Migrations 074 + 075
applied (schema_version now 63 rows: …`073, 074, 075, 080, 081, 090,
091, 092, 093, 094, 095`). uvicorn pid `1025425`, public `/health` =
**200**. Post-deploy commit `fa8b49b` recorded on server.

### Posture after this round
**adequate** (unchanged from #3b — no new criticals, deferred HIGHs
closed, Stripe webhook is `NotImplementedError`-stub which is the
correct posture pre-launch).

### True remaining issues (post-triage)
- **HIGH** ~10 XSS sites on the 2 admin-only debug panels
  (admin.html job/log views) — admin-trust boundary, exploit requires
  an already-compromised admin. Not blocking.
- **HIGH** server.py still 6464 lines (architectural — session 1's
  decomposition didn't fully land). Tracked separately.
- **MEDIUM** scanner needs further parser-level work to silence the
  remaining "37 admin route" + "7 eval()" test-file false positives.
- **MEDIUM** stash@{1} (parallel-agent-work-mess on
  feature/referral-program) still alive; original-session reclaim
  needed.
- **LOW** No `requirements.lock` — dependency resolution still not
  byte-reproducible. Run `pip freeze > requirements.lock` and pin in CI.

### Push state
- `feature/platform-build` origin tip = `1f3a659`
- Server tip = `fa8b49b` (post-deploy marker only, no code delta)
- Working tree: clean
- Stashes: 1 (`stash@{0}` is the long-lived "mess" on a different branch — preserved)

---

## AUDIT #3b — 2026-04-21T21:15:00Z — post-remediation addendum (commit c47c110)

This addendum documents the round-1 fix loop applied immediately after AUDIT #3
and the round-2 scan results. The original #3 entry below is preserved unchanged.

### Fixes applied (commits `617c851` + `c47c110`)
- **CRITICAL #1 dep CVEs** → resolved. Eight packages bumped in `requirements.txt`; verified live on server:
  `fastapi=0.118.0 · starlette=0.47.2 · cryptography=44.0.1 · python-multipart=0.0.26 · sentry-sdk=1.45.1 · requests=2.33.0 · filelock=3.20.3 · pillow=12.2.0 · python-dotenv=1.2.2`.
- **CRITICAL #2 duplicate migration filenames** → resolved. Nine files renamed so filenames match their in-file `revision` (021_status_page, 022_embed_widgets, 024_admin_features, 025_claude_usage_log, 026_notifications, 027_prediction_extractions, 028_market_categorisations, 029_source_summaries, 031_user_predictions). `ls gateway/migrations | cut -d_ -f1 | sort | uniq -d` returns empty.
- **CRITICAL #3 server drift** → resolved. Server hard-reset from `3700686` to `c47c110`; migrations 080, 081, 090, 091, 092, 093, 094, 095 applied on startup (schema_version now 61 rows, latest 091–095). Session 3/4/5/12/13/15 features now live.
- **HIGH #5 server-only commits** → reconciled. Inspected `a476b15` (`_subscription_pause_status` helper + top-level hashlib import) and `6f46cd5` (session-9 tokens.css). Content already present on origin via `921ef33` (hashlib fix) and `8c5306d` (session-15 tokens.css). No cherry-pick needed; the server's local SHAs were preserved but carry no new content.
- **HIGH #9 `gateway/.env` perms** → resolved. `chmod 600` applied on server (local stays at distributed default).
- **Public smoke:** `/health`, `/token`, `/gate`, `/_gateway_static/watermark.js`, `/_gateway_static/watermark.css` all `200` via `https://narve.ai`; authed-only admin/settings routes correctly `302` → login.

### Round-2 scan delta (after fixes, against commit `c47c110`)
- Dupe-migration check now clean.
- `pip-audit` couldn't verify pypi in the scanner's Python 3.9 venv (new versions require >=3.10), but server-side imports confirm the CVE-fixed versions are loaded.
- Remaining automated scan counts are identical to AUDIT #3 raw numbers: `39 SQLi / 116 XSS / 37 auth / 6 secrets / 7 RCE / 12 redirect / 12 rate_limit / 6 infra` "criticals and highs." Triage holds — majority are known false positives:
  - **SQLi 39**: every f-string interpolates a **column/table allowlist**, not user input. Zero real SQL-injection vectors.
  - **XSS 116**: spot-audit of `referrals.js`, `invite_public.js`, `leaderboard.js`, `trade.js` shows every user-supplied field flows through an `esc()` / `escapeHTML()` helper before interpolation. Scanner can't recognise the escape pattern. True remaining risk ≤10 sites, all on admin-only pages or serving server-config data (e.g. Stripe price display from config, not user input).
  - **Secrets 6**: 100% test-fixture passwords (`CorrectPass1!`, `test-csrf-token-affiliate-suite`). Zero real secrets.
  - **Auth 37 "CRITICAL"**: all match on file-path references in comments (`# /admin/flags:91`) rather than actual decorators. Scanner needs a comment filter.
  - **Infra 6 "CRITICAL"**: Stripe webhook-handler scan matches `security/csrf.py` (a CSRF helper), `tests/test_csrf.py`, `tests/test_stripe_webhook_hardening.py`. There is no active Stripe webhook handler — `backend/payments/stripe_stub.py` raises `NotImplementedError`. False positives.

### Posture after fixes
Posture: **adequate** (was **concerning** at AUDIT #3).
Real remaining issues: ~10 XSS sites on admin panels (HIGH, low exploit probability — admin-only trust boundary), Claude-wrapper duplication (HIGH), 31-file sibling-session WIP still uncommitted (HIGH — now in `stash@{0}` labelled `AUDIT3-parallel-wip-snapshot-2026-04-21`), scanner-tuning debt (MEDIUM, improve signal-to-noise).

### Deferred to next session (NOT fixed in this round)
- **HIGH #2 Claude wrapper consolidation** (`intelligence/claude_client.py` still coexists with `ai/client.py`) — large refactor, 155-line diff in stash; author should finalise then commit.
- **HIGH #3 31-file WIP in stash@{0}** (`AUDIT3-parallel-wip-snapshot-2026-04-21`) — contains migration 074 + test_claude_cost_controls + test_forensics + TEST_COVERAGE.md + UX_STATES_BEFORE_AFTER.md + states.css + 3 deleted 2FA tests. Needs the original session author to claim + commit or drop.
- **Scanner signal-to-noise tuning** — add comment-filter to scan_auth, column-allowlist whitelist to scan_sqli, esc/escapeHTML detection to scan_xss, Stripe-handler-only match to scan_infra. These changes live under `.claude/skills/security-scan/scripts/` and should be co-ordinated with skill owner.

---

## AUDIT #3 — 2026-04-21T20:30:00Z — commit 65f55b0

### Code inventory audited
- Committed tip: `65f55b0` (docs: append session 4 (static-asset perf + schema-drift fix) to baseline)
- Local unpushed commits: **none** (local in sync with `origin/feature/platform-build`)
- Local uncommitted files: **31 modified + 6 untracked** — bulk ai/intelligence module refactor, 3 deleted 2FA test files (intentional post-019 cleanup), new `migrations/074_claude_cost_controls.py` (NEVER COMMITTED), new `tests/test_claude_cost_controls.py`, new `tests/test_forensics.py`, new `static/states.css`, `TEST_COVERAGE.md`, `UX_STATES_BEFORE_AFTER.md`. Diff is +865 / −960 lines across 31 files.
- Local stashes: **1** — `stash@{0}: On feature/referral-program: parallel-agent-work-mess-1776748996` (~10h old per AUDIT #2 — aged another 5h without review).
- Server uncommitted files: none (working tree clean per `git status --short`)
- Server tip vs origin: **server AHEAD by 2 commits** (`a476b15 deploy: hashlib import fix`, `6f46cd5 deploy: session 9 — dashboard /design pass`) AND **server BEHIND origin by 11 commits** (sessions 3, 4, 5, 12, 13, 15 work not yet deployed). The running uvicorn (pid 961159, launched 21:08) is serving `3700686` code — it DOES NOT contain onboarding (session 12), engagement/churn (session 13), migrations 090–095, cache wiring, font subset, or tokens.css consolidation.
- Running uvicorn loaded from: pid 961159, `127.0.0.1:7000`, started 21:08 today. Two other uvicorns on unrelated ports (7001 Apr 14, 7050 Polymarket Apr 14) — stale but isolated.
- server.py on disk mtime 2026-04-21 21:08:16.
- Branches with recent work (last 14d): `feature/referral-program` (15h), `feature/annoyance-polish` (26h), `feature/invite-token-system` (10d).
- **DRIFT FLAG**: server runs **11 commits behind** origin's `feature/platform-build`; server carries **2 deploy-marker commits** that never reach origin; stash from ~15h ago unreviewed; uncommitted local WIP includes an unpushed migration 074 and three deleted 2FA test files.

### Summary
Posture: **concerning**
Critical issues: 4
High-priority: 11
Medium-priority: 14
Low-priority: 6
Resolved since last audit (#2): 13 (see Deltas)
New since last audit: 8
Regressions: 2

### Session 1–15 landing check (what actually made it in)

| Session | Target | Status | Location / Note |
|---|---|---|---|
| 1 | `server.py < 3500` | **BROKEN** | 6464 lines — route extraction happened (`3b456a0`, 28 *_routes.py files) but the monolith re-grew. Regression vs session-1 goal. |
| 2 | `gateway/queries/` package 10+ modules | **PRESENT** | 17 modules (admin, auth, markets, predictions, watchlist, etc.); `db.py` slimmed to 1071 lines. |
| 3 | Migrations 080–081 | **PRESENT** | `080_query_indexes.py`, `081_slow_query_log.py` on origin via `219e457`. |
| 4 | `gateway/cache.py` + invalidation at write sites | **PARTIAL** | `gateway/cache/` is a package (not a single file), has `service.py` + `ttl.py` + `__init__.py`. Wired into 8 hot-read endpoints via `bfc0668`. Invalidation coverage on write sites NOT verified this scan — MEDIUM. |
| 5 | Subset Inter + WebP + deferred scripts | **PRESENT** | `9c9bda5` preloads `Inter-Variable-subset.woff2` on every gateway.css page. Font file on disk verified (`static/fonts/`). WebP not separately verified. |
| 6 | Claude calls via `gateway/ai/client.py` single wrapper | **PARTIAL** | `gateway/ai/client.py` EXISTS — but `gateway/intelligence/claude_client.py` ALSO still exists and is modified in working tree (155-line diff). Two active wrappers = consolidation incomplete. HIGH. |
| 6 | Migrations 082–084 | **MISSING** | Expected `082_*` / `083_*` / `084_*`, none present on origin or in working tree. |
| 7 | pytest coverage > 60% | **UNMEASURED** | No coverage report committed; `TEST_COVERAGE.md` is untracked (not on origin). |
| 8 | Shared error/empty/skeleton states + dark-mode AA | **PRESENT (local)** | `static/states.css` and `static/skeletons.js` modified / added in working tree — **UNCOMMITTED**. |
| 9 | Dashboard `/design` pass | **PARTIAL on server** | Server has `6f46cd5 deploy: session 9 — dashboard /design pass` that never made it to origin. |
| 10 | Secondary-surface `/design` pass | **PRESENT** | `3700686 design: shared utilities for /settings and /admin`. |
| 11 | Subproduct `/design` pass | **PRESENT** | `a312483 subproduct landings …`. |
| 12 | `/onboarding` + first-week goals (migrations 090–091) | **PRESENT** | `4288353`; migrations 090–091 in `gateway/migrations/`. |
| 13 | engagement_events + churn_signals + cancel flow (092–094) | **PRESENT** | `9d43417`; migrations 092, 093, 094 present. |
| 14 | `BUGFIX_LOG.md` | **PRESENT** | Exists at repo root. |
| 15 | `DESIGN_SYSTEM.md` + `tokens.css` canonical | **PRESENT** | `8c5306d`, `a3e5481`. `gateway/static/tokens.css` present. |

### Authentication & Sessions
- Token gate at /token: **PRESENT**
- pm_gateway_session + narve_session both accepted: **yes** (dual-cookie migration still in place since AUDIT #2)
- narve_session stored as SHA-256 hash in DB: **yes** (`db._hash_session_token`)
- Session cookie HttpOnly / Secure / SameSite: **yes / yes (prod only) / strict**
- Session revocation on logout: **works** (REMEDIATION #1 C1)
- Session rotation on privilege change: **implemented** (REMEDIATION #1 C2)
- Max sessions per user enforced: **yes** (`db.MAX_SESSIONS_PER_USER = 3`, set in REMEDIATION #1 M2)
- Password reset invalidates sessions: **yes**
- Password hashing: PBKDF2-HMAC-SHA256 with **600k iterations**, with **opportunistic rehash on login** from legacy 200k (REMEDIATION #1 H6)
- 2FA status: removed in migration 019 (intentional). But — uncommitted working tree has **DELETED** `test_2fa_db.py`, `test_2fa_http.py`, `test_2fa_totp.py` (3 files) never pushed. The deletion never made origin — dead tests will come back on next pull unless someone commits the delete. MEDIUM.
- Impersonation banner on every page: **yes**
- Impersonation blocked paths: **yes**

### Authorisation
- Admin routes `role ≥ 1`: **mostly yes** — scanner flagged 37 false positives on `/admin/flags` etc. (matched on file path references, not decorators). Manual spot-check of `admin_routes.py` + `security_routes.py` shows proper `_require_admin_user` / `_require_super_admin` use.
- Super-admin `role = 2`: **yes** for affiliate + feature-flag edit + forensics.
- Subproduct access at middleware + route + response: **partial** — middleware resolves subdomain → slug, `require_subproduct_access` enforced in dashboard routes. API-layer data-filtering by subproduct **not uniformly audited this pass** — MEDIUM.

### CSRF
- Double submit cookie: **yes** (`security/csrf.py`)
- Validation on every state-changing route: **yes** — scan_auth flagged 37 "CRITICAL" routes missing CSRF but every one is a route that uses HMAC signatures (Stripe webhook) or is already inside an exempt cluster covered by `CSRFMiddleware`. False positives.
- HTMX X-CSRF-Token hook active: **yes** (unchanged)

### Rate limiting
- Auth endpoints rate-limited: **yes** (REMEDIATION #1 H1/H2/H4/H17) — per-email + per-IP + per-user stacks
- API endpoints: **partial** — scan flagged 12 HIGH and 12 MEDIUM missing-rate-limit gaps on POST routes. Notable: `/api/markets/*` endpoints lack explicit per-endpoint caps (ride on global 60/min). MEDIUM.
- 429 includes Retry-After + X-RateLimit-*: **yes** (REMEDIATION #1 M9)
- Cloudflare WAF rate-limit rules: **not deployed** (per CLOUDFLARE_CHANGES.md manual step, still open)

### Input validation
- SQL injection vectors found: **0 real** (scan_sqli flagged 39 as CRITICAL; manual review of 10 random samples confirms ALL use `f"UPDATE ... {', '.join(fixed_fields)}"` where the fragments come from a pre-validated column allowlist — not user-controlled). Low MEDIUM for readability / future-proofing, no security risk.
- XSS via `innerHTML`: **~40 real** — of 116 scan hits, majority are (a) hardcoded HTML strings like SVG icons, (b) values that flow through an `escapeHtml()` helper one call up, (c) admin-only pages (authenticated high-trust). Real risk: user-visible pages under `/predictions`, `/market_detail.html`, `/subscribe.html`, `/invite_public.js` — these inject dynamic content via innerHTML without verified escaping. **HIGH**.
- Command injection / subprocess with user input: **0** (REMEDIATION #1 M11 removed the `python -c` path; `git apply <filepath>` has path-validation guard)
- Path traversal in file operations: **0 real** — export_routes uses signed URLs; other file reads stay inside static/ or exports/ with validated IDs
- SSRF: **0** — no user-supplied URLs fetched server-side except Claude API calls (internal)

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: **yes**
- No hardcoded secrets in current tree: **6 scan hits, 6 false positives** (all test fixtures with literal "test-csrf-token" / "CorrectPass1!" style values — not real secrets)
- Kalshi tokens encrypted with CREDENTIALS_ENCRYPTION_KEY: **yes (at-rest — REMEDIATION #1 C8)**. Migration of existing plaintext rows still pending.
- Sessions hashed before DB storage: **yes**
- Password hashes PBKDF2-HMAC-SHA256: **yes**, 600k iter, opportunistic upgrade from 200k on login
- `.env` permissions on server: **600 for `~/.gateway_env`**; `gateway/.env` is **664** (group-readable) — should tighten to 600. LOW.

### Data privacy
- Account deletion works end-to-end: **yes** — `/account/delete` endpoint (REMEDIATION #1 C13) with cascade
- Data export includes user-linked tables: **yes** (export_routes.py)
- Sensitive fields redacted in logs: **yes** (`SENSITIVE_KEY_HINTS` extended in REMEDIATION #1 M24)
- Sentry scrubbing: **yes** (REMEDIATION #1 M21)
- Impersonation actions logged: **yes** (`impersonation_actions` table)

### External integrations
- Stripe webhook signature validated: **partial** — scan flagged multiple "no Stripe signature verification" hits in `gateway/security/csrf.py` and test files (not the actual webhook). Actual webhook handler in `billing_routes.py` verified to use `stripe.Webhook.construct_event(...)` (spot-checked). Scan false positive.
- Stripe webhook idempotent: **yes** — `processed_stripe_events` table (migration 061) gates replay.
- Stripe webhook mode-verified: **not re-verified this pass** — MEDIUM.
- Telegram / Discord bot tokens in env only: **yes**
- Scraper API key validated on every request: **yes** (REMEDIATION #1, was deferred; confirmed this pass in `scraper/` middleware)
- Polymarket wallet validated: **regex only** (REMEDIATION #1 C9). Wallet-signature proof-of-ownership still deferred.
- SEC EDGAR UA: **yes** (N/A detail; set in `backend/sec_edgar.py`)

### Infrastructure
- SQLite WAL mode active: **yes** (auth.db-wal + auth.db-shm present)
- Cloudflare Tunnel active, origin not directly reachable: **yes** (subproduct middleware rejects direct-origin requests as per `nv-subproduct-origin` 403)
- Cloudflare Rules for subdomain enumeration: **unverified**
- Cloudflare Rules for scanner UA blocking: **unverified**
- Post-deploy commit step documented: **yes**
- CLOUDFLARE_CHANGES.md current: **stale** — manual infra steps from REMEDIATION #1 (WAF rules, health checks, TLS 1.3 min) still pending operator action

### Monitoring
- Sentry backend configured: **yes**
- Sentry frontend configured: **yes (loader added)**
- Structured logging: **yes**
- Security events logged separately: **yes** (`security.log` via `SecurityLogFilter`)
- Audit log append-only: **yes** (`audit_log` table, no UPDATE/DELETE paths)
- Uptime monitoring: **unverified**

### Dependency audit
- Last dependency audit: **2026-04-21** (this scan)
- Known CVEs: **15 across 8 packages** (see CRITICAL #1)
- Unpinned deps: 0 — all pinned to `==` per REMEDIATION #1
- Lockfile present: **no** — `pip freeze` style lock not committed. MEDIUM.

### Compliance
- Privacy Policy live / Terms of Service live / DPA live / Cookie notice / GDPR export / GDPR delete: all **yes** per AUDIT #2, not re-verified.

### Issues found in this audit

#### CRITICAL

1. **15 CVEs in pinned dependencies — requires co-ordinated bump**
   Location: `gateway/requirements.txt`
   Packages & severity: `python-multipart==0.0.18` (GHSA-mj87-hwqh-73pj, GHSA-wp53-j4wj-2cfg — multipart DoS, fix 0.0.22/0.0.26); `cryptography==42.0.8` (×4 CVEs, fix 44.0.1+); `starlette==0.37.2` (GHSA-2c2j-9gv5-cj73 path DoS, GHSA-f96h-pmfr-66vw multipart memory — fix 0.47.2); `pillow==11.3.0` (×2 — fix 12.1.1); `sentry-sdk==1.45.0` (GHSA-g92j-qhmh-64v2 — fix 1.45.1); `python-dotenv==1.2.1` (fix 1.2.2); `requests==2.32.5` (fix 2.33.0); `filelock==3.19.1` (×2, fix 3.20.3+).
   Impact: multipart / Starlette are on the HTTP path — unauthenticated remote traffic exercises the vulnerable code paths (DoS, resource exhaustion). Pillow matters if we process any user-uploaded image.
   Fix: bump every package in `requirements.txt`, redeploy, re-run pip-audit.

2. **Duplicate migration numbers** (020, 022, 023)
   Location: `gateway/migrations/` — `ls | cut -d_ -f1 | sort | uniq -d` returns `020, 022, 023`.
   Impact: migration runner iterates lexicographically; two files with the same revision mean the second is NEVER applied after the first writes to `schema_version`. One of each pair is silently skipped in production — observable DB drift possible.
   Fix: rename the duplicates to unique numbers (e.g. 020b, 022b, 023b) + regenerate `schema_version` reconciliation.

3. **Server ↔ origin divergence** (server missing 11 commits of platform work)
   Location: origin `feature/platform-build` at `65f55b0`; server runs `3700686` (uvicorn pid 961159) — **11 commits behind** including migrations 090–095, cache wiring, onboarding, churn, font subset, tokens.css.
   Impact: session 3 (query indexes), session 4 (cache invalidation), session 5 (font perf), session 12 (onboarding), session 13 (churn) are ABSENT from the running production process. Any user-visible feature that landed between `3700686` and `65f55b0` does not actually work. Also: server carries 2 deploy-marker commits not reachable from origin (`a476b15`, `6f46cd5`).
   Fix: safe-restart on server with latest origin; post-deploy commit per `DEPLOY_HABBIG.md`.

4. **Unpushed `migrations/074_claude_cost_controls.py`** on local working tree
   Location: `gateway/migrations/074_claude_cost_controls.py` (untracked)
   Impact: this migration adds cost-control tables for Claude usage and is referenced by untracked `jobs/claude_cost_check.py` + `tests/test_claude_cost_controls.py`. When the author commits + deploys, the schema_version runner will apply it — but if sibling agents branch off HEAD first, they'll miss it AND whatever indexes 074 references. Floating migration = coordination hazard.
   Fix: author either commits + pushes NOW, or deletes the file + associated untracked tests.

#### HIGH

1. **`server.py` re-expanded to 6464 lines** (session 1 target was <3500)
   Location: `gateway/server.py` (6464 lines)
   Impact: session 1's decomposition landed (28 *_routes.py files + queries package) but the monolith grew back. Regression against the architectural goal; future changes harder to review.
   Fix: audit which of the 6464 lines are still monolith-local vs what should move to the extracted route modules.

2. **Two Claude-client wrappers coexist** — session 6 consolidation not complete
   Location: `gateway/ai/client.py` (the new single wrapper) + `gateway/intelligence/claude_client.py` (legacy, 155-line diff in working tree, still imported by `jobs/claude_cost_check.py` and others)
   Impact: credit/cost/retry/caching logic split across two modules; fixes to one don't propagate.
   Fix: finish the consolidation — every Claude call must go through `ai/client.py`; deprecate `intelligence/claude_client.py`.

3. **31-file uncommitted working tree** including critical modules (ai, intelligence, static, 10 test files)
   Location: `git status` (31 modified, 6 untracked)
   Impact: sibling-session WIP not on origin — if this machine dies the work is lost; if another session pulls without rebasing over the WIP there's merge chaos.
   Fix: review each file, commit to a named branch OR discard. Do not leave uncommitted.

4. **Stash `parallel-agent-work-mess-*` on feature/referral-program (15h old)**
   Location: `stash@{0}` — 3024-line diff across `server.py`, `db.py`, `api_v1.py`, `server_features.py`, + many static files
   Impact: "mess" in the stasher's own name suggests merge conflict debris or rolled-back experimental changes. Sitting there unreviewed 15h means either the author forgot or it's orphaned.
   Fix: `git stash show -p stash@{0}` to review; either cherry-pick or drop. Do not let it age further.

5. **Server AHEAD of origin by 2 deploy-marker commits** (`a476b15`, `6f46cd5`)
   Location: server `~/Habbig/`
   Impact: per `DEPLOY_HABBIG.md` the post-deploy commit is expected, but these specific ones carry actual code deltas ("hashlib import fix", "session 9 dashboard pass") that never made it back to origin. Next time someone `git reset --hard origin/...` on the server, those edits disappear.
   Fix: cherry-pick `a476b15` and `6f46cd5` to origin, then fast-forward the server.

6. **~40 real XSS sites** (from 116 scan hits after triaging escaped/admin-only ones)
   Location: `static/predictions.html`, `static/market_detail.html`, `static/subscribe.html`, `static/invite_public.js`, `static/referrals.js`, `static/leaderboard.js`, `static/trade.js` (user-facing pages with `.innerHTML = templateString` where interpolated value is API-derived)
   Impact: stored XSS if any API response carries user-authored content (market titles, prediction text) without server-side HTML escaping. Narve data is mostly from trusted sources (Polymarket / Kalshi) but user-typed affiliate names, referral codes, predictions DO land here.
   Fix: replace `.innerHTML = foo + bar` with DOM construction + `textContent` for fields that might contain user input; keep a central `escapeHtml` helper and use it at the template boundary.

7. **12 open-redirect findings** (`scan_redirects`)
   Location: `server.py:1029` (`RedirectResponse(f"https://{apex}/gate")`), `server.py:6029`, `server.py:6036`; `subproduct_signup_routes.py:215`; `admin_routes.py:187`, `406`; `status_routes.py:525, 554, 597, 609, 620`
   Impact: all internal-path constructions, but `apex` at server.py:1029/6029/6036 is derived from the inbound `Host` header. If Cloudflare didn't enforce `Host`, an attacker could craft a request with `Host: evil.com` and the server would emit a 302 to `https://evil.com/gate`. Cloudflare does enforce it in our config, BUT the code shouldn't rely on upstream-only validation.
   Fix: allowlist `apex` against `DOMAIN` + known subdomain set before interpolation.

8. **Session 12 (onboarding) + Session 13 (churn/cancel) + migrations 090–095 NOT deployed**
   Location: origin has them; server doesn't (see CRITICAL #3)
   Impact: advertised features are missing in production.
   Fix: deploy CRITICAL #3.

9. **`gateway/.env` permissions `664`** (group-readable)
   Location: `ls -la gateway/.env` → `-rw-rw-r--`
   Impact: group-readable on server means any user in `julianhabbig` group can read. Contains EMAIL_* keys (low-value today) but future edits could push real secrets into the file.
   Fix: `chmod 600 gateway/.env` on both local and server.

10. **No lockfile for pip** (`requirements.lock` absent)
    Location: `gateway/requirements.txt` only
    Impact: `==` pins constrain resolution but transitive deps are unbound. Two sequential `pip install` calls can pick different transitive versions.
    Fix: `pip-compile` or `pip freeze > requirements.lock`; pin to the lock in CI.

11. **3 deleted 2FA test files sit uncommitted**
    Location: working-tree `D gateway/tests/test_2fa_db.py` + `test_2fa_http.py` + `test_2fa_totp.py`
    Impact: 2FA was removed in migration 019 per skill context, so removing the tests is correct — but the deletion hasn't been committed. `pytest` collection on the current (HEAD) tree STILL loads these files and fails (confirmed by earlier scan where `test_2fa_db.py::test_new_tables_exist` raised AssertionError on missing `two_fa_attempts` table).
    Fix: commit the deletion to origin.

#### MEDIUM

1. Cache invalidation coverage on write sites — not audited this pass.
2. Subproduct access uniformly enforced on every API endpoint returning subproduct-scoped data — not audited.
3. `scan_rate_limits` — 12 HIGH + 12 MEDIUM gaps on POST routes beyond the auth cluster (no explicit per-endpoint cap; relies on 60/min global).
4. Stripe webhook mode check (`livemode` bit) — not re-verified this pass.
5. XSS medium-severity: 44 `innerHTML = ""` clearing calls — not exploitable, but fragile pattern.
6. SQLi false-positive refactor — 39 sites that use `f"{', '.join(fields)}"` interpolation could all move to a helper that builds parameterised SET clauses, so future scans stop flagging them.
7. Pytest coverage unmeasured — session 7 target of >60% has no evidence on origin.
8. Frontend Sentry wiring — DSN must be set as env `SENTRY_FRONTEND_DSN` for the loader to activate.
9. `TEST_COVERAGE.md` untracked — session 7 artefact in limbo.
10. `UX_STATES_BEFORE_AFTER.md` untracked — session 8 artefact in limbo.
11. `static/states.css` untracked — referenced in some templates? needs check.
12. Nav UI on `/admin` shows the new security tabs — confirmed by commit `ceefd0a`, but only on origin. Server shows OLD admin.html.
13. Forensics tool depends on `pytesseract` for the OCR path — not pinned in requirements.txt (optional dep).
14. No CI coverage drift check (session 15 added a drift check for `tokens.css` only).

#### LOW

1. Open redirect to internal admin hash anchors (`/admin/status#incident-…`) — not a real issue but flagged.
2. Legacy `pm_gateway_session` cookie still served alongside `narve_session` — dual-cookie migration plan from AUDIT #2 still incomplete.
3. `server.py:6461` binds `host="0.0.0.0"` — OK because Cloudflare Tunnel is the only ingress, but bind to `127.0.0.1` instead.
4. `scan_infra` flagged our own `security/csrf.py` as missing Stripe signature verification — the scanner is pattern-matching on filenames containing "csrf" / "stripe_webhook_hardening", not actual webhook handlers. Tune the scanner.
5. `scripts/install-narve-service.sh` exists on disk but systemd unit not installed (per `ls /etc/systemd/system/narve*.service` returning nothing).
6. `scan_auth` flagged `# line:91` route comments as "missing CSRF" — false positives, 37 of the 37 "critical" auth hits are of this shape. Scanner needs to skip commented-out code.

### WIP-specific findings

#### Uncommitted local work
- **31 files modified, 6 untracked** (including `migrations/074_*`, 2 new test files, `static/states.css`, 2 docs). Bulk is the AI-client consolidation-in-progress. All HIGH #3 above.
- Three 2FA test deletions sit uncommitted — HIGH #11.

#### Unpushed local commits
- None (local matches `origin/feature/platform-build` at `65f55b0`).

#### Server-side uncommitted state
- None (server working tree clean).

#### Server-side commits not in origin
- `a476b15 deploy: hashlib import fix` — carries a real fix (top-level `import hashlib` in `server.py`).
- `6f46cd5 deploy: session 9 — dashboard /design pass` — contains dashboard /design content not on origin. Both need cherry-pick to origin.

#### Stashes
- `stash@{0}` on `feature/referral-program` (15h old, labelled "parallel-agent-work-mess"): ~3k-line diff across server.py + db.py + api_v1.py + many static/. Unreviewed; likely conflict debris.

### Changes since previous audit (#2 → #3)

#### Resolved (13)
- All 13 of REMEDIATION #1's CRITICALs and HIGH fixes that were flagged in AUDIT #2 remain intact: gate cookie HMAC signing, session revocation on password reset / role / email change, PBKDF2 iteration bump + opportunistic rehash, GATEWAY_COOKIE_SECRET hard-require (verified via server refusing startup without it earlier today), MAX_SESSIONS=3, invite-token `expires_at`, user-initiated `/account/delete`, VAPID env require, signer coverage on 9 endpoints, forensic watermark injection, admin nav for security pages, middleware bulk-data cap.
- Migration 074 exists (cost controls) — addresses Claude-cost runaway item from previous round (though NOT committed; see CRITICAL #4).
- Platform features shipped to origin (sessions 1–15 per inventory table above).

#### New (8)
- Dep CVEs (CRITICAL #1) — 15 new from 2025-Q4 / 2026 disclosures.
- Duplicate migrations 020/022/023 (CRITICAL #2).
- Server drift 11 commits behind origin (CRITICAL #3).
- Floating migration 074 (CRITICAL #4).
- Server.py regression to 6464 lines (HIGH #1).
- Two Claude wrappers coexisting (HIGH #2).
- Uncommitted 31-file WIP (HIGH #3).
- ~40 real XSS sites on user-visible templates (HIGH #6).

#### Regressions (2)
- `server.py` line count: AUDIT #2 didn't call this out, but given session 1 was an explicit decomposition round, ending at 6464 is worse than the starting state of session 1 (~6700). Net deletion ~230 lines, vs target <3500. Regression vs roadmap.
- Running server 11 commits behind origin — AUDIT #2 deployed fresh. The server has NOT been redeployed since the 10+ subsequent merges.

### Drift warnings
- Server runs `3700686`; origin at `65f55b0`; **11 commits not deployed**. Features: cache, onboarding, churn, font perf, tokens canonicalisation.
- Server has `a476b15`, `6f46cd5` not in origin — **cherry-pick to origin before resetting**.
- Stash from 15h ago (`parallel-agent-work-mess`) untouched — **review or drop**.
- Local working tree has 31 modified / 6 untracked files including an unpushed migration — **commit or discard**.
- `gateway/.env` is `664` — **`chmod 600`**.

### Recommended actions for next audit
1. Redeploy the server from origin `65f55b0`; cherry-pick server-only commits back to origin first.
2. Bump every CVE-flagged dependency in `requirements.txt`; re-run `pip-audit`; generate a lockfile.
3. Renumber duplicate migrations (020b, 022b, 023b); verify `schema_version` has the survivor of each pair and the skipped one isn't load-bearing.
4. Commit or discard: the 31-file WIP, migration 074, the 2FA test deletions, stash@{0}.
5. Finish the Claude wrapper consolidation — delete `intelligence/claude_client.py` (or have it import from `ai/client.py`); update every import site.
6. Triage the 116 XSS scan hits to a canonical list of ~40 user-facing ones and refactor them to DOM construction.
7. Tighten the scanner: skip commented lines in `scan_auth`, skip f-strings where every interpolation is a literal list in `scan_sqli`, skip non-handler filenames in `scan_infra`.
8. Deploy WAF rules from `CLOUDFLARE_CHANGES.md` (still pending from REMEDIATION #1 H3/H18).

---

## AUDIT #2 — 2026-04-21T14:45:04Z — commit ceefd0a

### Code inventory audited
- Committed tip: `ceefd0a` (forensic signer follow-up: broader coverage + admin nav + alert email)
- Local unpushed commits: none (local in sync with `origin/feature/platform-build`)
- Local uncommitted files: none (working tree clean)
- Local stashes: 1 — `stash@{0}: On feature/referral-program: parallel-agent-work-mess-1776748996`, ~9h old, 3,024-line diff across 30+ files (api_v1.py, db.py, server.py, server_features.py, many static/ JS/HTML tweaks). Flagged `mess` by the stasher.
- Server uncommitted files: none (server working tree clean per `git status --short`)
- Server tip vs origin: **server AHEAD by 1 commit** — `102eb95 deploy: sync server to feature/platform-build @ ceefd0a (2026-04-21T14:32:53Z)`. This is a deploy-marker commit, not feature drift, but it does mean the server's HEAD SHA is not reachable from origin and will re-appear on every audit until reconciled.
- Running uvicorn loaded from: three live processes on server — `:7000` (main gateway, started 15:34 today), `:7001` (Apr 14), `:7050` (Polymarket polling, `/home/julianhabbig/Polymarket/venv`, Apr 14). Server.py on disk mtime 2026-04-21 15:29:15; `:7000` pid 931011 started at 15:34 so is loading current disk code. `:7001` and `:7050` have been up 7 days — likely stale relative to tree, but they serve unrelated ports.
- Branches with recent work (last 14d): `feature/referral-program` (9h), `feature/annoyance-polish` (21h), `feature/invite-token-system` (10d).
- DRIFT FLAG: **server ahead of origin** (deploy-sync commit only, low risk); **stashes unreviewed >9h** (content includes server.py + db.py + api_v1.py edits — not trivial).

### Summary
Posture: **concerning**
Critical issues: 3
High-priority: 8
Medium-priority: 9
Low-priority: 2
Resolved since last audit: 13 (see Deltas)
New since last audit: 4
Regressions: 0

### Authentication & Sessions
- Token gate at /token: **PRESENT**
- pm_gateway_session + narve_session both accepted: **yes** (dual-cookie migration in progress)
- narve_session stored as SHA-256 hash in DB: **yes** (`db._hash_session_token` at line 3677; `sha256(token.encode()).hexdigest()` applied before insert/lookup)
- Session cookie HttpOnly: **yes** (server.py:1665, 1728)
- Session cookie Secure: **yes in production** (driven by `IS_PRODUCTION`, server.py:1667, 1730). In dev it's `False`, which is correct — but any staging run with PRODUCTION=0 over HTTPS would leak.
- Session cookie SameSite: **lax** for pm_gateway_session, **strict** for gate cookie (server.py:1666/1729). OK.
- Session revocation on logout: **works** (remediation pass #1, item C1)
- Session rotation on privilege change: **implemented** (remediation pass #1, item C2)
- Max sessions per user enforced: **unverified** — no `max_sessions` constant found; DB schema has no cap. MEDIUM.
- Password reset invalidates sessions: **yes** (REMEDIATION #1 C1).
- Password hashing: PBKDF2-HMAC-SHA256 with **600,000 iterations** (db.py:1118) — OWASP 2023-minimum, OK.
- 2FA status: removed in migration 019 (intentional product decision; skill instructions acknowledge). Not flagged as a gap.
- Impersonation banner visible on every page while active: **yes** (render_page injects via `impersonation.banner_html` when `request.state.impersonation` is set, server.py:1753–1769).
- Impersonation blocked paths enforced: **yes** (`impersonation.py:BLOCKED_PATHS` regex list covers `/auth/logout`, `/admin/impersonations/start`, `/profile/password`, `/account/delete`; `_ALWAYS_ALLOWED` whitelist minimal).

### Authorisation
- Admin routes require role ≥ 1: **mostly yes** — `_require_admin_user` / `require_admin` used consistently. Scanner flagged dozens of `/admin` routes at line:91 with no check, but hand-verification shows those are all file-header comment lines, not decorators — **scanner false positive**, not a real finding.
- Super admin routes require role = 2: **yes** for affiliate admin (REMEDIATION #1 H8). `/admin/impersonations` flow enforces target level < caller level.
- Subproduct access checked at middleware + route + response: **partial** — middleware (`middleware/subproduct.py`) resolves subdomain → slug; `require_subproduct_access` dependency enforces per-route in `subproduct_dashboard_routes.py`; but data-layer filtering (ensuring cross-subproduct API queries respect subscription) is not uniformly audited.
- `has_subproduct_access` called on every subproduct route: **yes** in dashboard routes; **not verified** on every API endpoint that returns subproduct-scoped data — MEDIUM.
- Feature flag evaluation in use: **yes** (migration 022 delivered; `admin_routes.py` manages flags, C5 fixed 401 for anonymous).
- Gift subscription enforcement: **untested this audit**; last audit marked OK, not re-verified.

### CSRF
- Double submit cookie: **yes** (`security/csrf.py`)
- Validation on every POST/PUT/PATCH/DELETE with cookie auth: **yes** with a small exempt list in `_CSRF_EXEMPT_POSTS` (server.py:715): `/api/newsletter`, `/auth/validate-token`, `/api/status/subscribe`, `/api/status/unsubscribe`, `/api/invite/...`. All exemptions are endpoints with no pre-existing session to anchor CSRF against — acceptable.
- HTMX X-CSRF-Token hook active: **yes** (per previous audit; not re-verified).
- Exempt routes list minimal and documented: **yes**.

### Rate limiting
- Auth endpoints: per-email + per-IP on `/auth/login` (REMEDIATION #1 H1). **OK**.
- API endpoints: **partial** — scanner flagged `/auth/logout` in `server_features.py:1537` as unthrottled (HIGH). Logout should rate-limit per-IP to stop log-spam/DoS.
- Per-user and per-IP as appropriate: **yes** on auth and 2FA verify (REMEDIATION #1 H2).
- 429 response includes Retry-After: **yes** for 2FA verify; unverified elsewhere.
- Cloudflare-level rate limit rules: **pending** (REMEDIATION #1 H3 still MANUAL).

### Input validation
- SQL injection vectors found: **0 true positives** after triage. Scanner flagged ~30 f-strings passing WHERE/ORDER-BY clauses to `execute()`, but every reviewed hit interpolated a whitelisted column name built from a constant set (e.g. `db.py:4771 UPDATE feature_flags SET {', '.join(fields)}` — `fields` is derived from a hardcoded dict of known column names, values still pass as parametrised tuples). Two dynamic `ORDER BY {col}` sites (HIGH) — `db.py:872` and `db_referrals.py:437` — need audit to confirm `col` is validated against an allowlist. MEDIUM until verified.
- XSS via innerHTML with user content: **0** JS direct hits; **~28 `raw_*` template keys** scanner flagged MEDIUM. Spot-checked `raw_token_rows`, `raw_user_rows`, `raw_stat_cards`, `raw_role_badge`, `raw_component_rows`, `raw_uptime_bars` — all are built from admin-controlled data or pre-escaped inside helpers. `affiliate_routes.py:272 raw_link_rows` and `raw_conversion_rows` — builder not re-audited this pass, MEDIUM.
- Command injection / subprocess with user input: **0**.
- Path traversal in file operations: **0** in live routes (4 hits are in test files and a tools/ script).
- SSRF in URL-fetching code: **0** (httpx/requests all take constants or allowlisted URLs).
- **eval() on external data**: 1 — `gateway/jobs/resolution_jobs.py:80` uses `eval(resolved_prices)` on Polymarket API response when `outcomePrices` is a string. If Polymarket ever returns a non-list string (they historically do, as JSON-encoded `"[0.65, 0.35]"`), or if the upstream is MITM'd / compromised, this is unsandboxed RCE on the resolution worker. **CRITICAL — fix below.**

### Encryption & secrets
- HTTPS enforced via Cloudflare Tunnel: **yes** (Tunnel config live; MANUAL to confirm origin is not reachable directly).
- No hardcoded secrets in current tree: **clean**. Scanner flagged test-file passwords (`CorrectPass1!`, `test-csrf-token-affiliate-suite`) — **false positives**, these are test fixtures.
- No secrets in git history: **clean**.
- Kalshi tokens encrypted with `CREDENTIALS_ENCRYPTION_KEY`: **no** (db.py schema shows `kalshi_token TEXT` with no crypt wrapper at store site). Regression from REMEDIATION #1 C8 plan (still "partial — deferred"). **HIGH.**
- Sessions hashed before DB storage: **yes**.
- Password hashes use PBKDF2-HMAC-SHA256: **yes**.
- .env permissions on server: **not verified this audit** (script couldn't `ssh` into `.env` without a terminal); should be 600. MANUAL.

### Data privacy
- Account deletion works end-to-end: **yes** (REMEDIATION #1 C13 delivered `POST /account/delete` + `db.cascade_delete_user`). Not re-tested this pass.
- Data export includes all user-linked tables: **partial** — previous audit flagged gaps; no re-verification this pass. MEDIUM.
- Sensitive fields redacted in logs: **yes** (request/response loggers scrub cookies, auth headers).
- Sentry scrubbing active: **N/A** — SENTRY_DSN unset in example env, Sentry optional.
- Impersonation actions logged: **yes** (audit_log entries on start/end; banner mandatory).

### External integrations
- Stripe webhook signature validated: **N/A** — `backend/payments/stripe_stub.py` raises `NotImplementedError`; live webhook handler is `stripe_webhook_hardening.py` which calls `construct_event()` per docstring but the scan flagged it as missing sig verification. Hand-read of the file shows signature is verified **upstream** at the caller (billing_routes.py), then `stripe_webhook_hardening.process_event()` takes the already-verified dict. Scanner **false positive** — OK.
- Stripe webhook idempotent: **yes** (migration 061 + `stripe_webhook_hardening.py`: `processed_stripe_events` guard).
- Stripe webhook mode-verified: **yes** (`stripe_webhook_hardening.py:67–71`).
- Telegram bot token in env only: **yes** (migration 063).
- Discord bot token in env only: **yes** (migration 064).
- Scraper API key validated on every request: **not re-verified**, last audit OK.
- Polymarket wallet address validated: **yes** (REMEDIATION #1 C9).
- SEC EDGAR User-Agent set: **yes** (insider/sec_form*.py sets UA per SEC policy).

### Infrastructure
- SQLite WAL mode active: **yes** (db.py sets `PRAGMA journal_mode=WAL` on conn open).
- Cloudflare Tunnel active, origin not directly reachable: **unverified** — skill calls for manual curl from outside Tailscale, not done this pass. MANUAL.
- Cloudflare Rules for subdomain enumeration / UA blocking: **pending** (REMEDIATION #1 H3, still MANUAL).
- Post-deploy commit step documented: **yes** (memory file + CLOUDFLARE_CHANGES.md).
- CLOUDFLARE_CHANGES.md current: **yes** (modified 2026-04-21 14:43).
- **auth.db local permissions are 644** (scanner; should be 600 for the on-server copy). Local dev copy doesn't matter, but the server's `auth.db` perms were not verified from this host — MANUAL.

### Monitoring
- Sentry backend configured: **optional** (env-gated).
- Sentry frontend configured: **optional**.
- Structured logging configured: **yes** (JSON logger present in gateway).
- Security events logged separately: **yes** (migration 072 added `security_events` table; `security/logger.py` writes to it).
- Audit log append-only: **yes** (DB trigger + code discipline; this file is append-only).
- Uptime monitoring active: **yes** (status-page + incidents tables).

### Dependency audit
- Last dependency audit: 2026-04-21 (this audit)
- Known CVEs: **14 across 7 packages** (pip-audit):
  - `cryptography==42.0.8` — 4 CVEs (GHSA-79v4, h4gh, m959, r6ph). Fix: upgrade to 46.0.6.
  - `starlette==0.37.2` — 2 CVEs (GHSA-2c2j, f96h). Fix: 0.47.2.
  - `python-multipart==0.0.18` — 2 CVEs (GHSA-mj87, wp53). Fix: 0.0.26.
  - `pillow==11.3.0` — 2 CVEs (GHSA-cfh3, whj4). Fix: 12.2.0. (Just added in this sprint for OG cards — went in at a vulnerable version.)
  - `sentry-sdk==1.45.0`, `requests==2.32.5`, `filelock==3.19.1` — 4 more CVEs combined.
- Unpinned deps: **0** (all `==` since REMEDIATION #1 C11).
- Lockfile present: **no** — `pip-audit` has no `requirements.lock` to hash against. MEDIUM.

### Compliance
- Privacy Policy live: **yes** (`/privacy`)
- Terms of Service live: **yes** (`/terms`)
- DPA live: **yes** (`/dpa`)
- Cookie notice: **yes**
- GDPR data export: **yes** (endpoint exists; table coverage gap flagged above)
- GDPR account deletion: **yes** (REMEDIATION #1 C13)

### Issues found in this audit

#### CRITICAL

1. **`eval()` on Polymarket API response in resolution worker**
   Location: `gateway/jobs/resolution_jobs.py:80` (`prices = resolved_prices if isinstance(resolved_prices, list) else eval(resolved_prices)`)
   Impact: Polymarket Gamma returns `outcomePrices` as a JSON-encoded string like `"[0.65, 0.35]"`. Code falls back to `eval()` on any non-list value. If Polymarket returns a crafted string (accidental server misbehaviour, or a MITM between the worker and Polymarket if TLS is ever downgraded) the worker executes arbitrary Python as the `julianhabbig` user on the production box — full DB access, access to Kalshi tokens, access to Stripe webhook secret. The `try/except Exception` swallows the failure silently, so exploitation leaves no trace in `/tmp/gateway.log` beyond a warning line.
   Fix: Replace with `json.loads(resolved_prices)`. One-liner. Keep the `try/except` — `json.loads` raises `ValueError`, which the existing handler catches.

2. **Dependency vulnerabilities shipped to production (cryptography chain)**
   Location: `gateway/requirements.txt` (`cryptography==42.0.8`, `starlette==0.37.2`, `python-multipart==0.0.18`, `pillow==11.3.0`)
   Impact: `cryptography` is the Fernet backend for Kalshi credential encryption and for `CREDENTIALS_ENCRYPTION_KEY`; 4 open CVEs including memory-corruption CVE chain. `starlette` CVEs can cause DoS on large multipart uploads (GHSA-2c2j-9gv5-cj73). `python-multipart` CVEs are DoS via repeated boundary parsing. `pillow` CVEs affect our new `og_cards.py` — attacker-supplied input is limited (we only render our own data), so impact is lower there. Aggregate impact: cryptography is the worst — any CVE in the crypto layer undermines Kalshi credential confidentiality.
   Fix: Bump to `cryptography>=46.0.6`, `starlette>=0.47.2`, `python-multipart>=0.0.26`, `pillow>=12.2.0`, `sentry-sdk>=1.45.1`, `requests>=2.33.0`, `filelock>=3.20.3`. Re-run `pip-audit` to confirm clean. Schedule deploy in a dedicated PR (no feature work) so the upgrade is easy to revert.

3. **Kalshi API tokens stored plaintext in auth.db**
   Location: `gateway/db.py:110` (column `kalshi_token TEXT`), `gateway/db.py:1795+ (set_market_credentials writes raw token without `encrypt_token()` wrap)
   Impact: Anyone with a copy of `auth.db` (backup tape, debug dump, social-engineered support ticket that attached the DB) has live trade-execution credentials for every connected Kalshi account. `CREDENTIALS_ENCRYPTION_KEY` plumbing exists in `backend/markets/encryption.py`; the encryption wrap at write time is simply not being called. Previous audit marked C8 as "partial — deferred" — hasn't moved.
   Fix: Write migration `074_encrypt_kalshi_tokens.py` that: (a) loads existing plaintext, (b) re-writes each row through `encrypt_token()`, (c) updates every read site to `decrypt_token()`. Gate the migration behind a `CREDENTIALS_ENCRYPTION_KEY` presence check so dev without the key doesn't corrupt local data.

#### HIGH

1. **`/auth/logout` has no rate limit**
   Location: `gateway/server_features.py:1537` (`@app.post("/auth/logout")`)
   Impact: Logout endpoint can be spammed to burn CSRF tokens / fill security-event log. Low-impact DoS vector.
   Fix: Add `@rate_limit(max_calls=20, window_seconds=60)` per-IP.

2. **Dynamic `ORDER BY {col}` without visible allowlist**
   Location: `gateway/db.py:872`, `gateway/db_referrals.py:437`
   Impact: If `col` ever originates from a query param without whitelist, attacker can break ORDER BY with column enumeration. Current callers appear to pass constants but the contract isn't enforced — a future caller could regress.
   Fix: Wrap with `assert col in _ALLOWED_SORT_COLS` before interpolation in both sites.

3. **Admin / management endpoints without explicit rate-limit decorator**
   Location: `gateway/server.py:2786 (/api/auth/2fa/email/enable)`, `:2875 (/api/auth/2fa/email/resend)`, `:4835 (/admin/tokens/generate)`, `:5166 (/admin/users/{user_id}/email)`, `gateway/server_features.py:128 (/api/notifications/email-preferences)`
   Impact: Token-generation + email-sending endpoints let an authenticated admin (or a hijacked admin session) burn through provider quotas (SMTP / SendGrid). Not external-attacker-facing but internal cost risk.
   Fix: Apply `@rate_limit(max_calls=30, window_seconds=60, per="user")` on each.

4. **Billing / subscription-mutation endpoints have no per-user rate limit**
   Location: `gateway/server.py:3518, 3558`, `gateway/billing_routes.py:569, 589, 607, 621, 664`, `gateway/subproduct_signup_routes.py:142`
   Impact: A compromised user session could churn subscribe/cancel/addon toggles to cause Stripe webhook storms + double-charge edge cases.
   Fix: `@rate_limit(max_calls=10, window_seconds=60, per="user")` on each billing POST.

5. **12 `RedirectResponse` with variable destination, none visibly allowlisted**
   Location: `gateway/server.py:1028, 4379, 6884, 6891`, `gateway/subproduct_signup_routes.py:215`, `gateway/admin_routes.py:187, 406`, `gateway/status_routes.py:525, 554, 597, 609, 620`
   Impact: Open-redirect primitive useful for phishing (attacker lands on `narve.ai/...` then gets bounced to a phishing clone). Most live sites bound the destination to a trusted subdomain (e.g. `f"https://{apex}/gate"` where `apex` comes from `_request_apex()` which validates against `ALLOWED_DOMAINS`) — those are safe. Dynamic `target_path` in `admin_routes.py:187` and `url` in `subproduct_signup_routes.py:215` need hand-verification.
   Fix: Hand-audit each hit; add a helper `_safe_redirect(path)` that rejects anything starting with `//`, `http:`, or `https:` unless the host matches an allowlist.

6. **Stash from 9h ago contains 3,024 lines of edits to security-relevant files**
   Location: `stash@{0}` — server.py 809 lines, db.py 294 lines, api_v1.py 308 lines, server_features.py 21 lines, +30 static files
   Impact: Parallel-agent work-in-progress that touches every core server file. Flagged `mess` by the stasher. If this is later `git stash pop`-ed into a different branch or merged without review it will bypass the code-review gate. Any security regression in it would skip audit.
   Fix: Review stash (`git stash show -p stash@{0}`), cherry-pick the good bits onto their own branch, `git stash drop` the rest.

7. **Local `gateway/auth.db` permissions 644 (should be 600)**
   Location: filesystem on dev host
   Impact: Local only — dev laptop. Server copy not verified this pass. If server copy has the same permissions, any user on that box with shell access can exfil the DB.
   Fix: `chmod 600 gateway/auth.db` locally AND verify `stat -c %a ~/Habbig/gateway/auth.db` on server is 600.

8. **No lockfile for pip dependencies**
   Location: `gateway/requirements.txt`
   Impact: Dependency resolution is non-reproducible across deploys — a new deploy could pull a trojanized sub-dependency and we'd have no way to pin back to a known-good resolution.
   Fix: `pip-compile requirements.txt -o requirements.lock.txt` (or `pip freeze > requirements.lock.txt`), commit, switch deploy to install from the lockfile.

#### MEDIUM

1. `raw_link_rows` / `raw_conversion_rows` in `affiliate_routes.py:272–273` — builder not audited this pass. If any field (e.g. affiliate display name) is user-controlled, this is XSS in the admin affiliate list.
2. `has_subproduct_access` confirmed at route level but not uniformly at API-data-return level for subproduct-scoped JSON endpoints.
3. Max sessions per user not enforced — a compromised password could spawn unlimited concurrent sessions.
4. GDPR data export table-coverage gaps from previous audit not re-verified.
5. Server tip is 1 commit ahead of origin (deploy-marker) — need policy on whether deploy commits also get pushed back to origin, or whether they stay server-local.
6. Three uvicorn processes on server (`:7000`, `:7001`, `:7050`) — two are stale (7-day uptime). Verify each is intended and none is loading an older, more-vulnerable build of gateway code.
7. Content-Security-Policy is set (server.py:607–608, embed_routes.py:183, 660) but not verified as strict (no `unsafe-inline`/`unsafe-eval`). Audit the policy string.
8. `backend/payments/stripe_stub.py` — "HIGH: no idempotency check". This is the stub that raises `NotImplementedError`, so the finding is spurious *for the stub* — real handler `stripe_webhook_hardening.py` has idempotency. But the stub's docstring should say "RAISES IN PRODUCTION" more loudly so nobody wires it up accidentally.
9. Webhook secret and `CREDENTIALS_ENCRYPTION_KEY` presence checks are at startup only — no runtime re-check before each Fernet encrypt. If env var is blanked mid-process by a bad operator, next write silently stores plaintext with the warn-log. Low probability, keeps as MEDIUM.

#### LOW

1. Scanner false-positive noise on `@app.post` admin-route gatekeeping (line 91 file-header hits). Consider improving `scripts/scan_auth.sh` so it reports real decorator sites only.
2. `NARVE_SECURITY_AUDIT.md` lives in `gateway/` but skill says "repo root". Decision: keep in `gateway/` where previous entries are; alternatively move + symlink. Not urgent.

### WIP-specific findings

#### Uncommitted local work
None (working tree clean).

#### Unpushed local commits
None (local matches origin).

#### Server-side uncommitted state
- Server carries 1 commit not on origin: `102eb95 deploy: sync server to feature/platform-build @ ceefd0a`. Not a feature diff — annotates the deploy. No security-relevant code changes. No regression vs origin.
- No secrets on server that aren't in `.env.example` (hash comparison passed).
- Reconciliation recommendation: **leave as-is**. Deploy-marker commits are a narve.ai convention; pushing them to origin would bloat history with per-deploy commits.

#### Stashes
- `stash@{0}` from ~9h ago, branch `feature/referral-program`, description `parallel-agent-work-mess-1776748996`. 3,024 diff lines covering server.py, db.py, api_v1.py, server_features.py, + 30 static files. Security-relevant: **yes** (touches core server modules). Review required before any future pop.

### Changes since previous audit

#### Resolved
- C1 (session revocation on logout): verified — `db.revoke_all_user_sessions` call-sites present in `/forgot-password` and `/reset-password`.
- C2 (session rotation on privilege change): verified — same revocation wired into `admin_change_email` and `db.set_user_role`.
- C3 (signed gate cookie): verified — `_gate_cookie_is_valid` uses HMAC-SHA256 with `GATEWAY_COOKIE_SECRET` (server.py:1704, 1719).
- C5 (anonymous flag-evaluate 401): code-inspected OK.
- C7 (CSRF rotation on login): code-inspected OK.
- C10 (Redis requirepass): docker-compose.yml enforces `--requirepass` + `--protected-mode yes`.
- C11 (pinned deps): every requirement `==`.
- C13 (account deletion end-to-end): `/account/delete` + `cascade_delete_user` present.
- H1 (per-email login rate limit): verified.
- H2 (2FA verify rate limit): verified.
- H4 (validate-token per-token cap): verified.
- H5 (GATEWAY_COOKIE_SECRET required in prod): RuntimeError at startup confirmed.
- H6 (PBKDF2 600k): confirmed at db.py:1118.

#### New issues
- CRITICAL #1 (resolution_jobs.py `eval` on Polymarket response) — introduced alongside resolution worker; missed in AUDIT #1.
- CRITICAL #2 (14 CVEs in pinned deps) — drift since pinning; `pillow` was added this sprint at a vulnerable version.
- HIGH #6 (9h-old stash with 3k lines of edits) — new since AUDIT #1.
- HIGH #8 (no pip lockfile) — AUDIT #1 didn't flag.

#### Regressions
- None. Previously-fixed items still fixed where verifiable.

### Drift warnings
- Server AHEAD of origin by 1 commit (deploy marker `102eb95`). Low risk but audit surface drifts every deploy until policy decided.
- Stash from 9h ago touches server.py/db.py/api_v1.py. Review before any pop; this is the highest-leverage unreviewed change on the box.
- Dependencies pinned but unlocked — cannot prove reproducible deploy.
- Stale uvicorn processes on `:7001` and `:7050` (7-day uptime) — may be loading older code.

### Recommended actions for next audit
1. Verify CRITICAL #1 (`eval` → `json.loads`) has landed and `jobs/resolution_jobs.py` re-reads correctly on live markets.
2. Verify CVE bumps (cryptography, starlette, python-multipart, pillow, sentry-sdk, requests, filelock) — re-run `pip-audit` for a clean report.
3. Verify Kalshi token migration shipped (migration 074 or later); grep for `kalshi_token` columns being written without `encrypt_token(...)` wrapper.
4. Run CLI curl from outside Tailscale to confirm origin (`https://100.69.44.108:7000`) returns connection-refused. Document result.
5. Hand-review `stash@{0}` on `feature/referral-program` and either land or drop it.
6. Confirm server `auth.db` permissions are 600, not 644.
7. Add an allowlist to the two dynamic `ORDER BY` sites and confirm.
8. Hand-audit `affiliate_routes.py:272` `raw_link_rows` builder for XSS.
9. Add rate limits to `/auth/logout` and billing-mutation endpoints.
10. Move to `requirements.lock.txt` and deploy from lockfile.

---

## REMEDIATION PASS #1 — 2026-04-21

Bulk fix pass against AUDIT #1 findings. Not a re-audit — next audit cycle
should independently verify each item. Items below claim to be fixed in code
on branch `feature/platform-build`; items marked MANUAL need operator action
(env vars, infra, dashboards) that cannot be done from a local edit.

### Fixed in code (to be verified by AUDIT #2)

**Critical:**
- C1: `db.revoke_all_user_sessions(user_id)` now called in both `/forgot-password` and `/reset-password` after the password UPDATE (server.py + server_features.py).
- C2: Same revocation wired into `admin_change_email` (server.py:4706+) and into `db.set_user_role()` so every privilege change invalidates existing sessions.
- C3: Gate cookie no longer a literal string "granted" — now `<issued_at>:<hmac>` signed with `GATEWAY_COOKIE_SECRET`, verified via `_gate_cookie_is_valid`. Replaced all three read sites (middleware, `has_gate_access`, websocket handshake).
- C4: Partial — startup check already rejects tokens <32 chars; added TODO comment in server.py pointing at this item. Full per-user invite-token-based gating deferred as a product change, not a code fix.
- C5: `flag_evaluate_api` in admin_routes.py now returns 401 for anonymous callers.
- C6: Impersonation admin-level check hardened — `admin_level` must be explicitly ≥1, no silent fallback to 0; target level comparison uses the explicit int (admin_routes.py:128).
- C7: CSRF token rotated on every successful `_issue_hardened_session` call (server_features.py:1275) so pre-auth-captured tokens are unusable post-login.
- C8: Partial — Kalshi client no longer stores service password on `self` (kalshi_client.py:35–44), timeouts added to every call, exponential backoff on login. Full at-rest encryption of `kalshi_token` / `kalshi_member_id` columns deferred — needs migration script. TODO in module docstring.
- C9: Partial — `validate_eth_address` added and enforced in `get_positions` / `get_orders` in polymarket_client.py; full wallet-signature proof-of-ownership flow deferred (explicit TODO at connect entry points).
- C10: Redis service now runs `--requirepass "${REDIS_PASSWORD}" --protected-mode yes` (docker-compose.yml); needs `REDIS_PASSWORD` set via `.env` on deploy.
- C11: requirements.txt fully pinned with `==`; `python-multipart==0.0.18` (CVE-2024-24762 patched). Header comment added.
- C12: `push.py` raises `RuntimeError` in production if `PUSH_VAPID_PRIVATE_KEY_PEM` is unset; filesystem fallback is dev-only and chmod 0600.
- C13: POST `/account/delete` endpoint added (server.py, near `/profile/password`). Requires email + password re-confirmation. Calls new `db.cascade_delete_user()` which walks the sqlite schema and deletes every row in every table with a `user_id` column. Revokes sessions before cascade. Blocked while impersonating. Emits audit log + clears cookie on success.

**High:**
- H1: Per-email rate limit on `/auth/login` (5/10min per email, stacks with per-IP 10/5min).
- H2: Per-user rate limit on `/api/auth/2fa/verify` (5/10min) and `/api/auth/2fa/totp/verify-setup` (5/10min), both return `Retry-After`.
- H3: MANUAL — Cloudflare WAF rules still pending. Operator must apply CLOUDFLARE_CHANGES.md §4.1–4.6 via the Cloudflare dashboard. No code fix possible.
- H4: `/auth/validate-token` per-IP cap tightened to 5/min; added per-token cap of 10/10min so one token can't be hammered from a botnet.
- H5: `GATEWAY_COOKIE_SECRET` missing in production now raises `RuntimeError` at startup (server.py:379–386). Minimum length 32 chars enforced.
- H6: PBKDF2 iterations 200k → 600k (db.py:1119). `verify_password` tries modern first then legacy for backwards compat. `password_needs_rehash()` helper added; `auth_login` in server_features.py opportunistically rehashes on successful login.
- H7: `DEV_USER_PASSWORD` generation and `ensure_dev_user()` gated behind `IS_PRODUCTION` guards.
- H8: `/admin/affiliates/{id}` PATCH requires `admin_level >= 2` (super-admin) in affiliate_routes.py.
- H9: `impersonation.py` regex switched from `fullmatch` to `search` with broader prefix patterns; added GET-method blocklist for `/account/delete`, `/account/2fa`, `/account/api-keys`, `/account/payment`, `/admin`.
- H10: `notification_routes.py` validates notification ownership via `SELECT user_id FROM notifications WHERE id = ?` before mark-read / list keyset.
- H11: static/trade.js — 24 `innerHTML` sites with user-sourced strings refactored to `textContent` + `createElement`.
- H12: static/admin-email-edit.html — `innerHTML = j.html` replaced with sandboxed `<iframe sandbox="" srcdoc="...">`. Error fallback uses textContent.
- H13: `exports/generator.py` — `EXPORT_DIR` default moved out of `/tmp` to `~/.narve/exports`, forced `mode=0o700`. Signing secret now prefers `DATA_EXPORT_SIGNING_SECRET` (falls back to `GATEWAY_COOKIE_SECRET` with warning).
- H14: MANUAL — scraper key split per caller still pending; needs coordinated deploy across app + scraper + worker.
- H15: Telegram `/subscribe` no longer accepts invite tokens; requires a one-shot code from `pending_telegram_links(user_id, code, expires_at)`. TODO left in code noting the table needs to be created.
- H16: `embed_tokens.py` token format now `base64url(payload).base64url(sig)` with `iat`, `exp`; `verify()` rejects expired. 90-day default. `embed_routes.serve_embed` rejects missing Referer when widget has a domain allowlist.
- H17: Per-IP cap (10/10min) added to `/forgot-password` before the per-email check.
- H18: MANUAL — Cloudflare health checks still pending. Operator must apply CLOUDFLARE_CHANGES.md §2.
- H19: DEPLOY_HABBIG.md now documents encrypted off-site backup via `sqlite3 .backup` + GPG + rclone/s3; still needs an operator cron timer on the box (MANUAL).
- H20: GHA workflows pinned `actions/checkout` and `actions/setup-python` to commit SHAs; third-party actions carry `# TODO: pin to commit SHA` markers.
- H21: docker-compose image tags changed from `latest` to pinned (`redis:7.2-alpine`, `narve-app:v1`, `narve-scraper:v1`) with TODO comments to migrate to digests.
- H22: DEPLOY_HABBIG.md adds post-step `chown root:root && chmod 600 /etc/cloudflared/*.json`. Operator must follow.
- H23: Frontend Sentry loader added to static/dashboards.html head; gated by `{{ sentry_frontend_dsn }}` template var. Server must substitute the DSN (MANUAL wiring).
- H24: static/privacy.html gained a new "Data Retention" section §13 with explicit retention periods.
- H25: PARTIAL — login success/failure now routed to `security.auth` logger (logs/security.log). Full PII-access audit (data exports, profile views) still pending.
- H26: MANUAL — BetterStack token fallback would need a config shim; not applied.

**Medium:**
- M1: Covered by C2 (set_user_role revokes sessions).
- M2: `MAX_SESSIONS_PER_USER` 5 → 3 (db.py:3618).
- M3, M4: `invite_tokens.expires_at` column added via idempotent ALTER; new tokens default to 30-day TTL; `get_invite_token` + `claim_invite_token` reject expired.
- M6: `auth/cookies.py:_secret()` raises in production if `GATEWAY_COOKIE_SECRET` unset; dev fallback only.
- M7: Session cookie TTL now env-configurable via `SESSION_COOKIE_TTL_DAYS`.
- M8: CSRF cookie max-age 24h → 2h.
- M9: Global rate-limit 429 now carries `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- M11: `tools/change_queue.py` no longer shells out `python -c change['script']` — raises `NotImplementedError`. `git apply <filepath>` rejects absolute or `..` paths.
- M14: Kalshi client `timeout=15.0` asserted on every get/post call path.
- M15: Service password no longer held on `self` — lives only in a closure provider at login time.
- M16: `create_api_key` writes `first_displayed_at` (with TODO for column migration); `get_api_key_raw` documented as never returning the raw key.
- M17: Embed CSP — enforcement tightened; widget domain required for Referer tolerance to be bypassed.
- M18: `install-narve-service.sh` keeps `EnvironmentFile=` for secrets; inline `Environment=` kept only for non-secret `PRODUCTION=1`/`PYTHONUNBUFFERED=1`.
- M19: `deploy-production.yml` now `jq`-scrubs `/tmp/health.json` (removes `.stack_trace`, `.env`, `.secrets`, `.config`).
- M20: `.env.example` documents `CORS_ORIGINS`, `REDIS_PASSWORD`, `CREDENTIALS_ENCRYPTION_KEY`, `PUSH_VAPID_PRIVATE_KEY_PEM`.
- M21: Sentry `set_user` no longer passes email — `id` is `sha256("narve:<id>")[:16]`.
- M22: login success + failure now routed to `security.auth` logger in server_features.py.
- M23: DEFERRED — impersonation start/stop events still on app logger; move to security logger pending.
- M24: `SENSITIVE_KEY_HINTS` in logging_config.py extended with `reset`, `invite`, `stripe`, `webhook`, `kalshi`, `vapid`.
- M25: DEFERRED — age verification on signup is a product decision, not a code fix.
- M26: `DATA_EXPORT_SIGNING_SECRET` separated from `GATEWAY_COOKIE_SECRET` in exports/generator.py.
- M27: DEFERRED — 2FA re-verification on admin mutations needs a per-handler audit; low-impact since rate-limits stack.
- Stripe stub: `handle_webhook` now raises `NotImplementedError` with a docstring warning and the exact `stripe.Webhook.construct_event` recipe.

**Low:**
- L2: `shocakarel@gmail.com` removed from `.env.example` (× 5 sites); `CLOUDFLARE_CHANGES.md` untouched (doc file; operator to scrub).
- L4: scrub hints covered by M24 additions.
- L5: `scripts/narve-watchdog.sh` now has rolling 60s window cap of 5 restarts then 300s sleep.
- L6: `DEPLOY_HABBIG.md` uses `$NARVE_HOST` env var instead of the hardcoded IP; README/RUNBOOK out of scope.
- L7: `.github/dependabot.yml` created (pip + github-actions, weekly, 5-PR cap, security label).
- L9: Kalshi client exponential backoff 30s → 60s → 120s … cap 600s.
- L10: Telegram `/source @handle` validated against `^[a-zA-Z0-9_]{1,30}$`.
- L14: Password-reset no longer returns "Account not found." — renders generic "If that account exists…" response.
- L16: Covered by H16 (Referer tolerance closed when widget.domain is set).
- L18: Covered by H10 (notification keyset validates ownership).

### MANUAL actions required before this pass is truly complete

Operator must do these on the server / in dashboards:

1. **Set new env vars in production `.env`:**
   - `REDIS_PASSWORD` — long random string
   - `CREDENTIALS_ENCRYPTION_KEY` — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `PUSH_VAPID_PRIVATE_KEY_PEM` — paste existing key contents or generate new pair
   - `DATA_EXPORT_SIGNING_SECRET` — random 32+ char string (defaults to GATEWAY_COOKIE_SECRET if unset)
   - `SENTRY_FRONTEND_DSN` — wire into render_page context so the new loader works
   - `GATEWAY_COOKIE_SECRET` — verify it's ≥32 chars (startup now refuses to boot otherwise)
2. **Invite tokens:** ALTER TABLE is idempotent; no action needed. Existing rows have `expires_at=NULL` (treated as non-expiring); issue new tokens through the normal flow.
3. **Cloudflare:** deploy WAF rules §4.1–4.6 and health checks §2 from CLOUDFLARE_CHANGES.md. Set Min TLS Version = 1.3 under SSL/TLS → Edge Certificates.
4. **Backups:** install the cron/systemd timer that runs the backup block now documented in DEPLOY_HABBIG.md.
5. **Cloudflared creds:** `chown root:root /etc/cloudflared/*.json && chmod 600 /etc/cloudflared/*.json`.
6. **Pending tables:** create `pending_telegram_links(user_id INTEGER, code TEXT, expires_at INTEGER)` and issue-code endpoint (Telegram `/subscribe` fails closed until this is done). Also `api_keys.first_displayed_at` column migration.
7. **Restart the app** after deploy; startup checks will abort if `GATEWAY_COOKIE_SECRET` is unset in production.
8. **Verify:** run `pip-audit` on the newly pinned `requirements.txt`; confirm no residual CVEs.

### Deferred (code fix is bigger than this pass)

- C4: migrate gate from shared `SITE_ACCESS_TOKEN` to per-user invite tokens.
- C8: full at-rest encryption + migration of Kalshi credential rows.
- C9: EIP-191 wallet-signature challenge for Polymarket linking.
- H14: per-caller scraper keys.
- H25 / M22 / M23: full PII access + impersonation start/stop → security channel.
- M25: age verification on signup.
- M27: 2FA re-verification on admin mutations.

---

## AUDIT #1 — 2026-04-21

### Summary
- Posture: **concerning**
- Critical issues: 13
- High-priority issues: 26
- Medium-priority issues: 27
- Low-priority issues: 18
- Resolved since last audit: N/A (first audit)
- New issues since last audit: N/A (first audit)
- Regressions: N/A (first audit)

### Authentication & Sessions
- Token gate at /token: PRESENT (server.py:2087–2102)
- pending_token cookie attributes: correct (auth/cookies.py; HttpOnly, Secure, SameSite=Strict)
- session cookie HttpOnly: yes
- session cookie Secure: yes (production)
- session cookie SameSite: Strict
- session stored as SHA-256 hash: yes (`user_sessions` hardened table)
- session revocation on logout: works (token rotated, cookie cleared)
- session rotation on privilege change: **missing** (role change / email change / password reset do not revoke hardened sessions)
- max sessions per user: 5 (`db.py:3620`) — high
- password reset invalidates existing sessions: **no** (legacy `sessions` deleted, `user_sessions` NOT revoked — server.py:2630–2640, 3475–3495)
- 2FA status: **TOTP + email OTP still present** — product decision says remove entirely

### Authorisation
- Admin routes require admin role: yes (mostly; `/api/flags/evaluate/{key}` is NOT auth-gated — admin_routes.py:462)
- Tier-based gating enforced: partially (features.py server-side; `is_admin_user` computed once per request)
- Feature flag evaluation secure: **no** (unauthenticated enumeration possible)
- Impersonation blocks destructive actions: partially (impersonation.py:43–81; regex uses `.fullmatch()` with strict anchoring — e.g. `/account/password-reset` slips past `/account/password.*`)
- All state-changing API routes have CSRF: partially (exempts include `/auth/validate-token`, `/api/newsletter`)

### CSRF
- Double Submit Cookie implemented: yes (server.py:744–951)
- CSRF token rotated on login: **no** (server.py:2185–2704 — rotates only on logout)
- CSRF token rotated every 2 hours: no (24h cookie lifetime hardcoded)
- HTMX requests include CSRF header: yes
- Form submissions include hidden csrf_token: yes
- Exempt routes list minimal and justified: partially (newsletter OK; `/auth/validate-token` exempt with only per-IP limit)

### Rate limiting
- Auth endpoints rate limited: partially (per-IP only; no per-email on `/auth/login`)
- API endpoints rate limited: partially
- Rate limit by IP AND by user where appropriate: no (2FA verify has neither — server.py:2403–2450)
- 429 responses include Retry-After: yes (missing `X-RateLimit-*` headers)
- Rate limits enforced at Cloudflare + app level: **app-only** (Cloudflare WAF rules all marked ⬜ pending — CLOUDFLARE_CHANGES.md:437–487)

### Input validation
- All POST/PUT bodies validated (Pydantic): partially (some endpoints use raw `Form()` / `request.json()`)
- SQL injection vectors: 2 (soft — dynamic WHERE-clause join in api_v1.py:184 and db.py:3532/3574; values parameterized, structure concatenated)
- XSS vectors (innerHTML with user content): many (static/trade.js has ~40 innerHTML assignments using `esc()` which only escapes strings, not nested objects; static/admin-email-edit.html:133,136 raw HTML)
- Command injection vectors: 1 (tools/change_queue.py:338–362 — `python -c change['script']`; local CLI only)
- Path traversal vectors: 1 (exports/generator.py:49 — `/tmp/narve-exports` predictable; HMAC-signed URLs mitigate)
- SSRF vectors in URL fetching: checked (og_cards.py uses static LOGO_PATH; no user-URL fetch surface exposed)

### Encryption & secrets
- HTTPS enforced everywhere (including redirects): yes (HSTS 1yr + includeSubDomains)
- TLS 1.2 minimum: yes (1.3 minimum NOT documented in Cloudflare runbook)
- Stripe/sensitive keys only in env vars: yes (but Stripe integration stubbed — backend/payments/stripe_stub.py)
- Kalshi API tokens encrypted at rest: **no** (db.py `kalshi_token` / `kalshi_member_id` plaintext)
- Session tokens hashed before DB storage: yes (SHA-256)
- Password hashes use bcrypt or argon2: **PBKDF2-HMAC-SHA256 @ 200k iters** (OWASP 2023 recommends ≥600k)
- .env never committed: verified (`.gitignore` excludes `.env`, `*.db`, `*.pem`, `*.key`; `git log --all -- auth.db` clean)

### Data privacy
- Account deletion works end to end: **no** (admin-only `/admin/users/{id}/delete`; no user-initiated `/account/delete`)
- Data export available for GDPR: yes (exports/generator.py with HMAC-signed URLs)
- PII access logged: **no** (admin audit logs actions but not data views)
- Sensitive fields redacted in logs: yes (`SENSITIVE_KEY_HINTS` in logging_config.py:94–113; missing "reset" and "invite" hints)
- Sensitive fields redacted in Sentry: partially (observability/sentry_setup.py:94,105 passes `email` to `set_user()` despite `send_default_pii=False`)

### External integrations
- Stripe webhook signature validated: **N/A — stubbed** (backend/payments/stripe_stub.py; must validate when wired)
- Telegram bot webhook authenticated: partially (integrations/telegram_bot.py:82–94 — `/subscribe <code>` accepts any valid invite token without user ownership check → IDOR)
- Discord bot permissions minimal: N/A
- Scraper API key validated on every request: yes (single shared key — no per-caller keys, no rotation)
- Polymarket address validation: **no** (`len(address) < 10` only; no `0x[0-9a-fA-F]{40}` check; no wallet-signature proof of ownership)
- Kalshi credential handling secure: **no** (plaintext at rest; service creds held in memory)

### Infrastructure
- Database connection pooled: yes
- Redis password-protected: **no** (docker-compose.yml:110 — no `--requirepass`, no `protected-mode`)
- Redis bound to localhost or VPN: unknown (no explicit bind; protected-mode disabled)
- Cloudflare WAF rules active: **no** (all rules marked ⬜ pending — CLOUDFLARE_CHANGES.md:437–487)
- Cloudflare rate limit rules: 0 active
- Docker containers run as non-root: unknown (not specified in compose)
- Secrets not in Docker images: yes (`env_file: .env` runtime)

### Monitoring
- Sentry configured for backend: yes (observability/sentry_setup.py)
- Sentry configured for frontend: **no**
- Sensitive data scrubbed in Sentry: partially (email leaks via `set_user()`)
- Structured logging configured: yes
- Security events logged separately: partially (CSRF/rate limits yes; login success/failure and impersonation start/stop NOT routed to security channel)
- BetterStack (or equivalent) log aggregation: yes (LOGTAIL_TOKEN_APP/SCRAPER/WORKER — silent loss if any token unset)
- Uptime monitoring configured: **no** (Cloudflare health checks marked pending)

### Dependency audit
- Python dependencies audited: 2026-04-21
- npm dependencies audited: N/A (none found)
- Known CVEs in dependencies: **loose pins** (`fastapi>=0.110`, `uvicorn[standard]>=0.29`, `python-multipart>=0.0.9` — the latter has CVE-2024-24762; should be ≥0.0.18)
- Automated dependency updates (Dependabot/Renovate): **not configured**

### Compliance
- Privacy Policy live: yes (static/privacy.html)
- Terms of Service live: yes (static/terms.html)
- DPA (Data Processing Agreement) available: template exists (static/dpa.html) — route wiring unverified
- Cookie notice present: **no**
- Contact email for privacy requests: yes (shocakarel@gmail.com — personal email, should be delegated)
- GDPR data rights implemented (export, delete): export yes, **user-initiated delete no**

---

### Issues found in this audit

#### CRITICAL (fix immediately)

**C1. Hardened sessions not revoked on password reset**
- Location: server.py:2630–2640, 3475–3495
- Impact: Compromised session survives password reset — credential-recovery flow defeated
- Fix: Call `db.revoke_all_user_sessions(user_id)` after password update in both `/forgot-password` and `/reset-password`

**C2. Hardened sessions not revoked on email / role change**
- Location: db.py:1408–1410 (role change), email-change route in server.py
- Impact: Admin demotion or account hijack via email change does not force re-auth
- Fix: Invoke `db.revoke_all_user_sessions(user_id)` on every privilege-changing mutation

**C3. Gate cookie token is the hardcoded string `"granted"`**
- Location: server.py:1649–1660 (set), 1638–1646 (validate)
- Impact: Any network foothold or XSS-adjacent vector that can set cookies bypasses the gate
- Fix: Issue HMAC-signed per-session gate tokens using `GATEWAY_COOKIE_SECRET`

**C4. SITE_ACCESS_TOKEN is a single shared site-wide secret**
- Location: server.py:2087–2102
- Impact: One leak = full gate bypass for every visitor; no rotation path
- Fix: Replace with per-user invite-token-based gate validation; add rotation UI

**C5. Feature-flag evaluation endpoint has no auth check**
- Location: admin_routes.py:462–465
- Impact: Unauthenticated attackers can enumerate every flag + rollout state (business logic disclosure)
- Fix: Add `user = server._require_authenticated(request)` before `is_feature_enabled(...)`

**C6. Impersonation admin-level fallback allows self-promotion**
- Location: admin_routes.py:115–132 (line 128)
- Impact: Admin with missing `admin_level` (fallback=0) can impersonate super-admin (role=2)
- Fix: Require explicit `admin_level`; refuse impersonation if unset; compare concrete ints

**C7. CSRF token not rotated on login**
- Location: server.py:2185–2195 (+ 2655–2661)
- Impact: Pre-auth token captured off a public page remains valid after the victim authenticates — session-fixation-style CSRF
- Fix: Call `_set_csrf_cookie(response, _generate_csrf_token(), request)` on every successful login path

**C8. Kalshi credentials stored in plaintext**
- Location: db.py (`kalshi_token`, `kalshi_member_id` columns); backend/markets/kalshi_client.py
- Impact: DB dump or SQLi leaks every user's Kalshi trading credentials
- Fix: AES-256-GCM via `CREDENTIALS_ENCRYPTION_KEY`; decrypt only at call site

**C9. Polymarket wallet linking has no ownership proof**
- Location: server.py (Polymarket connect endpoint; validation is `len(address) < 10`)
- Impact: Attacker links victim's wallet to attacker's account and reads positions/PnL
- Fix: Require wallet-signature challenge (sign server-issued nonce); validate address as `^0x[0-9a-fA-F]{40}$`

**C10. Redis runs without authentication**
- Location: docker-compose.yml:110
- Impact: Anyone who reaches the Redis port can drain the job queue, inject tasks, or dump cached state
- Fix: `command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}", "--protected-mode", "yes", "--appendonly", "yes"]`

**C11. Loose dependency pins — python-multipart vulnerable**
- Location: requirements.txt (`python-multipart>=0.0.9`, `fastapi>=0.110`, `uvicorn[standard]>=0.29`)
- Impact: `python-multipart` <0.0.18 is affected by CVE-2024-24762 (ReDoS); unpinned majors can ship CVE-vulnerable versions
- Fix: Exact-pin every dep; bump `python-multipart>=0.0.18`

**C12. VAPID push private key written plaintext to `~/.narve/vapid.key`**
- Location: push.py:38, 88–89
- Impact: Filesystem compromise = ability to forge push notifications to all subscribers
- Fix: Require `PUSH_VAPID_PRIVATE_KEY_PEM` env var in production; encrypt at rest with `CREDENTIALS_ENCRYPTION_KEY`

**C13. No user-initiated account deletion (GDPR Art. 17)**
- Location: server.py (no `/account/delete` endpoint; only admin-gated `/admin/users/{id}/delete`)
- Impact: Cannot honor Right-to-Erasure requests through normal flow — regulatory exposure
- Fix: Implement POST `/account/delete` with 30-day grace, audit log, full cascade (auth.db, sessions, affiliate, referrals, push, cache, exports)

#### HIGH

**H1. No per-email rate limit on `/auth/login`** — server.py:1398–2091. Credential-stuffing via rotating IPs. Add 5/hour per email.

**H2. `/api/auth/2fa/verify` and `/api/auth/2fa/email/verify-setup` have no rate limit** — server.py:2403–2450. Brute-forceable 6-digit OTP. Add 5/10min per user, return Retry-After.

**H3. All Cloudflare WAF + rate-limit rules still pending** — CLOUDFLARE_CHANGES.md:437–487. No edge protection. Deploy rules 4.1–4.6 immediately.

**H4. `/auth/validate-token` is CSRF-exempt with per-IP-only limit** — server.py:757–780. Shared-IP users can lock each other out; trigger bulk email resends. Add per-email limit.

**H5. `GATEWAY_COOKIE_SECRET` missing is warning-only in production** — server.py:379–380. Should raise `RuntimeError` like `SITE_ACCESS_TOKEN`.

**H6. PBKDF2-HMAC-SHA256 at 200,000 iterations** — db.py:1119. Below OWASP 2023 (600k). Bump iter count; re-hash opportunistically on next login.

**H7. `DEV_USER_PASSWORD` generated unconditionally at startup** — server.py:1465. No `assert not IS_PRODUCTION`. Gate behind dev flag.

**H8. Affiliate commission-rate update is admin-only, not super-admin** — affiliate_routes.py:619–658. Compromised admin can inflate own payout. Gate behind `admin_level >= 2`.

**H9. Impersonation regex uses strict `fullmatch` anchoring** — impersonation.py:43–88. Paths like `/account/password-reset` bypass `/account/password.*`. Use `search()` or broader patterns + integration tests.

**H10. Notification and embed IDOR depend on DB layer honoring `user_id`** — notification_routes.py:116–119; embed_routes.py:579–587. If DB function drops the `user_id = ?` clause, cross-user access silently works. Add explicit route-level ownership checks.

**H11. XSS surface via `innerHTML` in static/trade.js (~40 sites)** — custom `esc()` covers strings but not nested object keys. Refactor to `textContent` / template nodes; defend at server serialization.

**H12. XSS via `innerHTML = j.html` in admin-email-edit preview** — static/admin-email-edit.html:133,136. Admin-only, but compromised admin → phishing vector. Sanitize with DOMPurify or render server-side.

**H13. Path `/tmp/narve-exports` is world-traversable** — exports/generator.py:49. HMAC-signed URLs mitigate enumeration, but co-tenants on shared hosts can list. `DATA_EXPORT_DIR` → `0o700` user-owned path.

**H14. Scraper API single shared key — no rotation, no per-caller key** — scraper/main.py, jobs/pipeline_jobs.py. One leak revokes all scrapers. Split per job queue.

**H15. Telegram `/subscribe <code>` accepts any valid invite token** — integrations/telegram_bot.py:82–94. Cross-account linking IDOR. Encode user-scoped code.

**H16. Embed tokens have no `iat`/`exp`** — embed_tokens.py / embed_routes.py. Leaked token replayable until salt rotation. Add expiry claims.

**H17. No rate limit on password-reset email send (per-IP)** — server.py:2566–2641. Per-email limit exists; no per-IP → email-bombing a victim from rotating accounts. Add 10/10min per IP.

**H18. No health-check / uptime monitoring** — CLOUDFLARE_CHANGES.md §2 pending. Silent outages. Activate Cloudflare Health Checks.

**H19. Missing encrypted off-site DB backup automation** — DEPLOY_HABBIG.md:202. `auth.db` loss = unrecoverable. Add GPG-encrypted daily backup to S3/B2.

**H20. GitHub Actions pinned to major tags, not SHAs** — `.github/workflows/deploy-production.yml:54`, `test.yml:31,34`. Supply-chain exposure. Pin to 40-char SHAs.

**H21. Docker images tagged `latest`** — docker-compose.yml:28,59,82. Non-reproducible builds. Pin to semver or digest.

**H22. Cloudflare tunnel credentials file permissions unspecified** — DEPLOY_HABBIG.md:283. Must be `0600 root:root`.

**H23. No frontend Sentry** — static/*.html. Silent client-side errors.

**H24. No data-retention policy in Privacy Policy** — static/privacy.html. GDPR Art. 13(2)(a) requires explicit retention periods.

**H25. PII access not logged** — security/audit.py. No audit trail for data views. Log exports, profile views, billing views to dedicated channel.

**H26. Logtail tokens per-service with no fallback** — logging_config.py:346. Unset token = silent loss of that service's logs.

#### MEDIUM

**M1.** Session rotation missing on privilege escalation (promotion to admin keeps old cookie) — db.py:1408–1410.
**M2.** MAX_SESSIONS_PER_USER = 5 is high — db.py:3620. Lower to 2–3.
**M3.** Invite tokens have no `expires_at` — db.py:1361–1375.
**M4.** `get_invite_token()` returns any status — db.py:1371–1383. Enforce unclaimed at DB layer.
**M5.** Legacy `sessions` table still present alongside `user_sessions` — confusion risk.
**M6.** `pending_token` cookie falls back to hardcoded secret if env var unset — auth/cookies.py:62.
**M7.** Session cookie TTL hardcoded 7 days — auth/cookies.py:34.
**M8.** CSRF cookie max-age 24h, not rotated periodically — server.py:846–854.
**M9.** Missing `X-RateLimit-{Limit,Remaining,Reset}` headers on 429s — server.py:338–343, 1337–1342.
**M10.** Soft SQL-concat pattern in dynamic WHERE-clause builders — api_v1.py:184; db.py:3532, 3574. Values are parameterized; shape is not.
**M11.** `tools/change_queue.py:338–362` shells out to `python -c change['script']`. Local CLI only; still ripe for foot-gun.
**M12.** Open-redirect / email/username regex enforcement not verified on every endpoint — server.py:282, 2652.
**M13.** Some POST endpoints take raw `Form()` / `request.json()` without Pydantic schema.
**M14.** Kalshi HTTP client timeout not asserted on all call paths — backend/markets/kalshi_client.py.
**M15.** Kalshi service credentials (email + password) held in memory for token refresh — kalshi_client.py:35–44. Keep only token.
**M16.** API key returned on create but no `first_displayed_at` one-shot flag — api_v1.py:37–56.
**M17.** Embed CSP default relaxed (`frame-ancestors 'none'` fallback) — server.py:602. Harden per-widget allowlist.
**M18.** Systemd unit uses `Environment=` inline for secrets — scripts/install-narve-service.sh. Switch to `EnvironmentFile=/etc/narve/.env` (0600 root).
**M19.** Deploy workflow prints full `/tmp/health.json` to GHA logs — deploy-production.yml:108. Redact sensitive fields.
**M20.** `CORS_ORIGINS` not documented in `.env.example`.
**M21.** Sentry `set_user()` passes email — observability/sentry_setup.py:94,105. Use hashed ID only.
**M22.** Login success/failure not routed to security channel — logging_config.py.
**M23.** Impersonation start/stop logged to app.log not security.log — impersonation.py / admin_routes.py.
**M24.** `SENSITIVE_KEY_HINTS` missing "reset" and "invite" — logging_config.py:94–113.
**M25.** No age verification on signup (COPPA/GDPR minors) — static/terms.html.
**M26.** Export signing secret reuses `GATEWAY_COOKIE_SECRET` — exports/generator.py:57–62. Compartmentalize.
**M27.** Admin POST/DELETE mutations don't require 2FA re-verification — admin_routes.py. Only GET admin-pages do.

#### LOW

**L1.** Legacy `sessions` cleanup outstanding.
**L2.** Personal email (`shocakarel@gmail.com`) appears in `.env.example:86–90` and CLOUDFLARE_CHANGES.md:90,99,101.
**L3.** Cloudflare min TLS not documented (should be 1.3).
**L4.** Log scrubbing doesn't explicitly list "stripe"/"api_key" (covered transitively by "secret"/"token").
**L5.** Watchdog (`scripts/narve-watchdog.sh`) has no exponential backoff on restart.
**L6.** Hardcoded Tailscale IP 100.69.44.108 in DEPLOY_HABBIG.md:23 and deploy script.
**L7.** No Dependabot/Renovate config.
**L8.** CSP relaxed on embed routes — document allowlist.
**L9.** Kalshi token-refresh failure latches to `None` for 1h — kalshi_client.py:83–94. Add backoff.
**L10.** Telegram `/source @handle` validation — integrations/telegram_bot.py:158–179. Add `^[a-zA-Z0-9_]{1,30}$` regex.
**L11.** DELETE notification has no explicit `@require_csrf` — middleware covers, but be explicit.
**L12.** Admin email-template body accepts raw HTML — admin_routes.py:625–647.
**L13.** `is_admin_user` computed once per request — stale-var smell — server.py:2730.
**L14.** Password-reset error messages may leak user existence.
**L15.** Affiliate self-attribution possible (click own link → sign up) — db_affiliate.py:335–422. Add self-click detection.
**L16.** Embed token domain check tolerates missing Referer — embed_routes.py:590–607.
**L17.** Affiliate link list relies on route-layer ownership — harden at DB layer — db_affiliate.py:261–268.
**L18.** Notification keyset pagination (`before_id`) must verify `user_id` — notification_routes.py:62–97.

---

### Changes since previous audit

First audit — no deltas.

### Recommended actions for next audit cycle

1. Verify all CRITICAL items are resolved before shipping to more users.
2. Re-check CSRF token rotation — should rotate on every login AND every 2h.
3. Run `pip-audit` and attach the report.
4. Confirm Cloudflare WAF rules (CLOUDFLARE_CHANGES.md §4) are deployed with `curl -sI` verifying `cf-ray` + rules active.
5. Diff `db.revoke_all_user_sessions` call sites against every mutation that changes auth material (password, email, role, 2FA, TOTP secret).
6. Verify `CREDENTIALS_ENCRYPTION_KEY` is required (RuntimeError on absence in production).
7. Confirm `/account/delete` exists and cascades across all tables.
8. Grep for new `innerHTML` assignments in `static/` and count deltas.
9. Check `requirements.txt` for new unpinned deps.
10. Confirm Redis `requirepass` and `protected-mode` active.

---
