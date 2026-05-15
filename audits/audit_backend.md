# Backend Internal-Service Audit — `gateway/backend/`

Adversarial audit of `gateway/backend/` focused on internal-service boundaries:
HMAC signing, request validation, downstream-timeout enforcement.

Date: 2026-05-15.

## Scope

`gateway/backend/` is an in-process Python library (not a separate microservice).
Boundaries audited:

1. **Downstream HTTP** to Polymarket (Gamma + CLOB) and Kalshi v2 trading API.
2. **Per-call request validation** — Ethereum address, order params, ticker / slug.
3. **Downstream timeout enforcement** — connect + read timeouts; explicit
   per-request `timeout=` vs. client-default reliance.
4. **HMAC / signing** — not applicable in-process (no internal-service
   handshake here); Stripe webhook signing lives in `gateway/stripe_webhook_*.py`
   and is **out of scope** for this audit. The `payments/stripe_stub.py` fence
   is reviewed only for the absence of a working ungated path.
5. **Token encryption boundary** — `markets/encryption.py` (Fernet) is the
   in/out crypto between DB rows and the Kalshi client.

Files reviewed:

- `/Users/shocakarel/Habbig/gateway/backend/__init__.py` (empty)
- `/Users/shocakarel/Habbig/gateway/backend/referrals.py`
- `/Users/shocakarel/Habbig/gateway/backend/payments/__init__.py` (empty)
- `/Users/shocakarel/Habbig/gateway/backend/payments/stripe_stub.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/__init__.py` (empty)
- `/Users/shocakarel/Habbig/gateway/backend/markets/encryption.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/kalshi_client.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/polymarket_client.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/movement_detector.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/portfolio_aggregator.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/portfolio_signals.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/portfolio_sync.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/unified_markets.py`
- `/Users/shocakarel/Habbig/gateway/backend/markets/whale_tracker.py`

No code changes. Findings only.

---

## Severity legend

- **C** Critical — immediate production exploit, financial loss, or RCE
- **H** High — exploit needs minor preconditions; significant impact
- **M** Medium — defence-in-depth gap; bypassable with care or operator-only
- **L** Low — hygiene / hardening / contradicted comments
- **I** Informational

## Severity counts

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 2 |
| Medium | 5 |
| Low | 4 |
| Informational | 2 |
| **Total** | **13** |

---

## Top 3 (rank-ordered)

1. **H-1 — Polymarket CLOB `submit_order` forwards an arbitrary client-controlled
   dict with no whitelist or shape check.** `PolymarketClient.submit_order`
   (`polymarket_client.py:155-174`) takes `signed_order: dict` and POSTs it
   verbatim to `https://clob.polymarket.com/order`. The trust model is "client
   signs with the user's wallet", but the backend never sanity-checks that the
   payload is the expected EIP-712 order shape, never enforces a per-user
   amount cap, never confirms the `maker` matches the wallet the user has
   linked, and never trims unexpected fields. On the error path it returns
   `e.response.text` verbatim into the route's response — leaking upstream
   error bodies to whatever caller routes consume `submit_order()`'s return.
   Compounding factors: the call path is `gateway/market_routes.py` →
   `submit_order` → CLOB, and there is **no downstream timeout** passed to
   the request — the route inherits whatever timeout the `_client` was built
   with at process start, which defaults to `15.0s` but is not reasserted
   per request (contrast Kalshi's M14 hardening).

2. **H-2 — `unified_markets._cache` is unbounded, process-global, and the cache
   key is partially attacker-controlled.** `unified_markets.py:202-214` defines
   a module-level `_cache: dict[str, tuple[float, object]]` with no size cap
   and no eviction. `fetch_single_market` (line 259-288) constructs the cache
   key as `f"market:{market_id}"` where `market_id` arrives from the
   `/api/markets/{id}` route in `gateway/market_routes.py`. Although the
   prefix (`poly:` / `kalshi:`) is checked before fetching upstream, the
   **cache key uses the full untrusted `market_id` string**, so any unique
   id seen — whether or not it ever resolves upstream — accretes an entry.
   Calling the route with a million distinct slugs grows the process heap by
   ~1M tuples (with cached `UnifiedMarket` dataclass instances) and never
   releases them. Same shape applies to `_ENRICHMENT_CACHE_KEY` (single
   bounded key, less abusable, but still no size cap on the `enriched`
   list reference it holds). No metric / log exposes cache size to operators.

3. **H-3 (Medium-promoted) — Kalshi service-account login lock is created
   non-atomically.** `kalshi_client.py:115-118`:
   ```python
   if self._service_login_lock is None:
       self._service_login_lock = asyncio.Lock()
   async with self._service_login_lock:
   ```
   Two concurrent first calls into `_get_service_token` can both observe
   `is None`, both construct a fresh `asyncio.Lock()`, and then each acquire
   *its own* lock — defeating the "serialise concurrent logins so we only
   issue one POST /login at a time" invariant the docstring promises.
   Probability is low (only at process start, before the first refresh), and
   the upstream rate-limit / backoff would catch a real abuser — but the bug
   is real, and the L9 exponential-backoff state on `self._login_next_attempt_at`
   is *also* read-modify-write without a lock, so a duplicated login burst
   could write back stale `_login_backoff` values. Fix: instantiate the lock
   in `__init__` (deferring loop-binding by using `asyncio.Lock()` lazily via
   `get_event_loop().create_task`-style guard is unnecessary post-3.10 where
   the lock binds on first acquire).

---

## Findings by file

### `markets/polymarket_client.py`

#### H-1 — `submit_order` accepts an arbitrary dict, no validation, leaks error body

(See Top 3, #1.)

Lines 155-174.

```python
async def submit_order(self, signed_order: dict) -> dict:
    ...
    resp = await client.post(f"{self.clob_base}/order", json=signed_order)
    ...
    except httpx.HTTPStatusError as e:
        body = e.response.text
        log.error("Polymarket CLOB order error HTTP %d: %s", e.response.status_code, body)
        return {"error": body, "status_code": e.response.status_code}
```

Concrete asks:

- Reject orders whose `maker` (or `signer`) field does not equal the user's
  verified wallet address (today: no EIP-191 challenge per the `TODO(security)`
  on line 132-138, so even ownership is unproven — but the linking step at
  least gives us *a* claimed address to compare against).
- Bound `signed_order` keys to a known allowlist (`signature`, `maker`,
  `taker`, `tokenId`, `makerAmount`, `takerAmount`, `side`, `salt`, `expiration`,
  `feeRateBps`, `nonce`, `signatureType`) — drop or 400 on anything else.
- Apply a per-user notional cap (env-configurable) before forwarding.
- Strip / generalise the upstream error body in the response. Log the raw
  body server-side (already happens at `log.error`) but return a stable
  enum to the caller (`"upstream_rejected"`, `"insufficient_balance"`, etc.)
- Add `timeout=REQUEST_TIMEOUT_SEC`-equivalent on the `.post(...)` call (see
  M-1 below).

#### M-1 — No per-call `timeout=` on Polymarket requests

Lines 79, 105, 118, 144, 164, 188 — every `client.get(...)` / `client.post(...)`
relies on the `httpx.AsyncClient`'s constructor-bound timeout. This is the
exact failure mode Kalshi explicitly hardened against in its `REQUEST_TIMEOUT_SEC`
comment (`kalshi_client.py:26-31`):

> Explicit per-call timeout (M14): even though the httpx.AsyncClient is
> constructed with a client-level timeout, passing ``timeout=`` on every
> request guarantees that an accidental client swap to one with
> ``timeout=None`` cannot leave us hanging on an unresponsive Kalshi endpoint.

Polymarket has the same risk but lacks the same mitigation. Add a module-level
`REQUEST_TIMEOUT_SEC` constant and pass it on every request.

#### M-2 — `search_markets` forwards untrusted query string verbatim to upstream

Line 114-125. No length cap, no whitespace / control-character stripping. A
caller can send a 10MB query string and we'll dutifully URL-encode and pass
it to `gamma-api.polymarket.com` — wasting our egress and upstream's ingress.
Cap `query` length (e.g. 200 chars) and strip control characters before the
outbound call.

#### L-1 — `validate_eth_address` is regex-only, comment acknowledges this

Lines 22-32 — well-documented; not exploitable for SSRF (the address is only
used as a query-string param, never as URL host or path), but the docstring
TODO on line 132-138 about EIP-191 wallet-ownership proof is a real gap that
crosses into product-level concerns (a user can ask narve "show me what
0xVitalik holds" and we'll show it). Polymarket position data is public, so
disclosure risk is low — flag for owner.

---

### `markets/kalshi_client.py`

#### H-3 — Service-login lock created non-atomically (see Top 3, #3)

Lines 115-118.

#### M-3 — `place_order` returns upstream error body in `error`

Lines 426-432:

```python
except httpx.HTTPStatusError as e:
    status = e.response.status_code
    if status in (401, 403):
        return {"error": "token_expired", "status_code": status}
    body_text = e.response.text
    log.error("Kalshi order error HTTP %d: %s", status, body_text)
    return {"error": body_text, "status_code": status}
```

Same shape as H-1. The order route in `gateway/market_routes.py` then surfaces
`error` to the API consumer — anything Kalshi puts in its response body (which
includes timestamps, request IDs, and sometimes JSON-encoded internal error
codes) leaves the gateway. Map to a stable enum.

#### M-4 — Service-account password held in closure, but the closure outlives the first login

Lines 65-71, 138-146. The M15 comment explains the intent: "store the password
in a closure-scoped `_password_provider` callable that is cleared as soon as
login succeeds" — but the code path that calls `_password_provider()`
(line 140) does **not** clear `self._password_provider` after success. A
forced refresh (`force_refresh=True`) on a long-running process re-invokes the
closure and re-reads the same captured local. The local `password = None`
on line 146 only nukes the local *reference*, not the captured cell `_pw`
in the closure. The plaintext password remains reachable for the process
lifetime. Either (a) bind the password into a `bytearray` and zero it after
the first successful login, or (b) replace `self._password_provider = None`
on success so a refresh has to re-fetch from a secret store.

#### L-2 — `get_market` swallows all HTTPStatusError to `None`

Lines 298-302. Caller cannot distinguish 404 (legitimately unknown ticker) from
401 (token expired, should trigger a refresh) from 500 (transient upstream
fault, should retry). Promote at minimum the 401/403 case to surface
`token_expired` like `get_balance`/`get_orders` do.

#### L-3 — Backoff state read-modify-write without the login lock

Lines 156-160. `self._login_backoff = min(self._login_backoff * _FACTOR, _MAX)`
inside the login-lock block is safe; but the `now < self._login_next_attempt_at`
read on line 131 happens **before** acquiring the lock, so two concurrent
callers can simultaneously decide they're within the backoff window and both
return `None`, or both decide they're outside it and the first wins the lock
while the second waits and then re-enters with `force_refresh=False`. The
re-check inside the lock (lines 119-126) handles the success case correctly.
Failure-backoff state is only mutated inside the lock, so this is hygiene, not
a real bug.

---

### `markets/encryption.py`

#### M-5 — `decrypt_token` swallows all exceptions and returns ciphertext as plaintext

Lines 54-67:

```python
def decrypt_token(encrypted: str) -> str:
    f = _get_fernet()
    if f is None:
        return encrypted
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        # May be a plaintext token from before encryption was enabled
        return encrypted
```

Two failure modes are conflated:

1. **Migration**: token was stored before encryption was enabled — return
   plaintext, fine.
2. **Key rotation gone wrong / DB corruption / wrong env var loaded**: the
   ciphertext is now an opaque blob the *Kalshi API* will receive as a Bearer
   token. Kalshi will (correctly) 401, the credential will be auto-deactivated
   in `portfolio_sync.py:63` via `set_market_credential_active(user_id, "kalshi", False)`,
   and a real operator-actionable signal ("the Fernet key changed under our
   feet") gets buried as a routine token-expiry. There is no log emitted in
   the except branch.

Recommend: log at `WARNING` when `f.decrypt` raises, including the first
several bytes of the encrypted blob (NOT the decrypted output) so an operator
can grep for cipher-prefix anomalies.

#### L-4 — `encrypt_token` returns plaintext fallback outside of PRODUCTION

Lines 39-51 — by design, but couple it with the M-5 above and a developer who
has `PRODUCTION=` empty but `CREDENTIALS_ENCRYPTION_KEY` set will silently
encrypt; if they then ship to a prod that doesn't have the key, all stored
tokens become unreadable AND the audit trail (a single `log.warning` at
process start) is easy to miss. Promote the "storing token without encryption"
warning to `ERROR` and emit it once per call, not once per init.

---

### `markets/unified_markets.py`

#### H-2 — Unbounded process-global cache, attacker-influenced key (see Top 3, #2)

Lines 202-214, 259-288.

#### M-6 — `enrich_markets_with_intelligence` does dynamic `import db` inside the function

Line 346 — `import db` inside a hot path. Not a security bug, but it means
the function depends on a process-level mutable: someone monkeypatching
`db.calculate_betyc_probability` in tests or in another module can change
the behaviour of the cache-population step. Move to a top-level import.

#### I-1 — `_guess_category` keyword list is in-source and lowercases user-controlled text

Lines 178-197. The `text` is `f"{title} {extra}".lower()`. `title` comes from
the upstream API, not user input, so this is informational. Worth noting
that if the categorisation result is ever used as a database key (it is
not today), an upstream-controlled title containing one of the keyword
strings can steer the category.

---

### `markets/portfolio_aggregator.py`

#### M-7 — `Exception` catch-alls expose `str(e)` to caller via `error` field

Lines 110-112, 144-146:

```python
except Exception as e:
    log.error("Kalshi portfolio aggregation error: %s", e)
    result["kalshi"]["error"] = str(e)
```

The portfolio route returns `result` to the client. If `str(e)` contains a
file path, an SQL error, or any internal hint, it lands in the JSON the
browser sees. The `/api/markets/portfolio` route should layer its own
response sanitisation, but defence-in-depth here would be: return
`{"error": "aggregation_failed"}` and only `log.exception(...)` the
detail.

#### L-5 — Float coercion path with default `0.0` masks bad upstream data

Lines 52-56, 122-126. `_to_float` / `_safe_float` swallow `(ValueError, TypeError)`
and return 0.0. Position-value computations later sum these to produce a
combined-portfolio total. A consistently-malformed Kalshi response field
yields silent zero — operator cannot tell that "user has $0 of positions"
means "actually closed everything" vs. "Kalshi changed their schema again".
Worth logging once per request when a coercion falls back.

---

### `markets/portfolio_sync.py`

#### L-6 — `prune_stale_positions` only runs when an active credential is present

Lines 113-114. Correct, but the inverse case — a user whose credential was
just deactivated by line 63 — never has their cached positions cleared. The
positions linger in the DB until the next sync, by which point the credential
is inactive and pruning is skipped (because the `if` on line 113 fails). Net:
positions for a disconnected Kalshi account stay in `user_positions`
indefinitely until the user reconnects. This is a UX bug (portfolio shows
"ghost" positions) not a security one, but it crosses an internal-service
boundary so worth flagging.

---

### `markets/movement_detector.py`

#### M-8 — `_db_path` reads `GATEWAY_DB_PATH` env var without base-dir validation

Lines 52-57:

```python
def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent.parent / p)
    return Path(__file__).parent.parent.parent / "auth.db"
```

`GATEWAY_DB_PATH` is operator-controlled, not user-controlled, so this is
defence-in-depth — but the relative-path branch does not constrain `p` to
stay within the gateway directory. An operator who sets
`GATEWAY_DB_PATH=../../../../tmp/x.db` would happily get a sqlite file in
`/tmp`. Acceptable for operator-only env, but document the contract.

#### I-2 — Detection runs on a separate sqlite3 handle, no WAL mode assertion

Line 60-63. The function opens a vanilla `sqlite3.connect(_db_path())` and
writes events. The main gateway opens `auth.db` in WAL mode in `db.py`.
Concurrent writes from the detector + the gateway are fine in WAL, but if
the gateway is ever run on a fork that switches to journal mode, the detector
will deadlock the gateway during a write. Worth asserting `PRAGMA journal_mode`
on the detector's first connect.

---

### `referrals.py`

Pure functional module, no I/O, no external boundary. No findings.

---

### `payments/stripe_stub.py`

Intentionally a no-op fence. Both `create_checkout_session` and `handle_webhook`
raise `NotImplementedError`. The docstring's M9 warning about Stripe webhook
signature verification is correct and the stub is the right shape. The real
implementation lives in `gateway/stripe_webhook_routes.py` and is out of
scope for this audit (see `audit_stripe_webhook.md`).

---

### `markets/whale_tracker.py`

#### M-9 — `PolymarketClient` instantiated per-call without explicit timeout, no rate limit on wallet count

`poll_whale_positions` (lines 56-127) constructs a fresh `PolymarketClient()`
each invocation (good — no shared mutable state), but inherits the same
no-per-call-timeout issue from M-1. It iterates over every entry in
`WHALE_WALLETS` env var with no upper bound, calling
`poly.get_positions(address)` for each. An operator who pastes 10,000 wallets
into the env var causes the cron job to do 10,000 sequential outbound
requests against gamma-api, with no backoff, no concurrency limit, and no
break on consecutive failures. Bound `WHALE_WALLETS` to e.g. 500 entries and
batch the loop with `asyncio.gather` + a `Semaphore`.

---

## Notes on what's NOT broken

- **HMAC at this boundary**: N/A. The `backend/` module is in-process; no
  internal-service auth happens here. HMAC-related machinery lives in
  `gateway/auth/cookies.py` (session signing) and `gateway/stripe_webhook_hardening.py`
  (Stripe webhook verification). Both are out of scope for this audit per
  the file scope rule.
- **TLS / cert validation**: every outbound `httpx` call uses default TLS
  verification — no `verify=False` anywhere in `gateway/backend/`.
- **Connect-phase timeout**: Polymarket and Kalshi both pass
  `httpx.Timeout(self._timeout, connect=5.0)` to the `AsyncClient` constructor,
  which keeps the *connect* phase bounded even when the per-request `timeout=`
  is missing (the issue in M-1). This is the half that's hardened correctly.
- **Token reuse across users**: each user's Kalshi token comes from
  `db.get_all_market_credentials(user_id)` in `portfolio_sync.py:40`, so
  there's no shared `_client` token state that could leak across requests.
  Service-account token *is* shared (by design — it's a service account)
  and is correctly used only on the public-market endpoints (`_public_headers`),
  not on portfolio/trade endpoints.
- **`validate_eth_address` is consistently applied** before every wallet-keyed
  outbound call (`get_positions`, `get_orders` on `polymarket_client.py`).

---

## Suggested remediation order

1. **H-1** (`polymarket_client.submit_order` validation + timeout) — affects
   any user with a linked Polymarket wallet who places an order.
2. **H-2** (unified_markets unbounded cache) — affects every prod server that
   exposes `/api/markets/{id}` — slow-burn memory leak.
3. **M-1** (Polymarket per-call timeout) — bundles with H-1 fix.
4. **M-3** (Kalshi error-body leak) — bundles with H-1 shape fix.
5. **M-4** (Kalshi service-account password closure lifetime) — schedule
   alongside next key-rotation work.
6. **M-5** (encryption silent decrypt failure) — quick win; one log line.
7. **H-3** (Kalshi service-login lock race) — move lock construction into
   `__init__`, one-line fix.
8. Remaining M and L items as hygiene.
