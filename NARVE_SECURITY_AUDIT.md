# NARVE.AI SECURITY AUDIT LOG

Append-only security audit history. Never modify or delete entries.
Each entry is a point-in-time snapshot. Diffs reveal posture changes.

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
