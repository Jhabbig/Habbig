# CSRF coverage audit — narve.ai gateway

## TL;DR

CSRF is enforced centrally by `CSRFMiddleware` in `server.py` (registered
near the top of the middleware stack). Every POST, PUT, PATCH, and DELETE
that reaches a handler has already been validated unless the exact path is
in an explicit allow-list. **No individual `validate_csrf_token()` call is
needed in route code** — the middleware is the single enforcement point.

## Middleware mechanism

1. CSRF token minted on first GET that reaches `render_page()` — stored
   as an httpOnly cookie (`_csrf`) and injected into every `<form>` as a
   hidden `_csrf` input.
2. Submit-time, `CSRFMiddleware.dispatch()` compares the `_csrf` cookie
   against the `_csrf` form field / `X-CSRF-Token` header. Mismatch → 403.
3. Cross-origin requests are rejected unless the Origin header matches the
   app's expected apex (`narve.ai` or a configured alias).

## Exempt paths (by design)

### Exact-match exemptions — `_CSRF_EXEMPT_POSTS`

| Path | Why exempt | Compensating control |
| ---- | ---------- | -------------------- |
| `/api/newsletter` | Called from anonymous `/` prerelease page | Per-IP rate limit + per-email rate limit |
| `/auth/validate-token` | Invite-token bootstrap — runs before any session exists | 10 attempts/minute/IP |
| `/api/status/subscribe` | Called from anonymous `/status` page | Email format check + rate limit |
| `/api/status/unsubscribe` | Called from anonymous `/status` page | Email format check + rate limit |

### Prefix exemptions — `_CSRF_EXEMPT_POST_PREFIXES`

| Prefix | Why exempt | Compensating control |
| ------ | ---------- | -------------------- |
| `/api/invite/` | `/api/invite/{code}/accept` accepts an invite-code dynamic segment; consumed from public share links | 20/hour/IP + 3/day/email |

### Not in the middleware but MUST stay CSRF-free

| Path | Why | Compensating control |
| ---- | --- | -------------------- |
| `/stripe/webhook` | Called server-to-server by Stripe, no cookie at all | HMAC signature verified against `STRIPE_WEBHOOK_SECRET` + event-id idempotency |
| `/api/v1/*` Bearer-auth endpoints | Developer API uses API keys, not session cookies | API key validation + per-key rate limits |

`/stripe/webhook` is explicitly bypassed inside the webhook route by
checking `stripe-signature` before anything else — even if `CSRFMiddleware`
didn't already skip it (it does), the handler is idempotent + HMAC-verified.

## Per-method coverage

```
grep -cE '@app\.(post|put|patch|delete)' server.py     # 55 state-changing routes
```

None of these 55 routes have a per-handler `validate_csrf_token(...)` call
because the middleware is the single enforcement point. That's the intended
architecture — centralising checks in middleware is safer than expecting
every route author to remember.

## HTMX integration

`static/base.html` has an `htmx:configRequest` hook that reads the `_csrf`
cookie and attaches it as an `X-CSRF-Token` header on every htmx-triggered
request. The middleware accepts either the header OR the form field, so
both `<form>` submissions and htmx requests are covered.

## Form injection

`server.render_page()` auto-injects a hidden `<input name="_csrf">` into
every `<form method="post">` that doesn't already have one. Templates
don't have to remember to add the field themselves.

## How to add a new exempt path

1. Confirm the endpoint has no session-anchored CSRF surface — i.e. it's
   called from a genuinely-public page with no existing cookie.
2. Add a per-IP rate limit before the handler runs (or document why it's
   not needed).
3. Add the path to `_CSRF_EXEMPT_POSTS` (exact) or `_CSRF_EXEMPT_POST_PREFIXES`
   (prefix) in `server.py`.
4. Update this file with the path + compensating control.

## How to audit yourself

```bash
# Routes registered as state-changing:
grep -nE '@app\.(post|put|patch|delete)' gateway/server.py | head -60

# Every one of these is covered unless the path is in the allow-list above.
# To find a specific route's exemption state:
grep -nE '_CSRF_EXEMPT_POSTS|_CSRF_EXEMPT_POST_PREFIXES' gateway/server.py
```

## Related

- Session cookie + rotation: `auth/middleware.py` (`SessionMiddleware`)
- Rate limiting: `_is_rate_limited()` in `server.py`, Redis-optional
- Impersonation destructive-action block: `impersonation.is_action_blocked()`
