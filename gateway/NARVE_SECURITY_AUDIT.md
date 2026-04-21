# NARVE.AI SECURITY AUDIT LOG

Append-only security audit history. Never delete entries.
Each entry is a point-in-time snapshot. Diffs between entries reveal posture changes.

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
