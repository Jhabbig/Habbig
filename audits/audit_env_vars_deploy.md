# Audit: production env-var inventory before tonight's deploy

**Date:** 2026-05-15
**Host:** `julianhabbig@100.69.44.108`
**Process:** PID `4077346` — `python3 -m uvicorn server:app --host 127.0.0.1 --port 7000`
**Owner:** `julianhabbig` (no sudo required to read `/proc/$PID/environ`)
**Cwd:** `/home/julianhabbig/Habbig/gateway`
**PRODUCTION:** `1` (so every `IS_PRODUCTION` startup guard is armed)

## Headline counts

| Bucket | Count | Names |
|---|---|---|
| **SET** on running uvicorn | 4 / 9 required | `GATEWAY_SSO_SECRET`, `CREDENTIALS_ENCRYPTION_KEY`, `EXTENSION_JWT_SECRET`, plus implicit `BACKGROUND_JOBS_HMAC_KEY` (reuses `GATEWAY_SSO_SECRET`) |
| **MISSING — deploy-blocking** | 2 | `IP_HASH_SALT`, `SUBPRODUCT_MAGIC_LINK_SECRET` |
| **MISSING — silent-degrade** | 3 | `STRIPE_LIVE_MODE`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_IP_ALLOWLIST_ENFORCE` |
| **UNKNOWN** (asked-about, not a real env var) | 1 | `BACKGROUND_JOBS_HMAC_KEY` — does NOT exist as a separate env in the codebase |

**Deploy-blockers if uvicorn restarts on tonight's fixes:** **2**
(`IP_HASH_SALT`, `SUBPRODUCT_MAGIC_LINK_SECRET`).

## Method

1. SSH to `julianhabbig@100.69.44.108`.
2. `tr '\0' '\n' < /proc/4077346/environ | cut -d= -f1 | sort -u` to enumerate
   env-var **names only** on the live process (values never read).
3. For each required name, locate the startup guard or signing helper in the
   repo at `/Users/shocakarel/Habbig/gateway/`:
   - `gateway/config.py` (`validate_config()` — `sys.exit(2)` on miss)
   - `gateway/server.py` lifespan (lines 394-438 — `RuntimeError` on miss)
   - `gateway/subproduct_signup_routes.py` (`_ensure_magic_link_secret_configured`)
   - `gateway/extension_routes.py` (`_jwt_secret`)
   - `gateway/jobs/registry.py` (`_job_hmac_secret`)
   - `gateway/stripe_webhook_routes.py` / `gateway/stripe_webhook_hardening.py`
4. Classify each as SET / MISSING and identify whether absence prevents
   startup under `PRODUCTION=1`.

## Full env-var list currently on PID 4077346

15 vars total. Names only (values intentionally not captured):

```
_
CREDENTIALS_ENCRYPTION_KEY
EXTENSION_JWT_SECRET
GATEWAY_COOKIE_SECRET
GATEWAY_SSO_SECRET
HOME
LANG
LOGNAME
OLDPWD
PATH
PRODUCTION
PWD
SHELL
SHLVL
SITE_ACCESS_TOKEN
```

Note: this is the env captured **at process start (May 15 00:05 UTC)**. Anything
the user has added to `~/.bashrc`, a systemd unit, a `.env` file, or a wrapper
script **after** that timestamp is NOT reflected here and will only land on
the next restart. The audit treats "what restart will see" as identical to
"what `/proc/$PID/environ` shows", which is the worst-case assumption for a
fail-closed startup guard.

## Required vars — per-row verdict

### 1. `GATEWAY_SSO_SECRET` — SET — OK

- **In running env:** yes.
- **Guard:** `gateway/server.py:411-416` — `RuntimeError("GATEWAY_SSO_SECRET
  must be set in production")` and a separate `len < 32` refuse.
- **Risk on restart:** none if the existing value is ≥32 chars. Cannot verify
  length from /proc without reading the value (deliberately not done).
- **Action:** confirm `wc -c` of the deploy-secrets source shows ≥32 chars
  for this key before restart. Not deploy-blocking unless length fails.

### 2. `IP_HASH_SALT` — **MISSING** — **DEPLOY-BLOCKING**

- **In running env:** no.
- **Guard:** `gateway/server.py:423-433`:
  - `not IS_PRODUCTION` branch logs a warning and uses
    `_IP_HASH_SALT_DEV_FALLBACK` — but `IS_PRODUCTION=1` here.
  - `IS_PRODUCTION and not _IP_HASH_SALT_ENV` →
    `RuntimeError("IP_HASH_SALT must be set in production (per-deploy
    random ≥32 chars)")`.
  - `len < 32` → `RuntimeError("IP_HASH_SALT must be ≥32 characters")`.
- **Risk on restart:** uvicorn refuses to start. **Deploy will fail.**
- **Action:** **set `IP_HASH_SALT` to a fresh `secrets.token_urlsafe(48)`
  value before tonight's restart.**

### 3. `CREDENTIALS_ENCRYPTION_KEY` — SET — OK (verify Fernet)

- **In running env:** yes.
- **Guard:** `gateway/config.py:94-98` validates `len >= 32`. Fernet's own
  loader will throw at first use (not at startup) if the bytes aren't a
  valid base64-encoded 32-byte key, so a 32+ char string that isn't a
  proper Fernet key will pass `validate_config` but fail in
  `gateway/backend/markets/encryption.py` on the next Polymarket/Kalshi
  credential read.
- **Risk on restart:** startup OK if length passes. Per-feature decrypt
  will fail with a Fernet error if the key isn't truly a Fernet key.
- **Action:** confirm the deployed value is the output of
  `Fernet.generate_key().decode()` (44 chars, ends with `=`). Not deploy-
  blocking but worth a one-liner check.

### 4. `EXTENSION_JWT_SECRET` — SET — OK

- **In running env:** yes.
- **Guard:** `gateway/extension_routes.py:47-55`. No startup-refuse — falls
  back to `GATEWAY_COOKIE_SECRET` (also set), then to a hardcoded dev
  string if both are empty. Production deploys are expected to set it
  explicitly, which this one does.
- **Risk on restart:** none.
- **Action:** none.

### 5. `SUBPRODUCT_MAGIC_LINK_SECRET` — **MISSING** — **DEPLOY-BLOCKING**

- **In running env:** no.
- **Guard:** `gateway/subproduct_signup_routes.py:97-129`
  (`_ensure_magic_link_secret_configured`, invoked from `register(app)` so
  it runs at startup):
  - `not secret` in production → `RuntimeError("SUBPRODUCT_MAGIC_LINK_SECRET
    must be set in production (signs single-use magic-link auth tokens
    used by the subproduct Stripe-Checkout success redirect)")`.
  - `len < 32` → `RuntimeError("SUBPRODUCT_MAGIC_LINK_SECRET must be at
    least 32 characters")`.
- **Risk on restart:** uvicorn refuses to start. **Deploy will fail.**
- **Action:** **set `SUBPRODUCT_MAGIC_LINK_SECRET` to a fresh
  `secrets.token_urlsafe(48)` value before tonight's restart.**

### 6. `BACKGROUND_JOBS_HMAC_KEY` — **UNKNOWN / does not exist**

- **In running env:** n/a.
- **Codebase reality:** there is **no `BACKGROUND_JOBS_HMAC_KEY` env var**.
  The retry_job HMAC fix in `gateway/jobs/registry.py:120-141`
  (`_job_hmac_secret`) reuses `GATEWAY_SSO_SECRET` (primary) and falls back
  to `EMBED_SIGNING_SECRET`, then to an in-memory `secrets.token_urlsafe(32)`
  that doesn't survive restart.
- **Risk on restart:** since `GATEWAY_SSO_SECRET` is set, retry_job HMACs
  will be computed with a stable secret and `retry_job(row_id)` will verify
  on rows enqueued before AND after the restart (HMAC depends only on
  GATEWAY_SSO_SECRET + canonical payload). **No deploy block.** However,
  if `GATEWAY_SSO_SECRET` is **rotated** as part of the deploy, every row
  enqueued before the rotation will fail HMAC verification on retry — a
  one-time data quirk, not a startup failure.
- **Action:** confirm the task brief's mention of `BACKGROUND_JOBS_HMAC_KEY`
  was either (a) shorthand for "ensure GATEWAY_SSO_SECRET stays stable" or
  (b) a planned new env var that hasn't been introduced yet. If it's a new
  env var, the codebase needs a corresponding read site before it can do
  anything.

### 7. `STRIPE_LIVE_MODE` — MISSING — not deploy-blocking, but silent-degrade

- **In running env:** no.
- **Guard:** `gateway/stripe_webhook_routes.py:61-66` — default false. When
  false, a `livemode=True` Stripe event is rejected by `reject_mode_mismatch`.
- **Risk on restart:** uvicorn starts fine. **All real Stripe webhooks will
  be silently rejected** at the mode-mismatch gate. Any subscription event
  that arrives during the deploy window will be dropped and the customer's
  tier will not flip until the webhook is replayed.
- **Action:** decide whether tonight's deploy goes live on Stripe. If yes,
  set `STRIPE_LIVE_MODE=true`. If staging-only / replay-only, leave unset.

### 8. `STRIPE_WEBHOOK_SECRET` — MISSING — conditional

- **In running env:** no.
- **Guard:** `gateway/config.py:111-133` (`CONDITIONAL_VARS`). Only required
  when `STRIPE_PRICE_ID_TRADERS_MONTHLY` is set. That price-ID env var is
  also not in the running env, so the condition is dormant and
  `validate_config()` will not fail on this row.
- **Risk on restart:** uvicorn starts fine. But if Stripe webhooks are
  enabled (per #7 above), `gateway/stripe_webhook_routes.py` will fail
  signature verification on every event and return 400. End result: same
  silent-degrade as #7.
- **Action:** if Stripe is going live, set both `STRIPE_WEBHOOK_SECRET`
  (`whsec_...`) and `STRIPE_PRICE_ID_TRADERS_MONTHLY`. Setting the
  webhook secret alone without the price ID is harmless (and lets the
  conditional check pass if/when the price ID lands later).

### 9. `STRIPE_IP_ALLOWLIST_ENFORCE` — MISSING — not deploy-blocking

- **In running env:** no.
- **Guard:** `gateway/stripe_webhook_hardening.py:99-111` — when unset,
  defaults to **enforce in production**. The default is the safe value, so
  the missing var is fine.
- **Risk on restart:** none. Production will keep enforcing the Stripe IP
  allowlist on webhook traffic.
- **Action:** none unless Stripe rotates its IP ranges and we need a
  temporary `STRIPE_IP_ALLOWLIST_ENFORCE=false` escape hatch.

## Deploy-blocking summary (read this before restart)

If tonight's restart happens with the env shown above, uvicorn will exit
with `RuntimeError` during `lifespan()` startup on:

1. **`IP_HASH_SALT`** — `gateway/server.py:423-433` (must be ≥32 chars).
2. **`SUBPRODUCT_MAGIC_LINK_SECRET`** —
   `gateway/subproduct_signup_routes.py:97-129` (must be ≥32 chars).

Both raise `RuntimeError` BEFORE the first request lands, so a deploy that
doesn't pre-set these two will leave port 7000 dark and the gateway 502'ing
behind Cloudflare. Plan: add both to the deploy-secrets source (whichever
mechanism populated `GATEWAY_SSO_SECRET`, `CREDENTIALS_ENCRYPTION_KEY`,
`EXTENSION_JWT_SECRET`, etc. on the previous boot) before re-running the
restart command.

Generation suggestion (run locally, paste into the secrets source):

```
python3 -c "import secrets; print('IP_HASH_SALT=' + secrets.token_urlsafe(48))"
python3 -c "import secrets; print('SUBPRODUCT_MAGIC_LINK_SECRET=' + secrets.token_urlsafe(48))"
```

## Silent-degrade summary (Stripe)

If `STRIPE_LIVE_MODE` stays unset and any real (live-mode) Stripe webhooks
arrive after the restart, the gateway will silently reject them at the
mode-mismatch gate. Symptoms in logs: `stripe.webhook` entries with
`reject_mode_mismatch`. Effect on users: subscription tier changes do not
propagate until the event is manually replayed. Decide live-mode status
explicitly before tonight's deploy and set the trio together if going live:

```
STRIPE_LIVE_MODE=true
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_TRADERS_MONTHLY=price_...   # also enables config validation
```

## Note on `BACKGROUND_JOBS_HMAC_KEY`

The task brief lists `BACKGROUND_JOBS_HMAC_KEY` as a required env, but the
codebase does not read that name anywhere. The retry_job HMAC fix from
audit #14 / HIGH-21 reuses `GATEWAY_SSO_SECRET` via
`gateway/jobs/registry.py::_job_hmac_secret`. As long as `GATEWAY_SSO_SECRET`
is set (it is) and not rotated during the deploy, retry_job HMAC continues
to verify rows on either side of the restart. If you want a dedicated env
var for this — semantically cleaner, lets you rotate the SSO secret without
breaking job retries — that's a code change, not a deploy-time fix.

## Files referenced

- `/Users/shocakarel/Habbig/gateway/config.py` — `REQUIRED_VARS`, `CONDITIONAL_VARS`,
  `validate_config()` (`sys.exit(2)` on miss).
- `/Users/shocakarel/Habbig/gateway/server.py` lines 394-438 — lifespan
  guards for `GATEWAY_COOKIE_SECRET`, `SITE_ACCESS_TOKEN`,
  `GATEWAY_SSO_SECRET`, `IP_HASH_SALT`.
- `/Users/shocakarel/Habbig/gateway/subproduct_signup_routes.py` lines
  60-130 — `_magic_link_secret` + startup guard.
- `/Users/shocakarel/Habbig/gateway/extension_routes.py` lines 47-55 —
  `_jwt_secret`.
- `/Users/shocakarel/Habbig/gateway/jobs/registry.py` lines 120-174 —
  `_job_hmac_secret`, `compute_job_hmac`, `verify_job_hmac` (REUSES
  `GATEWAY_SSO_SECRET`).
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py` lines 61-66 —
  `_stripe_live_mode_enabled`.
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_hardening.py` lines 99-111 —
  `_allowlist_enforced`.
