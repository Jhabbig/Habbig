# Adversarial Audit — `gateway/portfolio/routes.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M ctx)
Primary target: `/Users/shocakarel/Habbig/gateway/portfolio/routes.py` (196 LOC)
Supporting layers reviewed:
- `/Users/shocakarel/Habbig/gateway/portfolio/positions.py`
- `/Users/shocakarel/Habbig/gateway/portfolio/polymarket.py`
- `/Users/shocakarel/Habbig/gateway/portfolio/kalshi.py`
- `/Users/shocakarel/Habbig/gateway/portfolio/kelly.py`
- `/Users/shocakarel/Habbig/gateway/portfolio/__init__.py`
- `/Users/shocakarel/Habbig/gateway/jobs/sync_portfolios.py` (pacing / 429 handling)
- `/Users/shocakarel/Habbig/gateway/security/idempotency.py`
- `/Users/shocakarel/Habbig/gateway/server.py` (CSRF, global rate limit, middleware mount)
- `/Users/shocakarel/Habbig/gateway/migrations/062_portfolio_integration.py`

Note on filename: the brief named `gateway/portfolio_routes.py`. No file of that
name exists; the portfolio HTTP surface lives at `gateway/portfolio/routes.py`,
which is the file audited. Confirmed via `find`/`server.py:8547` registration
list.

---

## Scope vs. requested attacker classes

The brief asked for IDOR, share-link forgery, sync rate-limit abuse, export
size limit, and PII in export. Three of those (share-link, CSV/JSON export,
export PII) are **not implemented in this file or anywhere in
`gateway/portfolio/`** — there is no share endpoint, no CSV/JSON export
endpoint, and no public-link surface for portfolios. Those sections below are
kept as explicit "Not applicable / negative findings" so the audit answers
the brief rather than skipping it.

`gateway/exports/generator.py` is an account-data export bundler that DOES
read `polymarket_connections` / `kalshi_connections` / `user_positions` —
that's a separate surface and out of scope here, but is flagged as a
follow-up (see INFO-3).

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 4 |
| Low      | 3 |
| Info     | 3 |
| **Total**| **12** |

## Top 3 findings (ranked by exploitability × impact)

1. **HIGH-1** — `Trading add-on` access gate is **not enforced** in
   `portfolio/routes.py` despite the module docstring claiming it is. Every
   authenticated user can connect a Polymarket wallet, submit a Kalshi
   password, read positions, and burn the Kelly endpoint regardless of
   subscription state. (`routes.py:3-6` claims a gate; no `has_trading_addon`
   or equivalent check appears in any handler.)
2. **HIGH-2** — `POST /api/portfolio/kalshi/connect` accepts unrestricted
   `email` + `password` and proxies straight to upstream Kalshi `/login`
   with only a 10-second idempotency dedupe and the 600/min global per-IP
   cap. An authenticated attacker can credential-stuff Kalshi accounts at
   ~600 distinct attempts/minute per IP under your origin (and per-account
   under a single user_id), turning narve into a Kalshi password-spray
   amplifier. No per-user-account lockout, no exponential backoff, no
   honeypot-email throttle.
3. **MED-1** — `POST /api/portfolio/polymarket/connect` accepts any
   well-formed `0x…` address with **zero ownership proof** (no SIWE, no
   signature). An attacker can claim a celebrity / whale wallet and then
   read those positions back via `GET /api/portfolio/positions`, treating
   narve as an on-chain stalking lens with a clean per-user UI. Polymarket
   data IS public, but narve normalises + persists it under the attacker's
   user row and surfaces P/L deltas tied to the wallet, which is more
   convenient than the public CLOB endpoint. (See `polymarket.py:87-103`.)

---

## Findings

### HIGH-1 — Trading add-on gate is undocumented-only, not enforced

**Location:** `portfolio/routes.py:3-6` (docstring) vs. every route in the
file (no gate check). Confirmed by grepping the whole `portfolio/` package
for `has_trading_addon`, `trading_addon_active`, `require_addon`,
`require_trading` — zero hits.

**What:** The module docstring says:

> All routes here assume request.state.user is already populated by the
> existing session middleware. The access gate is "Trading add-on" — the
> same check applied to the existing /api/markets/connections endpoints.

`/api/markets/connections` (defined in `market_routes.py`) does in fact
gate on `db.has_trading_addon` / `db.get_trading_addon_status`. The new
`/api/portfolio/*` routes registered by `portfolio.routes.register(app)`
do not. `_require_user()` only checks that a session exists; there is no
follow-up check for the add-on flag, the subscription state, or the
trial window.

**Concrete impact:**
- A free-tier user can connect Polymarket + Kalshi, sync positions, run
  the Kelly calculator, and persist a bankroll — bypassing whatever
  pricing model the Trading add-on represents.
- `kalshi_connections` rows accumulate against `users` that never paid;
  the per-user UNIQUE index means a future "remove on cancel" job has
  to deal with rows that should never have been created.
- The Kelly endpoint at `routes.py:143-174` is an unmetered float-math
  surface that doesn't even need a Kalshi/Polymarket connection to
  invoke — `bankroll_override` lets the caller pass any number and get
  a Kelly table back. This is the cheapest abuse vector and the one
  most likely to be hit by reconnaissance bots.

**Why High (not Critical):** The leaked feature is gated behind sign-up
but signup is free, so the gate failure is universal. Not Critical
because there is no privilege escalation against another user's data
and no money movement.

**Fix:**
- Add a single helper `_require_trading_addon(user)` at the top of
  `routes.py` (mirroring `_require_user`) that calls
  `db.has_trading_addon(user["id"])` and 403s otherwise.
- Apply to all six routes (`connect_polymarket`, `connect_kalshi`,
  `api_portfolio_summary`, `api_portfolio_positions`,
  `api_kelly_calculate`, `api_set_bankroll`). The Kelly endpoint is the
  one easiest to forget — auditing the route file shows it has no
  connection prereq at all, so a separate check is required.

---

### HIGH-2 — Kalshi connect endpoint amplifies password-spray attacks

**Location:** `portfolio/routes.py:60-123` (`connect_kalshi`),
`portfolio/kalshi.py:75-87` (`login`).

**What:** The handler accepts an arbitrary `email`+`password` pair, calls
`kalshi.login(email, password)` which proxies straight to
`https://trading-api.kalshi.com/trade-api/v2/login`, and returns the
narve-side success/failure based on Kalshi's response. The only abuse
defences in front of this endpoint are:

1. CSRF (server-wide, enforced — confirmed `/api/portfolio/*` is NOT in
   `_CSRF_EXEMPT_POSTS` or `_CSRF_EXEMPT_POST_PREFIXES` at
   `server.py:1105-1154`).
2. `GLOBAL_RATE_LIMIT_PER_MIN=600` per IP (server.py:1765).
3. The 10-second idempotency window keyed on
   `(user_id, "kalshi_connect", email)` — but this **dedupes a retry of
   the same email by the same user**; it does not slow a sequence of
   distinct emails.

So an authenticated narve user can send up to 600 distinct
(email, password) pairs per minute per IP through narve to Kalshi's login
endpoint. From Kalshi's side, the traffic appears to come from narve's
egress IP, not the attacker's — narve becomes a proxy/amplifier for
credential-spray and password-spray.

**Compounding:** `kalshi.login` raises `httpx.HTTPStatusError` on a 401
from Kalshi; the route catches `Exception` at `routes.py:89` and returns
`401 "Kalshi login failed"`. The narve response time correlates with
upstream Kalshi response time and any block/lockout responses are
faithfully returned to the caller, giving the attacker a perfectly
serviceable oracle. No client backoff, no captcha at threshold, no
account-lockout-mirroring.

**Attack:**
1. Sign up for one narve account (free).
2. POST `/api/portfolio/kalshi/connect` with rotating
   `(email, password)` from a breach list.
3. Use distinct `Idempotency-Key` headers per attempt to avoid the
   10s dedupe (or just rotate emails — same effect).
4. At 600/min per egress IP, this is faster than scripting against
   Kalshi directly because Kalshi may have stricter per-source limits
   that don't apply when the source is narve.

**Why High (not Critical):** The attacker has to authenticate to narve
first, providing audit trail. But that's one free signup.

**Fix:**
- Add `_is_rate_limited(f"kalshi-connect:{user_id}", limit=5, window=900)`
  to mirror `_auth_rate_limited` (the login throttle at
  `server.py:1913`). Five attempts per 15 minutes covers a legitimate
  user re-typing a password after typos; anything beyond that is abuse.
- Add `_is_rate_limited(f"kalshi-connect-email:{sha256(email)}", limit=3, window=3600)`
  to cap any single target email at 3 attempts/hour across narve users.
- Consider a CAPTCHA after the 2nd failed attempt within 60 s.
- Optional: don't reflect the upstream Kalshi error message; return a
  constant "Kalshi login failed" with constant timing to defeat the
  oracle. (Current code reflects the upstream status code in
  `sync_error`; same concern.)

---

### MED-1 — Polymarket wallet connect has no ownership proof

**Location:** `portfolio/routes.py:43-57` (`connect_polymarket`),
`portfolio/polymarket.py:83-103`.

**What:** The handler accepts any 0x-prefixed 40-hex-char string as a
wallet address and stores it. There is no signed-message ("SIWE")
verification step like the one referenced in
`market_routes.py:80-90` (which the broader codebase clearly knows about
— it's used in the legacy `/api/markets/connections` flow). An
authenticated narve user can claim any wallet on Polygon as theirs and
then read its positions back via `GET /api/portfolio/positions`.

**Concrete impact:**
- Stalking / aggregation: bind a known whale wallet, then watch P/L
  evolution in narve's UI — basically a free "watch this wallet"
  feature that narve never advertised, and one that pre-fills the
  victim's data inside a stranger's account.
- Phishing pretext: an attacker can show a victim "look at all the
  positions I synced from your wallet" to claim insider knowledge.
- Mis-attribution: any narve feature that downstream sums "total user
  AUM" or "biggest positions" sees the attacker as a whale.
- Bot-driven address harvesting: an attacker can iterate Polymarket
  leaderboard wallets and have narve pre-cache + persist their
  position history (the sync job at
  `jobs/sync_portfolios.py:144` fetches every connected wallet on
  schedule).

**Why Medium (not High):** Polymarket position data is already public
on-chain; narve isn't leaking anything that isn't accessible via
`clob.polymarket.com/positions?address=…`. The harm is convenience +
mis-attribution within narve, not data exfiltration from a private
source. Bumps to High if narve ever uses connected-wallet stats in
public surfaces (leaderboards, social proof).

**Fix:**
- Require a SIWE signature: have the client sign
  `"Verify wallet ownership for narve.ai portfolio sync.\n<nonce>"`
  with the wallet's key and submit `signature` alongside
  `wallet_address`. The legacy SIWE flow in
  `market_routes.py:85` and the test scaffolding at
  `tests/test_polymarket_siwe.py` are the templates to follow.
- Until that ships, at least add a UI disclaimer that the wallet is
  unverified, and bind the connection record to "claimed but
  unverified" so downstream features (leaderboards, aggregations)
  exclude it.

---

### MED-2 — `connect_polymarket` accepts duplicate wallets across users

**Location:** `portfolio/polymarket.py:87-103`,
`migrations/062_portfolio_integration.py:43-58`.

**What:** `polymarket_connections.user_id` is `UNIQUE`, but
`wallet_address` is **not**. Combined with MED-1 (no ownership proof),
this means multiple narve users can independently claim the same
wallet. The position-sync job runs for each, so the same wallet's
positions get written into N user rows of `user_positions` (one per
claimant). Each user's `position_value_usd` reflects the real wallet —
no overlap detection, no aggregation, no per-wallet single-owner
enforcement.

**Concrete impact:**
- Resource cost: one wallet x N claimants = N×Polymarket-API calls per
  sync cycle. With MED-1, an attacker can fan a single popular wallet
  out to thousands of bot accounts and amplify upstream CLOB traffic
  proportionally.
- The pacing in `sync_portfolios.py:197-198`
  (`_REQUEST_INTERVAL = 0.2s`, ~5 req/s) caps total request rate, but
  the share of those requests "wasted" on duplicated wallets grows
  linearly with attackers.

**Why Medium:** Bounded by upstream pacing token-bucket, and Polymarket
absorbs the cost not narve. Bumps to High if narve ever bills
per-wallet-sync at COGS.

**Fix:**
- Add `UNIQUE (wallet_address)` to `polymarket_connections`, OR
- Add a `verified_at` column and only sync when set (after SIWE), OR
- Dedup at the sync-job level: group connections by `wallet_address`,
  fetch once, fan the normalised positions out to every claimant row.
  This costs one query and eliminates the amplification.

---

### MED-3 — No per-user rate limit on `/api/portfolio/polymarket/connect`

**Location:** `portfolio/routes.py:43-57`.

**What:** The Polymarket connect endpoint is an upsert, so spamming it
doesn't create rows — but each call still:
- runs a regex match (cheap),
- runs an INSERT/ON CONFLICT against `polymarket_connections`,
- resets `sync_error` and `sync_error_count` to NULL/0.

The reset behaviour is the interesting bit. The sync job at
`jobs/sync_portfolios.py:179` uses
`if (row["sync_error_count"] or 0) >= _MAX_ERROR_STREAK: skipped_streak`
to skip dead connections. An attacker who already has narve syncing a
wallet that's started erroring (rate-limited upstream, banned address,
etc.) can repeatedly POST `/api/portfolio/polymarket/connect` with the
same address to clear `sync_error_count` and force the sync job to
keep hammering the failing endpoint — re-attaching dead wallets to
the active sync queue forever.

**Why Medium:** Bounded by the global 600/min per-IP cap; abuse cost is
real (forces upstream traffic) but limited.

**Fix:**
- Add `_is_rate_limited(f"poly-connect:{user_id}", limit=10, window=3600)`.
- Only reset `sync_error` / `sync_error_count` when the wallet address
  actually changes — current ON CONFLICT clobbers them on every
  identical re-submit (see `polymarket.py:97-102`).

---

### MED-4 — Kalshi password reaches log paths even with hygiene comment

**Location:** `portfolio/routes.py:74-91`, `portfolio/kalshi.py:75-87`.

**What:** The connect handler's docstring at line 81 says

> We do NOT include the password in the fingerprint (logging hygiene).

The fingerprint is fine (only email). But:
1. `kalshi.login(email, password)` constructs an `httpx.AsyncClient`
   that, with the default httpx logger level, can log full request
   bodies including JSON. If `logging.getLogger("httpx").setLevel(DEBUG)`
   is ever set (e.g. by a future debug flag, or by `LOG_LEVEL=DEBUG` in
   an env), the cleartext password lands in stdout.
2. The exception catch at `routes.py:89` does
   `log.info("kalshi login failed: %s", exc)` — `httpx.HTTPStatusError`
   `repr()` includes the request URL and method; it does NOT include
   the body by default, so this is OK today, but a future move to
   `log.exception` or `repr(exc.request)` would change that.
3. Pydantic/Starlette validation errors that fire BEFORE
   `request.json()` returns (malformed JSON, oversize body) currently
   never see the password — but any future middleware that logs raw
   request bodies (an audit middleware, for example) would.

**Why Medium:** No leak today, but the password sits in narve's process
memory and inside an `httpx` request object across two `await`
boundaries; one debug-flag flip is enough. The codebase has a precedent
for explicit "do not log this" decorators (`security/redact.py`
patterns elsewhere) that aren't applied here.

**Fix:**
- Use a dedicated short-lived `httpx.AsyncClient(transport=…, event_hooks=…)`
  with an event hook that strips `password` from any logged request body.
- Explicitly `logging.getLogger("httpx").setLevel(logging.WARNING)` at
  app startup (server.py module init) — this guards against accidental
  DEBUG propagation.
- Add a regression test that runs the connect path under
  `LOG_LEVEL=DEBUG` and asserts the password byte sequence does not
  appear in captured logs.

---

### LOW-1 — `_user_id` swallows non-int ids silently

**Location:** `portfolio/routes.py:36-37`.

```python
def _user_id(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)
```

**What:** If both `id` and `user_id` are missing/falsy, this returns 0.
Every downstream query (`SELECT * FROM user_positions WHERE user_id =
?`, etc.) will then run against `user_id=0`. If a row with `user_id=0`
ever exists (admin seed, test fixture, NULL coercion), it leaks across
users.

**Concrete impact:** Low today — current session middleware always
sets `user["id"]`, and no `users(id=0)` row exists in the schema. But
the silent zero-fallback is a classic IDOR-by-misconfiguration
landmine.

**Fix:** Raise instead of falling back:

```python
def _user_id(user: dict) -> int:
    uid = user.get("id") or user.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid session")
    return int(uid)
```

---

### LOW-2 — Bankroll override is unbounded inside `/api/kelly/calculate`

**Location:** `portfolio/routes.py:161-169`.

**What:** The `bankroll_usd` param of `/api/kelly/bankroll` is bounded
to `[0, 10_000_000]` at line 190. But the same value passed as
`bankroll_usd` to `/api/kelly/calculate` (`routes.py:161`) is parsed
as a float with no upper bound. A caller can pass `1e308` and the
Kelly math at `kelly.py:90-98` will return `stake_usd = 1e308 *
fraction`, which JSON-encodes fine (`Infinity` is escaped as a string
in Starlette's default JSONResponse, but `1e308` itself round-trips).

**Concrete impact:** No money movement; this is a display-only
calculator. Worst case is a frontend bug rendering an ungodly number.
But the asymmetry between the two endpoints is a smell — the persisted
value is capped, the on-the-fly compute is not.

**Fix:** Apply the same `0 ≤ bankroll ≤ 10_000_000` check to the
override path in `/api/kelly/calculate`.

---

### LOW-3 — `our_probability` / `market_price` accept out-of-range values

**Location:** `portfolio/routes.py:144-157`, `portfolio/kelly.py:43-46`.

**What:** The route does `float(...)` and forwards to
`kelly.sizing_table`. The Kelly math correctly returns 0 for probs
outside `(0,1)` — good — but the route doesn't normalise inputs. A
client passing `-0.5` or `5.0` gets a 200 response with all-zero
sizes. That's not exploitable, but it's a violation of the route's
contract that a UI form might encode incorrectly. The route should
422 on inputs outside the conventional probability range.

**Fix:** Reject inputs outside `[0, 1]` (or `(0, 1)` strictly) with a
422 instead of returning a silently-zeroed payload.

---

### INFO-1 — Polymarket address normalisation drops case data

**Location:** `portfolio/routes.py:50, 57`, `portfolio/polymarket.py:91`.

**What:** Wallet addresses are stored lowercased. EIP-55 mixed-case
checksum addresses lose their checksum on insert. Functionally
identical at the Polygon RPC level (it's case-insensitive), but
displaying back to the user loses the checksum digits that humans
spot-check. No security impact; UX wart.

---

### INFO-2 — Idempotency degrades open if no Idempotency-Key & no fingerprint

**Location:** `security/idempotency.py:171-177`,
`portfolio/routes.py:114-121`.

**What:** `with_idempotency` returns
`return await body()` (no caching) when neither `client_key` nor
`fallback_fingerprint` resolves. For the Kalshi connect call, the
fallback fingerprint is `email`, so this never trips in practice
(emails are always submitted). But the failure mode of the helper
documentation ("feature degrades open so missing headers don't lock
users out") is worth knowing — an attacker who can elide both
headers + body gets no idempotency at all. For Kalshi connect, the
email is required at line 69, so we're safe; for any future routes
that adopt the same helper, this is a footgun.

---

### INFO-3 — Brief-named features that do not exist in this file

The brief asked about:

- **Share-link forgery (portfolio sharing):** No such feature in
  `portfolio/routes.py` or anywhere in `portfolio/`. Confirmed by
  `grep -r "share" gateway/portfolio/` (only matches are the
  `shares` numeric field on positions, not URL sharing). Not
  applicable — flag if a future PR adds a share/public-URL feature
  for portfolios, as it inherits MED-1's identity assumptions.
- **CSV / JSON export size limit:** No CSV export. JSON output of
  `/api/portfolio/positions` and `/api/portfolio/summary` is bounded
  only by the number of rows in `user_positions` for the current
  user. SQLite per-row size is small (no `BLOB` columns) and there's
  no pagination, but a user with very high position-count would
  produce a large JSON body. No `Content-Length` cap, no streaming.
  Practical cap is whatever the upstream CLOB returns (the sync job
  passes the full `/positions` payload through). Not exploitable
  inter-user, but a per-user resource issue if the wallet has
  thousands of positions.
- **PII in export:** Not applicable to this file. **However:**
  `gateway/exports/generator.py:425-440` DOES read
  `polymarket_connections` (wallet address + sync error) and
  `kalshi_connections` (email + encrypted_token + member_id +
  sync_error) into an account-data export bundle. **The
  `encrypted_token` column being included in the export is a
  legitimate concern** — even encrypted, that token is a Kalshi
  session credential, and including it in a user-downloadable
  export means it leaves the server's encryption boundary. The
  account export ought to redact `encrypted_token` and `email`
  (or at least the token). Out of scope for this audit's target
  file, but recommend a follow-up audit of
  `gateway/exports/generator.py` lines 420-445 and 830-850.
- **IDOR on portfolio view/edit:** All read endpoints
  (`/api/portfolio/summary`, `/api/portfolio/positions`) scope to
  `_user_id(user)` from the session — no `user_id` is accepted from
  query/path. No "edit position" endpoint exists (positions are
  read-only mirrors of upstream state). The only writes are
  upsert-keyed-on-user (`polymarket.upsert_connection`,
  `kalshi.upsert_connection`, `kelly.set_user_bankroll`), all of
  which use the session-derived user_id. **No IDOR vector in this
  file.** (Bumps to a real issue if a future endpoint accepts
  `?user_id=` for admin/internal use.)

---

## Other observations (not findings)

- CSRF coverage confirmed: `/api/portfolio/*` is not in either
  `_CSRF_EXEMPT_POSTS` or `_CSRF_EXEMPT_POST_PREFIXES`
  (`server.py:1105-1154`). All four POST endpoints are subject to
  the double-submit cookie check.
- The position-sync rate-limit story upstream (Polymarket) is solid:
  `sync_portfolios.py:128-140, 197-198, 208-216` implements a
  token-bucket pacer at ~5 req/s, a 429-typed abort with exponential
  backoff (`_BACKOFF_BASE_SECONDS`, doubling to `_BACKOFF_MAX_SECONDS`),
  and a per-connection error-streak skip (`_MAX_ERROR_STREAK`).
  Polymarket abuse via the sync job itself is not a concern — the
  abuse vector is via `connect` endpoints clearing error counters
  (MED-3) and via duplicate-wallet amplification (MED-2).
- The Kalshi 401-on-expiry path at `kalshi.py:194-205` records a
  typed `http_401` error in `sync_error` and increments the streak.
  Good — but the streak threshold for skipping at
  `sync_portfolios.py:179` only applies to the polymarket loop; the
  kalshi loop in the same file needs a parallel guard. Out of scope
  here, flagging for the kalshi sync audit.
- `polymarket.py:138-217` (`fetch_market_state`) implements a 60s
  in-process cache for Gamma market state, capped at 5000 entries
  with a 25%-oldest eviction. No DoS-via-cache concern (TTL is
  short, cap is hard, the entries are not user-controlled IDs).
- The `_normalise` functions in both polymarket.py and kalshi.py
  trust upstream JSON shape but defensively coerce via `_float` /
  `_cents_to_usd`. No injection vectors through these.
- All SQL is parameterised. The `platform` query parameter in
  `api_portfolio_positions` is whitelisted to
  `{polymarket, kalshi}` at `routes.py:136-137` before reaching the
  `WHERE platform = ?` placeholder. No SQLi.

---

## Recommended fix order

1. HIGH-1: add `_require_trading_addon` and gate all six routes (~10 LOC).
2. HIGH-2: add per-user and per-target-email rate-limits to
   `/api/portfolio/kalshi/connect` (~6 LOC plus two new
   `_is_rate_limited` keys).
3. MED-1: ship SIWE verification for Polymarket connect — already
   templated in `market_routes.py:80-90`.
4. MED-2 / MED-3: schema constraint + don't-reset-on-no-op-upsert.
5. MED-4: explicit `httpx` log level + a regression test.
6. LOW-1/2/3 + INFO items as time permits.

---

## Files referenced

- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py:25-37` — auth helpers
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py:43-57` — Polymarket connect
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py:60-123` — Kalshi connect
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py:126-140` — positions read
- `/Users/shocakarel/Habbig/gateway/portfolio/routes.py:143-196` — Kelly + bankroll
- `/Users/shocakarel/Habbig/gateway/portfolio/polymarket.py:83-103` — wallet upsert
- `/Users/shocakarel/Habbig/gateway/portfolio/kalshi.py:75-120` — login + upsert
- `/Users/shocakarel/Habbig/gateway/security/idempotency.py:147-190` — `with_idempotency`
- `/Users/shocakarel/Habbig/gateway/server.py:1105-1154` — CSRF exempt sets
- `/Users/shocakarel/Habbig/gateway/server.py:1761-1790` — global per-IP cap
- `/Users/shocakarel/Habbig/gateway/server.py:8540-8560` — module registration
- `/Users/shocakarel/Habbig/gateway/jobs/sync_portfolios.py:128-230` — sync pacing
- `/Users/shocakarel/Habbig/gateway/exports/generator.py:420-445` — export bundle (INFO-3)
- `/Users/shocakarel/Habbig/gateway/migrations/062_portfolio_integration.py:40-100` — schema
