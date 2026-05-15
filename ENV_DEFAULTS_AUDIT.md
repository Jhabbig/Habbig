# ENV DEFAULTS AUDIT — gateway/server.py & gateway/db.py

**Scope:** every `os.environ.get(...)` / `os.getenv(...)` call in
`gateway/server.py` and `gateway/db.py`, with an assessment of whether
the default is safe when the operator forgets to set the variable in
production.

**Method:** static read-only audit. Each call site was reviewed in
context for (a) the default value, (b) the security impact of running
with the default, and (c) whether `IS_PRODUCTION` startup checks
refuse to boot on an unsafe default.

**Severity legend:**
- **CRIT** — auth bypass / gate disabled / signature verification broken when default is used
- **HIGH** — data exfiltration risk, secret leakage, or critical integrity loss
- **MED** — functional degradation, monitoring blind spots, weakened defense-in-depth
- **LOW** — harmless or already gated by an `IS_PRODUCTION` startup guard

---

## Summary

26 env reads across the two files. **Zero CRIT findings** remain
uncovered because the relevant secrets (`SITE_ACCESS_TOKEN`,
`GATEWAY_COOKIE_SECRET`, `CREDENTIALS_ENCRYPTION_KEY` when TOTP users
exist) are guarded by `IS_PRODUCTION` startup checks at
`server.py:628-639,664-673` that raise `RuntimeError` and refuse to
boot.

The most notable residual risks are MEDIUM-level defaults around
downstream SSO (`GATEWAY_SSO_SECRET`), analytics salt
(`IP_HASH_SALT`), and a `"dev-gate-secret"` literal fallback inside
`_gate_cookie_secret()` that is unreachable in production *only
because* the startup checks block boot — remove either of those
checks and the fallback becomes a CRIT auth-bypass.

| Severity | Count |
|---|---|
| CRIT | 0 |
| HIGH | 3 |
| MED | 9 |
| LOW | 14 |

---

## Findings table (sorted by severity)

| # | Sev | Env var | File:Line | Default | Impact if default is used in prod | `IS_PRODUCTION` refuses boot? |
|---|---|---|---|---|---|---|
| 1 | HIGH | `GATEWAY_SSO_SECRET` | server.py:7871 | `None` (header omitted) | When unset, the dashboard-proxy path silently drops `X-Gateway-Secret` from forwarded requests. Every downstream dashboard (`annoyance`, `centralbank`, `whale`, `world-health`, `crypto`, `disasters`, etc.) rejects gateway-fronted requests as 401, so the user-visible failure mode is dashboards 401'ing rather than an auth bypass — BUT a separate, equally-empty value on the dashboard side would make `hmac.compare_digest("", "")` succeed and accept unauthenticated traffic. Treat as HIGH because the gateway side gives no warning at all when the secret is missing. | NO — no startup check. Silent failure → downstream 401s |
| 2 | HIGH | `CREDENTIALS_ENCRYPTION_KEY` | server.py:664,2995 | `""` | Fernet key for TOTP secrets and other at-rest credentials. With an empty key, TOTP secrets can't be decrypted → existing 2FA users locked out, and any *new* writes that go through the encryption helper (see backend/markets/encryption.py imported at 7653) would store plaintext or crash. | YES (conditional) — startup raises `RuntimeError` only when **TOTP users already exist** (server.py:664-673). A fresh deploy with zero TOTP users boots, and *new* writes that need the key may then fail/leak. Partial guard. |
| 3 | HIGH | `IP_HASH_SALT` | server.py:4870 | `"narve.ai/analytics/v1"` (hardcoded constant) | The salt is the only thing that stops a rainbow-table reversal of `ip_hash` rows in `analytics_events` if the analytics DB is exfiltrated. Because the default is a known constant published in this audit, anyone with read access to a leaked DB can precompute `SHA-256("narve.ai/analytics/v1:<ip>")` for the entire IPv4 space (~4B hashes — cheap). The docstring at 4866-4869 says "rainbow-table protection," but a global constant is not protection. | NO — no startup check. |

| # | Sev | Env var | File:Line | Default | Impact if default is used in prod | `IS_PRODUCTION` refuses boot? |
|---|---|---|---|---|---|---|
| 4 | MED | `SITE_ACCESS_TOKEN` | server.py:241 | `""` | Empty token = site-wide gate disabled (`has_gate_access` returns `not IS_PRODUCTION` at 2143-2144, which is `False` in prod — gate *would* be permanently closed in prod). Default itself is therefore self-denying, not auth-bypassing. | YES — startup raises `RuntimeError` at server.py:634-636 if empty in prod and at 637-639 if shorter than 32 chars. Hard guard. |
| 5 | MED | `GATEWAY_COOKIE_SECRET` | server.py:628,631,2150 | `""` (falls back to `SITE_ACCESS_TOKEN` then literal `"dev-gate-secret"` at 2150) | HMAC key for gate cookie. The literal `"dev-gate-secret"` is the alarming part — if a future refactor removes the startup check, an attacker who knows the source can forge gate cookies trivially. Currently dead code in prod *only because* of the boot check. | YES — startup raises `RuntimeError` at server.py:628-633 if unset or <32 chars in prod. Hard guard. **Recommend:** drop the `"dev-gate-secret"` literal entirely and make `_gate_cookie_secret()` raise if both env vars are empty; defense-in-depth costs nothing. |
| 6 | MED | `REDIS_URL` | server.py:1589 | `""` (empty → in-memory rate-limit fallback) | Rate limiter falls back to per-process in-memory store. In a multi-worker uvicorn deployment, each worker rate-limits independently, so the *effective* limit is `workers × configured_limit`. Login/signup/forgot-password floods become easier to mount. Functional degradation, not auth bypass. | NO — gracefully degrades with a `log.warning` on connection failure (1597-1599). |
| 7 | MED | `STAGING_BACKEND_URL` | server.py:952 | `"http://127.0.0.1:7001"` | Staging proxy middleware. The default is fine in single-host deploys; risk is only if production were to ever bind 7001 to something unintended. SSRF risk is low because the URL is hardcoded, not user-controlled. | NO — but path is gated by `Host: staging.*` matching, so prod hostnames never hit this code. |
| 8 | MED | `CSRF_PATCH_DELETE_ENFORCE` | server.py:1074 | `"false"` | PATCH/PUT/DELETE CSRF check is in log-only mode by default. POST is still enforced. The comment block at 1060-1076 acknowledges this is a two-phase rollout — Phase 2 (flip to `"true"`) is "next sprint." Until flipped, CSRF coverage on mutating non-POST verbs is observability-only. | NO — intentional rollout flag. |
| 9 | MED | `GLOBAL_RATE_LIMIT_PER_MIN` | server.py:1744 | `"600"` (parsed as int) | 600 req/min/IP is generous for a single human; aggressive scrapers stay under it. Lowering would be safer; raising via env var is fine. No upper-bound validation — an operator typo of `"60000"` silently disables the middleware as a practical limit. | NO — but `int()` will raise on garbage values, surfacing at import time. |
| 10 | MED | `EMAIL_DRY_RUN` | server.py:2987 | `""` (false) | Production sends real email. Fine for prod. Risk is the *inverse*: a misconfigured staging env without `EMAIL_DRY_RUN=1` would send real customer emails from staging. Default-false is the safe direction for prod. | NO — staging env file is the control plane. |
| 11 | MED | `NARVE_SKIP_SCHEDULER` | server.py:3033 | `""` (scheduler runs) | Skipping the scheduler in prod silently disables every cron job (health checks, weekly reports, newsletter blast tick, etc.). The default-false direction is correct; impact is only if an operator sets it on prod by accident. | NO — `_check_scheduler()` health endpoint surfaces it as "disabled". |
| 12 | MED | `GATEWAY_DB_PATH` | db.py:15 | `""` → `./auth.db` beside this file | Default points at `gateway/auth.db`. If staging ever runs with the default instead of `auth-staging.db`, staging and prod share the same SQLite file and a staging migration could corrupt prod data. Real-world incident class. The deploy `EnvironmentFile` is the only thing keeping these apart. | NO — no protection against the staging-env-file-missing-the-line case. **Recommend:** assert `"staging" in DB_PATH.name` when `APP_ENVIRONMENT == "staging"`. |

| # | Sev | Env var | File:Line | Default | Impact if default is used in prod | `IS_PRODUCTION` refuses boot? |
|---|---|---|---|---|---|---|
| 13 | LOW | `PRODUCTION` | server.py:233 | `""` (false) | Default-false means an unflagged deploy runs in dev mode (localhost bypass active, cookies non-secure). The actual prod deploy unit (`gateway-prod.service`) sets `PRODUCTION=1` explicitly. | NO — but the *consequence* of forgetting it is the entire site being open, which is loud. |
| 14 | LOW | `APP_VERSION` | server.py:390 | `"1.0.0"` | Cosmetic; surfaces in `/health`. No security impact. | NO |
| 15 | LOW | `ENVIRONMENT` | server.py:391 | `"production" if IS_PRODUCTION else "dev"` | Cosmetic label. No security impact. | NO |
| 16 | LOW | `GIT_SHA` / `GIT_COMMIT` | server.py:402-403 | `""` (falls back to `<repo>/GIT_SHA` file then `None`) | Build identifier on `/health`. No security impact. Hex-validates the input. | NO |
| 17 | LOW | `DEPLOYED_AT` | server.py:426 | `""` (falls back to `<repo>/DEPLOYED_AT` mtime then `None`) | Build identifier on `/health`. No security impact. | NO |
| 18 | LOW | `APP_URL` | server.py:2768 | `"https://narve.ai"` | Used for canonical URL injection in templates. Hardcoded default already matches prod. | NO |
| 19 | LOW | `SMTP_USER` | server.py:2989 | `""` | `_check_email_dry_run` reports `"unconfigured"` when empty; no security impact, just a health signal. | NO |
| 20 | LOW | `SECURITY_TXT_CONTACT` | server.py:3614 | `"mailto:security@narve.ai"` | Default matches production. Published to `/.well-known/security.txt`. | NO |
| 21 | LOW | `SECURITY_TXT_EXPIRES` | server.py:3615 | `"2027-04-08T00:00:00Z"` | Default already in the future. Should be refreshed annually but no security impact. | NO |
| 22 | LOW | `ANALYTICS_ENABLED` | server.py:4890 | `"true"` | Server-side analytics defaults to on. No security impact. | NO |
| 23 | LOW | `ANALYTICS_RATE_LIMIT_PER_MIN` | server.py:4905 | `"60"` | Per-principal rate limit on `/api/analytics/event`. Reasonable default. | NO |
| 24 | LOW | `POLYMARKET_GAMMA_API` / `POLYMARKET_CLOB_API` | server.py:7656-7657 | Polymarket public URLs | Hardcoded to known-good production hosts. | NO |
| 25 | LOW | `KALSHI_API_BASE` / `KALSHI_SERVICE_EMAIL` / `KALSHI_SERVICE_PASSWORD` | server.py:7660-7662 | Kalshi public URL / `None` / `None` | Optional creds. `None` means service-account features (e.g. listing markets without an authenticated user) silently no-op — degraded functionality, not security. | NO |
| 26 | LOW | `MARKETS_CACHE_TTL` | server.py:7664 | `"300"` (clamped 60-3600) | Cache TTL with sane min/max clamp. | NO |
| 27 | LOW | `GATEWAY_HOST` | server.py:8582 | `"127.0.0.1"` | Safe default (loopback). Per the comment at 8577-8581 this is a fix for an earlier audit finding. | NO |

---

## Top 3 highest-leverage fixes

1. **`GATEWAY_SSO_SECRET` (server.py:7871) — HIGH.** Add a startup check next to the cookie-secret and site-token guards: if `IS_PRODUCTION and not os.environ.get("GATEWAY_SSO_SECRET")` → `raise RuntimeError`. The 7-line block at lines 628-639 already establishes the pattern. Without this, a deploy that drops the env line silently breaks every subproduct dashboard while logging nothing useful from the gateway side.

2. **`IP_HASH_SALT` (server.py:4870) — HIGH.** Replace the hardcoded `"narve.ai/analytics/v1"` constant with `os.environ.get("IP_HASH_SALT") or _raise()` in production. A salt that is a string literal in version control offers zero protection against the threat model in its own docstring (rainbow tables against an exfiltrated DB). Mint a 32-byte random salt at deploy time, store it in the same env file as the other secrets, and rotate when the analytics DB is rotated.

3. **`"dev-gate-secret"` literal in `_gate_cookie_secret()` (server.py:2150) — MED, latent-CRIT.** Today this is unreachable in production because the startup checks at 628-639 raise. But the existence of the literal means any future refactor that weakens the startup check (e.g. dev-mode toggle for production-like load testing) instantly becomes a forge-any-gate-cookie vulnerability. Delete the `or "dev-gate-secret"` fallback and replace it with an explicit raise; defense-in-depth at zero cost.

---

## What was checked

- `grep -n "os.environ.get\|os.getenv"` over both files (26 hits)
- Each hit was read with ±10 lines of surrounding context
- Cross-referenced the production startup guards in the `lifespan` block (`server.py:617-700`)
- Cross-referenced downstream verification of `GATEWAY_SSO_SECRET` in every `*-dashboard/server.py` / `*-dashboard/auth.py` to assess realistic exploit chain
- Confirmed `_gate_cookie_secret()`'s `"dev-gate-secret"` fallback is currently unreachable in production via the boot-time `RuntimeError` paths

No source files were modified.
