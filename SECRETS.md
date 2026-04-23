# Secrets — inventory + rotation runbook

> **Scope:** every value that's secret, where it lives, how often it rotates,
> and the exact steps to rotate it. This file must stay in sync with
> `gateway/.env.example` (the variable catalogue) and `gateway/config.py`
> (the validator that ensures these are present + shape-correct at startup).
>
> **If you commit a secret to git:** rotate it immediately in the upstream
> service (Stripe dashboard, Anthropic console, etc.) and add a line to
> `SECURITY_LOG.md`. Do NOT attempt to rewrite git history — assume the
> history is already mirrored somewhere it can't be reached.

## Inventory

| Secret | Purpose | Location | Rotation interval | Validator in config.py |
|---|---|---|---|---|
| `SITE_ACCESS_TOKEN` | Gate password (pre-release). Hashed for comparison. | Server `.env` (600) | Quarterly | REQUIRED, min 16 chars |
| `GATEWAY_COOKIE_SECRET` | HMAC key for session + CSRF + pending-token cookies | Server `.env` (600) | On compromise only | REQUIRED, min 32 chars |
| `CREDENTIALS_ENCRYPTION_KEY` | Fernet key for Kalshi/Polymarket stored tokens | Server `.env` (600) | **Never rotate** — rotation would break every stored credential. If compromised, start a new key + force every user to re-connect. | REQUIRED, min 32 chars |
| `STRIPE_SECRET_KEY` | Stripe API auth (sk_live_… in prod, sk_test_… in dev) | Server `.env` (600) | Annually, or immediately on compromise | CONDITIONAL, prefix `sk_` |
| `STRIPE_WEBHOOK_SECRET` | Webhook signature verification (whsec_…) | Server `.env` (600) | Annually | CONDITIONAL, prefix `whsec_` |
| `ANTHROPIC_API_KEY` | Claude API auth (sk-ant-…) | Server `.env` (600) | Annually | OPTIONAL, prefix `sk-ant-` |
| `EXTENSION_JWT_SECRET` | HMAC key for browser-extension JWTs | Server `.env` (600) | Annually (short-lived tokens, but key rotation still matters) | — |
| `SCRAPER_API_KEY` | Shared secret gateway ↔ scraper sidecar | Server `.env` (600), plus scraper box `.env` | Quarterly, or on scraper host redeploy | — |
| `EMAIL_RELAY_SECRET` | Shared secret with the Cloudflare Email Worker | Server `.env` + Worker secret store | Annually | — |
| `SMTP_PASSWORD` | Outbound email auth (Mailchannels / Resend / SES) | Server `.env` (600) | Provider-driven (many auto-rotate) | — |
| `SENTRY_AUTH_TOKEN` | Sentry REST API for the admin dashboard tile | Server `.env` (600) | Annually | — |
| `LOGTAIL_TOKEN_APP` | BetterStack log shipping | Server `.env` (600) | Annually | — |
| `TELEGRAM_BOT_TOKEN` | Telegram bot identity | Server `.env` (600) | On compromise only | — |
| `DISCORD_BOT_TOKEN` | Discord bot identity | Server `.env` (600) | On compromise only | — |
| `FEC_API_KEY` / `CAPITOLTRADES_API_KEY` / `UNUSUALWHALES_API_KEY` / `QUIVERQUANT_API_KEY` | Insider-data provider API keys | Server `.env` (600) | Provider-driven; monitor usage dashboards | — |
| `PUSH_VAPID_PRIVATE_KEY_PEM` | Web-Push payload signing | Server `.env` (600) | **Never rotate** — rotation invalidates every subscribed browser; would need a re-subscribe flow | — |
| `REDIS_PASSWORD` | Redis auth (when ARQ backend is active) | Server `.env` + Redis config | On compromise only | — |

## Generation commands (copy-paste)

```bash
# SITE_ACCESS_TOKEN / GATEWAY_COOKIE_SECRET / EXTENSION_JWT_SECRET / SCRAPER_API_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# CREDENTIALS_ENCRYPTION_KEY — Fernet (produces 32 url-safe base64 bytes)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# REDIS_PASSWORD
openssl rand -hex 32

# VAPID keypair (first-time setup only; never rotate)
python3 -c "from py_vapid import Vapid01; v=Vapid01(); v.generate_keys(); \
  v.save_key('vapid_private.pem'); v.save_public_key('vapid_public.pem')"
```

## Rotation procedure — generic template

For any secret below:

1. **Generate** the new value using the command in the table above, or the
   provider dashboard.
2. **Stage** — update the server `.env` (owner-only `600` perms) but do not
   restart yet; run the config validator dry-run:
   ```bash
   ssh julianhabbig@100.69.44.108 "cd ~/Habbig/gateway && \
     python3 -c 'import config; errs = config.validate_config(); \
     print(\"ok\" if not errs else \"\\n\".join(errs))'"
   ```
3. **Restart** `uvicorn` (or the matching systemd unit). The startup hook
   runs `config.validate_config()` again — missing / shape-broken vars
   cause `sys.exit(2)` so the server won't come back up.
4. **Verify** the feature that uses the secret (see per-secret smoke test
   below).
5. **Retire the old value** in the upstream service / dashboard.
6. **Log** — append a one-line entry to `SECURITY_LOG.md`:
   ```
   2026-04-22  rotated STRIPE_WEBHOOK_SECRET  (annual)  verified via test event in dashboard
   ```

## Per-secret rotation

### STRIPE_SECRET_KEY

1. Stripe dashboard → Developers → API keys → **Create restricted key** (or
   roll the existing standard key).
2. Set `STRIPE_SECRET_KEY=sk_live_…` (or `sk_test_…` in staging) in server `.env`.
3. Restart `uvicorn`.
4. **Verify:** send a test event via Stripe → Developers → Webhooks → Send test
   webhook, confirm handler 200s in logs.
5. In Stripe, **Reveal & revoke** the old key.

### STRIPE_WEBHOOK_SECRET

1. Stripe dashboard → Developers → Webhooks → your endpoint → **Signing secret**
   → **Roll**.
2. Copy new secret to `STRIPE_WEBHOOK_SECRET` in server `.env`.
3. Restart.
4. **Verify:** Stripe dashboard → Send test webhook → response 200 in narve
   logs with no `Signature verification failed` entry.
5. Old secret is auto-revoked by Stripe at roll.

### ANTHROPIC_API_KEY

1. Anthropic console → API Keys → **Create key**.
2. Copy to `ANTHROPIC_API_KEY` in server `.env`.
3. Restart.
4. **Verify:** trigger one Claude call via `/admin/ai-usage` "Send test" button
   (or any live Claude feature) and confirm the request appears in the
   Anthropic console usage log.
5. Anthropic console → old key → **Revoke**.

### SITE_ACCESS_TOKEN (gate password)

1. Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Set `SITE_ACCESS_TOKEN=…` in server `.env`.
3. Restart.
4. **Verify:** open a private-window `/gate`, enter the new token, confirm
   redirect to `/dashboards`.
5. **Distribute** the new token to the current invite list out-of-band
   (never email it; use 1Password shared link or equivalent).
6. Log to `SECURITY_LOG.md`.

### GATEWAY_COOKIE_SECRET

**Rotation invalidates every active session + pending-token cookie.** Only
rotate on suspected compromise.

1. Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. Set `GATEWAY_COOKIE_SECRET=…`.
3. Restart.
4. Every signed-in user will be logged out on next request. Expected.
5. **Verify:** sign in via a fresh browser, exercise 1 protected route, confirm
   the session persists.

### CREDENTIALS_ENCRYPTION_KEY (Fernet)

**Never rotate in place** — encrypted rows in `credentials` would become
undecryptable. If compromised:

1. Generate NEW Fernet key.
2. Set both `CREDENTIALS_ENCRYPTION_KEY` (new) and
   `CREDENTIALS_ENCRYPTION_KEY_FALLBACK` (old) on the server.
3. Deploy code change that tries new key first, falls back to old on decrypt
   failure.
4. Run a one-time job that re-encrypts every `credentials` row with the new
   key.
5. Remove the fallback env var.

(This path isn't in code yet — filing as a follow-up ticket the moment it's
needed rather than building it speculatively.)

### SCRAPER_API_KEY

1. Generate new value; set it in BOTH the gateway `.env` AND the scraper
   sidecar `.env`.
2. Restart gateway + scraper in either order — the scraper starts issuing
   `Authorization: Bearer <new>`, the gateway accepts it.
3. If they're out of sync for more than a few seconds, the scraper's
   in-flight POSTs 401 and retry once the peer catches up (retry logic is
   already in `scraper/transport.py`).

## Never do

- Never commit secrets to git — the TruffleHog CI workflow
  (`.github/workflows/secret-scan.yml`) will fail the PR, but don't rely
  on that as your primary defence.
- Never log secret values. Structured-log sanitiser strips env-style
  strings but a `log.debug(env)` dump would leak.
- Never echo secrets to terminal (where they land in `~/.zsh_history`).
  Use `pass`, 1Password CLI, or paste into the `.env` directly.
- Never share over email or Slack. Use 1Password shared vault links or
  Signal.
- Never put in client-side code — no secrets in HTML / JS / inline
  `<script>` blocks. The `SENTRY_DSN_PUBLIC` frontend DSN is an
  exception; it's designed to be public (project write-only).

## Related files

- `gateway/.env.example` — canonical list of every env var (REQUIRED /
  OPTIONAL annotations).
- `gateway/config.py` — startup validator; enforces REQUIRED vars exist
  and shape-correct secrets (Stripe/Anthropic prefix checks).
- `.github/workflows/secret-scan.yml` — TruffleHog scans every push for
  committed secrets.
- `SECURITY_LOG.md` — append-only rotation log.
- `DEPLOY.md` — env-per-environment layout (local vs staging vs prod).
