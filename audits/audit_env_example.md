# audit: `gateway/.env.example`

**Date:** 2026-05-15
**Branch:** `feature/platform-build`
**Subject:** `/Users/shocakarel/Habbig/gateway/.env.example` (353 lines, 134 unique
keys)
**Method:**

1. Grep all env reads in the gateway code (`os.environ.get`, `os.getenv`,
   `os.environ[]`, plus the `_env()` wrapper in
   `gateway/queries/integrations.py`).
2. Extract every `KEY=` (commented or not) from `.env.example`.
3. Diff.
4. Hand-verify every entry in the diffs to label "true missing" vs
   "consumed by sibling dashboard / script / external".

**Files referenced**
- `/Users/shocakarel/Habbig/gateway/.env.example`
- `/Users/shocakarel/Habbig/gateway/config.py` (REQUIRED/CONDITIONAL/OPTIONAL
  spec table — startup validator)
- `/Users/shocakarel/Habbig/gateway/queries/integrations.py:53` (`_env()`
  wrapper that bypasses the raw-regex grep)

No code changes. Documentation gaps only.

---

## 1) Overall posture

- **Real secrets committed?** No. All filled-in values are either public URLs,
  defaults (`PRODUCTION=0`, `LOG_LEVEL=INFO`, `MAX_POSTS_PER_KEYWORD=50`),
  branded fallbacks (`legal@narve.ai`, `noreply@narve.ai`), or obvious
  placeholders (`change-me-long-random-string`, `your-email@example.com`).
  Safe to publish.
- **Header / structure:** File is split into ~12 sections with banner
  separators (e.g. `# ── Feature 1: Email system ──`,
  `# OBSERVABILITY — Sentry + BetterStack`). Good navigability.
- **`config.py` cross-check:** The three REQUIRED vars (`SITE_ACCESS_TOKEN`,
  `CREDENTIALS_ENCRYPTION_KEY`, `GATEWAY_COOKIE_SECRET`) are all present,
  uncommented and empty in the example — correct.
- **Generation directives ("GENERATE WITH ...")**: present in 5 places
  (Fernet, VAPID, watermark, SCRAPER_API_KEY, REDIS_PASSWORD). See §3 for
  the sensitive vars that are missing such directives.

---

## 2) Duplicate / conflicting keys inside `.env.example`

The file has **three duplicated keys** that should be consolidated — they
are not harmful (dotenv parsers take the last value), but they make
audits confusing and one of them has divergent comments.

| Key                          | Lines       | Note                                                                 |
|------------------------------|-------------|----------------------------------------------------------------------|
| `CREDENTIALS_ENCRYPTION_KEY` | 43, 162     | Two empty declarations. Line 42 has the Fernet-generate hint; line 156 has a richer block. Pick one home. |
| `LOG_LEVEL`                  | 141, 237    | Both `INFO`. Consolidate into the LOGGING block (line 232+).         |
| `REDIS_PASSWORD`             | 112, 275    | Line 112 sets `change-me-long-random-string` placeholder; line 275 leaves empty. Confusing — leave only line 275 (empty, with `openssl rand -hex 32` directive). |

The file also has **two parallel Sentry sections**: lines 63–73 (commented)
and lines 200–226 (uncommented). The second supersedes the first; the
first should be removed or merged.

---

## 3) Vars READ in code, **MISSING from `.env.example`** (true documentation gaps)

53 vars total. Sorted by sensitivity, then by surprise factor.

### A. Sensitive — should be added with **"GENERATE WITH `openssl rand -hex 32`"** directive

| Var                                | Read at                                              | Why sensitive                                                  |
|------------------------------------|------------------------------------------------------|----------------------------------------------------------------|
| `GATEWAY_SIGNING_SECRET`           | `saved_views_db.py:39`                               | HMAC for saved-view share tokens. Falls back to `GATEWAY_SSO_SECRET`. |
| `GATEWAY_INTERNAL_KEY`             | `server_features.py:1323`                            | Bearer key for internal-only endpoints. Compromise = bypass auth.|
| `EMBED_SIGNING_SECRET`             | `embed_tokens.py:61`, `saved_views_db.py:41`         | HMAC for embed JWTs. Falls back to `GATEWAY_SSO_SECRET` — should be split per the doc-comment. |
| `DATA_EXPORT_SIGNING_SECRET`       | `exports/generator.py:83`                            | HMAC for signed export URLs. Falls back to `GATEWAY_COOKIE_SECRET`. |
| `DATA_EXPORT_SIGNING_KEY`          | `export_routes.py:77`                                | Same family; verify-side counterpart.                          |
| `IP_HASH_SALT`                     | `server.py:4891` (default `"narve.ai/analytics/v1"`) | Salt for analytics IP hashing. Default is committed → an attacker can pre-compute the hash table. Should be set in prod. |
| `KALSHI_SERVICE_PASSWORD`          | `server.py:7683`                                     | Plaintext password for shared Kalshi service account.          |
| `KALSHI_SERVICE_EMAIL`             | `server.py` (paired with above)                      | Not a secret itself, but reveals the service account name.     |
| `UNUSUAL_WHALES_TOKEN`             | `insider/unusual_options.py:28`                      | Third-party API token. **Name mismatch** — see §5.             |

### B. Operational / observability — non-secret, but should be documented

| Var                                  | Read at                                       | Purpose                                                  |
|--------------------------------------|-----------------------------------------------|----------------------------------------------------------|
| `DEPLOYED_AT`                        | `server.py:565`                               | Build timestamp surfaced on `/admin`.                    |
| `NARVE_RELEASE`                      | various                                       | Sentry release tag.                                      |
| `STAGING_BACKEND_URL`                | `server.py:973`                               | Proxy target for `/staging/*` routes.                    |
| `SLOW_REQUEST_THRESHOLD_MS`          | `middleware/perf.py:46` (default 500)         | Slow-request log threshold.                              |
| `SECURITY_TXT_CONTACT`               | `server.py:3635`                              | `.well-known/security.txt` Contact header.               |
| `SECURITY_TXT_EXPIRES`               | `server.py:3636`                              | `.well-known/security.txt` Expires header. Default `2027-04-08`. |
| `SESSION_COOKIE_TTL_DAYS`            | `auth/cookies.py:35` (default 7)              | Session lifetime in days.                                |
| `GATEWAY_COOKIE_DOMAIN`              | `auth/cookies.py:46`                          | Cookie domain (e.g. `.narve.ai`).                        |
| `GATEWAY_COOKIE_SECURE`              | `server_features.py:212`                      | Legacy flag (forced on in production via `PRODUCTION=1`).|
| `GATEWAY_HOST`                       | `server.py:8603` (default `127.0.0.1`)        | uvicorn bind address.                                    |

### C. Feature toggles / scraper / insider tuning

| Var                                       | Read at                                  | Purpose                                                 |
|-------------------------------------------|------------------------------------------|---------------------------------------------------------|
| `AFFILIATE_PAYOUT_ADMIN_EMAIL`            | `affiliate_routes.py:55`                 | Notifies on payout requests.                            |
| `EMAIL_FORENSIC`                          | `admin_routes.py:959`                    | Recipient of forensic incident emails (preferred over `LEGAL_EMAIL`). |
| `DIGEST_DRY_RUN`                          | (intelligence digest job)                | Print emails instead of sending.                        |
| `STRIPE_LIVE_MODE`                        | `stripe_webhook_routes.py:64`            | Required `true` to accept live-mode webhooks.           |
| `STRIPE_IP_ALLOWLIST_ENFORCE`             | `stripe_webhook_hardening.py:108`        | Force Stripe-IP enforcement outside prod.               |
| `MONITORED_TICKERS`                       | `insider/sec_form4.py:37`                | SEC Form 4 ticker filter.                               |
| `MONITORED_13F_CIKS`                      | `insider/sec_form13f.py:31`              | SEC Form 13F CIK filter.                                |
| `WHALE_WALLETS`                           | (whale ingest)                           | Whale wallet allowlist.                                 |
| `SUBSTACK_FEEDS`                          | `scraper/scrapers/substack.py:38`        | Comma-separated Substack feed URLs.                     |
| `DATA_EXPORT_DIR`                         | `exports/generator.py`                   | Output dir for GDPR exports.                            |
| `DATA_EXPORT_TTL_SECONDS`                 | `exports/generator.py`                   | Signed-URL lifetime.                                    |
| `WEEKLY_REPORTS_DIR`                      | (weekly reports generator)               | Output dir.                                             |
| `ENGAGEMENT_SYNC_FOR_TESTS`               | (engagement worker)                      | Force synchronous sync in tests.                        |
| `ANALYTICS_RATE_LIMIT_PER_MIN`            | (analytics ingest)                       | Per-IP analytics ingest cap.                            |

### D. AI model swappers (mostly duplicated knobs)

Two parallel sets exist; only one is the live path. Document both or
deprecate the dead set.

| Var                       | Read at                          | Notes                                       |
|---------------------------|----------------------------------|---------------------------------------------|
| `EXTRACTION_MODEL`        | `intelligence/claude_usage.py:32`| Default `claude-haiku-4-5-20251001`.        |
| `CATEGORISATION_MODEL`    | `intelligence/claude_usage.py:33`| Default `claude-haiku-4-5-20251001`.        |
| `SUMMARISATION_MODEL`     | `intelligence/claude_usage.py:34`| Default `claude-sonnet-4-5-20250929`.       |
| `INTELLIGENCE_MODEL`      | `intelligence/environmental.py:49`| Used for environmental signals.            |
| `AI_MODEL_EXTRACTION`     | `ai/client.py:38`                | Parallel knob — `ai/client.py` family.      |
| `AI_MODEL_CATEGORISATION` | `ai/client.py:39`                | "                                            |
| `AI_MODEL_SUMMARISATION`  | `ai/client.py:40`                | "                                            |
| `AI_MODEL_ENVIRONMENTAL`  | `ai/client.py:41`                | "                                            |
| `AI_MODEL_CORRELATION`    | `ai/client.py:42`                | "                                            |
| `AI_MODEL_WEEKLY_REPORT`  | `ai/client.py:43`                | "                                            |
| `CLAUDE_DAILY_SPEND_THRESHOLD_USD` | `jobs/ai_jobs.py:33`, `jobs/claude_cost_check.py:32` | Default $50. |
| `CLAUDE_KILL_SWITCH_THRESHOLD_USD` | `jobs/claude_cost_check.py:33`, `queries/integrations.py:176` | Default $200 — flips Anthropic kill switch. |

### E. Test infra — fine to leave out of prod example, but a comment is warranted

`NARVE_AXE_BASE`, `NARVE_BROWSER_HEADED`, `NARVE_LEGACY_CRON_LOOP`,
`NARVE_MOBILE_TEST_BASE`, `NARVE_RUN_AXE`, `NARVE_SCHEDULER_LEADER`,
`NARVE_SKIP_SCHEDULER`, `NARVE_TEST_SERVER`, `PWDEBUG`.

These are read only in `gateway/tests/**` and the playwright conftest.
Recommend a single commented-out `# Test-only env (see gateway/tests/README)` line
rather than per-var entries.

---

## 4) Vars in `.env.example`, **NOT read by gateway** (vestigial or external-only)

43 vars. Split by category.

### A. Owned by sibling dashboard processes (not gateway) — leave but **annotate which process reads them**

| Var                       | Actually read by                                          |
|---------------------------|-----------------------------------------------------------|
| `FIRMS_MAP_KEY`           | `disasters-dashboard/server.py:123`                       |
| `DISASTERS_DISABLE_WARMUP`| `disasters-dashboard/server.py:881`                       |
| `SENTRY_DSN_WHALE`        | `whale-dashboard/observability.py:129`                    |
| `SENTRY_DSN_CENTRALBANK`  | `centralbank-dashboard/observability.py:129`              |
| `SENTRY_DSN_HEALTH`       | `world-health-dashboard/observability.py:129`             |
| `SENTRY_DSN_LOVE`         | `love-dashboard/observability.py:129`                     |
| `LOGTAIL_TOKEN_WHALE`     | `whale-dashboard/server.py:81` AND `queries/integrations.py:425` (admin panel only) |
| `LOGTAIL_TOKEN_CENTRALBANK`| `centralbank-dashboard/server.py:71` AND `queries/integrations.py:426` |
| `LOGTAIL_TOKEN_HEALTH`    | `world-health-dashboard/server.py:134` AND `queries/integrations.py:427` |
| `LOGTAIL_TOKEN_SCRAPER`   | `queries/integrations.py:421` (admin display only; the scraper subprocess itself doesn't read it yet) |
| `LOGTAIL_TOKEN_WORKER`    | `queries/integrations.py:422` (same)                       |
| `SENTRY_DSN_PUBLIC`       | `queries/integrations.py:380` (admin display only; the frontend Sentry init uses a separate path via the HTML template) |

### B. Cron / script-only (not Python-readable)

`BACKUP_GPG_RECIPIENT`, `BACKUP_MAILTO`, `BACKUP_OFFSITE_RETENTION_WEEKS`,
`BACKUP_OFFSITE_RSYNC_OPTS`, `BACKUP_OFFSITE_RSYNC_TARGET` — consumed by
`scripts/backup_offsite.sh` per the comment block on line 323. Correct.

### C. Cloudflare email-routing recipients (external — Cloudflare reads them, not us)

`EMAIL_DMARC`, `EMAIL_FEEDBACK`, `EMAIL_LEGAL`, `EMAIL_PRIVACY`,
`EMAIL_SUPPORT`. These are Cloudflare side; the gateway code does not
consume them. Currently the example labels them clearly. **Recommend**
deleting from `.env.example` OR moving to a separate
`docs/CLOUDFLARE_EMAIL_ROUTING.md` since they aren't process env vars.

### D. Actually dead — recommend deletion

| Var                              | Status                                                              |
|----------------------------------|---------------------------------------------------------------------|
| `FEEDBACK_EMAIL`                 | Commented out at line 29 and never read. Use `SUPPORT_EMAIL`.       |
| `CORS_ORIGINS`                   | Line 21 — referenced only in `SECURITY.md` / `narve_security_audit.md`. No code reads it. Either remove or wire it into the CORS middleware. |
| `TRUSTED_PROXY_IPS`              | Line 17 — same. Code uses a hardcoded `_TRUSTED_PROXY_HOSTS` set in `server.py:1728`. |
| `CAPITOLTRADES_API_KEY`          | `insider/congressional_trades.py:27` uses the public BFF URL; no key. |
| `QUIVERQUANT_API_KEY`            | `insider/congressional_trades.py:28` uses the public beta URL; no key. |
| `UNUSUALWHALES_API_KEY`          | **Wrong name.** Code reads `UNUSUAL_WHALES_TOKEN` (with underscore + suffix `_TOKEN`). See §5. |
| `SEC_EDGAR_USER_AGENT`           | Referenced only in `.env.example` line 176; the EDGAR fetchers (`insider/sec_form4.py`, `insider/sec_form13f.py`) build their UA from a hardcoded string. |
| `DISCORD_APPLICATION_ID`         | No gateway code reads it. Possibly used by the Discord bot subproject (`bots/`). |
| `DISCORD_BOT_TOKEN`              | Same — verify against `bots/`.                                      |

### E. Stripe price IDs documented but not yet live

`STRIPE_PRICE_PRO_ANNUAL`, `STRIPE_PRICE_PRO_MONTHLY`,
`STRIPE_PRICE_TRADER_ANNUAL`, `STRIPE_PRICE_TRADER_MONTHLY`,
`STRIPE_PRICE_TRADING_ADDON_ANNUAL`, `STRIPE_PRICE_TRADING_ADDON_MONTHLY`,
`STRIPE_PUBLISHABLE_KEY` — documented only inside `backend/payments/stripe_stub.py`
as "to implement". OK to keep but mark `# STUB — not wired yet`.

`STRIPE_PRICE_ID_*_MONTHLY` (13 subproducts) — these are read indirectly
via `subproduct.py` → `subproduct_signup_routes.py:62`. My grep matched
them as in-code-but-not-example wrong; corrected: they ARE in example and
ARE indirectly read. Keep as is.

---

## 5) Bugs (name mismatches between code and example)

These are **load-bearing** because the env example is the authoritative
hand-off for ops. Setting the example name does nothing.

| Example name             | Code expects               | Where (code)                         |
|--------------------------|----------------------------|--------------------------------------|
| `UNUSUALWHALES_API_KEY`  | `UNUSUAL_WHALES_TOKEN`     | `gateway/insider/unusual_options.py:28` |
| `SMTP_PASS` (line 27)    | `SMTP_PASSWORD`            | `_env("SMTP_PASSWORD")` in `queries/integrations.py` and elsewhere. Line 86 of the same example correctly has `SMTP_PASSWORD`. **Line 27 is a leftover.** |

---

## 6) Recommended action list (no code changes — doc-only)

Priority order:

1. **Fix the two name mismatches in §5.** Operators following the example
   will silently get a no-op fetcher.
2. **Add the 9 sensitive vars in §3-A** with `# REQUIRED in prod — generate
   with: openssl rand -hex 32` directives. Especially `IP_HASH_SALT` (the
   committed default is a known string).
3. **Deduplicate** the three duplicated keys in §2 and merge the two
   Sentry sections.
4. **Delete or wire up** the vestigial vars in §4-D: `CORS_ORIGINS`,
   `TRUSTED_PROXY_IPS`, `CAPITOLTRADES_API_KEY`, `QUIVERQUANT_API_KEY`,
   `SEC_EDGAR_USER_AGENT`, `FEEDBACK_EMAIL`. Either remove (preferred)
   or implement the read.
5. **Add the operational vars in §3-B** with sensible defaults documented.
6. **Annotate** the sibling-dashboard vars (§4-A) with a comment like
   `# Read by whale-dashboard/server.py, not gateway`.
7. **Consolidate** AI model knobs (§3-D) — pick one family
   (`AI_MODEL_*` vs `EXTRACTION_MODEL` / etc.) and deprecate the other.
8. **Stub-flag** the Stripe Pro/Trader price IDs (§4-E) with
   `# STUB — payments not yet wired (see backend/payments/stripe_stub.py)`.

---

## Appendix: Reproduction commands

```bash
# Direct env reads in the gateway code
grep -rn 'os\.environ\.get\|os\.getenv\|os\.environ\[' \
  /Users/shocakarel/Habbig/gateway/ --include='*.py' \
  | grep -oE '(os\.environ\.get|os\.getenv|os\.environ\[)\s*\(?["'\''][A-Z][A-Z0-9_]*["'\'']' \
  | grep -oE '["'\''][A-Z][A-Z0-9_]*["'\'']' \
  | tr -d '"'\''' | sort -u

# Indirect reads via _env() / getenv() wrappers
grep -rn '_env(\|getenv(' /Users/shocakarel/Habbig/gateway/ --include='*.py' \
  | grep -oE '(_env|getenv)\(\s*["'\''][A-Z][A-Z0-9_]*["'\'']' \
  | grep -oE '["'\''][A-Z][A-Z0-9_]*["'\'']' \
  | tr -d '"'\''' | sort -u

# Keys in .env.example (commented or active)
grep -oE '^\s*#?\s*[A-Z][A-Z0-9_]*=' /Users/shocakarel/Habbig/gateway/.env.example \
  | sed -E 's/[^A-Z0-9_]//g' | sort -u
```
