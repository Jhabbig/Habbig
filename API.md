# API reference

narve.ai exposes two API surfaces:

1. **Developer API** (`/api/v1/...` via `gateway/api_v1.py`) — Bearer
   token authentication with API keys issued from the admin panel.
   Designed for quant funds, bot builders, and researchers who want
   programmatic access to credibility scores, predictions, and edge
   data. Rate-limited per key tier.

2. **Application API** (`/api/...`) — consumed by the site frontend
   and the Chrome extension. Session-cookie authentication (plus CSRF
   for state-changing verbs). Most endpoints paid-gated.

OpenAPI JSON: `GET /api/v1/openapi.json`
Swagger UI: `GET /api/v1/docs` (public)
Version meta: `GET /api/version` (no auth)

---

## Developer API (Bearer)

### Authentication

Every developer-API request carries an `Authorization: Bearer <key>`
header. Keys are issued from `/admin/api-keys` and rotated per key.
Rate limits attach to the issuing tier; see the key-rotation admin
page for current caps.

### `GET /api/v1/version`

Unauthenticated. Returns the current + supported versions + docs URL.

```json
{
  "current": "v1",
  "supported": ["v1"],
  "deprecated": [],
  "docs_url": "https://narve.ai/api/v1/docs",
  "sunset_unversioned": "2026-12-31"
}
```

### `GET /api/v1/sources`

List credibility-scored sources. Paged.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `category` | string | — | Filter: `politics`, `crypto`, `sports`, … |
| `min_cred` | float | 0.0 | Global credibility ≥ this value |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | |

```json
{
  "sources": [
    {
      "handle": "@alice.forecaster",
      "platform": "twitter",
      "global_credibility": 0.74,
      "total_predictions": 312,
      "correct_predictions": 221,
      "categories": ["politics", "crypto"]
    }
  ],
  "total": 4812,
  "limit": 50,
  "offset": 0
}
```

### `GET /api/v1/sources/{handle}`

Single source with per-category breakdown + recent snapshots.

### `GET /api/v1/predictions`

Extracted predictions. Filterable by source, market, category, date
range, resolution status.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `source` | string | — | `@handle` |
| `market` | string | — | `poly:<slug>` or `kalshi:<ticker>` |
| `category` | string | — | |
| `since` | unix int | — | extracted_at ≥ |
| `resolved` | bool | — | filter by resolution status |
| `limit`, `offset` | int | 100 / 0 | |

Response rows include `direction`, `predicted_probability`, `market_id`,
`extracted_at`, `resolved`, `resolved_correct`, `resolved_at`.

### `GET /api/v1/markets/{slug:path}/consensus`

Network-adjusted consensus probability for a market. Returns the
credibility-weighted blend of predictions on that market, with
echo-chamber detection applied.

Slug uses the unified `poly:<polymarket-slug>` / `kalshi:<ticker>`
format; consumers must URL-encode the colon.

```json
{
  "market_slug": "poly:will-x-happen",
  "consensus_probability": 0.68,
  "market_probability": 0.52,
  "edge": 0.16,
  "n_sources": 14,
  "independent_sources": 9
}
```

### `GET /api/v1/markets/edge`

Top-edge opportunities — markets where consensus diverges most from
the market price. Rate-limit heavy; cached 60 s.

### `GET /api/v1/forecasts/compare/{market_slug}`

Side-by-side comparison of market price, narve consensus, and external
forecasts (Metaculus, Manifold, 538, Silver Bulletin) where available.

### `GET /api/v1/forecasts/providers`

List of external forecast providers currently ingested + their last
sync timestamps.

---

## Application API (session cookie)

Every state-changing verb requires the CSRF double-submit pattern:

- `x-csrf-token` header matching the `_csrf` cookie, OR
- hidden form field `csrf_token` matching the cookie

Exemptions (webhook / pre-session POSTs) are whitelisted in
`_CSRF_EXEMPT_POSTS` in `server.py`.

### Community Takes

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `GET /api/v1/markets/{slug}/takes` | public | list + filter + sort |
| `POST /api/v1/markets/{slug}/takes` | paid | one live take per (user, market), 50–2000 chars reasoning |
| `PATCH /api/v1/takes/{id}` | paid owner | 24h edit window |
| `DELETE /api/v1/takes/{id}` | owner | soft-delete |
| `POST /api/v1/takes/{id}/vote` | auth | body: `{"vote": +1}` or `{"vote": -1}` or `{"vote": 0}` to clear |
| `DELETE /api/v1/takes/{id}/vote` | auth | idempotent clear |
| `POST /api/v1/takes/{id}/report` | auth | body: `{"reason": "...", "details": "..."}` |
| `POST /api/v1/admin/takes/{id}/delete` | admin | hard-delete + cascade reports |
| `POST /api/v1/admin/reports/{id}/resolve` | admin | body: `{"action": "deleted"|"dismissed"|"warned_user"}` |

### Billing

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `GET /api/v1/billing/invoices` | auth | list past invoices |
| `GET /api/v1/billing/invoices/{id}/pdf` | owner | invoice PDF |
| `GET /api/v1/billing/portal` | auth | 302 to Stripe customer portal |

### Affiliate program

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `GET /api/v1/affiliate` | affiliate | dashboard summary |
| `GET /api/v1/affiliate/conversions` | affiliate | paged conversions |
| `POST /api/v1/affiliate/links` | affiliate | create tracking link |
| `POST /api/v1/affiliate/payout-request` | affiliate | request payout |

### Feed + saved + follows

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `GET /api/search` | auth | unified search (markets, sources, predictions) |
| `POST /api/saved/{prediction_id}` | auth | bookmark a prediction |
| `DELETE /api/saved/{prediction_id}` | owner | remove bookmark |
| `GET /api/saved` | auth | list bookmarks |
| `PATCH /api/saved/{prediction_id}` | owner | update note |
| `POST /api/sources/{handle}/follow` | auth | follow source |
| `DELETE /api/sources/{handle}/follow` | auth | unfollow |
| `PATCH /api/sources/{handle}/follow` | auth | update follow prefs |
| `GET /api/sources/following` | auth | list follows |
| `GET /api/markets/{slug:path}/chart` | auth | price history |
| `POST /api/markets/{slug:path}/snapshot` | admin | record snapshot |
| `POST /api/markets/{market_slug}/track-view` | auth | analytics ping |

### Newsletter / waitlist

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `POST /api/newsletter` | public | CSRF-exempt, per-IP + per-email rate-limited |
| `GET /api/newsletter/position` | public | position + share URL |

### Account lifecycle

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `POST /api/account/delete` | auth | schedule deletion (30-day window) |
| `POST /api/account/delete/cancel` | auth | cancel scheduled deletion |
| `POST /api/notifications/email-preferences` | auth | `{digest: bool, marketing: bool}` |
| `POST /api/set-language` | auth | set `preferred_language` |
| `GET /api/auth/sessions` | auth | list active sessions |
| `DELETE /api/auth/sessions/{id}` | owner | revoke one |
| `DELETE /api/auth/sessions` | owner | revoke all others |

---

## Error shape

Validation failures return HTTP 400 with:

```json
{ "error": "reasoning must be at least 50 characters", "field": "reasoning" }
```

Auth failures return 401 (no/expired session) or 402 (paid required) or
403 (wrong role / blocked by impersonation) with a `detail` field.

Rate-limit responses return 429 with `Retry-After` and a JSON body
indicating the bucket that tripped.

---

## Webhook events

### Stripe (inbound)

`POST /api/stripe/webhook` — verifies the Stripe signature, idempotency-
keys on `event.id`, rejects events from the wrong mode (test ↔ live
mismatch → 400). Handled events:

- `checkout.session.completed` → activates subscription
- `customer.subscription.created` / `.updated` / `.deleted` → syncs
  entitlement
- `invoice.payment_succeeded` → credits affiliate commission if linked
- `invoice.payment_failed` → triggers payment-failed email +
  grace-period flag

Duplicate events return `{"status": "already_processed"}` with 200.

See `gateway/stripe_webhook_hardening.py` for the canonical handler.

---

## Rate-limit headers

Present on every response from `/api/v1/*`:

| Header | Meaning |
| --- | --- |
| `X-RateLimit-Limit` | Requests allowed in the current window |
| `X-RateLimit-Remaining` | Remaining in this window |
| `X-RateLimit-Reset` | Unix seconds when the window resets |
| `Retry-After` | Seconds to wait (only on 429 responses) |

---

## Legacy `/api/...` redirect

Unversioned `/api/foo` paths redirect 301 (GET) or 308 (non-GET) to the
`/api/v1/foo` equivalent, plus:

- `Deprecation: true`
- `Sunset: Thu, 31 Dec 2026 23:59:59 GMT`
- `X-API-Deprecated: This endpoint will be removed on 2026-12-31. Use /api/v1/ instead.`
- `Link: </api/v1/foo>; rel="successor-version"`

Do not consume the unversioned paths in new integrations.
