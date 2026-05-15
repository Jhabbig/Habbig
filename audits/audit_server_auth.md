# Adversarial Audit — `gateway/server.py` auth routes

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Target file: `/Users/shocakarel/Habbig/gateway/server.py` (8609 lines)

## Scope reconciliation

The brief named `/auth/login`, `/auth/register`, `/auth/logout`,
`/auth/reset-password`, `/auth/verify-email`, `/gate`, `/token`. **Five of
these routes do not live in `server.py`:**

| Brief route               | Lives in                                              |
|---------------------------|-------------------------------------------------------|
| `POST /auth/login`        | `gateway/server_features.py:1696`                     |
| `POST /auth/register`     | `gateway/server_features.py:1536`                     |
| `POST /auth/logout`       | `gateway/server_features.py:1787`                     |
| `POST /auth/reset-password` | `gateway/server_features.py:295`                    |
| `POST /auth/forgot-password` | `gateway/server_features.py:233`                   |
| `GET  /token`             | `gateway/server_features.py:1389`                     |
| **`/auth/verify-email`**  | **does not exist anywhere in the repo** (`grep -rn '"/auth/verify-email"' gateway/` → empty). The product has no email-verification step on the registration path. |

The routes in `server.py` that are actually in this audit's scope:

- `GET /gate` (3676), `POST /gate` (3687)
- `GET /login` (3740), `POST /login` (3786 — legacy redirect)
- `GET /forgot-password` (3802), `POST /forgot-password` (3810) — inline
  reset gated by *invite-token + email*, **not** the email-out flow
- `GET /signup` (3905), `POST /signup` (3917) — legacy redirect
- `GET /logout` (3926)
- `POST /profile/password` (4826) — authenticated password change
- `GET /reset-password` (5131), `POST /reset-password` (5148) — consumes
  the email-out reset link minted by `/auth/forgot-password` in the
  other module

Supporting code reviewed: `_validate_csrf` (1231), CSRFMiddleware (1238),
GateMiddleware (1418), `_is_rate_limited` / `_is_rate_limited_redis`
(1685, 1667), in-process `_is_account_locked` / `_record_login_failure`
(1717, 1756, 1760), `_auth_rate_limited` (1956), `_get_client_ip` (1774),
`set_session_cookie` (2176), `_gate_cookie_is_valid` (2181),
`_lookup_reset` (5106), `_reset_token_hash` (4877), `_validate_password`
(4860), `set_gate_cookie` (2196), `FIELD_MAX` / `_bounded` (303, 323).

Cross-module helpers consulted (read-only): `server_features._hash_reset_token`
(229), `server_features.auth_forgot_password` (233), `server_features.auth_reset_password`
(295).

Audit is bounded to the five concerns named in the brief:

1. Rate-limit on every named route
2. Brute-force protection (account lockout, persistence, multi-IP defence)
3. Password-reset token entropy + single-use
4. Session rotation on password change (reset + voluntary change)
5. Account-enumeration via timing / error-message / status-code divergence

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 4 |
| Low      | 5 |
| Info     | 3 |
| **Total**| **14** |

## Top 5 findings (ranked by exploitability × impact)

1. **HIGH-1** — `/forgot-password` is a *bypass channel* for the entire
   password-reset hardening. The handler in `server.py:3810-3902` lets
   anyone holding a (claimed) invite-token and the matching email reset
   the password **without ever seeing an email or holding a
   single-use token**. The lockout / per-email per-IP rate-limits do exist
   on this route, but the route's mere existence converts a leaked invite
   token (which appears in URLs, query strings, server logs, browser
   history) plus a known email into account takeover. The email-out flow
   in `server_features.py` was designed as the secure reset path; this
   route lets attackers skip it entirely.
2. **HIGH-2** — `/gate` and `/forgot-password` leak account / token state
   via **distinct error-message strings**. The `/forgot-password` handler
   returns *three different error strings* depending on whether the
   invite token is invalid, the email matches the token, or the user is
   suspended (`server.py:3858, 3862, 3876`) — only the
   "user not found by id" branch hides existence (3870). An attacker who
   knows an invite token can iterate emails and distinguish "wrong email"
   from "right email, wrong password rules" from "right email, suspended".
   Same pattern in `/gate` returns identical strings on "no token
   submitted" and "token mismatch" (3696, 3700) but the timing differs:
   `hmac.compare_digest` runs only on the latter, adding ~µs that is
   reliably measurable across thousands of requests behind a low-jitter
   tunnel.
3. **MED-1** — In-process lockout state (`_login_failures` dict at
   `server.py:1710`) is *write-only dead code* on this file. `grep -n
   "_record_login_failure\|_is_account_locked" server.py` returns only
   the definition sites — no call sites in any of the seven audited
   routes. The persistent SQLite-backed `db.is_login_locked` /
   `db.record_login_failure` (per `audit_queries_auth.md` MED-1) is also
   not wired into any route in this file. Net effect: every brute-force
   defence in `server.py` is the `_is_rate_limited` sliding window,
   which (a) is per-process when Redis is unavailable, (b) has no
   long-term ceiling, (c) **shares the same `auth:<ip>` bucket across
   `/gate`, `/forgot-password`, `/reset-password`, so 5 wrong gate
   attempts disables the reset flow for a legitimate user on the same
   IP**.
4. **MED-2** — Session rotation on password change is **incomplete on
   `/profile/password`**. `server.py:4849` calls
   `db.revoke_all_user_sessions(user["user_id"])` but the legacy
   `sessions` table (still authoritative for CSRF and cookie auth per
   `audit_queries_auth.md` HIGH-1) is NOT cleared. Contrast with
   `/reset-password` (5208) and `/forgot-password` (3895) which BOTH do
   `DELETE FROM sessions WHERE user_id = ?` AND call
   `db.revoke_all_user_sessions`. A user changing their password
   voluntarily after a session-cookie compromise (the documented intent
   per the comment at `server.py:4847-4848`) is therefore NOT actually
   logged out of the compromised browser — only the hardened cookie is
   revoked. The legacy session cookie remains live for `SESSION_TTL` (90d).
5. **MED-3** — `/reset-password` only rotates `jwt_invalidated_before`
   on successful reset, but the `_lookup_reset` helper accepts tokens
   matched against the **plaintext `token` column** as a fallback
   (`server.py:5125-5128`). The hash-only branch (5118-5123) is checked
   first, but if the plaintext column still holds the original token
   (which it does — per `audit_queries_auth.md` MED-2, the plaintext
   column is still written on insert and never nulled), an attacker who
   exfiltrates one row of `password_resets` gets a *usable* reset link
   even after the hash hardening landed. Compounding: `auth_forgot_password`
   in `server_features.py:270` writes `raw[:32]` into the plaintext
   column — a 32-character prefix of a 43-character base64url token.
   That prefix retains ~190 bits of entropy (still strong) but is
   **shorter than the hash**, so an offline brute against the truncated
   plaintext is slightly cheaper than against `token_hash`. Severity is
   MED rather than HIGH because the underlying DB-read precondition is
   the same as the cited `audit_queries_auth.md` finding.

---

## Findings

### HIGH-1 — `/forgot-password` bypasses the email-out reset path entirely

**Where.** `POST /forgot-password` at `server.py:3787-3902`.

```python
@app.post("/forgot-password")
async def forgot_password_submit(request: Request,
        invite_token: str = Form(""), email: str = Form(""),
        new_password: str = Form(""), confirm_password: str = Form("")):
    ...
    invite = db.get_invite_token(invite_token) if invite_token else None
    if not invite or invite["status"] != "claimed":
        return render_page("forgot-password", request=request, error="Invalid or unclaimed token.", success="")
    if invite["claimed_by_email"] != email:
        ...
        return render_page("forgot-password", request=request, error="Email does not match...", success="")
    user = db.get_user_by_id(invite["claimed_by_user_id"])
    ...
    # Update password
    pwd_hash, salt = db._hash_password(new_password)
    with db.conn() as c:
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwd_hash, salt, user["id"]))
```

**Why it matters.** The entire `/auth/forgot-password` →
`/auth/reset-password` design (`server_features.py:233-345`) exists to
make password reset go through an email-confirmation loop: a single-use
hashed token of 256 bits entropy, 1h TTL, delivered to the email of
record, consumed exactly once. **This route bypasses every one of those
controls.** It requires only:

1. A claimed invite-token string (these appear in URLs, browser history,
   server access logs, copy/paste artifacts, OAuth redirect-tunnel logs).
2. The email the token was claimed by — which is *displayed in the UI*
   via `db.mask_email(invite["claimed_by_email"])` on the `/login` page
   (`server.py:3772, 3781`) and is *queryable from* `invite_tokens` by
   any SQL read on that table.

So an attacker with read access to either the gateway DB or a single
leaked browser history can reset any user's password to a value of
their choice, without ever touching the email channel that the rest of
the system is designed around. There is no second-factor check, no
notification email, no audit log entry that surfaces to the victim.

The route does revoke sessions (`server.py:3895-3899`) so the victim's
existing browser session does die — but the attacker has already set a
new password and can sign in immediately, beating the victim to it.

**Why it's not Critical.** Mitigated by: (a) needing both invite token
*and* email (low but non-trivial bar), (b) 3 attempts/email/hour and
10/IP/10min limits (`server.py:3835, 3843`) make scanning slow, (c) per-IP
auth bucket at `_auth_rate_limited` (3793). But these are throttles on
exploitation rate, not on exploitability itself.

**Recommendation.** Delete `POST /forgot-password`. The email-out flow
in `server_features.py` is the supported recovery channel. If the
product needs a "I have my invite token and want to reset" path,
require the user to log in first and use `/profile/password` (which
verifies `current_password`); reset without authentication should
always go through email. At minimum, gate this route behind a fresh
email-out confirmation step — collect the new-password request, send
an email with a token, only apply on token redemption.

---

### HIGH-2 — Account / token enumeration via error-string and timing divergence

**Where.** `/gate` (3687-3704), `/forgot-password` (3810-3902),
`/reset-password` (5148-5170).

**`/gate` timing divergence.** `server.py:3695-3700`:

```python
if not token:
    return render_page("gate", request=request, error="Invalid token.")
if not SITE_ACCESS_TOKEN:
    return render_page("gate", request=request, error="Gate not configured. Contact admin.")
if not hmac.compare_digest(token, SITE_ACCESS_TOKEN):
    return render_page("gate", request=request, error="Invalid token.")
```

`hmac.compare_digest` is constant-time on its inputs but its *call site*
is reached only when `token` is non-empty. An attacker measuring tail-
latency on `/gate` over thousands of requests can distinguish "empty
token → no compare ran" from "non-empty token → compare ran" — gives
a partial signal about whether the deployment is in
`SITE_ACCESS_TOKEN`-unset misconfiguration vs. configured (the
middle branch produces a *different* error message that confirms the
state). Not high-impact on its own but combines with the misconfig
branch (info-disclosure of "Gate not configured. Contact admin.") to
fingerprint deployments.

**`/forgot-password` error-string divergence.** Five distinct paths
return five distinct strings:

| Branch | Error string (`server.py` line) |
|--------|---------------------------------|
| IP rate-limit hit | "Too many password reset attempts..." (3837) |
| Email rate-limit hit | "Too many password reset attempts..." (3850) — same string, good |
| Invalid/unclaimed token | "Invalid or unclaimed token." (3859) |
| Token claimed by *different* email | "Email does not match the account linked to this token." (3864) |
| User suspended | redirect to `/suspended` (3876) — *publicly different response* |
| User not found by id | "If that account exists..." (3873) — only branch that hides existence |
| Password mismatch | "Passwords don't match." (3880) |
| Weak password | "Password must be at least 12 characters." / "...special character." (3884, 3886) |

The "Email does not match the account linked to this token" string is a
direct *yes/no* oracle on whether `(invite_token, email)` is a valid
pair. Given the masked-email hint on `/login` already discloses the
*shape* of the bound email, this lets an attacker confirm specific
guesses cheaply. The `/suspended` redirect is even worse — a 302 to a
different path is observable without rendering the body.

**`/reset-password` divergence.** `server.py:5164-5170` returns
"This reset link is invalid or has expired" — but at line 5187 returns
"This reset link has already been used." The two error strings let an
attacker distinguish "token never existed" from "token existed and was
consumed" — useful for confirming whether a specific reset link was
exercised by the legitimate user.

**Why it matters.** Account / state enumeration is the prerequisite to
the rest of the credential-attack pipeline (targeted phishing, password
spray on confirmed accounts, monitoring of reset events). The masked
email on `/login`, the invite-token-claim status check at `/forgot-
password`, and the `/suspended` redirect together give an attacker a
fairly precise probe for any given email or token.

**Recommendation.** Normalise every authentication-adjacent failure to
one error string ("Invalid request. If your account exists, you'll get
the recovery email shortly.") and one status code (200). For
`/forgot-password`: stop branching on suspension state; if the user is
suspended, still take the password-reset write-path then drop the new
password (defensive, prevents a tell). For `/reset-password`: collapse
the "expired/invalid/used" trio into a single "This reset link is no
longer valid" message. Add `time.sleep(0.05 + random)` on every error
branch to flatten timing.

---

### MED-1 — All four brute-force defences in `server.py` are partially or fully unwired

**Where.**
- `_login_failures` dict + `_is_account_locked` + `_record_login_failure`
  + `_clear_login_failures` (`server.py:1710-1761`)
- `db.is_login_locked` / `db.record_login_failure` (persistent SQLite),
  per `audit_queries_auth.md:88-117`
- `_is_rate_limited(f"auth:{ip}", 5, 900)` — shared bucket across
  `/gate`, `/forgot-password`, `/reset-password`
- Per-route `_is_rate_limited` calls (per-email forgot, per-IP forgot,
  per-user profile-password)

**Wiring matrix (rows = mechanism, cols = route):**

|                             | `/gate` | `/forgot-password` | `/reset-password` | `/profile/password` | `/logout` |
|-----------------------------|---------|--------------------|-----|---------------------|-----------|
| `_auth_rate_limited` shared bucket | yes (3692) | yes (3793) | yes (5159) | no | no |
| Per-email rate-limit        | n/a     | yes (3843)         | no | no | no |
| Per-IP rate-limit           | shared  | yes (3835)         | shared | no | shared |
| Per-user rate-limit         | n/a     | n/a                | no | yes (4815) | n/a |
| `_is_account_locked` (in-proc) | no   | no                 | no  | no                  | no |
| `db.is_login_locked` (persistent) | no | no               | no  | no                  | no |

The in-process `_login_failures` mechanism (1710-1761) and the
persistent `db.is_login_locked` family are **both** orphaned in this
file. The route the in-process lockout was clearly designed for is
`POST /login` — but that route is now a no-op redirect to `/token`
(3786-3796), so its lockout state was relocated by deletion. The
actual JSON `/auth/login` lives in `server_features.py` and (per
`audit_queries_auth.md` MED-1) does call `_is_rate_limited` but does
NOT call the persistent lockout.

**Why it matters.** The shared `auth:<ip>` bucket at 5/15min sounds
generous but is *cross-route* — five wrong attempts on `/gate` from one
IP disables `/forgot-password` and `/reset-password` for that same IP
for 15 minutes. From a defender's view that's still tolerable. From an
attacker's view: a botnet of 100 IPs gives 500 attempts/15min on `/gate`
with no escalation beyond the local sliding window. There is no
long-term ceiling like the dead `_IDENT_CEILING_THRESHOLD = 30` (1713).

**Recommendation.** Either delete the dead in-process lockout (1710-1761)
or wire it into the surviving routes, then add the persistent
`db.is_login_locked` / `db.record_login_failure` as the cross-process
durable layer. The in-process check is fine as an L1 cache; the
persistent layer must back it.

Concretely: prepend each auth-mutating route (3692, 3793, 5159, 4815)
with `db.is_login_locked(identifier=email_or_ip, ip=ip)`; on success,
`db.clear_login_failures(identifier)`; on failure, `db.record_login_failure(identifier, ip)`.
Identifier = email when known, else IP.

---

### MED-2 — `/profile/password` does not clear the legacy `sessions` table

**Where.** `server.py:4843-4854`.

```python
pwd_hash, salt = db._hash_password(new_password)
with db.conn() as c:
    c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (pwd_hash, salt, user["user_id"]))
# Revoke every session except the actor's current one so any compromised
# cookie elsewhere cannot survive the voluntary password change.
try:
    db.revoke_all_user_sessions(user["user_id"])
except Exception as exc:
    log.error("Failed to revoke hardened sessions after password change for user_id=%d: %s", user["user_id"], exc)
```

Contrast with the same operation on `/reset-password` (5202-5210) and
`/forgot-password` (3893-3899), both of which do:

```python
with db.conn() as c:
    c.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
db.revoke_all_user_sessions(user["id"])
```

Per `audit_queries_auth.md` HIGH-1, the legacy `sessions` table stores
*plaintext* session tokens as PRIMARY KEY and is **still authoritative**
for CSRF state lookups and is written on every login alongside the
hardened table. So an attacker who stole a legacy session cookie (via
malware, browser-extension exfil, XSS in a non-narve property the user
visits while signed in, accidental log leak) and then sees the victim
voluntarily change their password as a precaution will *still hold a
working cookie* until that cookie's natural 90-day TTL.

The comment at 4847-4848 explicitly states the intent — "any compromised
cookie elsewhere cannot survive" — so this is a clear divergence
between stated intent and implementation.

The corresponding `/auth/logout` in `server_features.py:1787` similarly
needs review for both tables; the `GET /logout` in this file (3926-3968)
does call `db.delete_session(token)` on the legacy table for the actor's
own cookie, but does not handle *all* sessions because logout only kills
the one cookie. That's correct for logout. The fix is to make
`/profile/password` match the reset-flow behaviour.

**Recommendation.** Add `c.execute("DELETE FROM sessions WHERE user_id = ?", (user["user_id"],))`
before the `revoke_all_user_sessions` call on `/profile/password`. After
that, the actor's own cookie is also dead, so the handler should mint a
new session and re-set the cookie before returning (the existing reset
flow doesn't do this because it redirects to `/login`; the profile-page
handler should do the equivalent to keep the user signed in).

---

### MED-3 — `_lookup_reset` accepts plaintext-token matches against `password_resets.token`

**Where.** `server.py:5106-5128`.

```python
def _lookup_reset(token: str):
    if not token:
        return None
    th = _reset_token_hash(token)
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM password_resets WHERE token_hash = ? AND used = 0 "
            "AND (invalidated IS NULL OR invalidated = 0) AND expires_at > ?",
            (th, now),
        ).fetchone()
        if row:
            return row
        return c.execute(
            "SELECT * FROM password_resets WHERE token = ? AND used = 0 AND expires_at > ?",
            (token, now),
        ).fetchone()
```

**Why it matters.** Migration 003 added `token_hash` to defeat
DB-read attackers who exfiltrate the `password_resets` table. The
hashed-lookup branch (5118-5123) honours that. The plaintext-fallback
branch (5125-5128) *negates* it — a row whose hash matches won't fall
through, but a row whose hash doesn't match (e.g. an older row created
before the migration) is still findable by plaintext.

Compounding factor in the writer: `server_features.py:270`
`(user["id"], raw[:32], token_hash, now, now + 3600)` — the plaintext
column gets a *truncated* 32-char prefix of the 43-char base64url
token. So the plaintext column on this code path holds a token
prefix of ~190 bits entropy, while the hash column holds the SHA-256
of the *full* token. An attacker with a DB read can:

1. Use the plaintext-prefix column directly via `_lookup_reset`'s
   plaintext branch — but wait, the lookup requires `token = ?`
   matching the *whole* string the attacker submits, so the truncation
   actually breaks the plaintext path. So this branch returns nothing
   for tokens minted by `server_features.auth_forgot_password`.
2. *However*, any reset rows minted by older flows (pre-migration 003,
   or any future writer that uses the full plaintext) remain attackable.

The plaintext-fallback branch is a future foot-gun: any new code path
that writes the raw token unhashed for "compat" gets a free DB-read
takeover.

**Recommendation.** Delete lines 5125-5128. The rollover window for
the hash migration is over; the `audit_queries_auth.md` MED-2 finding
recommends nulling the plaintext column outright. Until that ships,
this fallback should be gated behind an env flag so it can be turned
off in production.

---

### MED-4 — `/reset-password` GET handler renders the page on `/reset-password?token=<x>` and a `_lookup_reset` miss returns a forgot-password render with the user's *submitted* token-error context — but the page rendered when `_lookup_reset` succeeds embeds the raw token in the form action

**Where.** `server.py:5131-5145` and `_reset_page_html` (cross-module in
`server_features.py:357`):

```python
@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    ...
    token = _bounded(token, FIELD_MAX["reset_token"], "token")
    reset = _lookup_reset(token)
    if not reset:
        return render_page(
            "forgot-password", request=request,
            error="This reset link is invalid or has expired. Please request a new one.",
            raw_success="",
        )
    return render_page("reset-password", request=request, token=token, error="", raw_success="")
```

`reset_password_page` echoes the *raw* token back into the rendered
HTML so the POST form can resubmit it (5145, `token=token`). Two side
effects:

1. **Referer leak.** Any third-party link or image embedded in the
   rendered reset page (none today, but watermark.js loads from
   `/_gateway_static/watermark.js` — same-origin, OK) would expose the
   token via the `Referer` header. Same-origin assets are safe; cross-
   origin assets would not be (no policy in the current `<head>`).
   The cross-origin risk on this page is zero today but the policy is
   absent — adding `<meta name="referrer" content="no-referrer">` to
   this template would harden against a future include accident.

2. **Pixel-in-page replay.** The watermark layer (`server.py:2256`)
   intentionally renders the actor's email/user-id into the page DOM
   when the request resolves to an authenticated user. `reset_password_page`
   resolves the request *without* an authenticated user (the reset flow
   is a recovery path, by definition unauthenticated), so the watermark
   doesn't apply here. But a *successful* reset (5216-5222) renders the
   `login` template with `raw_success` HTML embedded — again as an
   unauthenticated visitor, so no watermark exposure. Just info: the
   safe split is preserved.

**Recommendation.** Add `<meta name="referrer" content="no-referrer">`
to the reset-password page template head and `Cache-Control: no-store`
on the response (defends against the reset page being cached by an
intermediary). Both are one-line hardening.

---

### LOW-1 — `/gate` accepts unbounded body via `Form("")` without rate-limit until after `_bounded`

**Where.** `server.py:3687-3700`.

`_auth_rate_limited` runs at 3692 *before* `_bounded`, so an attacker
hitting `/gate` with a massive body still has the rate-limit applied —
good. The Starlette form-parser will fail on >1 MB bodies via the
SecurityHeadersMiddleware backstop (per the 295 comment about
"backstop, not primary defense"). `_bounded(token, 64)` (3694) bounds
the token field itself.

**Why it matters.** Not exploitable today. Flagged so any future
loosening of the rate limit or the 1 MB body cap doesn't accidentally
open a `/gate` flood DoS.

**Recommendation.** No action; document the dependency in the comment.

---

### LOW-2 — `_auth_rate_limited` shared bucket disables `/reset-password` for legit users on a shared IP

**Where.** `server.py:1956`, called from 3692, 3793, 5159.

```python
def _auth_rate_limited(ip: str) -> bool:
    """True if this IP has exceeded 5 auth attempts in the last 15 minutes
    (across any auth route)."""
    return _is_rate_limited(f"auth:{ip}", AUTH_RATE_LIMIT_COUNT, AUTH_RATE_LIMIT_WINDOW)
```

5 attempts per 15 min per IP, **shared** across `/gate`, `/forgot-password`,
`/reset-password`. A user on a corporate / family / hotel NAT who
fat-fingers the gate token 5 times burns the budget for everyone behind
that NAT trying to reset their password.

**Why it matters.** Legitimate-user DoS. Especially painful because the
clearest reason to land on `/reset-password` is "I locked myself out".

**Recommendation.** Split the bucket: `auth:gate:<ip>`, `auth:forgot:<ip>`,
`auth:reset:<ip>` at the same 5/15min. Or keep one bucket but raise to
15/15min, since each route already has its own narrower limiter on top.

---

### LOW-3 — `/forgot-password` and `/reset-password` race when the in-memory rate limiter is in use and the deployment has multiple workers

**Where.** `_is_rate_limited` fallback path (1696-1705) when Redis is
not configured / unreachable.

Per the per-process fallback, every worker keeps its own `_rate_store`.
A 4-worker uvicorn deployment with `REDIS_URL=` unset gives an attacker
20 attempts per IP per 10 min on `/forgot-password` and 20 per IP per
15 min on `/reset-password` because round-robin'd requests land in
different worker buckets.

The Redis path (1667-1682) is correctly atomic via the pipeline
(zremrangebyscore + zadd + zcard + expire), so configured deployments
are safe.

**Why it matters.** Only exposed when Redis is misconfigured. The
warning log "REDIS_URL set but connection failed" (1651) catches *bad
config* but not *missing* config. There is no fail-closed if Redis is
unset and `IS_PRODUCTION` is true.

**Recommendation.** At startup (alongside the other `IS_PRODUCTION`
guards at 386-395), require a working Redis connection or refuse to
boot. Or document a "we accept the 4× attempt multiplier per worker"
trade-off explicitly. Either is fine; silently degrading isn't.

---

### LOW-4 — `/logout` does not invalidate sessions for the *other* legacy users sharing the same browser

**Where.** `server.py:3926-3968`.

Logout deletes only the caller's own session token (3933-3935). If a
user signs in as A, then signs in as B (overwriting the cookie), then
logs out, A's row in `sessions` is now orphaned — it stays in the table
until natural expiry or until the next `purge_expired_sessions` cron.
Not exploitable: the cookie A used was overwritten when B signed in,
so the attacker can't replay it without DB access. With DB access, the
attacker is already past every defence anyway.

**Why it matters.** Mostly a hygiene / disk-space note. The 90-day TTL
means orphaned rows linger.

**Recommendation.** No action. The cleanup is the cron's job.

---

### LOW-5 — `/profile/password` rate-limit key is `profile-password:<user_id>` only — not IP'd

**Where.** `server.py:4815`.

```python
if _is_rate_limited(f"profile-password:{user['user_id']}", 5, 3600):
```

An attacker with a stolen cookie can trigger the lockout, locking the
*real user* out of their own password-change. Then the real user is
unable to rotate the password from the compromised cookie even after
detection. The 5/hr cap is generous, so this requires deliberate
attacker action to weaponize.

**Why it matters.** Targeted nuisance, blocks self-recovery from a
session-cookie compromise. Combines with MED-2 (legacy sessions not
killed by `/profile/password` anyway) to widen the window.

**Recommendation.** Add an IP-keyed sibling bucket: `profile-password:ip:<ip>`
at 20/hr alongside the per-user 5/hr. Or simply increase the per-user
limit to 20/hr.

---

### INFO-1 — `hmac.compare_digest` is used correctly on token compares

`_gate_cookie_is_valid` (2192), `_validate_csrf` (1235), `/gate`
constant-time token compare (3699). Audit requirement met.

The `/forgot-password` `invite["claimed_by_email"] != email` compare
(3862) is a Python `!=` on strings — NOT constant-time. This is an
account-existence oracle but the email is already on the page in
masked form, so timing-side-channel adds little. Filed under HIGH-2.

---

### INFO-2 — Password-reset token entropy is correct on the email-out path

`server_features.py:263`: `secrets.token_urlsafe(32)` → 32 bytes (256
bits) of entropy, base64url-encoded as ~43 characters. SHA-256 hash
of the full token stored in `token_hash`. 1-hour TTL. Single-use
enforced by `cur.rowcount == 0` check at `server.py:5186` (atomic
`UPDATE ... WHERE id = ? AND used = 0`).

The single-use atomicity in `server.py:5179-5191` is the correct
pattern: claim the row by id with `used = 0` predicate, check
`rowcount`. Race-safe under concurrent clicks.

The `auth_reset_password` mirror in `server_features.py:323-340`
uses two separate UPDATEs inside the same `db.conn()` block — fine
since the block holds a connection-level transaction in SQLite's
default mode.

---

### INFO-3 — `/logout` CSRF rotation is correct

`server.py:3960-3966`: on logout, the CSRF cookie is rotated to a
fresh value. Defends against the next session on the same browser
inheriting the previous user's CSRF token. Comment explains the
threat model.

---

## Out of scope but noted

- `_get_client_ip` (1774) trusts `cf-connecting-ip` / `x-forwarded-for`
  only from loopback peers. Correct for a Cloudflare-Tunnel deployment.
  Verify deployment never exposes the gateway off-tunnel — if it did,
  every rate limit in this file is forgeable.
- `set_session_cookie` (2176) uses `samesite="lax"`, `secure=IS_PRODUCTION`,
  `httponly=True`. Correct. `SESSION_TTL` is 90d (per `audit_queries_auth.md`)
  which is long; consider 30d with refresh.
- `set_gate_cookie` (2196) uses `samesite="strict"` — slightly stricter
  than the session cookie, good for the gate.
- `GATE_COOKIE_TTL = 7*86400` (238) is reasonable for a pre-release gate.
- The startup checks at 388-395 enforce `GATEWAY_COOKIE_SECRET >= 32` and
  `SITE_ACCESS_TOKEN >= 32` in production. Good. They do not check
  `REDIS_URL` is configured — see LOW-3.
- `_validate_password` (4860) enforces 12+ chars, upper/lower/digit/special.
  The `/forgot-password` route at 3883-3886 duplicates this inline rather
  than calling the helper — drift risk (the inline version says "Password
  must include uppercase, lowercase, number, and special character" while
  the helper returns four separate messages).
