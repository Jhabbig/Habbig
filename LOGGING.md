# Logging

Operational logs + audit trail for the narve.ai gateway.

## Boot

Logging is set up in one line at server startup — do not add a second
configuration:

```python
# gateway/server.py (already present)
from logging_config import configure_logging, get_logger
configure_logging(base_dir=BASE_DIR)
log = get_logger("gateway")
```

`configure_logging()` is idempotent per-process — calling it twice is a
no-op after the first.

## Levels

| Level    | When to use |
|----------|-------------|
| `DEBUG`   | Development only. Disabled in production. Noisy diagnostics (loop counters, retry bodies). |
| `INFO`    | Normal operations worth recording: login, webhook received, job started, subscription change. |
| `WARNING` | Unexpected but recoverable: rate-limit trip, stale cache served, retry fired, feature flag missing with fallback. |
| `ERROR`   | Failed operation needing attention: query timeout, upstream API 5xx that won't retry, assertion failure. |
| `CRITICAL`| System-level failure requiring immediate action: database unreachable, migration stuck, boot failure. |

Rules of thumb the audit should catch:

* `logger.info("email failed to send")` — usually wrong. If retry will
  fire, use `WARNING`. If permanently lost, use `ERROR`.
* `logger.error("unexpected response shape")` — usually wrong. If it's
  intermittent (1-in-10k), `ERROR` with enough context to debug. If
  it's every request after a provider change, `WARNING` + a fix PR.
* `logger.info("query returned 0 rows")` — almost always `DEBUG`.

## Format

JSON per line, one field per line is not supported (jq first). Every
log record carries these fields at minimum:

```json
{
  "timestamp": "2026-04-23T19:05:12.801Z",
  "level": "INFO",
  "service": "app",
  "environment": "production",
  "logger": "gateway.auth",
  "message": "auth.register: user_id=1234 email=x@y.com via token=abc12345...",
  "request_id": "3f0a8e7c",
  "user_id": 1234,
  "version": "<git sha>"
}
```

Additional fields flow from `extra={}` kwargs on the log call and from
the request-context vars set by `LoggingContextMiddleware`.

## Request correlation

`LoggingContextMiddleware` sets a `request_id` + `user_id` context var
for the duration of every HTTP request. Every log line emitted during
that request carries both in its JSON.

The middleware:

* Honours `X-Request-ID` inbound headers (sanitized to
  `[A-Za-z0-9_-]`, max 64 chars) so upstream proxy / client trace ids
  flow through intact.
* Mints a fresh 8-char hex id when no inbound header is present.
* Echoes the id back on the response as `X-Request-ID` so the caller
  (and CDN / browser error reporter) can correlate.

User-id is a best-effort lookup from the session cookie. It is
intentionally not freshness-validated — handlers still enforce auth.
The purpose here is log correlation, not access control.

## PII redaction

Two layers, in order:

1. **Key-based scrub** (`_scrub_value` in `logging_config.py`). Every
   `extra=` kwarg whose key contains `password` / `token` / `secret` /
   `api_key` / `session` / `cookie` / `jwt` / `bearer` / `card` / `cvv`
   / `authorization` / `auth` / `pin` / `private` / `invite` / `stripe`
   / `webhook` / `kalshi` / `vapid` is replaced with `[REDACTED]`.
   See `SENSITIVE_ALLOWLIST` for fields that escape the hint match
   (`request_id`, `token_id`, `session_id`, `user_id`, …).

2. **Message-content regex** (`_redact_message`). Runs over the
   already-interpolated message string. Catches:
     * `bearer <token>` prefixes (Authorization headers leaked into log
       messages).
     * `password=` / `api_key=` / `secret=` / `token=` in URL query
       strings or key=value interpolations.
     * Basic-auth creds embedded in URLs (`scheme://user:pass@host`).

### What is NOT redacted (by design)

* **Email addresses.** The admin audit trail (`log.info("Admin %s
  suspended user id=%d", admin["email"], uid)` etc.) needs the admin's
  identity visible for accountability. Emails of target users in
  destructive admin actions also appear intentionally — the audit UI
  filters by them.
* **Usernames / user_ids.** Same reason.
* **Prediction / market text.** Product-level content. Not PII.

If you need to log a target email in a context where it shouldn't
appear at all, use `db.mask_email(addr)` before interpolation. The
admin operational logs don't — but the public-facing logs (newsletter
unsubscribe, signup attempts by strangers) do.

## Audit log vs operational log

**Operational log** (this doc): stdout JSON, ephemeral, for ops /
debugging. Use `log = logging.getLogger(__name__)`.

**Audit log** (`security/audit.py` → `audit_log` table): append-only
database table, retained forever, for compliance. Use
`security.audit.log_action(...)`.

Every sensitive action MUST write to the audit log. Current coverage:

* Impersonation start / end
* Admin destructive actions (suspend / delete user, revoke token, role
  change, email change, gift subscription)
* Payment events (via Stripe webhook handlers)
* Subscription changes (upsert / cancel)
* Forensics toolkit uses (watermark extraction requests)

Never query the operational log for compliance purposes — it rotates
and can be dropped. Query `audit_log` via `/admin/audit`.

### Append-only invariant

`audit_log` is append-only:
* No `UPDATE audit_log …` in application code (verified by grep).
* No `DELETE FROM audit_log …` except in historical migrations removing
  rows tied to retired features.

## Viewing

```bash
# Live stream as JSON
tail -f /tmp/gateway.log | jq .

# Errors only
tail -f /tmp/gateway.log | jq 'select(.level == "ERROR" or .level == "CRITICAL")'

# All events for one request
tail -f /tmp/gateway.log | jq 'select(.request_id == "3f0a8e7c")'

# All events for one user
tail -f /tmp/gateway.log | jq 'select(.user_id == 1234)'
```

## Alerts

Recommended thresholds — wire these via the log aggregator once one is
provisioned (see "Log shipping" below). None of these are automated
today; the file is the source of truth until the aggregator lands.

| Rule | Window | Action |
|------|--------|--------|
| Any `CRITICAL` | instant | page on-call |
| `ERROR` rate > 5 / min | 2 min | email on-call |
| Auth failure rate > 20 / min | instant | credential-stuffing signal, page |
| HTTP 5xx > 1 % of requests | 5 min | email on-call |

## Log shipping

BetterStack / Logtail tokens expected in the environment:

* `LOGTAIL_TOKEN_APP` — gateway (this service)
* `LOGTAIL_TOKEN_SCRAPER` — scraper pipeline
* `LOGTAIL_TOKEN_WORKER` — background jobs

If a token is unset, `configure_logging()` falls back to stdout +
local file only — no remote shipping. Check for the running config
via the admin dashboard or the first line emitted at boot.

## Print statements

Bare `print()` in production request-path code is rejected by
`tests/test_logging.py::test_no_print_statements`. Exceptions are
whitelisted in that test's `EXCLUDED_EXACT` / `EXCLUDED_PREFIXES`:

* `scripts/` — operator CLI tools (status output).
* `migrations/` — migration runners.
* `scraper/setup_*_session.py`, `scraper/scrapers/{twitter,truthsocial}.py`
  — interactive session-setup prompts.
* `forensics/extract_watermark.py` — CLI extractor (stdout JSON).
* `i18n/auto_translate.py` — manual translation runner.

Adding another entry to the whitelist is fine when the file is
genuinely a human-invoked CLI; do not whitelist request-path code.
