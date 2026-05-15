# Audit: Chrome Extension Auth (`gateway/extension_routes.py`)

Scope: JWT secret, token rotation, allowlisted origins, max session length.
Out of scope: prerelease gate, business logic of the bundle payload.

Files touched:
- `gateway/extension_routes.py` (the whole SDK; 360 LOC)
- Cross-references: `gateway/security/csrf.py`, `gateway/server.py` (gate +
  middleware stack), `gateway/SECRETS.md`, `gateway/security/rate_limiter.py`.

---

## 1. JWT secret (`EXTENSION_JWT_SECRET`)

How it's loaded — `extension_routes.py:47-55`:

```python
def _jwt_secret() -> bytes:
    val = os.environ.get("EXTENSION_JWT_SECRET", "").strip()
    if val:
        return val.encode()
    fallback = os.environ.get("GATEWAY_COOKIE_SECRET") or "narve-extension-dev"
    return fallback.encode()
```

### Gaps

- **HIGH — silent fallback to `GATEWAY_COOKIE_SECRET`.** If
  `EXTENSION_JWT_SECRET` is unset in prod, signing falls back to the
  shared cookie secret. Two consequences:
  1. The same key now signs both browser session cookies and 7-day
     extension JWTs. A leak in either system compromises both. The
     blast radius of either secret leaking now covers the other.
  2. There is **no startup guard** for `EXTENSION_JWT_SECRET` in
     `server.py:394-399` — only `GATEWAY_COOKIE_SECRET` is hard-checked
     for prod + length. A prod deploy that forgets `EXTENSION_JWT_SECRET`
     boots cleanly, fallback kicks in, and ops never sees it.
- **HIGH — `"narve-extension-dev"` literal fallback.** If both env vars
  are empty, the code happily signs tokens with a public string from
  the repo. `IS_PRODUCTION` is never checked here. The
  `GATEWAY_COOKIE_SECRET` codepath at least crashes at startup; this
  one doesn't. Mirrors the `"dev-gate-secret"` issue already flagged in
  `ENV_DEFAULTS_AUDIT.md` row 5 — fix the same way (raise if both env
  vars are empty and `IS_PRODUCTION`).
- **MED — secret is read on every sign/verify.** `_jwt_secret()` is
  called inside `_sign_jwt` and `_verify_jwt`, so every request re-reads
  `os.environ`. Cheap, but it means a `setenv` mid-process during a
  rotation drill would silently change behaviour for in-flight tokens
  without restart — surprising for an audit.
- **LOW — no `kid` in JWT header.** Header is hard-coded
  `{"alg":"HS256","typ":"JWT"}`. Without a key id, rotating the secret
  invalidates **every** active extension session in lock-step — there's
  no two-key window to overlap old + new. See section 2.
- **LOW — hand-rolled HS256 instead of PyJWT.** The implementation
  itself looks correct (HMAC-SHA256, `hmac.compare_digest`,
  urlsafe-b64 with padding). But there is **no algorithm check on
  verify** — `_verify_jwt` ignores `header_b64` entirely. If a future
  refactor reads `alg` from the header to dispatch, the "alg=none"
  family of CVEs becomes plausible. Recommend pinning `alg` explicitly
  on verify (assert header decoded → `alg == "HS256"`) or switching to
  PyJWT with `algorithms=["HS256"]`.

---

## 2. Token rotation

What exists today:

- 7-day TTL hard-coded (`_JWT_TTL_SECONDS = 7 * 24 * 3600`,
  `extension_routes.py:42`).
- A fresh JWT is minted **every** time the user visits `/extension/auth`
  (`extension_routes.py:306-319`). The handshake page itself has no
  per-user rate limit — only the global rate limit middleware.
- The token is *opaque to the extension* but **stateless on the server**
  — no row, no `jti`, no `exp_at` table.

### Gaps

- **HIGH — no revocation path.** A leaked or compromised JWT is valid
  for the full 7 days. The user can change their narve password, log
  out everywhere, delete sessions in the admin shell, and the extension
  JWT keeps working. There is no `extension_tokens` table, no `jti`
  blocklist, no per-user `min_iat` ("reject tokens issued before X").
  This is the single biggest gap — minimum fix is a `min_iat` column on
  `users` and a check in `_verify_jwt`.
- **HIGH — no secret-rotation plan.** `SECRETS.md:23` says "Annually
  (short-lived tokens, but key rotation still matters)" for
  `EXTENSION_JWT_SECRET`, but the runbook section for
  `GATEWAY_COOKIE_SECRET` (115-130) has explicit rotation steps and
  there is **no equivalent section for `EXTENSION_JWT_SECRET`**.
  Combined with the missing `kid`, a rotation today is a hard cutover
  that signs out every extension user. Recommend (a) add a
  `_jwt_secret_previous()` lookup for `EXTENSION_JWT_SECRET_OLD`, (b)
  fall through to the old key on verify only (never sign with it), (c)
  document the rotation in `SECRETS.md`.
- **MED — `iat` is trusted but never validated.** `_verify_jwt`
  (lines 86-116) reads `exp` and `sub` but never inspects `iat`. If
  clock skew or a future bug produces a token with `iat` decades in
  the future, the token still passes as long as `exp` is in the
  future. Cheap fix: reject if `iat > now + small_skew`.
- **MED — no rate limit on the mint endpoint itself.**
  `/extension/auth` only requires `request.state.user` to be present.
  An XSS-foothold on any narve.ai page can hit `/extension/auth` in
  the background and exfiltrate a fresh JWT (the response body
  contains the raw token in a `<script>` block). The handshake page
  posts it via `window.postMessage(..., location.origin)` so same-origin
  JS can read it. Recommend (a) per-user rate limit on `/extension/auth`
  (e.g. 5/hour using the same `security/rate_limiter`), (b) return the
  JWT as an HttpOnly server-side relay rather than in HTML body if the
  externallyConnectable codepath is reachable.
- **LOW — no refresh path.** A token simply expires after 7 days and
  the user has to re-open `/extension/auth`. Fine, but undocumented —
  extension users will hit a silent failure on day 8.
- **LOW — `sub` cast.** `int(payload.get("sub"))` in `_verify_jwt`
  catches `TypeError/ValueError` but a `sub == 0` from a forged but
  valid-signed token (impossible without the secret, so theoretical)
  would map to user_id 0. Probably defensive overkill, but worth
  rejecting `uid <= 0` for symmetry with how `extension_auth` already
  coalesces `id`/`user_id` to `0` and never rejects.

---

## 3. Allowlisted origins

What exists today:

- The `/api/extension/market/{slug}` endpoint accepts a `Bearer` token
  in the `Authorization` header. No origin check.
  (`extension_routes.py:321-329`).
- The handshake page hard-codes
  `window.postMessage(..., location.origin)` — so the dev-fallback
  posts only to the current tab's own origin. Good.
- The Chrome `chrome.runtime.sendMessage(extId, ...)` codepath uses the
  Chrome-Web-Store ID at `NARVE_EXTENSION_ID` (line 119-127). The
  extension's manifest is presumably what controls
  `externallyConnectable.matches`, but **this server has no opinion**
  on which extension ID is allowed — `NARVE_EXTENSION_ID` is just
  metadata that goes into the response HTML; any signed-in user can
  open `/extension/auth` and the server is willing to push a token to
  whatever Chrome extension is loaded.
- No `CORSMiddleware` is registered in `server.py` (verified by
  grepping `CORSMiddleware|allow_origins`). The browser will enforce
  same-origin on the content-script `fetch()` only if the response
  lacks `Access-Control-Allow-Origin` — which currently it does. So
  cross-origin reads are blocked **by the browser**, but the server
  doesn't actively assert anything.

### Gaps

- **HIGH — no server-side check on which extension receives the JWT.**
  `NARVE_EXTENSION_ID` is purely an env var. There is no allow-list, no
  log line tying "this token was issued for ext_id X", no audit row.
  If multiple extension builds (stable + canary) coexist, the gateway
  can't distinguish them. Recommend (a) bake the extension ID into
  the JWT payload (`"aud": "ext:cspjbk…"`), (b) validate `aud` on
  verify, (c) keep the env var as the source of truth.
- **MED — handshake page leaks JWT into DOM.** `_auth_page_html` (lines
  130-183) writes `jwt.token` directly into a `<script>` literal:
  ```
  var jwt = {jwt_json};
  ```
  Any extension or browser bookmarklet running at narve.ai origin can
  read `document.documentElement.outerHTML` or hook
  `window.postMessage` and steal it. The
  `setTimeout(window.close, 1800)` is mitigation-by-vibe — not
  enforcement. The token is also embedded in `console`-readable text;
  any CSP `unsafe-inline` carve-out makes this worse.
- **MED — no CSP on `/extension/auth`.** The route returns
  `HTMLResponse(...)` with no per-route `Content-Security-Policy`
  header. Whether `SecurityHeadersMiddleware` (server.py:974) sets a
  default for this endpoint is worth confirming — if it does, ensure
  `script-src` is locked tight enough that no third-party JS can run
  on the handshake page. (Don't have eyes on that middleware in this
  audit; recommended follow-up.)
- **MED — no origin check on the bundle endpoint.** Any client with
  the Bearer token can call `/api/extension/market/...` from anywhere
  — curl, a competitor's scraper, a hijacked extension fork. The
  `60/min/JWT` rate limit caps damage but doesn't establish identity
  of caller. If the JWT had `aud=ext:<id>`, the server could log
  mismatches and detect token theft + replay outside the official
  extension. Without `aud`, theft is invisible.
- **LOW — `slug:path` allows trailing slashes and embedded chars.**
  The path converter `slug:path` accepts `/`, dots, etc. Used as
  `cache.get_or_set` key (`ext_bundle:{slug}`) and passed to
  `db.get_predictions_for_market`. Probably safe (SQL is
  parameterised in `db`), but the cache namespace is shared globally;
  an attacker who can hit the endpoint can prime the cache with
  arbitrary keys and (for 2 minutes) influence what other extension
  users see for that slug. Recommend a strict `[a-z0-9-]{1,128}`
  regex on the path converter or a `re.match` guard before
  cache-keying.

---

## 4. Max session length

- 7 days hard-coded (`_JWT_TTL_SECONDS`).
- Documented in the file's docstring ("Baked in — not configurable —
  so the extension can cache it without re-checking server policy",
  lines 40-42).

### Gaps

- **MED — 7 days is long given no revocation.** Sessions in
  `_HardenedSessionMiddleware` rotate every cookie cycle; extension
  JWTs do not. A 7-day window with no revocation is effectively a
  7-day pwn-the-bundle window. Recommend either (a) shorten to 24h
  with a documented refresh handshake the extension performs against
  the existing narve session cookie when it has access to the
  narve.ai origin, or (b) keep 7d but ship the revocation path
  described in section 2 first.
- **LOW — no idle timeout.** A token issued at T0 is valid until
  T0+7d even if the user hasn't used the extension since. Standard
  fix is `max(exp_at, last_used_at + idle_window)` — requires server
  state, but the same `min_iat`/`extension_tokens` table fixes both
  this and revocation.
- **LOW — not surfaced anywhere.** No env var, no admin shell row,
  no documentation that an operator can adjust the lifetime without
  a code change. Counterpart to "baked in" choice. If a security
  incident wants a 1-hour TTL across the fleet, today that requires a
  redeploy. Recommend `EXTENSION_JWT_TTL_HOURS` env var with a
  conservative default and a startup log line confirming the value.

---

## Summary — gaps to fix (ranked)

1. **HIGH — Add revocation.** `min_iat` column on `users` (or an
   `extension_tokens` table with a `jti` blocklist) and a check in
   `_verify_jwt`. Without this, every other improvement is theatre.
2. **HIGH — Harden secret loading.** Raise if `EXTENSION_JWT_SECRET`
   is unset in prod (don't silently fall back to
   `GATEWAY_COOKIE_SECRET`, never to the dev literal). Mirror the
   `server.py:394-399` startup check.
3. **HIGH — Allowlist the receiving extension.** Bake `aud=ext:<id>`
   into the JWT, validate on verify, log mismatches.
4. **MED — Rotation runbook.** Add `EXTENSION_JWT_SECRET_OLD`
   fallback on verify (never sign with it), document the two-key
   window in `SECRETS.md`.
5. **MED — Pin `alg` on verify.** Reject anything other than HS256;
   or migrate to PyJWT with `algorithms=["HS256"]`.
6. **MED — Rate-limit `/extension/auth`** (per-user, e.g. 5/hour) so
   an XSS foothold can't farm fresh tokens.
7. **MED — Move JWT out of the response body.** Use a server-relayed
   postMessage with a one-time nonce, or have the extension's content
   script call a JSON endpoint instead of parsing HTML.
8. **MED — Shorten TTL** to 24-48h once revocation is in place; until
   then, treat 7d as a known risk.
9. **LOW — Tighten the slug regex** on
   `/api/extension/market/{slug:path}`.
10. **LOW — Validate `iat`** and reject `uid <= 0`.

No critical bug blocks shipping the extension as it stands, but
1, 2, and 3 should land before any non-trivial rollout (anything
beyond closed-alpha invitees).
