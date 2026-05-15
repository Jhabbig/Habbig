# Audit: admin nav link integrity

**Date:** 2026-05-15
**Scope:** Every `href="/admin*"` value inside `gateway/static/admin*.html`
verified against routes registered in `gateway/admin_routes.py`.
**Pre-release routes are out of scope** (none appear in the scanned hrefs).

## Method

1. Enumerated 18 files matching `gateway/static/admin*.html`.
2. Extracted unique `href` attribute values pointing at `/admin*` (14 unique
   hrefs).
3. Cross-referenced each href against the path strings passed to
   `app.add_api_route(...)` inside `register()` in
   `gateway/admin_routes.py` (lines 3028-3189).
4. Any href whose path (or whose path-template after stripping path params)
   is not registered in `admin_routes.py` is flagged **dead**.

> Important: per the audit's hard rule, a link is "dead" if it is not
> registered in `gateway/admin_routes.py`. Several flagged links DO resolve
> in production because they are registered elsewhere (server.py,
> security_routes.py, take_routes.py, affiliate_routes.py, etc.). Those are
> noted inline so the fix is "move the route" or "update the audit's
> scope", not "delete the link".

## Files scanned (18)

`/Users/shocakarel/Habbig/gateway/static/`:

- admin-churn.html
- admin-email-edit.html
- admin-emails.html
- admin-feedback.html
- admin-flag-edit.html
- admin-flags.html
- admin-impersonation-detail.html
- admin-impersonations.html
- admin-sharing.html
- admin.html
- admin_affiliates.html
- admin_api_keys.html
- admin_equivalences.html
- admin_moderation.html
- admin_security_bulk.html
- admin_security_forensics.html
- admin_status.html
- admin_webhooks.html

## Routes registered in `gateway/admin_routes.py::register()`

```
/admin/api/sentry
/admin/backups
/admin/cache
/admin/cache/clear
/admin/cache/stats
/admin/churn
/admin/email-templates
/admin/email-templates/{key}
/admin/email-templates/{key}/preview
/admin/email-templates/{key}/reset
/admin/flags
/admin/flags/{key}
/admin/flags/{key}/delete
/admin/impersonations
/admin/impersonations/end
/admin/impersonations/{session_id}
/admin/newsletter
/admin/newsletter/preview
/admin/newsletter/recipients
/admin/newsletter/send
/admin/sharing
/admin/trace-watermark
/admin/users
/admin/users/bulk-actions
/admin/users/{user_id}/export
/admin/users/{user_id}/impersonate
/admin/users/{user_id}/revoke-sessions
```

## Results per unique href

| href | In `admin_routes.py`? | Status | Resolves elsewhere? |
|---|---|---|---|
| `/admin` | No | DEAD | Yes (`server.py:5725`) |
| `/admin/affiliates` | No | DEAD | Yes (`affiliate_routes.py:531`) |
| `/admin/audit` | No | DEAD | No (likely a typo for `audit-log`; only `take_routes.py` has `/admin/moderation` audit context) |
| `/admin/audit-log` | No | DEAD | Yes (`server.py:6615`) |
| `/admin/churn` | Yes | OK | n/a |
| `/admin/email-templates` | Yes | OK | n/a |
| `/admin/emails` | No | DEAD | Yes (`admin_emails_routes.py:363`) |
| `/admin/equivalences` | No | DEAD | Yes (`forecast_routes.py:375`) |
| `/admin/flags` | Yes | OK | n/a |
| `/admin/impersonations` | Yes | OK | n/a |
| `/admin/incidents` | No | DEAD | Partially (`status_routes.py:493` exists but **POST-only** — a GET nav link will 405) |
| `/admin/moderation` | No | DEAD | Yes (`take_routes.py:542`) |
| `/admin/security/bulk-fetches` | No | DEAD | Yes (`security_routes.py:419`) |
| `/admin/security/forensics` | No | DEAD | Yes (`security_routes.py:420`) |

## Dead links (10)

Each row lists the href and every file in `gateway/static/` containing it.

### 1. `/admin`

Not registered in `admin_routes.py`. Lives in `server.py:5725`.

Used in (18 files):
- admin-churn.html
- admin-email-edit.html
- admin-emails.html
- admin-feedback.html
- admin-flag-edit.html
- admin-flags.html
- admin-impersonation-detail.html
- admin-impersonations.html
- admin-sharing.html
- admin.html
- admin_affiliates.html
- admin_api_keys.html
- admin_equivalences.html
- admin_moderation.html
- admin_security_bulk.html
- admin_security_forensics.html
- admin_status.html
- admin_webhooks.html

### 2. `/admin/affiliates`

Not registered in `admin_routes.py`. Lives in `affiliate_routes.py:531`.

Used in:
- admin.html
- admin_affiliates.html

### 3. `/admin/audit`

Not registered anywhere in `gateway/` as `/admin/audit` (only `/admin/audit-log` exists). Almost certainly a typo.

Used in:
- admin_moderation.html

### 4. `/admin/audit-log`

Not registered in `admin_routes.py`. Lives in `server.py:6615`.

Used in:
- admin.html

### 5. `/admin/emails`

Not registered in `admin_routes.py`. Lives in `admin_emails_routes.py:363`.

Used in:
- admin.html

### 6. `/admin/equivalences`

Not registered in `admin_routes.py`. Lives in `forecast_routes.py:375`.

Used in:
- admin.html
- admin_equivalences.html

### 7. `/admin/incidents`

Not registered in `admin_routes.py`. `status_routes.py:493` defines it as **POST-only** — there is no GET handler, so a nav `href` to it returns 405 Method Not Allowed.

Used in:
- admin.html
- admin_status.html

### 8. `/admin/moderation`

Not registered in `admin_routes.py`. Lives in `take_routes.py:542`.

Used in:
- admin_moderation.html

### 9. `/admin/security/bulk-fetches`

Not registered in `admin_routes.py`. Lives in `security_routes.py:419`.

Used in:
- admin.html

### 10. `/admin/security/forensics`

Not registered in `admin_routes.py`. Lives in `security_routes.py:420`.

Used in:
- admin.html
- admin_security_bulk.html

## Truly broken (no handler in any gateway/ file)

| href | Source file(s) | Notes |
|---|---|---|
| `/admin/audit` | `admin_moderation.html` | Probably should be `/admin/audit-log` |
| `/admin/incidents` (GET) | `admin.html`, `admin_status.html` | Handler exists but is POST-only — GET nav link 405s |

## Summary

- **Unique hrefs scanned:** 14
- **Registered in `gateway/admin_routes.py`:** 4 (`/admin/churn`, `/admin/email-templates`, `/admin/flags`, `/admin/impersonations`)
- **Dead (per audit scope):** 10
- **Of those dead, also broken in production:** 2 (`/admin/audit`, `/admin/incidents` GET)
- **Of those dead, resolves via another route file:** 8

## Recommendation

The audit's narrow "must be in `admin_routes.py`" scope flags 10 hrefs, but
only 2 are user-impacting (`/admin/audit` typo and the `/admin/incidents`
GET-vs-POST mismatch). The other 8 work fine because the routes are
registered elsewhere in `gateway/`. Either:

1. Widen the audit scope to "any `@app.get` decorator under `gateway/`", or
2. Migrate the eight handlers into `admin_routes.py` to match the
   convention this audit assumes.

Fix the two truly-broken links regardless.
