# Error Handlers Audit — `gateway/error_handlers.py`

**Date:** 2026-05-15
**Scope:** Per-error review of `/Users/shocakarel/Habbig/gateway/error_handlers.py`
covering (a) HTTP-code appropriateness, (b) info-leak surface, (c) request_id
surfacing for traceability, (d) HTML/JSON content negotiation. Auxiliary refs:
`gateway/ERROR_HANDLING.md`, `gateway/static/error_page.html`,
`audits/audit_error_leak.md`, `audits/audit_design_errors.md`.
**Method:** Synchronous read of the module (507 lines) and dependent template /
docs. No live HTTP probes. **Pre-release surface deliberately off-limits per
brief.**
**Branch:** `feature/platform-build`

---

## Severity counts

| Severity | Count |
|----------|------:|
| Critical | 0     |
| High     | 2     |
| Medium   | 5     |
| Low      | 4     |
| Info     | 3     |

Headline: the module's three handlers (`http_exception_handler`,
`validation_exception_handler`, `app_exception_handler`) plus
`RequestIDMiddleware` are correctly wired (`register()` at line 500-507) and
the JSON envelope contract matches `ERROR_HANDLING.md`. The 500-catch-all
**does** log a traceback and **does** suppress `str(exc)` from the wire — that
is the load-bearing invariant and it holds. The defects are at the edges:
(1) HTML pages hide `request_id` for every status < 500 by design
(`render_error_page` line 247-252), so a user hitting a 4xx that turns out to
be a server-misconfiguration bug has nothing to quote to support, and the
`X-Request-ID` response header is the only fallback — invisible in normal
browser UX; (2) `_looks_like_trace()` is shallow (matches a handful of tokens,
240-char gate), so a `HTTPException(400, str(value_error))` still leaks the
business message verbatim if the message is short and clean of those tokens —
this is the same gap flagged in `audit_error_leak.md` and is unfixed here;
(3) per-error coverage of the slug/title/message tables is incomplete — 405
and 415 have slugs but no titles or messages, so they fall to the generic
`"Error" / "Something went wrong."` strings; (4) status 502 and 504 have
titles+messages but no CTA branch in `_action_buttons_for_status` — they fall
to the generic "Back to dashboard" default, which is fine but undocumented.

---

## Top 3 findings

### 1. [HIGH] `_looks_like_trace()` is too narrow — short clean `exc.detail` strings still leak business detail through `http_exception_handler`
**Location:** `gateway/error_handlers.py:355-356`, heuristic at `:441-460`.

`http_exception_handler` echoes `exc.detail` straight into the user-facing
`message` field whenever the detail is a string, non-empty, and
`_looks_like_trace()` returns False:

```py
if isinstance(exc.detail, str) and exc.detail and not _looks_like_trace(exc.detail):
    message = exc.detail
```

`_looks_like_trace` only catches: length > 240, or one of {`Traceback`,
`traceback`, ` at 0x`, `sqlite3.`, `IntegrityError`, `OperationalError`,
`psycopg`, `column `, `FOREIGN KEY`, `UNIQUE constraint`, `NOT NULL
constraint`}. Anything else — `ValueError("user 1273 already linked to
team alpha")`, `HTTPException(400, f"invalid window={window!r}")`,
`HTTPException(409, f"slug '{slug}' is reserved")` — is short, doesn't
match a token, and ships verbatim. This matches the gap previously
flagged in `audits/audit_error_leak.md:18` ("shallow defence … short
`ValueError("invalid foo")`-style messages and most `httpx.HTTPError`
strings pass straight through"), and the heuristic was not strengthened
in the interim. Risk: business-logic enumeration (user IDs, slugs,
internal IDs, version strings) reaches anonymous callers via
intentionally-raised `HTTPException` whose author assumed the global
handler would sanitise it.

**Recommendation:** invert the contract — only echo `exc.detail` when the
raising code opts in (e.g. `HTTPException(..., headers={"X-Safe-Detail":
"1"})` or a typed wrapper class), otherwise always use the generic
message. Keeps the safe defaults; lets routes still surface message
copy when they've vetted it. Add a `_DENY_TOKENS` expansion at minimum
(API key prefixes, `id=`, `email=`, `at line`, file path patterns) but
that's a band-aid, not the fix.

### 2. [HIGH] HTML error pages hide `request_id` for all 4xx — no traceable handle for a non-API user reporting a `400`/`403`/`409`
**Location:** `gateway/error_handlers.py:244-252`.

```py
meta_block = ""
if status >= 500:
    meta_block = (
        '<p class="nv-error__meta">Request ID: '
        f'<code>{_html_escape(request_id)}</code></p>'
    )
```

The rationale comment ("Request ID is only useful when there's a
server-side incident worth quoting in a support ticket — 5xx. Showing
it for 404 / 403 adds noise without action.") is reasonable for 404,
but breaks for 401/402/403/409/422 — a paying user who hits a wrong-tier
403 or a 422 they can't decode has no handle to give support. The
fallback channel is the `X-Request-ID` response header set by
`RequestIDMiddleware.dispatch` (line 494), but that header is not
visible in any normal browser UX (no devtools), so for the typical
non-engineer user it doesn't exist. `ERROR_HANDLING.md` line 60 says
"Shows the request id in monospace for support tickets" without a
status carve-out — the implementation and the spec disagree.

**Recommendation:** surface `request_id` in `meta_block` for **every**
status, or at minimum every 4xx >= 401. Render it small/muted (already
done via `.rid` class in the fallback template at line 173) so it
doesn't dominate. Update the rationale comment or
`ERROR_HANDLING.md` to match whichever rule wins.

### 3. [MEDIUM] `RequestIDMiddleware` trusts inbound `X-Request-ID` from any origin — header-injection / log-poisoning vector
**Location:** `gateway/error_handlers.py:485-495`.

```py
incoming = request.headers.get("x-request-id", "") or ""
if incoming and len(incoming) <= self.MAX_INBOUND_LEN and incoming.isprintable() and " " not in incoming:
    rid = incoming
else:
    rid = generate_request_id()
```

A printable, space-free, ≤64-char string is accepted unconditionally
and then:
1. Pushed into structured logs via the `extra` dict in
   `app_exception_handler` (line 423) and any other downstream logger.
2. Reflected in the `X-Request-ID` response header (line 494).
3. HTML-escaped only in the HTML path (line 251) — the JSON path
   embeds the raw string into the envelope (line 336).

Two concrete risks:

- **Log injection.** A caller sets
  `X-Request-ID: foo<lookalikes>` or a value with control characters
  that survive `.isprintable()` (most C0/C1 controls are caught, but
  zero-width chars, RTL overrides, and many Unicode lookalikes are
  printable). Downstream log aggregators that don't quote the field
  see corrupted entries. The docstring says "tests / retries / proxy"
  but does not gate on `request.client` or a header from the proxy
  itself (e.g. Cloudflare `cf-ray`).
- **Cache poisoning / response splitting (low odds in 2026 ASGI).**
  Echoing a caller-controlled string into a response header. Starlette
  rejects CR/LF and that's the canonical defence, but the safer
  posture is to constrain the inbound value to `[A-Za-z0-9_.-]{1,64}`.

**Recommendation:** replace `incoming.isprintable() and " " not in
incoming` with a strict regex (`re.match(r"^[A-Za-z0-9._-]{8,64}$",
incoming)`), or accept inbound IDs only from a trusted-proxy IP
allowlist (Cloudflare). Always re-mint on mismatch.

---

## Per-error review

Each handler / status reviewed against the four axes from the brief:
**(a) HTTP code**, **(b) info leak**, **(c) request_id surfaced**,
**(d) HTML/JSON content negotiation**.

### `http_exception_handler` (line 345-389) — generic `StarletteHTTPException`

| Axis | Verdict | Notes |
|------|---------|-------|
| a    | OK      | Re-uses `exc.status_code` verbatim; no remapping. Correct. |
| b    | **HIGH** — see finding #1 | `exc.detail` echoed when short + clean of trace tokens. |
| c    | PARTIAL | JSON envelope always carries `request_id` (line 336). HTML path only renders `request_id` for >=500 (line 248) — see finding #2. |
| d    | OK      | `is_api_request` covers (i) `/api/` path prefix, (ii) `Accept: application/json` w/o `text/html`, (iii) `Content-Type: application/json`. **MEDIUM**: doesn't handle `Accept: */*` from `curl`/`wget` — those get the HTML page by default. Defensible (HTML is the safe fallback) but worth documenting. |

Per-status sub-review (table-driven via `_STATUS_TO_*`):

| Status | Slug                       | Title             | Message               | CTA branch | Notes |
|-------:|----------------------------|-------------------|-----------------------|------------|-------|
| 400    | bad_request                | Bad request       | OK                    | default    | OK    |
| 401    | authentication_required    | Sign in to continue| OK                   | dedicated `/login` + `/enquire` | OK |
| 402    | subscription_required      | Subscription required | OK                | dedicated, plus 402-only extra-line at :203-209 linking `/pricing` | OK |
| 403    | authorization_required     | You don't have access | OK                | dedicated (same as 402: pricing + dashboards) | **LOW**: 402 and 403 share the same CTAs (lines 280-286). Intentional per comment "audit flagged 403 as overdesigned", but a true 403 (e.g. impersonation block) wants a `/account` link, not `/pricing`. |
| 404    | resource_not_found         | Not found         | OK                    | dedicated + 404-only search box + `_TOP_LINKS_404` (line 99-106) | OK; **INFO**: `_TOP_LINKS_404` is hardcoded; no test asserts the links still resolve. |
| 405    | method_not_allowed         | **missing**       | **missing**           | default    | **MEDIUM** — slug present, no title/message, falls to generic `"Error" / "Something went wrong."`. Should add row to both tables. |
| 409    | duplicate_resource         | Already exists    | OK                    | default    | OK    |
| 413    | payload_too_large          | **missing**       | **missing**           | default    | **LOW** — slug only, no title/message. |
| 415    | unsupported_media_type     | **missing**       | **missing**           | default    | **LOW** — slug only, no title/message. |
| 422    | validation_failed          | Check your input  | OK                    | default    | Validation handler has its own path (below). |
| 429    | rate_limit_exceeded        | Slow down         | OK                    | `javascript:history.back()` | OK; `Retry-After` is parsed from `exc.headers` (line 368-373) and rendered in the page at :197-201. **INFO**: `Retry-After` header is **not** re-attached to the HTML response (line 270 only attaches when `retry_after is not None` was passed as kwarg, which it is — OK). JSON path passes the original headers through (line 382). OK both ways. |
| 500    | internal_error             | Something broke   | OK                    | dedicated  | OK    |
| 502    | upstream_error             | Upstream error    | OK                    | default    | **LOW** — no dedicated CTA; falls through to "Back to dashboard". |
| 503    | service_unavailable        | Temporarily down  | OK                    | dedicated `/status` + 503-only extra-line linking `/status` (line 210-214) | OK |
| 504    | upstream_timeout           | Upstream timeout  | OK                    | default    | **LOW** — no dedicated CTA. |

Default fallback (unknown status, e.g. `418`, `451`, `499`):
- Slug → `"error"` (line 110). Generic, OK.
- Title → `"Error"` (line 189). OK.
- Message → `"Something went wrong."` (line 190). OK.
- CTA → "Back to dashboard". OK.
- HTML 5xx-meta block: rendered for any status >= 500 (line 248), so an
  un-tabled 599 still gets `request_id` rendered. OK.

### `validation_exception_handler` (line 392-412) — pydantic `RequestValidationError`

| Axis | Verdict | Notes |
|------|---------|-------|
| a    | OK      | Always 422. Correct per RFC 7807-ish convention. |
| b    | PARTIAL — **MEDIUM** | Per-field messages are `str(e.get("msg"))` passed through `_sanitize_validation_msg` (line 463-467), which only checks `_looks_like_trace` and truncates to 200 chars. Pydantic v2 messages can contain the **input value** verbatim (e.g. `Input should be a valid email address, ['<rejected>']`) — those values may include PII the caller submitted (their own email, an attempted other-user's email in an admin context, etc.). The truncation+token check does not strip values. |
| c    | OK in JSON | JSON envelope carries `request_id`. HTML 422 page does **not** (status < 500, see finding #2). |
| d    | OK      | Same `is_api_request` gate. HTML path renders a 422 page with no field detail (line 412) — correct, because the HTML page is for browser nav users who are mid-form and don't need a JSON error blob. |

`loc` handling (line 398-399):
- Strips the first segment (`body` / `query` / `path` / `header`) for
  a tidier field path. OK; matches docstring.
- Edge case: if `loc` is empty (rare), `field_parts` becomes `["__root__"]`-ish.
  No crash, but the JSON `field` value will be empty string. **INFO** —
  fall back to `"_"` or the full `loc` for traceability.

### `app_exception_handler` (line 415-436) — catch-all `Exception`

| Axis | Verdict | Notes |
|------|---------|-------|
| a    | OK      | Always 500. Correct — true unhandled exceptions are 500 by definition. |
| b    | OK      | **Never** echoes `exc.message` or `exc.args`. Renders `_STATUS_TO_MESSAGE[500]` only. This is the load-bearing safety property and it holds. |
| c    | OK      | `log.exception` includes `request_id`, `path`, `method` in `extra` (line 421-428). JSON envelope carries it. HTML 500 renders it (status >= 500). |
| d    | OK      | Same content-negotiation gate. |

**MEDIUM**: `log.exception` writes the traceback to whatever Python logging
config is in place; verifying that those logs are scrubbed at the
aggregator boundary is out of scope for this file but flagged as
prerequisite — if the log sink is misconfigured (e.g. shipping to a
3rd-party that returns log lines in support-portal UI), the leak
moves there. Cross-ref: `audits/audit_logging_config.md`,
`audits/audit_pii_logs.md`.

### `is_api_request` (line 135-152) — content-negotiation gate

| Axis | Verdict | Notes |
|------|---------|-------|
| a/b  | n/a     | No status emitted. |
| c    | n/a     | No request_id touched. |
| d    | OK with caveats | Three triggers:<br>1. Path prefix `/api/` — correct.<br>2. `Accept` contains `application/json` and not `text/html` — correct; explicit JSON-only.<br>3. `Content-Type: application/json` — **INFO**: a JSON-body POST that 415s in the middleware will still get JSON. Defensible.<br>**MEDIUM**: doesn't include `/api_public/`, `/api_v1/`, `/.well-known/`, `/webhooks/`. Anything not under `/api/` but conceptually an API endpoint will get HTML unless the client sets `Accept`. Reviewer should cross-check the actual public-API mount points in `gateway/server.py` and broaden the prefix list, or document the contract that all JSON endpoints must live under `/api/`. |

### `RequestIDMiddleware` (line 475-495) — request_id minting + echo

| Axis | Verdict | Notes |
|------|---------|-------|
| a    | n/a     |       |
| b    | PARTIAL — see finding #3 | Inbound trust too permissive. |
| c    | OK      | Stamps `request.state.request_id` (line 491) and `X-Request-ID` header (line 494). `setdefault` correctly avoids stomping an upstream-set header. |
| d    | n/a     |       |

**LOW**: Middleware is registered with `app.add_middleware` *after* the
exception handlers (line 502-506). FastAPI/Starlette runs middleware
in reverse-add order, so this is fine — but a future contributor
adding more middleware needs to remember that ordering. Comment at
line 505-506 says "Middleware last so it wraps everything else"
which is correct but worth a one-line ASCII diagram for clarity.

### `RequestIDMiddleware.MAX_INBOUND_LEN` = 64 (line 483)

**INFO**: 64 chars is generous given the internally-minted IDs are 8
chars (`secrets.token_hex(4)`). A common upstream value is the
Cloudflare ray id (`8b3a5d1f7a8e1234-LHR`, 20 chars). 64 leaves headroom
for distributed-trace IDs (W3C traceparent is 55 chars). OK.

### `generate_request_id` (line 113-115)

8 hex chars = 32 bits of entropy. Collision probability over 1M
requests/day ≈ 1 in 8500 — collisions in logs are possible but rare
and traceable via `(path, method, timestamp)`. **INFO**: if scaling
materially, expand to 16 hex chars.

### `_load_template` (line 160-165) + `_FALLBACK_TEMPLATE` (line 168-176)

| Axis | Verdict | Notes |
|------|---------|-------|
| a/d  | OK      | Fallback ensures the error page never fails to render. |
| b    | OK      | Fallback uses `{title}` / `{message}` / `{request_id}` placeholders, but those are filled by `tpl.replace(...)` in `render_error_page` (line 254-266) using `{{ name }}` substitution. **MEDIUM bug**: the `_FALLBACK_TEMPLATE` uses single-brace placeholders (`{title}`, `{message}`, `{request_id}`) but `render_error_page` calls `.replace("{{ title }}", ...)` — single braces will never be replaced. If the template file fails to load, the fallback renders literal `{title}` / `{message}` / `{request_id}` in the body. Verify with: a quick `chmod 000` on `gateway/static/error_page.html` would surface this immediately. |

### `_html_escape` (line 313-319)

| Axis | Verdict | Notes |
|------|---------|-------|
| b    | OK      | Escapes `& < > "`. Does **not** escape `'` — defensible since all attributes in the template use double quotes, but a future template change to single-quoted attrs would break. **LOW**: add `.replace("'", "&#39;")` defensively. |

### `_TOP_LINKS_404` (line 99-106)

Six hardcoded curated links. **INFO**: no test asserts these resolve;
a renamed route silently produces dead links on the 404 page. Recommend
a regression test that hits each href and asserts 200.

---

## Additional cross-cutting notes

### Status codes used vs. RFC

All used statuses are RFC-valid. No 419/418/420/451 misuses. 402 is used
deliberately for paywall (non-standard but widely adopted convention).

### `Retry-After` handling

`http_exception_handler` extracts `Retry-After` from `exc.headers`
(line 368-373) and forwards both:
- To the JSON response via `headers=headers` (line 382). OK.
- To the HTML render via `retry_after` kwarg, which produces a
  visible "Try again in N seconds" line **and** re-attaches
  `Retry-After` to the response (line 268-269). OK.

`ValueError` on parse falls through to `retry_after = None` — silent,
not logged. **LOW**: a malformed `Retry-After` is a sign of an upstream
bug worth logging at WARN.

### Logging

Only `app_exception_handler` calls `log.exception`. `http_exception_handler`
and `validation_exception_handler` are silent — correct, because those
errors are not server bugs and would flood the log. **INFO**: consider a
WARN log on 500-class `HTTPException` (e.g. `HTTPException(503, ...)`
raised by the app itself) so true-outage signals reach ops.

### No PII / secret-shaped pattern stripping

`_looks_like_trace` doesn't look for API-key prefixes (`sk-`, `pk_`,
`whsec_`), JWT-shape (`eyJ` followed by base64), or
PostgreSQL-connection-string fragments (`postgres://`). All would
pass the gate today. **MEDIUM** — same category as finding #1; same fix
(opt-in echo).

### Wire contract drift

`ERROR_HANDLING.md` line 17-25 documents the JSON envelope. Module
matches:
- `error` (slug) — `_json_envelope` line 333. OK.
- `message` — line 335. OK.
- `request_id` — line 336. OK.
- `details` — optional, only when truthy (line 338-339). OK.

Validation envelope adds `details.errors[]` — matches doc line 45-51. OK.

### Tests

Not in scope for this file, but referenced for completeness. The handlers
are covered by tests under `gateway/tests/` — the audit does not verify
those tests run or pass. Cross-ref `audits/audit_test_conftest.md`.

---

## Pre-release surface

Per brief: not probed. All findings are based on static read of
the module + adjacent docs. No live request was sent to any
narve.ai / staging / pre-release host.

---

## Action items (priority order)

1. **[HIGH]** Invert `exc.detail` echo contract — opt-in only.
   (`http_exception_handler`, `_looks_like_trace`.)
2. **[HIGH]** Surface `request_id` in HTML pages for every 4xx >= 401
   (or update `ERROR_HANDLING.md` to match current code).
3. **[MEDIUM]** Tighten `RequestIDMiddleware` inbound validation to
   `^[A-Za-z0-9._-]{8,64}$`.
4. **[MEDIUM]** Add 405 / 413 / 415 rows to `_STATUS_TO_TITLE` and
   `_STATUS_TO_MESSAGE`.
5. **[MEDIUM]** Fix `_FALLBACK_TEMPLATE` placeholder mismatch
   (`{title}` → `{{ title }}` or change `replace` call).
6. **[MEDIUM]** Strip pydantic input values from `_sanitize_validation_msg`.
7. **[MEDIUM]** Broaden `is_api_request` path prefixes (or document the
   "all JSON endpoints under `/api/`" contract).
8. **[LOW]** Add 502 / 504 CTA branches.
9. **[LOW]** Differentiate 402 vs. 403 CTAs.
10. **[LOW]** Log WARN on unparseable `Retry-After`.
11. **[LOW]** Escape `'` in `_html_escape`.
12. **[INFO]** Add regression test for `_TOP_LINKS_404` href resolution.
13. **[INFO]** Consider 16-hex `generate_request_id` if scaling.
