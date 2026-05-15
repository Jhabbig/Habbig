# Adversarial Audit — `gateway/portfolio/kalshi.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M ctx)
Primary target: `/Users/shocakarel/Habbig/gateway/portfolio/kalshi.py` (253 LOC)
Supporting layers reviewed:
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py` (caller; `/api/portfolio/kalshi/connect`)
- `/Users/shocakarel/Habbig/gateway/migrations/062_portfolio_integration.py` (`kalshi_connections` schema)
- `/Users/shocakarel/Habbig/gateway/jobs/sync_portfolios.py` (background sync; 401 handling)
- `/Users/shocakarel/Habbig/gateway/security/idempotency.py` (`with_idempotency` semantics)
- `/Users/shocakarel/Habbig/gateway/server.py` (global rate limit; session middleware)
- `/Users/shocakarel/Habbig/gateway/.env.example` (`CREDENTIALS_ENCRYPTION_KEY` documentation)

---

## Requested attacker classes (scope)

1. **Token encryption-at-rest** — is the Kalshi bearer token stored at rest with
   confidentiality and integrity guarantees? Can a DB-read attacker (SQL dump,
   stolen `narve.db`, leaked backup) recover Kalshi sessions?
2. **Password-spray protection** — can an unauthenticated attacker, or an
   authenticated attacker rotating victim emails, abuse
   `/api/portfolio/kalshi/connect` to probe Kalshi credentials at scale?
3. **Session-cookie handling** — does `kalshi.py` interact with the narve
   session cookie correctly (it is the access-control gate for everything in
   the file)? Does the Kalshi bearer ever leak into the cookie surface?
4. **Kalshi API error masking** — does the response back to the client expose
   Kalshi-specific error text, response bodies, headers, or stack traces that
   would help an attacker enumerate accounts or fingerprint upstream state?

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 3 |
| Low      | 3 |

Top 3 (see findings below):
1. **H-1** — Plaintext Kalshi password sits in the narve process memory and
   may be captured by Sentry / structured logs via `kalshi.login`'s
   `httpx.HTTPError` traceback (request body redaction is not enforced here).
2. **H-2** — `connect_kalshi` has no per-user / per-IP rate limit on failed
   login attempts. The 10 s idempotency window keys on `email`, which an
   attacker can rotate freely; the global 600/min/IP cap is the only ceiling
   and is far above what is needed to probe a list.
3. **M-1** — Fernet key rotation has no story. `_fernet()` reads a single
   key from env; there is no `MultiFernet`, no version tag in
   `encrypted_token`, and no decrypt-then-rewrite path. A key compromise
   forces every user to re-connect, and a key rotation is impossible
   without that same forced re-connect.

---

## H-1 — Plaintext Kalshi password reachable via exception path

**Where:** `kalshi.py:75-87` (`login`), `routes.py:136-141` (`_do_connect`).

**Mechanic:**
- `login(email, password)` posts `{"email": email, "password": password}` to
  Kalshi over `httpx`.
- `httpx`'s `HTTPStatusError` carries `.request` with the serialised request
  body when raised by `raise_for_status()`. The caller catches the broad
  `Exception` and runs `log.info("kalshi login failed: %s", exc)`
  (`routes.py:140`).
- `httpx.HTTPStatusError.__str__` does *not* include the request body by
  default — but **Sentry's `httpx` integration and stdlib's `traceback`
  do** record the request object, and any logger that emits exception
  context (e.g. `log.exception`, or `log.info(..., exc_info=True)`) will
  serialise `exc.request.content` which contains the plaintext password
  as JSON bytes.
- Today the call uses `log.info("...: %s", exc)` (no `exc_info`), so the
  default stdlib formatter is safe. But the file has *no defensive
  scrubber* for the password field; any future change to
  `log.exception(...)`, any Sentry capture (`sentry_sdk.capture_exception`
  is global on this app), or any uncaught propagation past `_do_connect`
  will pipe the plaintext password to whichever sink is mounted.

**Concrete impact:** A bug or a Sentry replay on the connect endpoint
exfiltrates the user's Kalshi password to narve's log retention and to
Sentry's external storage. Kalshi reuses the same password for the trading
session — so an attacker with log read access gains full trading-API
control of the victim's Kalshi account, not just narve's view.

**Why High (not Critical):** Today's exact call site does not trip the
leak. The vulnerability is "one logging-line change away." But the file
documents (`kalshi.py:6`) that "we do NOT retain the password" — the
absence of a scrubber means that contract is enforced by convention, not
by code.

**Fix:**
1. In `login`, wrap the body in `httpx.Request` constructed without
   serialising the password into a logged request object, or catch
   `httpx.HTTPStatusError` inside `login` and re-raise a sanitised
   exception type (e.g. `KalshiLoginError("status=%d" % resp.status_code)`)
   that carries only the status code.
2. Add `__repr__` to that exception that elides any args.
3. Add a Sentry `before_send` filter for any frame whose module starts
   with `portfolio.kalshi` to drop `request.content` from breadcrumbs.

---

## H-2 — No per-user / per-IP rate limit on `/api/portfolio/kalshi/connect`

**Where:** `routes.py:110-173` (route handler), referenced from this file
because every call path that invokes `kalshi.login` runs through it.

**Mechanic:**
- The route's only abuse defence is `with_idempotency(..., ttl_seconds=10,
  fallback_fingerprint=email)`. The fingerprint hashes the *email*, not
  the email+password tuple, so any attacker that rotates the email skips
  the cache.
- The route has no `_is_rate_limited(...)` call. The codebase has the
  primitive (`server._is_rate_limited`, used by `affiliate_routes.py`,
  `security_routes.py`, `routes_sharing.py`), so the omission is a gap,
  not a missing capability.
- The global per-IP ceiling is `GLOBAL_RATE_LIMIT_PER_MIN = 600`
  (`server.py:1808`). At 600/min/IP an attacker can issue ~36k probes/hour
  per IP — well above the threshold needed for a targeted password spray
  against a known-victim email list.
- `_require_trading_addon` gates the route at 402, so an unauthenticated
  attacker cannot hit it directly. But:
  - Trading add-on is a paid product; a single attacker subscription
    grants 36k/hour of probes.
  - Multi-account: free signup + add-on rotation is cheaper than CAPTCHA
    farming.
  - Kalshi itself rate-limits, but the failure mode there is HTTP 429,
    which we catch as a generic exception and return "Kalshi login
    failed" with `_status: 401` — indistinguishable from a real bad
    password from the attacker's side, which is helpful to the attacker
    (no clear back-off signal).

**Concrete impact:** A paying attacker can use narve as a credential-probe
proxy against Kalshi, taking advantage of narve's outbound IP reputation.
Even if Kalshi blocks narve eventually, the cost is borne by every
*other* narve user (their connects break too), not the attacker.

**Why High (not Critical):** Attacker has to pay; Kalshi's own rate
limits cap the practical damage; and Kalshi accounts already require an
email which the attacker had to obtain elsewhere. Not Critical because
this is "narve as a spray reflector," not "narve gives away credentials."

**Fix:**
1. In `connect_kalshi` add:
   `if _is_rate_limited(f"kalshi_connect_user:{uid}", limit=5, window=300):`
   → return 429. Five attempts per five minutes per user is enough for an
   honest fat-fingered typist.
2. Add IP-keyed limit:
   `if _is_rate_limited(f"kalshi_connect_ip:{ip}", limit=20, window=3600):`
   → return 429. Catches the rotate-email-per-user pattern.
3. On Kalshi 429 specifically, return our own 429 with the upstream
   `Retry-After` echoed — gives honest clients back-off and removes the
   "401 means bad password" signal collision for attackers.

---

## M-1 — Fernet key rotation is impossible without re-connect

**Where:** `kalshi.py:36-54` (`_fernet`), 57-72 (`encrypt_token`,
`decrypt_token`).

**Mechanic:**
- `_fernet()` reads exactly one key, `CREDENTIALS_ENCRYPTION_KEY`, and
  constructs a single `Fernet` instance. No `MultiFernet`, no key list,
  no rotation envelope.
- `encrypt_token` returns Fernet-format bytes (which contain a key
  version byte, but only meaningful inside a single `Fernet` — not
  across primary-vs-secondary keys).
- There is no per-row `kek_version` column on `kalshi_connections`
  (confirmed against migration `062_portfolio_integration.py:63-77`).

**Concrete impact:**
- **Key compromise:** if `CREDENTIALS_ENCRYPTION_KEY` leaks, the only
  remediation is to delete every row in `kalshi_connections` and force
  every Trading add-on user to re-login. That blocks credential rotation
  hygiene; operators tend to defer "delete all live sessions" so the
  compromise window stretches.
- **Routine rotation:** key hygiene best-practice is to rotate the data
  key every N months. The code path forbids this — rotation = full
  forced re-connect.
- **No integrity issue:** Fernet does authenticate the ciphertext, so
  this is purely a key-management observation. Confidentiality at rest
  is intact today.

**Why Medium:** No data is exposed today; consequences are operational
(can't rotate quietly) and incident-response (slow remediation on
compromise).

**Fix:**
1. Switch `_fernet()` to `MultiFernet([Fernet(primary), Fernet(secondary)])`
   keyed on `CREDENTIALS_ENCRYPTION_KEY` (primary) and an optional
   `CREDENTIALS_ENCRYPTION_KEY_PREV` (secondary).
2. Add a background job that decrypts each row with `MultiFernet` and
   re-encrypts with `MultiFernet.rotate(ciphertext)` — Fernet supports
   this natively.
3. Document the rotation runbook in `/Users/shocakarel/Habbig/docs/`
   (out of scope for this file, but the operational contract belongs
   next to the code).

---

## M-2 — Decryption failure silently downgrades to "not connected"

**Where:** `kalshi.py:64-72` (`decrypt_token`), 182-189 (`sync_positions`).

**Mechanic:**
- `decrypt_token` swallows any `Exception` and returns `None` with a
  `log.warning`. The caller in `sync_positions` interprets `None` as
  `{"count": 0, "error": "decrypt_failed"}` and returns silently — no
  bump of `sync_error_count`, no surfacing to the user dashboard.
- The background job (`jobs/sync_portfolios.py:268-273`) reads
  `result.get("error")` and treats `decrypt_failed` the same as any
  generic error. There is no path that distinguishes "the ciphertext is
  unreadable" (key rotated, key revoked, DB tampering) from "Kalshi is
  down."

**Concrete impact:**
- A user whose row was *tampered with* (or whose key was changed
  unilaterally) sees their positions stop updating but gets no signal in
  the UI that re-connect is needed.
- More importantly: if an attacker can flip a single bit in
  `encrypted_token`, Fernet rejects the row, narve quietly drops to
  "not synced" — the attacker has DoS'd the victim's view without
  touching anything else.

**Why Medium:** Read-availability DoS, not a confidentiality breach.
Bounds: requires DB write access (which is a higher bar than DB read).

**Fix:**
1. Distinguish `InvalidToken` (Fernet's specific exception) from generic
   exceptions inside `decrypt_token`. Raise a typed
   `KalshiTokenUnreadable` upward.
2. In `sync_positions`, catch it explicitly and update `sync_error =
   'decrypt_failed'`, `sync_error_count = sync_error_count + 1` so the
   dashboard surfaces the disconnect-required signal.

---

## M-3 — Email is stored / lower-cased without normalisation parity to login

**Where:** `kalshi.py:118` (`email.lower()` on insert), 75-87 (`login`,
which passes the email through verbatim).

**Mechanic:**
- `upsert_connection` writes `email.lower()`.
- `login` sends the email to Kalshi *un-normalised* (whatever the user
  posted).
- Result: the row stored under `email = "shoc@x.com"` may correspond to
  a Kalshi account that authenticated as `"Shoc@x.com"`. Today Kalshi
  treats emails case-insensitively, so functionally this works — but
  narve's audit log / export view (`exports/generator.py:487-493`)
  emits the lower-cased form, which is a different string than the user
  typed.
- Bigger issue: a unicode-normalisation mismatch (NFC vs NFKC, IDN
  homographs) is possible. A user with `"ёx@example.com"` (Cyrillic ё)
  vs `"ëx@example.com"` (Latin ë) will collide in some normalisers and
  not others; narve does no IDN handling and Kalshi may or may not.

**Concrete impact:** Confusion in audit / GDPR export. Not a security
breach by itself. Could become one if any *other* part of the code uses
the stored email as a user-identity assertion (it does not today — the
join is on `user_id`).

**Why Medium:** Low likelihood, moderate hygiene cost in incident
forensics.

**Fix:** Either store the email exactly as the user posted it, or apply
the same `lower()` (or full `email.utils.parseaddr` normalisation) to
the value sent to Kalshi. Match both sides.

---

## L-1 — `frame-src` allows Kalshi in CSP but `kalshi.py` does no framing

**Where:** `server.py:917` (CSP), `kalshi.py` (no UI surface).

**Mechanic:** CSP allows `frame-src https://kalshi.com
https://*.kalshi.com`. `kalshi.py` makes JSON API calls only — there is
no embedded Kalshi UI, no OAuth popup, no widget. The CSP allowance is
unused for this code path.

**Concrete impact:** None today. Slightly wider CSP surface than
required. If a future XSS lets an attacker inject an `<iframe
src="https://kalshi.com/...">`, that frame can render — but Kalshi
itself has its own X-Frame headers, so the practical risk is near zero.

**Fix (optional):** Drop `https://kalshi.com https://*.kalshi.com` from
`frame-src` if no other code path embeds Kalshi UI. Confirm via grep
across `/Users/shocakarel/Habbig/gateway/static/`.

---

## L-2 — `member_id` and `token_expires_at` accepted from upstream verbatim

**Where:** `kalshi.py:90-120`, `routes.py:145-151`.

**Mechanic:** The route grabs `result.get("member_id")` and
`result.get("expires_at")` from the Kalshi login response and feeds them
into `upsert_connection` untyped and unbounded.
- `member_id` is stored as TEXT — a malicious upstream could ship a
  multi-MB string; SQLite has no column limit, so it would be written.
- `token_expires_at` is an INTEGER column — a non-numeric value would
  raise at the bind step (acceptable: fail loud), but a value far in the
  past or future would still be accepted.

**Concrete impact:**
- Storage bloat if upstream misbehaves. Bounded by Kalshi's own
  trustworthiness — they're the auth oracle, so if they're hostile we
  have larger problems.
- A `token_expires_at` of `0` (or far past) makes our refresh path think
  the token is already dead, which is fine — we re-login on next 401
  anyway.

**Why Low:** Upstream is trusted; impact is bounded.

**Fix:** Validate `member_id` is `<= 64` chars and matches a sane
charset; validate `token_expires_at` is within +/-10 years of now.
Bound checks, not security.

---

## L-3 — `_normalise` math: division by 100 is silent on bad cents

**Where:** `kalshi.py:161-180`.

**Mechanic:** `_cents_to_usd` divides by 100.0 and returns None on
`TypeError | ValueError`. Kalshi sometimes ships strings like
`"125.5"` (decimals already in cents) — the divisor still works, but
the meaning drifts. Not a security issue; could break Kelly math.

**Concrete impact:** Data-quality risk, not a security risk. Flagged
for completeness because it's on the path between Kalshi and the user's
displayed PnL.

**Fix:** Tighten the contract: only accept `int`/`float`, reject strings
with a `log.warning("kalshi sent string in numeric field: %s", v)` so
upstream contract drift surfaces.

---

## Things checked and OK

- **Bearer token isolation:** `fetch_positions(token)` takes the token
  as a parameter, decrypted only inside `sync_positions` and discarded
  when the function returns. The token does not propagate to logs,
  cookies, or other functions.
- **No SQL injection:** All `c.execute` calls are parameterised.
- **No bearer-token-in-URL:** Kalshi request uses the `Authorization`
  header, not a querystring (no risk of token in reverse-proxy logs).
- **No bearer token in cookies:** `kalshi.py` does not touch the
  response object and cannot accidentally `set_cookie` the Kalshi
  bearer. Narve's session cookie is set by `server.py:2176` and is
  untouched by this module.
- **`CREDENTIALS_ENCRYPTION_KEY` missing → 503 not silent fallback:**
  `upsert_connection` returns False, the route returns 503, the user
  cannot create a row that would have stored plaintext. Good.
- **Cryptography library is optional but enforced:** missing package →
  503, not silent downgrade. Good.
- **`raise_for_status()` on Kalshi response:** 401/403/4xx propagate
  rather than being swallowed.
- **Sync error masking:** `sync_positions` returns
  `"error": f"http_{status}"` (e.g. `"http_401"`) — sanitised,
  no Kalshi response body, no upstream error message. The full message
  *is* persisted in `sync_error` (DB column) for operator visibility,
  but never reaches the HTTP client.
- **Idempotency body fingerprint excludes password:**
  `fallback_fingerprint=email` — the code explicitly avoids hashing the
  password (`routes.py:130-131`). Good logging hygiene.
- **`/api/portfolio/kalshi/connect` is gated by `_require_trading_addon`:**
  free-tier and unauthenticated users get 401/402, not 200.

---

## Session-cookie scope clarification

`kalshi.py` does **not** create, read, mutate, or invalidate the narve
session cookie. The session-cookie surface lives entirely in
`server.py` (`set_session_cookie`, `clear_session_cookie`) and
`auth/cookies.py` (hardened variants). The Kalshi bearer token is
*never* placed into a `Set-Cookie` header anywhere in the file.

The only session-cookie interaction adjacent to this code is
upstream of it: `request.state.user` is populated by
`server.py:1897-1899`'s session-loader middleware, then
`_require_trading_addon` in `routes.py:52-75` reads that. Both
mechanisms were audited in `audit_server_auth.md` and
`audit_middleware.md`; no new defects observed at the kalshi
seam.

---

## Out of scope (and why)

- **Pre-release / prod deploy posture:** explicitly excluded by the
  brief. No live-host curls, no DNS, no Cloudflare config touched.
- **`/api/portfolio/kalshi/disconnect`:** lives in `routes.py:295+`,
  outside the file under audit. Briefly inspected — it deletes the row
  and clears positions; no decrypted bearer touches the response.
- **`positions.py`, `kelly.py`, `polymarket.py`:** not the audit target.
  Covered in `audit_portfolio_routes.md`.
- **`exports/generator.py`'s GDPR export of `kalshi_connections`:**
  emits encrypted token ciphertext (not plaintext) per
  `exports/generator.py:487-493`; covered in
  `audit_gdpr_export_completeness.md`.

---

## Suggested order of remediation

1. **H-1** (password leak via exception path) — single-file change to
   `kalshi.py:login`, low risk to ship.
2. **H-2** (rate limit) — single-file change to `routes.py`, requires a
   minor migration to `rate_limits` table if you want persistence
   across worker restarts (already exists, just need keys).
3. **M-1** (Fernet rotation) — design + small migration; not urgent
   but blocks key hygiene.
4. **M-2** (decrypt failure path) — small, do alongside M-1.
5. The rest (M-3, L-1, L-2, L-3) — opportunistic.
