# PII / Sensitive-Data Log Audit — narve.ai gateway

**Date:** 2026-05-15
**Auditor:** Automated grep sweep of `log.info/error/warning/debug` calls
**Method:** Synchronous grep over `gateway/**/*.py` for log statements that
interpolate values matching `password|token|secret|api_key|cookie|email`.
**Scope:** Read-only audit, no code changes.

---

## 1. Method

Primary command (verbatim, as specified):

```bash
grep -rn "log\.\(info\|error\|warning\|debug\)" gateway/ --include='*.py' \
  | grep -iE "password|token|secret|api_key|cookie"
```

Supplementary scans:

```bash
# Freeform email leakage
grep -rn "log\.\(info\|error\|warning\|debug\)" gateway/ --include='*.py' \
  | grep -iE "email"

# Stripe customer-id leakage (cus_ prefix, customer=)
grep -rn "log\.\(info\|error\|warning\|debug\)" gateway/ --include='*.py' \
  | grep -iE "stripe|customer_id|cus_"
```

Note on existing defence: `gateway/logging_config.py` ships a
`_redact_message` pass that catches `bearer <token>`, `password=<value>`,
`token=<value>`, `api_key=<value>` and `scheme://user:pass@host` shapes in
log message strings, and a per-field key scrub for structured extras. It
does **not** redact:

- Freeform emails interpolated as `%s` arguments
- Truncated token prefixes (e.g. `%s...` showing first 8 chars)
- Stripe customer IDs (`cus_…`) — no pattern for these
- Usernames

So the legitimate audit trail (admin-action attribution) is intact, but
some interpolations below still leak data that the redaction layer
cannot catch.

---

## 2. Headline numbers

| Bucket | Count |
|---|---|
| Logs matching `password|token|secret|api_key|cookie` | **36** |
| Logs interpolating freeform `email` value (raw, not `mask_email`) | **48** of 67 email-mentioning lines |
| Logs touching Stripe customer/sub identifiers in plaintext | **24** |
| **Total PII-leaking log lines (deduped, after manual review)** | **~52** |

Only **one** call site (`server.py:3840`) uses `db.mask_email()` before
interpolating an email. Every other `log.info("%s", email)` writes the
full address.

---

## 3. Top 3 worst offenders

### 3.1 `gateway/server_features.py:1692` — registration log writes
**raw token prefix AND raw email together**

```python
log.info("auth.register: user_id=%d email=%s via token=%s...",
         user_id, email, raw_token[:8])
```

Why it's the worst:
- Full plaintext email
- First 8 chars of an invite-token (entropy: 8 base32-ish chars = ~40
  bits, enough to narrow a brute-force to a small search space when
  paired with the `user_id` it's logged next to)
- `log.info` → reaches BetterStack ingestion and the rotating local file
- The redaction regex `(?i)(password|...|token|...)=[^\s&\"']{6,}` does
  NOT match `token=%s...` because the trailing `...` literal is in the
  format string, and the truncated 8-char value is short. The redactor
  is bypassed twice over.

### 3.2 `gateway/public_routes.py:171` — checkout log writes **full email + plan + interval**

```python
log.info("Subscription checkout: %s -> %s (%s), token generated",
         email, plan, interval)
```

Why it's bad:
- Full plaintext email of every paying-intent visitor lands in logs
- Unauthenticated endpoint — anyone can spray this by hitting
  `/api/subscribe-checkout`
- Easily generates an enumerable mailing-list of subscribers' emails
  in BetterStack
- Sibling line `public_routes.py:93` (`"New enquiry from %s (%s)", email,
  job_title`) and `public_routes.py:221` (`"Support ticket from %s", email`)
  have the same shape and same exposure.

### 3.3 `gateway/server.py:402` — startup logs partial admin invite token

```python
log.info("  FIRST ADMIN INVITE TOKEN: %s... (query DB for full value)",
         first_token[:12])
```

Why it's bad:
- A 12-char prefix of an invite token that can claim the **first admin
  seat** of a fresh deployment is written to logs at boot
- Fires unconditionally during cold-start; any redeploy regenerates
  and re-logs
- Same redaction-bypass story as 3.1 — the literal `...` and short
  truncated value evade `_MESSAGE_REDACT_PATTERNS`
- Honourable mention: the same admin-token prefix logging pattern
  appears 5 more times — `server.py:5711` (admin-generated invite),
  `server.py:5863` (per-enquiry token), `server.py:6117` (super-admin
  token reissue), `server_features.py:1692` (registration). Each
  writes `new_token[:8]` + admin email. Eight chars of token is
  ~48 bits when the alphabet is base64-url-safe — not catastrophic,
  but combined with logged email and user_id this is a clear PII +
  partial-credential leak pattern.

---

## 4. Full inventory — category breakdown

### 4.1 Token-prefix leaks (`new_token[:8]` / `[:12]` interpolated)

| File:line | Field logged |
|---|---|
| `server.py:402` | first admin invite token, 12-char prefix, **boot log** |
| `server.py:5711` | admin-generated invite token, 8-char prefix + admin email + target email |
| `server.py:5863` | per-enquiry invite token, 8-char prefix + admin email + enquiry email |
| `server.py:6117` | super-admin token reissue, 8-char prefix + admin email |
| `server_features.py:1692` | registration, 8-char prefix + user email + user_id |

All five lines log a token prefix alongside an email. Concatenated,
this is a partial-credential leak. The 8 characters are statistically
unguessable on their own; the concern is correlation in log search.

### 4.2 Full-email leaks (`%s` against raw email, no `mask_email`)

48 log lines write a complete email address. Highest-traffic call sites:

| File:line | Context |
|---|---|
| `public_routes.py:93` | every contact-form enquiry — **unauthenticated** |
| `public_routes.py:171` | every checkout-token mint — **unauthenticated** |
| `public_routes.py:221` | every support ticket submitted — **unauthenticated** |
| `server.py:3878` | password reset success — `user["username"] or user["email"]` |
| `server.py:4537` | every subscription event |
| `server.py:4771` | self-delete initiation — `account.delete` log line |
| `server.py:4853` | every password change |
| `server.py:5174` | password reset completion |
| `server.py:5221` | admin rate-limit trip — `user.get("email")` |
| `server.py:5711`, `5740`, `5807`, `5830`, `5863`, `6031`, `6068`, `6090`, `6117`, `6145`, `6170`, `6205`, `6255` | admin-action audit trail — admin's email + sometimes target's email |
| `admin_routes.py:154` | impersonation start — admin email + reason |
| `billing_routes.py:1025`, `:1045`, `:1073`, `:1098` | every billing self-service action |
| `intelligence_routes.py:258` | Pro user force-refresh |
| `status_routes.py:527`, `:563`, `:628` | incident management — admin email |

A meaningful fraction is the legitimate admin-audit trail (super-admin
attribution), which `logging_config.py` explicitly carves out of
redaction. But the unauthenticated-endpoint leaks
(`public_routes.py:{93,171,221}`) and the
self-service ones (`billing_routes.py`, `intelligence_routes.py`)
are not audit-trail material and should mask.

### 4.3 Stripe identifier leaks

| File:line | What it logs |
|---|---|
| `stripe_webhook_routes.py:184` | `(sub=%s cust=%s)` — raw `cus_…` and `sub_…` |
| `stripe_webhook_hardening.py:130` | `non-Stripe IP %s` (IP, not customer — informational) |
| `stripe_webhook_hardening.py:165` | `livemode=%s production=%s type=%s id=%s` (event id only — OK) |
| `subproduct_access.py:184` | `live stripe verify failed for %s` — sub id |
| `jobs/reconcile_subscriptions.py:59` | `stripe fetch failed for %s` — sub id |

`cus_…` and `sub_…` IDs are PII-adjacent (not directly identifying
without DB access, but unique per user). The only call site that
combines them with an email-like value is `stripe_webhook_routes.py:184`
inside an exception path — low frequency, low severity.

### 4.4 Cookie / session-token leaks

| File:line | Verdict |
|---|---|
| `affiliate_routes.py:104` | logs the affiliate cookie value via `%r` — `cookie %r does not resolve` — **flag**: the affiliate code is short and low-entropy, but logging it does leak the referral-attribution mechanism |
| `server.py:386`, `389`, `392`, `395` | startup FATALs about cookie/site-access secrets being unset or short — log message contains no secret value, only its length. Safe. |
| `security/timezones.py:160` | `set_cookie failed: %s` — exception, not value. Safe. |

### 4.5 API-key / secret / bot-token leaks

All instances flagged by the primary grep are **error-path** logs that
say a key is *missing*, *unset*, or *failed to decrypt*. None
interpolate the key value itself.

| File:line | Verdict |
|---|---|
| `scraper/transmission/pusher.py:83` | `SCRAPER_API_KEY not set` — safe |
| `insider/fec_campaign.py:34` | `no FEC_API_KEY configured` — safe |
| `insider/unusual_options.py:30` | `no UNUSUAL_WHALES_TOKEN` — safe |
| `integrations/telegram_bot.py:44` | `TELEGRAM_BOT_TOKEN not set` — safe |
| `backend/markets/encryption.py:49` | `Storing token without encryption — set CREDENTIALS_ENCRYPTION_KEY` — safe |
| `backend/markets/kalshi_client.py:243` | `Kalshi service token rejected, refreshing` — safe |
| `portfolio/kalshi.py:71` | `Kalshi token decrypt failed: %s` — exception, not key value. Safe. |
| `stripe_webhook_routes.py:259` | `STRIPE_WEBHOOK_SECRET not set` — safe |
| `queries/api_keys.py:287` | `record_usage failed key_id=%s` — internal key_id, not raw key. Safe. |
| `embed_tokens.py:65` | warns that `EMBED_SIGNING_SECRET` is unset — safe |

### 4.6 Password leaks

Zero. Every `password`-mentioning log is a *reset attempted* / *mismatch*
/ *changed* event, not the value. `server.py:3840` correctly uses
`db.mask_email()`. `server_features.py:1749` (`"wrong password for
user_id=%d"`) logs only the user id.

---

## 5. Severity summary

| Severity | Count | Pattern |
|---|---|---|
| **High** | 5 | Token prefix + email in same log line (4.1 list) |
| **Medium** | ~43 | Full email in unauthenticated or self-service logs (4.2 list, excluding admin-audit-trail subset) |
| **Low / informational** | ~24 | Stripe sub-id leakage, affiliate-code cookie log, admin audit trail with email-by-policy |
| **Safe / false positive** | ~25 | "secret missing", "key not set", exception messages without value |

---

## 6. Recommendations (no code change in this audit)

1. Replace freeform `email` interpolations in
   `public_routes.py:{93,171,221}` with `db.mask_email(email)` — these
   are unauthenticated entry points and have no audit-trail
   justification.
2. Drop the `new_token[:8]` / `[:12]` truncated-token-prefix idiom
   from `server.py:402,5711,5740,5863,6117` and `server_features.py:1692`.
   Either log the token id (`token_id=%d`) or omit entirely. The
   prefix is not useful for incident response and the correlation
   with the user/admin email weakens it.
3. Extend `_MESSAGE_REDACT_PATTERNS` in `logging_config.py` to
   redact:
   - 6-12 character base32/base64 fragments that follow the literal
     `token=` or `token: ` or `token %s...` shapes
   - Bare email shapes (`\S+@\S+\.\S+`) in non-admin-attribution
     loggers — gate by logger name (`security` / `audit` keep them).
4. Audit `stripe_webhook_routes.py:184` to drop `cust=%s` from the
   exception log — `sub=%s` is sufficient for correlating to the
   subscriptions table.

---

## 7. Reproducibility

Re-run with:

```bash
grep -rn "log\.\(info\|error\|warning\|debug\)" \
  /Users/shocakarel/Habbig/gateway/ --include='*.py' \
  | grep -iE "password|token|secret|api_key|cookie"
```

Audit timestamp: 2026-05-15. Branch: `feature/platform-build`.
