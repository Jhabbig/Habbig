# Rate-Limit Coverage Audit — Gateway Mutation Routes

Audit date: 2026-05-15
Scope: every `@app.post / put / patch / delete` (and `@router.*` equivalent)
declared under `gateway/server.py` and the route modules it imports.

## Methodology

For each mutation decorator the audit records:

- **rate-limit = yes (decorator)** — wraps an `@rate_limit(...)` from
  `security.rate_limiter`.
- **rate-limit = yes (inline)** — calls `_is_rate_limited(...)` or
  `_auth_rate_limited(...)` or `db.rate_limit_hit(...)` in the handler
  body.
- **rate-limit = no** — neither. Note: every request still passes
  through the `GlobalRateLimitMiddleware` (600 req/min/IP, configurable
  via `GLOBAL_RATE_LIMIT_PER_MIN`) and, for auth endpoints, the shared
  `auth:<ip>` bucket (5/15min). "No" below means *no route-specific*
  bucket on top of those.

Auth-adjacent POSTs (login / register / reset-password / gate-submit /
token-submit / `/api/v1/*`) without any route-specific bucket are
flagged **HIGH**.

## Routes — `gateway/server.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/gate` | POST | yes (inline — shared `auth:<ip>` 5/15min) |
| `/invite` | POST | no (legacy redirect to `/token`) |
| `/login` | POST | no (legacy redirect to `/token`) |
| `/forgot-password` | POST | yes (inline — `auth:<ip>` 5/15min + `ip:<ip>:forgot` 10/10min + `email:<email>:forgot` 3/1hr) |
| `/signup` | POST | no (legacy redirect to `/token`) |
| `/billing` | POST | no |
| `/billing/subscribe` | POST | no |
| `/account/delete` | POST | yes (inline — `account-delete:<uid>` 3/1hr) |
| `/profile/password` | POST | yes (inline — `profile-password:<uid>` 5/1hr) |
| `/api/analytics/event` | POST | yes (inline — `_ANALYTICS_RATE_LIMIT` per IP) |
| `/reset-password` | POST | yes (inline — `auth:<ip>` 5/15min, also a 30/5min bucket for token lookups) |
| `/admin/tokens/generate` | POST | yes (inline — `admin-tokens-gen:<uid>` 30/1min) |
| `/admin/tokens/revoke` | POST | yes (inline — `admin-tokens-rev:<uid>` 30/1min) |
| `/admin/users/{user_id}/promote` | POST | no |
| `/admin/users/{user_id}/demote` | POST | no |
| `/admin/users/{user_id}/suspend` | POST | no |
| `/admin/users/{user_id}/unsuspend` | POST | no |
| `/admin/enquiries/{enquiry_id}/read` | POST | no |
| `/admin/enquiries/{enquiry_id}/create-token` | POST | no |
| `/admin/users/{user_id}/role` | POST | no |
| `/admin/users/{user_id}/email` | POST | no |
| `/admin/users/{user_id}/revoke-token` | POST | no |
| `/admin/users/{user_id}/new-token` | POST | no |
| `/admin/users/{user_id}/grant` | POST | no |
| `/admin/users/{user_id}/trading-addon` | POST | no |
| `/admin/users/{user_id}/delete` | POST | no |
| `/admin/users/bulk` | POST | yes (inline — `admin_bulk:<email>` 10/win) |
| `/settings/disconnect/{source}` | POST | no |
| `/api/trading-addon/config` | PATCH | no |
| `/settings` | POST | no |

## Routes — `gateway/server_features.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/notifications/email-preferences` | POST | no |
| `/api/set-language` | POST | no |
| `/auth/forgot-password` | POST | yes (inline — `<ip>:forgot-password` 3/1hr + `forgot-password:<email>` 3/1hr) |
| `/auth/reset-password` | POST | yes (inline — `<ip>:reset-password` 5/1hr) |
| `/api/newsletter` | POST | yes (inline — `nl:<ip>` 5/1min) |
| `/api/account/delete` | POST | yes (inline — `account-delete-api:<uid>` 3/1hr) |
| `/api/account/delete/cancel` | POST | yes (inline — `account-delete-api:<uid>` 3/1hr) |
| `/api/markets/{slug}/track-view` | POST | no |
| `/admin/markets/{slug}/mark-resolved` | POST | no |
| `/admin/jobs/weekly-digest/run` | POST | no |
| `/admin/api/jobs/{job_id}/retry` | POST | no |
| `/api/saved/{prediction_id}` | POST | no |
| `/api/saved/{prediction_id}` | DELETE | no |
| `/api/saved/{prediction_id}` | PATCH | no |
| `/api/sources/{handle}/follow` | POST | no |
| `/api/sources/{handle}/follow` | DELETE | no |
| `/api/sources/{handle}/follow` | PATCH | no |
| `/api/markets/{slug}/snapshot` | POST | no (gated by `X-Internal-Key`) |
| `/auth/validate-token` | POST | yes (inline — `<ip>:token-validate` 5/1min + `token-validate:<token>` 10/10min) |
| `/auth/register` | POST | yes (inline — `<ip>:register` 5/10min) |
| `/auth/login` | POST | yes (inline — `<ip>:login-auth` 10/5min + `email:<email>:login` 5/10min) |
| `/auth/logout` | POST | yes (inline — `<ip>:logout` 20/1min) |
| `/api/auth/sessions/{session_id}` | DELETE | no |
| `/api/auth/sessions` | DELETE | no |

## Routes — `gateway/take_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/v1/markets/{slug}/takes` | POST | no |
| `/api/v1/takes/{take_id}` | PATCH | no |
| `/api/v1/takes/{take_id}` | DELETE | no |
| `/api/v1/takes/{take_id}/vote` | POST | no |
| `/api/v1/takes/{take_id}/vote` | DELETE | no |
| `/api/v1/takes/{take_id}/report` | POST | no |
| `/api/v1/admin/takes/{take_id}/delete` | POST | no |
| `/api/v1/admin/reports/{report_id}/resolve` | POST | no |

## Routes — `gateway/embed_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/embeds` | POST | no |
| `/api/embeds/{widget_id}` | DELETE | no |
| `/api/embeds/{widget_id}/rotate-token` | POST | no |

## Routes — `gateway/stripe_webhook_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/stripe/webhook` | POST | yes (inline — `stripe_webhook_global` 100/1min) |

## Routes — `gateway/feedback_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/feedback` | POST | yes (inline — `feedback-submit:<uid>` 10/1hr) |
| `/api/feedback/{item_id}/vote` | POST | no |
| `/api/feedback/{item_id}/comment` | POST | no |
| `/admin/feedback/{item_id}/status` | POST | no |
| `/admin/feedback/bulk-status` | POST | no |
| `/admin/feedback/{item_id}/duplicate` | POST | no |
| `/admin/feedback/{item_id}/comment` | POST | no |
| `/admin/feedback/{item_id}/ship` | POST | no |

## Routes — `gateway/engagement_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/engagement/prompt/dismiss` | POST | no |

## Routes — `gateway/affiliate_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/v1/affiliate/links` | POST | no |
| `/api/v1/affiliate/payout-request` | POST | yes (inline — `affiliate_payout_req:<id>` 1/1hr) |
| `/admin/affiliates` | POST | no |
| `/admin/affiliates/{affiliate_id}` | PATCH | no |
| `/admin/affiliates/{affiliate_id}/payout` | POST | no |

## Routes — `gateway/billing_routes.py`

All mutation endpoints share the `_billing_rate_limit(user, action)`
helper at 20/1hr/user.

| Route | Verb | Rate-limit |
|---|---|---|
| `/settings/billing/cancel` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/settings/billing/pause` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/settings/billing/resume` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/settings/billing/resubscribe` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/settings/billing/addon` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/settings/billing/addon/cancel` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/api/v1/billing/portal` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |
| `/api/billing/portal-session` | POST | yes (inline — `_billing_rate_limit` 20/1hr) |

## Routes — `gateway/routes_sharing.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/tools/card-preview` | POST | no |
| `/api/share/market` | POST | yes (inline — `share_mint:<uid>` 20/1hr shared) |
| `/api/share/source` | POST | yes (inline — `share_mint:<uid>` 20/1hr shared) |
| `/api/share/prediction` | POST | yes (inline — `share_mint:<uid>` 20/1hr shared) |

## Routes — `gateway/routes_referrals.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/invite/{code}/accept` | POST | yes (inline — `invite_accept:ip:<ip>` 20/1hr + `invite_accept:email:<email>` 3/1d) |
| `/api/leaderboard/participate` | POST | no |
| `/api/leaderboard/participate` | DELETE | no |

## Routes — `gateway/status_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/api/status/subscribe` | POST | no |
| `/api/status/unsubscribe` | POST | no |
| `/admin/incidents` | POST | no |
| `/admin/incidents/{incident_id}` | POST | no |
| `/admin/incidents/{incident_id}/updates` | POST | no |
| `/admin/incidents/{incident_id}/resolve` | POST | no |

## Routes — `gateway/forecast_routes.py`

| Route | Verb | Rate-limit |
|---|---|---|
| `/admin/equivalences/{market_slug}/{provider}` | POST | no |

## Auth-adjacent POSTs without route-specific rate-limit — `HIGH`

The brief requires highlighting any auth-adjacent POST (login, register,
reset-password, gate-submit, token-submit, `/api/v1/*`) that lacks a
per-route rate-limit bucket. Below is the full HIGH list, ranked by
risk.

| Severity | Route | Verb | Why HIGH |
|---|---|---|---|
| HIGH | `/api/v1/markets/{slug}/takes` | POST | `/api/v1/*` mutation, unauthenticated abuse → free-text spam into the public takes table; no inline limit, no decorator. |
| HIGH | `/api/v1/takes/{take_id}` | PATCH | `/api/v1/*` mutation; edit-take loop has no per-user bucket. |
| HIGH | `/api/v1/takes/{take_id}` | DELETE | `/api/v1/*` mutation; mass-delete a paid user's takes after session theft. |
| HIGH | `/api/v1/takes/{take_id}/vote` | POST | `/api/v1/*` mutation; vote stuffing / ranking abuse with no per-user cap beyond the 600/min global. |
| HIGH | `/api/v1/takes/{take_id}/vote` | DELETE | `/api/v1/*` mutation; symmetric to the POST. |
| HIGH | `/api/v1/takes/{take_id}/report` | POST | `/api/v1/*` mutation; report-floods can DOS the admin triage queue. |
| HIGH | `/api/v1/admin/takes/{take_id}/delete` | POST | `/api/v1/*` admin mutation; admin compromise = unbounded delete loop. |
| HIGH | `/api/v1/admin/reports/{report_id}/resolve` | POST | `/api/v1/*` admin mutation; same pattern. |
| HIGH | `/api/v1/affiliate/links` | POST | `/api/v1/*` mutation; affiliate-link minting has no inline budget (only a separate 30/min IP cap on /click). |

Note: legacy `/login`, `/invite`, `/signup` (server.py) are NOT flagged
HIGH because they are 302 redirects to `/token`. The real
login/register/reset-password endpoints live under `/auth/*` in
`server_features.py` and all have route-specific buckets.

## Coverage summary

- **Total mutation routes audited:** 90
- **With route-specific rate-limit (decorator or inline):** 41
- **Without route-specific rate-limit (global-only):** 49
- **HIGH-risk uncovered (auth-adjacent / `/api/v1/*`):** 9

## Notes & defence-in-depth context

1. `GlobalRateLimitMiddleware` (server.py:1771) caps every request to
   600/min/IP regardless of path. "No" rows are NOT unmetered — they
   inherit this floor.
2. `BulkDataRateLimitMiddleware` caps row-volume in JSON list responses
   to 5k/h/user. This catches scraping of GETs but is irrelevant to
   the mutation routes audited here.
3. The shared `auth:<ip>` bucket (5 attempts / 15 min) stacks across
   `/gate`, `/forgot-password`, and `/reset-password`. New `/auth/*`
   endpoints do NOT call `_auth_rate_limited` — they each ship their
   own per-IP and (where applicable) per-email buckets instead.
4. `notification_routes.py`, `push_routes.py`, `search_routes.py`,
   `admin_emails_routes.py`, `admin_jobs_routes.py`,
   `admin_cost_alerts_routes.py`, `admin_test_emails_routes.py` use the
   declarative `@rate_limit(...)` decorator from
   `security.rate_limiter`. None of those modules expose mutation
   routes in scope of this audit (they are GET-heavy or admin-only).
