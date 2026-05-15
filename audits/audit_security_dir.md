# Adversarial Audit — `gateway/security/`

**Date:** 2026-05-15
**Auditor:** Claude (Opus 4.7, 1M context), running synchronous-only bash, no code changes.
**Scope:** Every file under `gateway/security/` (8 files, ~64 KB).
**Audit lenses (per task):**
1. CSRF-token generation entropy
2. Validator timing-attack resistance
3. Rate-limit-bucket key derivation
4. Header-injection in security headers (X-Frame-Options, CSP, HSTS)

**Severity scale:** CRIT (exploit ships RCE / auth bypass / silent fund movement) > HIGH (data leak, account takeover, durable bypass with realistic attacker) > MED (defence-in-depth weakness, hard-to-reach bypass, telemetry / log integrity) > LOW (hygiene, ambiguous policy, observation only) > INFO (note for future maintainers; no finding).

**Scope clarification (lens #4 — security headers):**
The four lenses include header-injection in security headers, but `gateway/security/` does **not** actually emit `X-Frame-Options`, `Content-Security-Policy`, or `Strict-Transport-Security`. Those live in `gateway/server.py` (`SECURITY_HEADERS` dict + `SecurityHeadersMiddleware`, lines 827-931) and are also written ad-hoc by `embed_routes.py` and `admin_test_emails_routes.py`. For this audit I reviewed the security-relevant headers actually set by code inside `gateway/security/` (`X-CSRF-Error`, `X-RateLimit-*`, `Retry-After`, the `narve_tz` cookie) for the same injection / header-smuggling classes, and explicitly note where the canonical CSP/HSTS audit should be done in another sweep.

Files audited (`ls gateway/security/`):
```
__init__.py
audit.py
csrf.py
idempotency.py
input_hygiene.py
logger.py
rate_limiter.py
timezones.py
```

---

## File: `__init__.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/__init__.py` (1 line of code, docstring only)

**Findings:** None. The module docstring (`"""Security package — CSRF, rate limiting, and security event logging."""`) is purely descriptive and exports nothing. No entropy / validator / key-derivation / header surface in this file.

**Severity:** INFO.

---

## File: `audit.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/audit.py`
**Purpose:** Append-only admin action audit log writer + filter helper for the admin audit page / CSV export.

### Lens 1 — Token-generation entropy
N/A. This file emits no tokens, secrets, or IDs.

### Lens 2 — Validator timing-attack resistance
No secret comparisons. The filter helpers (`filter_to_query_kwargs`, `filter_to_search_kwargs`) accept arbitrary query params and pass them straight to `db.query_audit_log`. None of these are secrets and none are equality-compared against confidential values, so timing-attack class does not apply.

### Lens 3 — Rate-limit-bucket key derivation
N/A. No rate-limit logic in this file. The closest analog is the audit-log filter keys (`action`, `admin_id`, `target_type`, `from`, `to`, `range`) — these are not security buckets.

### Lens 4 — Header injection
`_get_ip()` (line 161-174) takes the first comma-separated hop of `X-Forwarded-For`, falling back to `request.client.host`. The IP is then stored verbatim in the `audit_log` table via `db.insert_audit_log(ip_address=...)`. There is no validation that the value parses as an IP — an attacker can set `X-Forwarded-For: <script>alert(1)</script>, ::1` and that string lands in the audit log. Risk depends on how `admin/audit-log` renders this field (out of scope here; flag for cross-file audit).

`_get_user_agent()` (line 177-183) caps at 500 chars and stores verbatim — same caveat: stored XSS into audit log if rendered without escaping in admin UI.

`_get_request_id()` (line 186-192) reads `X-Request-ID` from the request and stores verbatim with no length cap, no character filter. An attacker controls the request-id header (it's request-supplied, not server-assigned) and can stuff CRLF, HTML, or megabytes into the audit log row. **MED** — log-injection (CRLF) into audit_log + potential stored XSS in admin viewer.

### Other findings
- **MED — Trust of `X-Forwarded-For` first hop.** `_get_ip` (line 167) uses `X-Forwarded-For` first, which is correct **only behind a trusted proxy that rewrites this header.** The same logic in `gateway/security/logger.py` and `rate_limiter.py` prefers `cf-connecting-ip` first (Cloudflare-aware). `audit.py` does not — so admin actions logged on a path that doesn't go through the same trust boundary record the attacker-supplied IP. This is inconsistent across the security module: pick one trust model and apply it everywhere. Recommend mirroring the `cf-connecting-ip` → `x-forwarded-for` → `request.client.host` order used in `logger.py` so audit-log evidence is consistent.
- **LOW — `_to_json` swallows serialisation errors silently** (line 195-201). If a `before`/`after` dict happens to contain a non-JSON-serialisable value the audit entry stores `None` for that side of the diff. The function uses `default=str` so most cases survive, but objects with broken `__str__` go silently to `None`. For an audit log this is a small evidentiary gap. Consider re-trying with `default=repr` before giving up.
- **LOW — `_parse_date` accepts unrestricted user input** (line 269-280 / 319-330). Uses `time.mktime` with the host's local TZ for date filters in the audit-log search page. An admin on a server in a different TZ sees a window shifted by the offset. Functional bug not a security one, but it can let an audit search miss the actions an attacker performed at the edges of the window.
- **INFO — `log_action` is documented as "NEVER raises"** (line 217). Verified: every backend call is wrapped in a single `try/except Exception` (line 222-239). Failure modes degrade to a `log.warning`, which is the correct posture for an audit subsystem.

### File severity tally
- HIGH: 0
- MED: 2 (header injection via X-Request-ID into audit log; XFF trust inconsistency)
- LOW: 2
- INFO: 1

---

## File: `csrf.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/csrf.py`
**Purpose:** Double-submit cookie + session-bound CSRF, with header/form parsing and a Phase-1 soft-warn rollout for PATCH/PUT/DELETE.

### Lens 1 — Token-generation entropy
`generate_csrf_token()` (line 83-85) uses `secrets.token_urlsafe(32)` — 32 *bytes* of CSPRNG output, base64url-encoded. That's 256 bits of entropy, which is dramatically over the 128-bit threshold for unguessable tokens. The docstring claim "32 bytes, URL-safe base64 encoded (43 chars)" is accurate. **No finding.**

### Lens 2 — Validator timing-attack resistance
`validate_csrf_token` (line 106-139) calls `hmac.compare_digest(reference_token, submitted_token)` (line 130). Constant-time. Good.

However, **MED — early-return short-circuits before constant-time compare** at lines 120-127:
```
if not submitted_token: return False, "missing"
reference_token = session_token if session_token else cookie_token
if not reference_token: return False, "no_reference"
```
An attacker can distinguish "no submitted token" from "no reference token" from "mismatch" via the three error reasons that the middleware echoes back in the `X-CSRF-Error` response header (line 253: `headers={"X-CSRF-Error": reason}`). This is the documented response and matches the test suite, but it leaks state:
- A blank `X-CSRF-Error: no_reference` proves the user has no session (no `_csrf` cookie either) — useful for distinguishing "logged-in target" from "not-logged-in target" from the response without touching the auth cookie. Low data value on its own; could refine a CSRF-driven phishing flow.
- `X-CSRF-Error: expired` proves the user is logged in but their session predates the last 2-hour rotation — combined with rotation timing it gives a coarse "last activity" estimate of any victim the attacker can persuade to issue a request.

This isn't classical timing-attack territory (no secret-byte oracle) but the response-header-as-side-channel is the same shape. Recommend collapsing to a single `"invalid"` reason in the user-facing response while preserving the granular reason in `log_csrf_failure` (server side).

**LOW — `hmac.compare_digest` is constant-time only across equal-length inputs.** `secrets.token_urlsafe(32)` always emits 43 chars so the legitimate reference token is fixed-length. If an attacker submits a longer or shorter token, `compare_digest` falls back to its non-constant-time short-circuit path on length mismatch. Practical impact: zero for this token shape (entropy is too high to exploit anyway), but worth knowing if the token length is ever made variable.

**LOW — `_CSRF_SKIP_PREFIXES = ("/_gateway_static", "/ws")` uses `startswith`** (line 175). A route like `/wstest` (no slash) would also bypass CSRF. None currently exist on the main app — verified with a grep at lens-3 below — but the prefix should be `"/ws/"` (with trailing slash) to make this a structural guarantee rather than a "we'd notice in code review" guarantee. Same comment applies to `/_gateway_static`; a route named `/_gateway_static_admin` would bypass.

### Lens 3 — Rate-limit-bucket key derivation
N/A — CSRF doesn't rate-limit. (`log_csrf_failure` does emit a log event that downstream alerting can rate by, but that's `logger.py`, not here.)

### Lens 4 — Header injection
Two response headers are set from server-controlled values:
1. `X-CSRF-Error: <reason>` (line 222, 253) — `reason` comes from the validator's hard-coded enum (`"missing" | "no_reference" | "mismatch" | "expired" | "origin_mismatch"`). Not user-controllable. **No injection.**
2. `set_csrf_cookie` (line 88-103) writes the `_csrf` cookie. The cookie *value* is the generated token (CSPRNG bytes — URL-safe base64, no CR/LF). The `domain` kwarg comes from `cookie_domain_fn(request)` if production; the audit hands `cookie_domain_fn=None` if not provided, so unless server.py passes a hostile callback the domain is `None`. Verified safe.

**MED — `_CSRF_EXEMPT_PATHS` includes `/api/newsletter` and `/api/scraper/ingest`.** These bypass CSRF entirely. The newsletter endpoint is documented as "no user session to anchor CSRF to — protected by per-IP rate limit + email format validation." That is correct **for now**. If a future change adds session-side effects to `/api/newsletter` (e.g. "if logged in, auto-subscribe the user") it inherits a CSRF-free POST that mutates the session-user state. The comment at line 49 already warns about this; recommend hardening with a runtime assertion in the handler that there is no authenticated session attached to the request, so a future regression fails closed.

**HIGH — Phase-1 soft-warn for PATCH/PUT/DELETE.** Lines 189-263: when `CSRF_PATCH_DELETE_ENFORCE=false` (the **default**), CSRF-invalid PATCH/PUT/DELETE requests are logged but **allowed through**. The comment promises "Phase 2 once the warning rate is zero" but the file's default is still soft-warn. Every route that uses PATCH/PUT/DELETE for a mutation is currently CSRF-bypassable from a cross-origin POST that the browser lets through. This is the highest-impact finding in the file because the default is unsafe and the env var is opt-in. Recommend either:
  - Flip the default to `true` and add a `CSRF_PATCH_DELETE_SOFT_WARN=true` opt-out for diagnostics, **or**
  - Document the exact set of PATCH/PUT/DELETE routes currently in flight and confirm every one of them has independent auth that doesn't depend on cookies (Bearer-only / API-key-only).

  Note: this is "high" because the *configuration* is unsafe-by-default, not because the code is wrong. A pentester who knows about the flag can probe.

**MED — Origin/Referer check is production-only** (line 210: `if origin and self.is_production and self.domain`). In dev / staging the secondary defense is silently disabled. This is intentional (dev tools often run on localhost without the cookie domain set), but if a staging environment ever serves real user data, the defense is off. Add a `CSRF_REQUIRE_ORIGIN=true` opt-in for staging-with-real-users deployments.

**MED — JSON-content-type detection is loose.** Line 197: `if "application/json" in content_type` matches `application/json-but-not-really`, `application/json; charset=utf-8`, etc. The string check is forgiving by intent, but it also matches the multipart-form-data fallback below. A request with `Content-Type: multipart/form-data; charset=application/json` (technically invalid but accepted by curl/requests) hits the JSON branch, skips `await request.form()`, and is only validated via header. Since the header path is also accepted by the form branch (line 203-204), no practical bypass — but the parsing logic could be simplified to: "try header first, then form." Currently it's interleaved and fragile.

**LOW — `_CSRF_EXEMPT_PREFIXES = ()` is empty.** Good. The comment at line 67 documents the audit lesson and the trade-off. No finding.

### File severity tally
- HIGH: 1 (PATCH/PUT/DELETE soft-warn default)
- MED: 4 (reason-leak in `X-CSRF-Error`; exempt-paths regression risk on `/api/newsletter`; origin check non-prod; loose JSON content-type match)
- LOW: 2 (`startswith` prefix-match for skip paths; constant-time short-circuit on length mismatch)
- INFO: 0

---

## File: `idempotency.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/idempotency.py`
**Purpose:** Short-lived idempotency ledger for billing-critical writes; Redis-with-memory-fallback.

### Lens 1 — Token-generation entropy
N/A — idempotency keys are caller-supplied or derived from a fingerprint hash, not generated server-side. The hash truncation at line 139 (`hashlib.sha256(...).hexdigest()[:24]`) is fine: 24 hex chars = 96 bits, comfortably collision-resistant for a 10-second TTL window scoped to `(user_id, op)`.

### Lens 2 — Validator timing-attack resistance
N/A — no secret comparisons. The cache key is looked up directly in Redis/memory; there is no equality compare against a confidential value.

### Lens 3 — Rate-limit-bucket key derivation
This is effectively a rate-limit bucket (per `(user_id, op, key)`), so the lens applies.

**Findings:**
- **MED — Caller-supplied `Idempotency-Key` is trusted with weak normalisation.** Line 136: `trimmed = client_key.strip()[:128]`. Two clients can send `"abc"` and `" abc "` and collide deliberately. More importantly, **case-sensitivity is preserved** — `"ABC"` and `"abc"` are separate keys. The Stripe convention is case-sensitive opaque keys, so this is defensible, but the comment promises "two users using the same token don't collide" via the `user_id` namespace, which is true. Recommend documenting "case-sensitive, leading/trailing whitespace trimmed, 128 char cap" near the public API so client SDKs don't surprise themselves.
- **MED — Fallback fingerprint collisions are silent.** Line 139 truncates SHA-256 to 24 hex chars. The body says "no fingerprint → no idempotency" (degrade open) which is reasonable for billing webhooks but means an attacker who can force fingerprint generation to fail (omit the optional `fallback_fingerprint`) gets unrestricted retries. Caller responsibility — recommend at the call site a stronger pattern: derive `fallback_fingerprint` server-side from `request.body()` so the caller can't accidentally skip it.
- **LOW — Hash truncation locks in.** 24 hex chars is fine today; if this code is later reused for a longer TTL (e.g. multi-day "don't double-charge" use), drop the truncation. SHA-256 full digest fits trivially in a Redis key.
- **LOW — `_get_redis()` sticky-failure on first error** (line 99-118). Documented and intentional. A flaky Redis at startup permanently downgrades all idempotency to in-process for the lifetime of the process. With horizontal scaling, this turns idempotency into a per-worker bucket — and an attacker who can pin retries to different workers (via a load balancer) gets multi-worker amplification. This is documented in the module docstring ("Storage is Redis when available... and in-process otherwise — same pattern as the rest of `gateway/security/`"). Recommend at least a warning-level log per N minutes while in fallback mode so ops can spot the regression.

### Lens 4 — Header injection
N/A — `idempotency.py` does not set response headers. The result is JSON-serialised and returned to the caller via whatever response object the body produced.

### Other findings
- **MED — `_memory_get` returns whatever was cached, including a previous error.** Line 79-85. If `body()` raised but `_store_result` was reached (it isn't — `store_result` is in the success branch only), the cache returns a stale `None`. Verified safe in current code (line 183-185: cache only after `body()` returns), but if a future maintainer adds `try/except` around `body()` that stores the exception, a retry within TTL would return the cached error and skip a healthy retry. Add a comment marking "store on success only."
- **LOW — JSON-serialisation with `default=str`** (line 214). Datetime objects, decimals, etc. are stringified silently. For a billing return value that includes a `subscription_id` and `amount_cents` this is fine. For one that includes a `Decimal` price, two retries would round-trip through `str(Decimal('9.999'))` → `"9.999"` → string-not-decimal in the cached path. Caller is expected to know this; document at the public API.
- **LOW — `reset_for_tests()` is a public symbol** (line 233). Anyone who imports it from prod code can wipe the namespace. Convention dictates `_reset_for_tests` (underscore prefix). Not a vulnerability, but a footgun.

### File severity tally
- HIGH: 0
- MED: 3
- LOW: 3
- INFO: 0

---

## File: `input_hygiene.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/input_hygiene.py`
**Purpose:** `clean_text`, `clean_int`, `clean_float`, `clean_email`, `clean_handle`, `clean_page`, `clean_per_page` — first-line sanitation before DB / template / business logic.

### Lens 1 — Token-generation entropy
N/A.

### Lens 2 — Validator timing-attack resistance
**LOW — `_EMAIL_RE` is non-pathological but does linear regex backtracking.** Line 333: `^[^\s@]+@[^\s@]+\.[^\s@]+$`. No nested quantifiers, no catastrophic backtracking — fine. Same for `_HANDLE_RE` (line 359). No timing leak from regex.

**LOW — `_CTRL_CHAR_RE.search(raw)` (line 149) returns boolean.** Constant-time-ish (no secret in the input). Not a timing concern.

### Lens 3 — Rate-limit-bucket key derivation
N/A.

### Lens 4 — Header injection
N/A — file is pure data hygiene, no response surface.

### Other findings (this file's primary security value is **enforcement**, so findings here are about gaps in coverage):

- **HIGH — `clean_email` lowercases the entire address** (line 350). Documented as "lossy for rare case-sensitive local parts." This breaks RFC 5321 strictly speaking, but the comment's claim that "every consumer email provider treats local parts case-insensitively" is **wrong** for some enterprise / B2B accounts (and for Gmail's `+` aliasing the local part *is* case-insensitive but historically Yahoo, AOL, and a handful of older corporate mailservers honor case). Practical risk: a user signs up with `Alice@Corp.com` and the server stores `alice@corp.com`. If `corp.com` rejects mail to `alice@`, the welcome / password-reset / billing email silently bounces. Severity is HIGH not for security but for the auth-recovery flow it can deadlock — a user locked out with no working email is an account-takeover risk because the support flow is then the only recovery path. Recommend: lowercase **domain** part only; keep the local part case-preserved. (`local, _, domain = s.rpartition("@")` then `s = local + "@" + domain.lower()`.)

- **MED — `clean_text` strips invisible characters and then enforces `min_len`** (line 144-176). An attacker can submit `​​​...` (zero-width spaces) and the post-strip string is empty, so `required` fires and returns a 400 — that's fine. But an attacker can also submit `"abc​def"` — invisible removed → `"abcdef"` (6 chars). A username field expecting `min_len=4, max_len=8` accepts this, and now the same `"abcdef"` collides with a legitimate user named `abcdef`. This is good (prevents the "looks like a real username, isn't" homoglyph attack) — but it's also a **mutation** of user-submitted data. Document at every call site that the cleaned value, not the raw value, is what the user "really" submitted. Otherwise a handler that echoes the value back ("you registered as `abcdef`") confuses the user who thought they registered as `abc[ZWSP]def`.

- **MED — `_INVISIBLE_RE` strips bidi-isolate marks U+2066-U+2069 but `_CTRL_CHAR_RE` does NOT cover the bidi-isolate marks** (line 56, 61-71). The bidi isolate marks **ARE** stripped by `_INVISIBLE_RE` (line 144). Verified safe. But the documentation in `_INVISIBLE_RE` says "bidi isolate" — actually U+2066 (LRI), U+2067 (RLI), U+2068 (FSI), U+2069 (PDI). All stripped. **No finding** — this was just a verification pass; the file is correct.

- **MED — `clean_int` accepts `-0` as `0` and silently coerces**. Line 226: `re.fullmatch(r"-?\d+", s)` matches `"-0"`. `int("-0")` is `0`. Edge-case: a `lo=0` check passes, but downstream code that wanted to reject "negative-signed inputs" would not catch it. Practical impact near zero; flag for completeness.

- **LOW — `clean_text` truncates at `_HARD_MAX_LEN = 10_000`** for callers that don't supply `max_len` (line 162-166). Caller convention is to always supply `max_len`. The 10 KB cap is forgiving (a single comment field of 10 KB is large). Recommend lowering the default to e.g. 2 KB and forcing callers to opt into longer inputs explicitly.

- **LOW — `clean_handle` allows `_.-` in any position after char 0** (line 359). `_HANDLE_RE = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"`. Two issues:
  - Allows handles ending in `.` (e.g. `alice.`). If downstream code does `f"{handle}.json"` for file paths, `alice..json` is the result. Caller's job to escape, but a defensive `clean_handle` would reject trailing `.` / `-`.
  - Allows consecutive separators (`alice..bob`). Same caller-responsibility caveat.

- **LOW — `clean_float` permits leading/trailing whitespace** (line 261) — `s = raw.strip()` then `float(s)`. Python's `float()` already strips whitespace, but the explicit strip means `clean_float("  1.5  ")` returns 1.5 — which might be the intent for query-string floats. No finding, just observation.

- **INFO — `_ABSOLUTE_MAX_LEN = 1_000_000` (1 MB) is the global string cap.** Defended by the FastAPI `request_max_size` middleware higher up (server.py line 298 comment). Belt-and-braces; fine.

### File severity tally
- HIGH: 1 (email-lowercasing-deadlocks-account-recovery, account-takeover-adjacent)
- MED: 2 (mutation-vs-display semantics; `-0` edge case)
- LOW: 3
- INFO: 1

---

## File: `logger.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/logger.py`
**Purpose:** Dedicated security event logger — CSRF failures, rate-limit hits, auth events.

### Lens 1 — Token-generation entropy
N/A.

### Lens 2 — Validator timing-attack resistance
N/A — no secret comparisons.

### Lens 3 — Rate-limit-bucket key derivation
**LOW — `log_rate_limit_hit` takes `key` from caller** (line 84-98). The caller (rate_limiter.py line 180-184) passes the unredacted bucket key. The bucket key includes the IP. The IP is logged separately. The log line therefore contains the IP twice (once in `"key"`, once in `"ip"`). Minor disk-space waste / log-volume cost; not a security issue.

### Lens 4 — Header injection
Two surfaces:
1. **MED — User-Agent stored verbatim** (line 114): `request.headers.get("user-agent", "")`. No length cap, no newline filter. An attacker submits `User-Agent: legit\nfake_event_line` → JSON-line log gets `"user_agent": "legit\nfake_event_line"`. JSON encoding of `\n` is `\\n` (escaped) — so the disk format is safe. **But** if the log is grepped with line-oriented tools and then displayed in an admin viewer that interprets `\n` literally (e.g. the audit log viewer), the second half of the User-Agent can appear as a separate event. JSON serialisation in Python (`json.dumps`) escapes by default — verified safe at line 113 via `security_logger.warning(json.dumps({...}))`. **No injection at the file boundary.** Concern moves to the log viewer; out of scope here.

2. **MED — IP from `cf-connecting-ip` / `x-forwarded-for` trusted verbatim** (line 51-63). Same risk class as `audit.py`. An attacker on a path that bypasses Cloudflare (e.g. direct origin connection, if the deploy doesn't enforce CF) can spoof `CF-Connecting-IP: 127.0.0.1` and have the security log record `"ip": "127.0.0.1"`. Severity depends on origin-direct-access posture — if Cloudflare strips this header from inbound traffic at the edge and the origin is firewalled to only accept CF IPs, this is fine. **Cannot verify deployment posture from this file alone.** Flag for cross-file audit: confirm Cloudflare is configured to strip + reset `CF-Connecting-IP` and that the origin firewall denies non-CF source IPs.

### Other findings
- **MED — `configure_security_logging` is callable multiple times but only adds handlers if `not security_logger.handlers`** (line 35). A subprocess reload that creates a new logger instance (e.g. uvicorn `--reload`) but inherits the same module-level singleton will deduplicate correctly. Edge case: a test that monkey-patches the logger and then calls `configure_security_logging` will silently no-op. Behaviour is documented; not a finding.

- **LOW — Security log goes to a file (`logs/security.log`) and to stderr.** Stderr is captured by uvicorn's logs. Risk: in a multi-instance / container deployment without persistent disk, the on-disk `security.log` is ephemeral. The intent of having a "dedicated security log file" is partially defeated. Flag for ops docs.

- **LOW — `log_suspicious_activity` and `log_csrf_failure` emit at `.warning`, `.error` respectively** (line 73, 108, 127). Inconsistent: `csrf_failure` is warning, `suspicious_activity` is error, `auth_event` is warning. CSRF failure is plausibly worse than a generic auth warning. Pick a severity matrix and document it.

- **LOW — No PII redaction.** Lines 91, 122 log `email` directly into the security log. GDPR / DSAR posture depends on log retention — for an event log this is usually fine, but worth documenting that the security log is subject to the same delete-on-account-deletion sweep as `audit_log`.

### File severity tally
- HIGH: 0
- MED: 2
- LOW: 3
- INFO: 0

---

## File: `rate_limiter.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/rate_limiter.py`
**Purpose:** Sliding-window rate limiter with Redis backend; decorator + `auth_rate_limit` helper.

### Lens 1 — Token-generation entropy
N/A.

### Lens 2 — Validator timing-attack resistance
N/A — the bucket lookup is hash-table-by-key. No secret comparison.

### Lens 3 — Rate-limit-bucket key derivation
This is the lens of primary concern for this file.

- **HIGH — Default key includes only IP, not user.** Line 174: `key = f"{func.__module__}.{func.__name__}:{get_client_ip(request)}"`. For routes that DON'T provide a `key_func`, every request from an IP is bucketed together. Two consequences:
  1. **Carrier-grade NAT (mobile networks, university campuses, large corporate offices) share one egress IP across hundreds of users.** A single attacker on the same NAT can DoS legitimate users by burning the shared bucket. The `auth_rate_limit` 5-per-15-min limit (line 234) is plausibly DoS'd by a single attacker against an entire NATed cohort.
  2. **An IPv6 attacker has /64-or-larger control of a single physical interface.** The current limiter buckets by **full IPv6 address**, so an attacker rotates the source address across their /64 and gets unlimited attempts. This is the inverse of the NAT issue.

  Recommend:
  - For IPv6, bucket on the `/64` prefix, not the full address.
  - For authenticated routes, bucket on `user_id` first and IP second (chain-fall to IP only for pre-auth endpoints).
  - For `auth_rate_limit`, since by definition there's no user yet, also bucket on `(email, ip)` so a hopeless attacker against one account doesn't lock out others on the same NAT.

- **MED — `_check_redis` returns `True` on Redis error** (line 105-106). Documented: "Redis error — fall back to allowing the request." For a rate limiter, this is fail-open. An attacker who can DoS Redis (or who arrives at the moment Redis is restarting) gets unlimited requests. The in-memory limiter also exists (used when Redis isn't configured at all), so the safer fallback is "on Redis error, use the in-memory limiter for this worker." Code today bypasses both. Recommend: on Redis exception, fall through to `_check_memory(...)` rather than allowing the request.

- **MED — The Redis path checks `count > limit`, the memory path checks `count >= limit`** (line 82 vs 101). Off-by-one: the memory backend permits `limit - 1` requests; the Redis backend permits `limit` requests. The contract of `rate_limit(limit=5, ...)` is ambiguous between backends. Pick one and align. (Memory path is the more cautious of the two.)

- **MED — `_check_redis` uses `now` as both the score and the member** (line 94: `pipe.zadd(redis_key, {str(now): now})`). Two requests arriving in the same `time.time()` tick (well under a microsecond) hash to the same member and zadd dedupes. On bursty traffic the limiter undercounts. Recommend including a per-call nonce as the member (e.g. `f"{now}:{uuid.uuid4().hex[:8]}"`).

- **MED — Sticky Redis-init failure also lives here** (line 41-50). Same pattern as `idempotency.py`. If Redis is misconfigured at boot, the limiter silently falls back to per-worker in-memory buckets for the process lifetime, and the effective limit becomes `limit * num_workers`. Production almost certainly runs multiple workers behind uvicorn. Recommend a startup log line ("rate limiter using IN-MEMORY backend; multi-worker installs are per-worker limited") and a deploy-time health-check that fails the deploy if `REDIS_URL` is set but the client can't ping.

- **LOW — `auth_rate_limit` uses a single shared bucket across all auth routes** (line 234: `f"auth:{get_client_ip(r)}"`). Intentional and documented — prevents an attacker from rotating across `/login`, `/signup`, `/gate`, `/forgot-password` to multiply their budget. Good design. Pairs poorly with the HIGH NAT finding above though: legitimate users on the same NAT lock each other out.

- **LOW — `get_client_ip` order is `cf-connecting-ip` → `x-forwarded-for[0]` → `request.client.host`** (line 122-133). Documented and matches `logger.py`. Inconsistent with `audit.py` (which doesn't check `cf-connecting-ip`). See `audit.py` MED finding.

- **LOW — No "X-RateLimit-Reset" on success responses** (line 199-201). The 429 response sets `X-RateLimit-Reset` but the success path only sets `X-RateLimit-Limit` and `X-RateLimit-Remaining`. Some API clients need the reset timestamp to back off proactively. Hygiene only.

### Lens 4 — Header injection
- `X-RateLimit-*` and `Retry-After` (line 188-193) are integer-formatted (`str(int(time.time()) + retry_after)`, `str(limit)`, etc.). Server-controlled, no user input reaches a response header. **No injection.**
- `error_message` is a Python string baked into the decorator (line 140) — caller-controlled at decorator-attachment time, not request time. **No injection.**

### File severity tally
- HIGH: 1 (NAT-shared-bucket / IPv6 rotation)
- MED: 4 (Redis fail-open; off-by-one between backends; zadd dedupe at same tick; sticky in-memory fallback)
- LOW: 3
- INFO: 0

---

## File: `timezones.py`

**Path:** `/Users/shocakarel/Habbig/gateway/security/timezones.py`
**Purpose:** Resolve a user's preferred IANA timezone from header / cookie / Cloudflare-injected header.

### Lens 1 — Token-generation entropy
N/A.

### Lens 2 — Validator timing-attack resistance
**LOW — `_validate` performs unbounded `ZoneInfo(name)` lookups** (line 67-71). Each call hits the system tz database. There's no LRU around it — every request to a route that calls `resolve_timezone` does this. A request flood with random TZ values forces the tz database to scan its index each time. Mitigated by the `_MAX_LEN = 80` cap and the pre-zoneinfo charset reject (line 56), so an attacker can't supply path-traversal-y strings. Performance concern more than security.

**LOW — `any(c.isspace() or c in "\0<>\"'" for c in name)` (line 56)** rejects spaces, NUL, angle brackets, quotes. Good. Does NOT reject backslash, semicolon, carriage return (`\r`), line feed (`\n`), or backtick. `\r\n` could matter for header injection in `set_cookie` below if Starlette ever stops escaping cookie values. Currently `response.set_cookie(value=tz)` is defended by Starlette's cookie sanitiser, but defense-in-depth suggests adding `\n`, `\r`, `;` to the reject set.

### Lens 3 — Rate-limit-bucket key derivation
N/A.

### Lens 4 — Header injection
**MED — Trust of `X-Timezone`, `Cf-Timezone`, and `narve_tz` cookie** (line 93-104). Resolution order trusts the request-supplied value if it parses as a valid IANA name. An attacker sets `X-Timezone: Etc/GMT+12` and gets a TZ-shifted view of time-dependent data. Practical impact:
  - If "today's prediction market" snapshot is rendered with the user's TZ and an admin's `X-Timezone` can shift them across the midnight boundary, an audit-log search filtered by "today" misses an attacker's actions on the actual server day.
  - More concerning: cookie name `narve_tz` is **not HTTPOnly** (line 156: `httponly=False`). Documented as "by design — the JS reads it." But this means **any XSS** anywhere in the same eTLD+1 can write a hostile TZ. Combined with audit-log search by relative range (`range=24h|7d|30d|today`, audit.py line 364-379), an XSS that sets `narve_tz` could shift the audit view. **Not a direct compromise** but a way to hide tracks during an active attack.

**MED — `set_cookie` sets `secure=True` unconditionally** (line 157). If the deploy ever runs over plain HTTP (e.g. during a local dev container test against a Cloudflare bypass), the browser silently refuses the cookie and the TZ resolution falls back to UTC. Operational only; not a vulnerability.

**LOW — `set_cookie` does not set `domain`** (no `domain=` kwarg). Defaults to the request host. If the user is on `app.narve.ai` the cookie won't be readable from `narve.ai` and vice versa. May be intentional (per-host TZ); document.

**LOW — `Cf-Timezone` header is trusted on all requests** (line 96). If Cloudflare is not configured to strip this header from inbound traffic (it normally is — Cloudflare adds `CF-*` headers itself and strips client-supplied ones), an attacker on a CF-bypass path can inject. Same caveat as `logger.py` — confirm CF stripping at the edge.

**INFO — `format_epoch` is render-only**. Uses `strftime` with caller-supplied format string. If a caller ever passes `fmt` from user input, format-string vulnerabilities don't apply (strftime is type-restricted) but unbounded width specifiers could. No caller does this; flag for future maintainers.

### File severity tally
- HIGH: 0
- MED: 2
- LOW: 3
- INFO: 1

---

## Summary

### Severity counts (across all 8 files)
- **CRIT:** 0
- **HIGH:** 3
- **MED:** 19
- **LOW:** 19
- **INFO:** 3

### Top 5 findings (ranked by exploitability × blast radius)

1. **HIGH — `csrf.py:189-263` Phase-1 soft-warn defaults PATCH/PUT/DELETE to CSRF-pass-through.** Env var `CSRF_PATCH_DELETE_ENFORCE` defaults to `false`. Every PATCH/PUT/DELETE handler is currently CSRF-soft-warn, not enforced. A cross-site request with the right cookie can succeed on any cookie-authenticated mutation. The comment promises a Phase 2 flip but the default is still unsafe. Recommend flipping the default to `true` and confirming no client-side regressions remain.

2. **HIGH — `rate_limiter.py:174` Default rate-limit key buckets all requests from one IP regardless of user.** Two impacts: NAT (mobile networks, corporate egress) lets one attacker DoS legitimate users on the same egress IP, and IPv6 (where a single host owns a /64) lets an attacker rotate addresses for unlimited attempts. Especially bad for `auth_rate_limit` (line 223-236), which is the very limiter protecting login. Recommend: bucket on `/64` for IPv6, and chain `(user_id, ip)` for authenticated routes.

3. **HIGH — `input_hygiene.py:350` `clean_email` lowercases the local part of the address.** Some mail providers honour case in the local part. A user signing up as `Alice@example.com` may have their welcome / password-reset email silently bounce. This is an account-recovery deadlock, which becomes account-takeover-adjacent because the only recovery path becomes manual support — a path an attacker can social-engineer.

4. **MED — `csrf.py:243-254` CSRF-failure reason is echoed back in the `X-CSRF-Error` response header.** This is a side-channel that lets an attacker distinguish "victim has no session," "victim's session expired," "victim sent a stale token," etc. without seeing the auth cookie. Useful as a reconnaissance primitive in a phishing flow. Recommend echoing a single opaque `"invalid"` to clients and logging the granular reason server-side only.

5. **MED — `rate_limiter.py:90-106` `_check_redis` fails open on Redis exception, and `idempotency.py:99-118` Redis sticky-failure permanently drops to in-memory.** Both subsystems silently lose their cross-worker guarantee on transient Redis failure. The rate-limit one is worse (Redis error = unlimited requests for the duration of the outage); the idempotency one is more subtle (one bad init = per-worker buckets forever, so retries can double-charge with the right load-balancer behaviour). Recommend fail-closed (rate limit returns 429 on Redis error) or fall-through to in-memory rather than open.

### Cross-cutting observations

- **Three different `get_client_ip` implementations** across `audit.py`, `logger.py`, `rate_limiter.py`. `audit.py` is the outlier — it doesn't check `cf-connecting-ip`. Recommend a single shared helper.

- **Header-injection lens (#4) for `X-Frame-Options` / `CSP` / `HSTS` is not satisfiable inside `gateway/security/`** because those headers are emitted by `gateway/server.py:SecurityHeadersMiddleware` (lines 827-931). For this lens, audit headers actually set in this directory (`X-CSRF-Error`, `X-RateLimit-*`, `Retry-After`, `narve_tz` cookie) all derive from server-controlled values; no direct injection found. **Recommend a separate audit pass on `server.py:SecurityHeadersMiddleware` and `embed_routes.py` for the canonical CSP/HSTS/XFO review.**

- **No CSRF protection on websocket upgrades** (`_CSRF_SKIP_PREFIXES` excludes `/ws`). Websocket auth is typically session-cookie-based and the WebSocket handshake is a regular HTTP GET, which means a cross-origin page can establish an attacker-controlled WS connection if browsers don't enforce the Origin check. Out of scope for this audit (no WS code in `gateway/security/`), but flag for the WS file audit.

- **No mention of subscription-bypass concerns in any file under `gateway/security/`**, despite this being the primary monetisation gate. The security module focuses on infrastructure (CSRF, rate-limiting, audit) but not on the business-logic boundary. Recommend a `gateway/security/subscription_gate.py` or similar dedicated module so subscription-check logic isn't scattered across handlers.

---

*End of audit.*
