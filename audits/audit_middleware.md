# Adversarial audit — `gateway/middleware/`

Date: 2026-05-15
Scope: every file in `/Users/shocakarel/Habbig/gateway/middleware/`
Focus areas (from request):
- Subproduct middleware host validation + `cf-connecting-ip` enforcement in prod
- CSRF middleware exemption list
- Rate-limit middleware key derivation
- Body-size limit
- Signed-secret HMAC for inter-service calls

Severity legend: **CRITICAL / HIGH / MEDIUM / LOW / INFO**

---

## Directory inventory

```
gateway/middleware/
├── __init__.py                  (6 lines — docstring only)
├── bulk_data_ratelimit.py       (203 lines)
├── perf.py                      (173 lines)
└── subproduct.py                (141 lines)
```

### Scope clarification — what is NOT in this directory

The request asked about CSRF exemptions and signed-secret HMAC for inter-service calls. **Neither lives in `gateway/middleware/`.**

- **CSRF middleware** is defined inline in `gateway/server.py` at line 1356 (`class CSRFMiddleware`). The exemption list is therefore out of scope for this audit but worth tracking separately — see "Cross-cutting findings" below.
- **Signed-secret HMAC for inter-service calls** does not exist as a middleware in this directory at all. The only HMAC reference in the codebase is the subdomain HMAC for embeds (referenced in a docstring in `server.py:492`) and Stripe webhook signature verification in the billing routes — neither is a generic inter-service signed-secret guard. **No middleware in this directory implements signed-secret authentication.** Whether that is a gap depends on the architecture; see Finding S-MISSING-1 below.
- **Body-size limit** also does not exist as a dedicated middleware. There is no `BodySizeMiddleware` anywhere in `gateway/middleware/`. See Finding S-MISSING-2.

Because the directory only contains three substantive files, the audit covers them exhaustively and additionally documents the missing controls.

---

## `__init__.py`

6-line docstring stub. Nothing to audit. **No findings.**

---

## `subproduct.py` — `SubproductMiddleware`

Purpose: allowlist host header, reject direct-origin requests in production, attach `request.state.subproduct`.

### Findings

#### S-1 — `cf-connecting-ip` check trusts a request-supplied header — **MEDIUM**

Lines 126–136:

```python
if _is_production():
    if not request.headers.get("cf-connecting-ip"):
        ...
        return JSONResponse({"error": "Forbidden"}, status_code=403)
```

The middleware checks for the **presence** of `CF-Connecting-IP`, not whether the request actually transited Cloudflare. Any client that can talk to the origin and sets `CF-Connecting-IP: 1.1.1.1` bypasses the check. The docstring acknowledges Cloudflare WAF + origin firewall is the primary control ("WAF rules in CLOUDFLARE_CHANGES.md make this unreachable from the internet, but the middleware is the second layer"), so the deployed posture is defence-in-depth — but as a control on its own, this is bypassable in seconds by any attacker who reaches the origin TCP socket directly.

Mitigations to consider:
- Cloudflare authenticated origin pull (mTLS at the edge) — the strongest control.
- Verify the IP shape (`CF-Connecting-IP` must be a parseable IPv4/IPv6 — currently *any* non-empty string passes).
- Reject when both `CF-Connecting-IP` is present *and* the connecting socket isn't in Cloudflare's published IP ranges (would require setting `forwarded_allow_ips` at the uvicorn layer; not currently done).

#### S-2 — Allowlist depends on import-time `_CATALOG`; degraded mode opens nothing but locks out everything — **LOW**

Lines 41–46:

```python
try:
    from subproduct import SUBPRODUCTS as _CATALOG
except Exception:
    _CATALOG = {}
```

If the import fails (which the broad `Exception` catch silently allows), `_subproduct_hosts()` returns the empty set, and **every** subproduct host like `crypto.narve.ai` becomes a 400. The apex still works because `_APEX_HOSTS` is static. Fail-closed for subproducts is the right call, but the silent `except Exception` means an admin reading logs sees no signal — only a flood of 400s. Recommend logging the import failure at `log.error` instead of swallowing it.

#### S-3 — Empty `host` header bypasses both checks — **MEDIUM**

Line 117–120:

```python
host_header = request.headers.get("host", "")
host = _strip_port(host_header)
allow = allowed_hosts()
if host and host not in allow:
    ...
    return JSONResponse({"error": "Invalid Host header"}, status_code=400)
```

The guard is `if host and host not in allow`. When the Host header is empty or absent, `host == ""` so the `if` is `False`, and the request **falls through** to the Cloudflare-IP check (which is then the only gate). In production with WAF in front this is mostly cosmetic — HTTP/1.1 requires Host — but a Starlette test client or a direct-origin attacker can easily craft an empty Host. Combined with S-1, an attacker with an empty Host header who also sets `CF-Connecting-IP: anything` passes both checks and `request.state.subproduct = None`, landing on the apex narve.ai brand. Probably not exploitable on its own, but a needless gap.

Fix: reject `host == ""` explicitly, or default the empty case to `"unknown"` so it fails the allowlist.

#### S-4 — Port stripping doesn't handle IPv6 hosts — **LOW**

`_strip_port` (line 84) does `host.split(":", 1)[0]`. For an IPv6 bracketed host like `[::1]:8080`, this returns `[` — which then fails the allowlist (correct outcome), but for an unbracketed IPv6 like `::1` it returns `""` (the empty case from S-3). Dev-only; production goes through Cloudflare. **Informational.**

#### S-5 — `_is_production()` reads env every request — **INFO**

Line 107–110. Intentional per the comment ("Re-read each request so tests can flip PRODUCTION via monkeypatch") and `os.environ.get` is microseconds. Not a real perf issue. Documenting for completeness.

#### S-6 — No rate limit on rejected hosts → cheap log-amplification — **LOW**

A scanner hitting random Host values on the origin generates one `log.info` line per request. Cloudflare drops most of this, but the GlobalRateLimitMiddleware sits *outside* this one in the stack (registered later → wraps it), so most spam is already capped. **Informational** — confirmed the ordering provides the necessary backstop.

#### S-7 — Missing-from-directory: `BodySizeMiddleware` — **HIGH**

No middleware in this directory caps request body size before the handler reads it. Starlette/FastAPI will happily buffer multi-MB JSON bodies in memory before a route handler decides to 413. The only backstop is uvicorn/Cloudflare default limits. For an authenticated PATCH/POST to a take or signup endpoint, an attacker can DoS memory by uploading e.g. a 50 MB body — handled, but expensive. Recommend a `BodySizeLimitMiddleware` returning 413 above N MB (different cap for `multipart/form-data` if image uploads are added).

#### S-8 — Missing-from-directory: inter-service signed-secret HMAC middleware — **MEDIUM (context-dependent)**

If the gateway is the only service (no separate dashboards calling back), this is N/A. If subproduct dashboards (`crypto-dashboard`, `centralbank-dashboard`, etc., all visible at the Habbig root) make calls back to the gateway as services rather than as users, those calls should carry an HMAC-signed request and be verified before any auth state is considered. Worth confirming the architecture; if such calls exist they currently rely on the same session cookie path as users, which means a token leak from a subproduct dashboard escalates to full gateway impersonation.

---

## `bulk_data_ratelimit.py` — `BulkDataRateLimitMiddleware`

Purpose: per-user hourly row budget on JSON list responses (5k/h → 429; 20k/24h → flag).

### Findings

#### B-1 — Counter increments **before** the budget check → first over-budget response is served before the 429 — **HIGH**

Lines 156–203. The middleware first runs `call_next(request)` (line 156), then reads the body, then calls `_record_and_check(user_id, rows)` (line 175) which **first inserts/updates** the counter and **then** reads back to decide whether to 429. The effect:

- The work to compute the response (DB query, serialization) is paid every time, even for over-budget users.
- The over-budget user still *receives* the data on the request that pushed them over. They get a 429 on the next call only.
- Worse: because the row count is taken from the response body, the user effectively "pre-pays" by getting the over-budget data once, then is locked out — but they have the data.

For an exfiltration backstop this is the wrong shape. A motivated attacker who knows the budget is 5000/h can issue 1 request that returns 100,000 rows, get all the data, and *then* be rate-limited.

Mitigations:
- Hard cap the response page size at the route level so no single request can blow the budget.
- Or: check the existing hour total **before** running the handler and 429 if the user is already over (preserves the "pay once to learn the limit" behaviour but prevents a single mega-fetch).
- Or: refuse to return rows beyond `ROW_BUDGET_HOUR - current_total` (clip the response). Complicated but truest to "budget."

#### B-2 — `int(uid)` raises on non-numeric session user-id — **MEDIUM**

Line 90:

```python
uid = user.get("user_id") or user.get("id")
if uid:
    return int(uid)
```

If `user_id` is ever a UUID string or anything non-numeric, `int(uid)` raises `ValueError` — which is **not** caught by the function. It bubbles up into the `await call_next` chain → 500. The hardened session writes `user_id` as an int today, but the contract is implicit. Wrap in try/except or document the contract in the session middleware.

The impersonation branch at line 92 *does* catch its `int(...)` in try/except — inconsistent handling.

#### B-3 — Skip threshold of `rows < 20` is an exfil corridor — **MEDIUM**

Line 167–168:

```python
if rows < 20:
    return response
```

An attacker paginating with `?limit=19` will never increment the counter. 19 rows × infinite requests = unbounded exfiltration. The threshold exists to avoid burning budget on tiny notification responses, but the floor needs to be paired with a request-count guard (which lives elsewhere as GlobalRateLimitMiddleware — confirm it covers these list endpoints with a *per-user* not per-IP key, otherwise the exfil corridor stands).

#### B-4 — `json.loads(body)` runs on every JSON response — **MEDIUM**

Line 62. For responses that aren't list-shaped (e.g. a 1 KB user-profile JSON) the middleware still parses the body to discover it returns 0. Across the whole API this is wasted CPU on the hot path. Cheap fix: peek the first byte — if it isn't `[` and `data` field isn't likely present, skip parse. Or check `Content-Length` against a threshold.

#### B-5 — Streaming responses silently bypass the budget — **HIGH**

Lines 162–165:

```python
body = getattr(response, "body", None)
if not body:
    return response
```

`StreamingResponse` has no `.body` attribute (or returns empty). The docstring acknowledges this ("StreamingResponse, which is skipped"). For an exfiltration backstop, **any** route that uses streaming responses to return user data — admin exports, CSV downloads, large reports — is **completely** exempt. Confirm the inventory of streaming routes in the gateway; document them as explicitly out of budget; consider adding a streaming-aware counter (count chunks/lines).

#### B-6 — Counter UPSERT happens on every list response — DB contention vector — **LOW**

`bulk_fetch_counters` writes one row per `(user_id, hour_bucket)` per response. Under sustained list-API traffic, the SQLite write lock is hit hard. The `_db.conn() as c` (line 111) is presumably a write-capable connection. SQLite serializes writes, so on a single-writer DB this throttles concurrent list responses for ALL users. Confirm whether the write path runs in WAL mode and that the gateway has a single writer pool (typical narve.ai pattern). If not, this is a latency-spike risk on busy hours.

#### B-7 — `_resolve_user_id` does not fall back to `state.session_user` or any legacy key — **LOW**

Line 79–97 only checks `state.user` (hardened) and `state.impersonation`. If any legacy code path attaches user under another attribute (e.g. `state.session_user`), bulk fetches by that user run **unmetered**. Confirm there is no second auth path. server.py mentions a "legacy session cookie path still works" — that path's user-attachment is worth verifying.

#### B-8 — `last_updated` is `now` from `time.time()` — no clock-skew handling — **INFO**

If the host clock jumps backwards (NTP correction), the same `hour_bucket` may be written twice with conflicting `last_updated` values. The UNIQUE constraint on `(user_id, window_start)` catches it via the ON CONFLICT clause, so no bug — just informational.

#### B-9 — Day-budget flag is set but the request is allowed through — by-design — **INFO**

Line 138–143. Documented in the docstring at the top of the file ("20000 rows / 24h → flag for review (but allow the current request to pass)"). Confirmed intentional. **No issue.**

#### B-10 — Headers leak to error responses — **LOW**

When `over` is True (line 186), the 429 response is created **fresh** with only `Retry-After` and `X-Bulk-Rows-Remaining` set — the budget-day header is lost on the 429. Minor UX issue, not a security one. Document or move the header logic above the branch.

#### B-11 — `Retry-After` derived from `_hour_bucket(int(time.time()))` recomputes inside the response constructor — **INFO**

Line 187 vs 199 — recomputes `int(time.time())` twice within ~microseconds. Cosmetic. Cache to local.

---

## `perf.py` — `RequestTimingMiddleware`

Purpose: set `X-Response-Time-ms` header, log slow requests to `slow_request_log`.

### Findings

#### P-1 — `_log_slow_request` opens a DB write on every slow request on the response hot path — **MEDIUM**

Lines 82–116. The DB write happens synchronously after the response is computed but before `return`. For a route already crossing 500 ms, adding a synchronous write under contention is cumulative latency. The docstring claims "Never block the hot path" but the implementation does block in the slow-path tail. Comparable to B-6.

Fix options:
- Background task / async queue.
- Sample (e.g. log 1-in-10 slow requests above some duration).
- Increase the threshold or batch-flush.

#### P-2 — Slow-log table is unbounded — **HIGH**

`slow_request_log` (migration 096) has no retention policy visible in the migration or this middleware. Every slow request appends forever. With 1k slow events/day and a several-month-old gateway, the table grows. Indexes on `timestamp DESC`, `(path, timestamp DESC)`, `duration_ms DESC` further amplify disk pressure. Confirm: is there a scheduled prune job? If not, this is a slow-burn disk-space DoS, particularly under attack (an attacker who deliberately triggers slow paths grows the table). Wasn't found in the migration directory.

#### P-3 — `path` is stored verbatim — PII-by-URL risk — **MEDIUM**

Line 138, 165. The docstring explicitly justifies storing path: "the /admin dashboard doesn't care about the user's username; it cares about which *handler* is slow." But many routes carry IDs in the path (e.g. `/u/<username>`, `/api/predictions/<id>`, `/admin/users/<email>`). Storing these verbatim means the `slow_request_log` *is* a log of who-did-what-when, despite the IP being hashed. Subpoena/data-leak surface bigger than the docstring implies.

Mitigations:
- Strip path segments to handler templates (e.g. `/u/<username>` → `/u/:username`). Requires hooking the route resolver — non-trivial but doable.
- Truncate to first 2 path segments.
- Apply same redaction the access logger uses (assumes one exists; confirm in `logging_config.py`).

#### P-4 — `_hash_ip` truncates SHA-256 to 16 hex chars — **LOW**

Line 79. 16 hex = 64 bits. With 2^32 distinct daily-active IPs (overkill but possible), birthday collisions become non-trivial. Not a security issue per se because the hash isn't auth-bearing, but cross-referencing IPs between tables would be ambiguous after enough rows. The bigger concern: the truncated hash is **unsalted**, so a global rainbow table of IPv4 SHA-256 prefixes lets anyone with the slow-log decrypt IPs. Recommend a server-side salt (constant for the deployment, secret).

#### P-5 — User-agent bucketer is permissive to attackers — **LOW**

Lines 58–66. Any UA without `bot|crawl|spider|mobile|iphone|android` defaults to "desktop". A scraper that sets UA to `Mozilla/5.0 Chrome/120` looks like a human in the slow log. Probably fine for the dashboard's purpose, but the slow log is then **wrong** for abuse-detection — confirm the admin dashboard isn't filtering "exclude bots" and missing the real attacker traffic.

#### P-6 — Exception path also writes to `slow_request_log` — **LOW**

Lines 127–147. When the downstream raises, the timing middleware logs the slow request **and** re-raises. If the downstream exception is caused by a DB error (e.g. the same DB the middleware is about to write to), the write inside the slow-log can re-raise too — not caught here, it would bubble out of dispatch as a *different* exception. That said, `_log_slow_request` does have its own try/except. OK on closer read — keep informational.

#### P-7 — `X-Response-Time-ms` header leaks request timing to clients — **LOW**

Line 150. Exposing precise server-side timing helps timing-attack reconnaissance (e.g. distinguishing "user exists" vs "user does not exist" auth responses by latency). Probably mitigated by the route-level constant-time comparisons assumed elsewhere, but the header makes recon trivial. Consider dropping the header in production or only on admin/internal endpoints.

#### P-8 — Threshold env var read once at import — **INFO**

Line 45–47. `SLOW_REQUEST_THRESHOLD_MS` is parsed once. Changing it requires a restart. Documented constraint.

#### P-9 — `_SKIP_PATHS = frozenset()` — empty, so noisy paths *do* get logged — **INFO**

Line 54. Comment says "extend if a particular path dominates the log and isn't actionable." Confirm `/healthz` and similar do not flood the log. **Cross-cutting** — the bulk-data middleware *does* skip `/healthz` but the timing middleware does not.

---

## Cross-cutting findings (about the directory as a whole)

#### X-1 — Three of four files swallow import errors at registration site — **MEDIUM**

`server.py` registers each middleware inside a `try/except Exception` and logs at WARNING. If any middleware fails to import (e.g. due to a refactor renaming `db.conn`), the gateway boots with that protection **silently disabled**. There is no startup-time assertion that all expected middlewares are active. Recommend a startup check listing which middlewares were registered, surfaced in `/healthz?deep=true` or boot logs at INFO/ERROR.

#### X-2 — No tests visible in this directory — **INFO**

There is no `tests/test_subproduct_middleware.py` (or similar) in the audited directory. Tests may live under `gateway/tests/` — confirm coverage exists for:
- Empty Host header → 400 (S-3)
- Missing `CF-Connecting-IP` in prod → 403 (and present-but-fake passes — S-1)
- Bulk-data 429 boundary
- Bulk-data streaming-response bypass (B-5)
- Slow-log writes when threshold crossed
- Slow-log skipped when below threshold

#### X-3 — CSRF middleware not in this directory — **OUT OF SCOPE for this audit**

The CSRF middleware (`server.py:1356`) was specifically requested in the focus list. Because it does not live under `gateway/middleware/`, its exemption list is not audited here. A separate audit of `server.py:1356-1470` is warranted; flagging only.

#### X-4 — No rate-limit middleware in this directory at all — **OUT OF SCOPE caveat**

The `GlobalRateLimitMiddleware` is defined inline in `server.py:1889`. Its key derivation was requested in the focus list. Not audited here for the same reason as X-3.

---

## Severity counts (across the audited directory)

| Severity | Count |
|----------|------:|
| CRITICAL |     0 |
| HIGH     |     4 |
| MEDIUM   |     8 |
| LOW      |     9 |
| INFO     |     6 |
| **Total**|  **27** |

HIGH findings: S-7 (no body-size cap), B-1 (counter races response), B-5 (streaming bypasses budget), P-2 (slow-log unbounded).

---

## Top 5 priority findings (recommended fix order)

1. **B-1** — Bulk-data middleware records counter *after* serving the response, so the request that breaches the budget is served in full. Hard cap response page size at the route level, or check the existing total *before* `call_next`. (HIGH)
2. **B-5** — Streaming responses are completely exempt from the bulk-data budget. Any CSV/export/admin-dump endpoint bypasses the exfiltration backstop. Add a streaming-aware accounting path or block streaming for non-admin users. (HIGH)
3. **S-7** — No request-body-size middleware exists. A multi-MB POST body is buffered and parsed before any handler can reject it. Add a `BodySizeLimitMiddleware` returning 413 at, e.g., 256 KB for JSON / configurable for multipart. (HIGH)
4. **P-2** — `slow_request_log` has no retention policy. Slow-path requests append forever; under attack this is a disk-space DoS and a privacy growing-ground. Add a nightly prune to N days (e.g. 14). (HIGH)
5. **S-1** — `cf-connecting-ip` presence check is bypassable by anyone who can talk to the origin. Move to Cloudflare authenticated origin pulls (mTLS) or validate against Cloudflare's published IP ranges at the uvicorn `forwarded_allow_ips` layer. (MEDIUM, but high real-world impact because it's the second-layer guard the docstring relies on.)
