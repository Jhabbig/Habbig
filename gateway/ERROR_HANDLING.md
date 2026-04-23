# Error handling

Every 4xx / 5xx response from `narve.ai` goes through the same two shapes:
a JSON envelope for API clients, a branded HTML page for browsers. Both
carry a **request id** the user can quote to support; neither ever leaks
a stack trace, SQL detail, or internal module name.

## The rules

1. **Every 5xx is logged**, with traceback, request id, path, and method.
2. **No 5xx message is echoed to the client**. Callers always see the
   generic `"An unexpected error occurred. Please try again."` message
   plus the request id. Ops can correlate by request id.
3. **Every response carries `X-Request-ID`.** If the caller sends one
   (short, printable, no spaces) we respect it; otherwise we mint a fresh
   8-char token.
4. **JSON envelopes have one shape.** Always:
   ```json
   {
     "error":       "error_code_slug",
     "message":     "Human-readable.",
     "request_id":  "abc12345",
     "details":     {...}
   }
   ```
   Slug codes are stable. Clients switch on `error`, not `status_code`.

## JSON error envelope — status → slug

| Status | `error` slug              | Default message                                              |
|-------:|:--------------------------|:-------------------------------------------------------------|
| 400    | `bad_request`             | That request was malformed. Double-check and try again.      |
| 401    | `authentication_required` | You need to sign in to see this.                             |
| 402    | `subscription_required`   | This feature needs a subscription.                           |
| 403    | `authorization_required`  | Your account doesn't have access to this.                    |
| 404    | `resource_not_found`      | This page doesn't exist…                                     |
| 409    | `duplicate_resource`      | A resource with the same identifier already exists.          |
| 422    | `validation_failed`       | Some fields need attention.                                  |
| 429    | `rate_limit_exceeded`     | Too many requests. Try again in a moment.                    |
| 500    | `internal_error`          | An unexpected error occurred. Please try again.              |
| 502    | `upstream_error`          | One of our upstream services returned an error.              |
| 503    | `service_unavailable`     | narve.ai is temporarily unavailable.                         |
| 504    | `upstream_timeout`        | An upstream service took too long to respond.                |

Validation-error envelopes additionally carry:
```json
{
  "error": "validation_failed",
  "details": { "errors": [{"field": "email", "message": "not a valid email"}] }
}
```

## HTML error pages

The same copy feeds a branded standalone page (see
`gateway/static/error_page.html`) for browser navigations. Every page:

- States the status number and a one-line title.
- Gives one plain-English paragraph of what happened.
- Shows the request id in monospace for support tickets.
- Offers context-appropriate CTAs:
  - 401 → **Sign in**, **Request an invite**
  - 402 → **Upgrade**, **Back to dashboard** (+ pricing link)
  - 403 → **Back to dashboard**
  - 404 → **Go to dashboard**, **Go to homepage**
  - 429 → **Back to dashboard** + "try again in N seconds"
  - 500 → **Retry**, **Go to dashboard**
  - 503 → **Check status**, **Back to dashboard**
- Monochrome. No red/green/yellow. Zero external assets (inline CSS)
  so the page renders even when the SW has evicted `gateway.css`.

## How handlers plug in

`gateway/error_handlers.py` exposes `register(app)` which attaches:

- `StarletteHTTPException` → `http_exception_handler` (JSON or HTML)
- `RequestValidationError` → `validation_exception_handler` (422 +
  per-field details)
- `Exception` → `app_exception_handler` (logs traceback, returns
  generic 500 without the exception detail)
- `RequestIDMiddleware` to stamp every request with `request.state.request_id`.

Registered once in `server.py`, just after `SecurityHeadersMiddleware`.

## Rules for raising errors in app code

- `raise HTTPException(status_code=4xx, detail="user-facing message")` is
  fine — the detail string is echoed as `message` if it doesn't look like
  a stack trace / DB detail (heuristic check in `_looks_like_trace`).
- Never `raise HTTPException(detail=str(exc))` — that leaks the upstream
  error. Instead: `log.exception(...)` + raise with a generic message.
- Never format an SQL constraint / psycopg exception into the detail
  string. The handler's trace-detector catches most of these but the
  right move is to not build them in the first place.

## Retrying external calls

`gateway/common/retry.py` exposes a minimal pure-stdlib `@retry(...)`
decorator (API-compatible with `tenacity` for an eventual swap):

```python
from common.retry import retry, raise_for_retry_after
import httpx

@retry(
    stop_after_attempt=3,
    wait_exponential_min=1.0,
    wait_exponential_max=10.0,
    retry_on=(httpx.TimeoutException, httpx.ConnectError),
    name="polymarket.get_market",
)
async def get_market(slug):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GAMMA}/markets/{slug}", timeout=5.0)
        raise_for_retry_after(resp)  # 429 → waits for Retry-After
        resp.raise_for_status()
        return resp.json()
```

Rules:
- **Never retry 4xx** (other than 429). The decorator's `retry_on`
  whitelist should only include transient / network exceptions.
- **429 waits for `Retry-After`** via the `raise_for_retry_after()` helper.
- Every retry attempt logs at WARNING so ops can track flappy upstreams.

## Circuit breakers

`gateway/common/circuit_breaker.py` has one breaker per upstream:
`claude_breaker`, `stripe_breaker`, `polymarket_breaker`, `kalshi_breaker`,
`sec_edgar_breaker`.

State machine: closed → (N consecutive failures) → open → (recovery_timeout)
→ half-open → (success) → closed. When a breaker is open:

- `can_call()` returns `False` immediately.
- Calls should raise `CircuitOpen` (the decorator does this automatically).
- Callers should surface a **503** to the user and, if possible, return
  a cached fallback.

Tuning:

| Breaker    | Threshold | Recovery |
|:-----------|:----------|:---------|
| claude     | 5         | 60 s     |
| stripe     | 3         | 30 s     |
| polymarket | 5         | 60 s     |
| kalshi     | 5         | 60 s     |
| sec_edgar  | 3         | 120 s    |

All breakers are per-process; a second worker's breaker is independent.
That's deliberate — you don't want a single slow worker to open the
breaker for every peer that's still healthy.

Minimal usage:

```python
from common.circuit_breaker import claude_breaker, CircuitOpen

@claude_breaker.wrap()
async def summarise(text: str) -> str:
    # raises CircuitOpen if breaker is tripped
    return await anthropic.messages.create(...).content[0].text
```

In the calling handler:

```python
try:
    summary = await summarise(payload)
except CircuitOpen:
    raise HTTPException(status_code=503, detail="Summary temporarily unavailable. Retry in a minute.")
```

## What this pass did NOT change

- The existing ~370 `except Exception:` sites across the codebase.
  Each is legitimate-looking (fire-and-forget logging, best-effort
  fan-out) and tightening them one-by-one is a separate sweep. The
  new catch-all handler covers the case where one of them lets an
  exception escape.
- Existing `HTTPException(status_code=..., detail="...")` call sites.
  A spot-check showed no `detail=str(exc)` patterns or SQL-flavoured
  detail strings; if one slips in later, the `_looks_like_trace`
  heuristic in the handler falls back to the generic message.
