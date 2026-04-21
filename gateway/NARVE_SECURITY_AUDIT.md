# NARVE.AI SECURITY AUDIT LOG

Append-only security audit history. Never delete entries.
Each entry is a point-in-time snapshot. Diffs between entries reveal posture changes.

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
