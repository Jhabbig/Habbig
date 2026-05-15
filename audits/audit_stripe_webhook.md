# Adversarial Audit — `gateway/stripe_webhook_routes.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7)
Target: `/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py`
Commit under review: `68b00c9` ("feat(stripe): wire /stripe/webhook handler — IP allowlist + signature + idempotency", 2026-05-14).
Supporting layer reviewed:
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_hardening.py`
- `/Users/shocakarel/Habbig/gateway/migrations/061_processed_stripe_events.py`
- `/Users/shocakarel/Habbig/gateway/migrations/185_users_stripe_customer_id.py`
- `/Users/shocakarel/Habbig/gateway/server.py` (`_get_client_ip`, `_is_rate_limited`, `_TRUSTED_PROXY_HOSTS`)
- `/Users/shocakarel/Habbig/gateway/db.py` (`conn()`, `subscriptions` table)
- `/Users/shocakarel/Habbig/gateway/tests/test_stripe_webhook_route.py`

Scope was scoped tightly to the six attacker classes named in the brief:

1. Signature-validation timing
2. IP-allowlist bypass via X-Forwarded-For (and CF-Connecting-IP)
3. Idempotency-key race
4. Livemode-event acceptance in test mode (or vice versa)
5. Event-replay window
6. customer-id-to-user-id lookup IDOR

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 1 |
| Medium   | 3 |
| Low      | 4 |
| Info     | 3 |
| **Total**| **11** |

## Top 3 findings (ranked by exploitability x impact)

1. **HIGH-1** — `extract_client_ip` in `stripe_webhook_hardening.py:136-149`
   trusts `CF-Connecting-IP` from **every** caller, with no check that the
   TCP peer is the Cloudflare tunnel. Any attacker who reaches the gateway
   off-tunnel (open origin port, accidental LB exposure, internal SSRF
   pivot, or any non-CF ingress path) can set
   `CF-Connecting-IP: 3.18.12.63` and walk past the IP allowlist. Compare
   `server._get_client_ip` (`server.py:1731`), which correctly gates the
   header on `peer in _TRUSTED_PROXY_HOSTS`. The signature check still
   stands behind this, so this is defence-in-depth degraded to
   defence-in-zero, not full bypass — but the audit comment at
   `stripe_webhook_hardening.py:124-125` advertises this layer as the
   backstop against a leaked signing secret. (See HIGH-1.)

2. **MED-1** — Live-mode gate is **asymmetric and the symmetric helper is
   unused**. The route at `stripe_webhook_routes.py:266-274` only rejects
   `livemode=True` events in non-live env. The reverse case
   (`livemode=False` test webhook hitting a `STRIPE_LIVE_MODE=true`
   production gateway) is **accepted and dispatched**. A signed test-mode
   webhook from any compromised dev account (or a leaked test-mode signing
   secret, which Stripe explicitly says receives less protection than
   live-mode secrets) can grant a real `subscriptions` row in production.
   The `reject_mode_mismatch()` function in
   `stripe_webhook_hardening.py:152-173` was *designed* to cover both
   directions and is referenced in that module's docstring at line 16, but
   `stripe_webhook_routes.py` never imports or calls it. (See MED-1.)

3. **MED-2** — `_user_id_from_event` in
   `stripe_webhook_hardening.py:241-269` (and the parallel metadata reads
   in `_grant_access` / `_update_plan` at
   `stripe_webhook_routes.py:91-105, 132-147`) treats `metadata.user_id`
   from the Stripe event payload as authoritative with **no proof that the
   Stripe Customer belongs to that user**. A user who can edit subscription
   metadata in their own Stripe customer object — possible via the Customer
   Portal `update` action, or via the API if a checkout flow ever takes
   metadata from the client — can rewrite `metadata.user_id` to another
   narve.ai user's id and (on the next `customer.subscription.updated`
   event) write a `subscriptions` row UPSERTed onto the victim. The
   gateway never stores `stripe_customer_id` (see Info-2) so there is no
   binding to check against. (See MED-2.)

---

## Findings

### HIGH-1 — IP allowlist trivially bypassed by spoofed `CF-Connecting-IP`

**Location:** `stripe_webhook_hardening.py:136-149` (`extract_client_ip`),
called from `stripe_webhook_routes.py:221`.

**What:**

```python
def extract_client_ip(request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP") or ""
    if cf_ip:
        return cf_ip.strip()
    try:
        return request.client.host or ""
    except AttributeError:
        return ""
```

There is **no check** that `request.client.host` is in `_TRUSTED_PROXY_HOSTS`
before honoring `CF-Connecting-IP`. The comparable helper in
`server.py:1731-1741` does it correctly:

```python
peer = (request.client.host if request.client else "") or ""
if peer in _TRUSTED_PROXY_HOSTS:
    cf_ip = request.headers.get("cf-connecting-ip")
    ...
return peer or "unknown"
```

The docstring at `stripe_webhook_hardening.py:142-144` even acknowledges
the threat ("under Cloudflare would be a Cloudflare edge IP") but stops
at the half-fix.

**Attack:**

1. Reach the gateway off-tunnel. Concrete paths observed in this repo:
   - Direct hit to the FastAPI port if a future deploy ever exposes the
     origin without the CF Tunnel proxying first (the
     `SubproductMiddleware` comment at `server.py:1574-1575` confirms the
     gateway is expected to be reachable on a non-CF path during boot /
     local replay tooling — anything that escapes that envelope leaks
     the webhook surface).
   - Lateral movement from a co-located container that can reach the
     gateway listener directly on a private network.
   - SSRF pivot from any other Habbig route that lands an outbound HTTP
     request (e.g. via a misconfigured forecast scraper) on the loopback
     interface.

2. POST `/stripe/webhook` with header
   `CF-Connecting-IP: 3.18.12.63` (one of the literal allowlisted Stripe
   IPs). `reject_non_stripe_ip` returns `None` -> signature check is the
   only remaining defence.

3. The signature still fails without the real signing secret, so this is
   not full RCE. But the audit comment at
   `stripe_webhook_hardening.py:124-127` markets the allowlist as the
   layer that catches "any attacker who discovered the signing secret but
   can't spoof Stripe's source IPs". A test-mode signing secret leak (an
   ex-employee, a dev box dump, an accidental commit) is the realistic
   threat model for live mode, and this layer no longer stops it.

**Why not Critical:** Signature verification still runs after the
allowlist, and an attacker also needs (a) off-tunnel ingress and (b)
either the signing secret or some other downstream bug.

**Fix:**

```python
def extract_client_ip(request) -> str:
    peer = ""
    try:
        peer = request.client.host or ""
    except AttributeError:
        pass
    if peer in {"127.0.0.1", "::1", "localhost"} or _is_cf_edge_ip(peer):
        cf_ip = request.headers.get("CF-Connecting-IP") or ""
        if cf_ip:
            return cf_ip.strip()
    return peer
```

Or just import `server._get_client_ip` and use it directly — the trust
list is already maintained there. As a tactical second-line: drop the
allowlist check off entirely in environments where the gateway can be
reached off-tunnel, and harden by widening the rate limit instead; the
allowlist creates a false sense of security if the trust boundary is
porous.

---

### MED-1 — Asymmetric live-mode gate (production accepts test events)

**Location:** `stripe_webhook_routes.py:266-274`.

**What:**

```python
if event.get("livemode") and not _stripe_live_mode_enabled():
    return JSONResponse(
        {"error": "Live events not accepted in this environment"},
        status_code=400,
    )
```

This blocks `livemode=True` events when the env is in test mode. It does
**not** block `livemode=False` events when the env is in live mode. The
`reject_mode_mismatch` helper in `stripe_webhook_hardening.py:152-173`
covers both directions:

```python
livemode = bool(event.get("livemode", False))
prod = _is_production()
if livemode != prod:
    ...
```

— but the live route never imports it (`grep -rn "reject_mode_mismatch"
gateway/` confirms the only callers are the helper's own tests).

**Attack:**

1. Compromise / leak a test-mode signing secret. These are weaker by
   policy: a test-mode signing secret is shared with dev tooling, often
   pasted into chat or stored on shared CI runners (this repo's own
   `STRIPE_GO_LIVE.md` enumerates both secrets at `Habbig/STRIPE_GO_LIVE.md`).
   The IP allowlist degraded to HIGH-1 above is still the only other
   network-layer check.

2. Forge a `livemode=False` `customer.subscription.created` event signed
   with the test secret, hand it to a production webhook endpoint.

3. Production accepts it. `_grant_access` writes a fresh
   `subscriptions` row with `status='active'` and `source='stripe'`.
   `subproduct_access.has_active_subscription` (cached) now reports the
   victim as paid. The attacker has free access for as long as nobody
   audits the row.

**Why not High:** Two preconditions stack — the test secret must leak,
and the attacker must reach the live webhook either via HIGH-1 or via a
real Stripe sender (test events from the real Stripe test backend will
be sent from the same IP list but signed with the test secret; this is
exactly what `reject_mode_mismatch` was meant to catch). Still, "test
secret leaks more than live secret" is the explicit Stripe threat model.

**Fix:** swap the asymmetric check for the existing helper. Two lines:

```python
from stripe_webhook_hardening import reject_mode_mismatch
...
mismatch = reject_mode_mismatch(event)
if mismatch is not None:
    return mismatch
```

This also keeps the "production accepts only livemode" invariant in one
function used by tests, which removes the divergence between the helper
and the route. Either drop the `_stripe_live_mode_enabled()` helper or
fold it into `reject_mode_mismatch` for clarity.

---

### MED-2 — `metadata.user_id` is attacker-controllable; no customer-to-user binding

**Location:**

- `stripe_webhook_routes.py:93-96` (`_grant_access`)
- `stripe_webhook_routes.py:134-137` (`_update_plan`)
- `stripe_webhook_hardening.py:241-269` (`_user_id_from_event`)

**What:** Every dispatch handler reads `user_id` out of
`event["data"]["object"]["metadata"]["user_id"]` and writes
`subscriptions` rows keyed by that value. There is no check that the
Stripe `customer` on the event belongs to that local user. The fallback
in `_user_id_from_event` (mapping `customer` -> `users.stripe_customer_id`)
exists but is **never populated by this codebase** (Info-2 below): a
`grep -rn "UPDATE users SET stripe_customer_id"` returns only the test
suite. So in practice the metadata path is the *only* path.

**Attack chain (assuming attacker has a paid Stripe customer of their
own):**

1. Attacker visits the Stripe Customer Portal (the codebase exposes one
   at `billing_routes.py:1157+`). The Customer Portal can be configured
   to allow editing subscription metadata. Stripe's default does not
   permit user-side metadata edits, but the portal config is
   account-level and a misconfiguration here is one checkbox away. Even
   without portal edits: any future checkout flow that lets the client
   contribute metadata (a JS call to
   `stripe.subscriptions.create({metadata: ...})` driven from a SPA via
   a backend that doesn't pin metadata) re-opens the vector.

2. Attacker sets `metadata.user_id = <victim_uid>` on their own
   subscription, then triggers a `customer.subscription.updated`
   event (e.g. by changing plan once).

3. Stripe signs the event (live mode, real secret). It reaches
   `/stripe/webhook` with a valid signature, from a Stripe IP.

4. `_update_plan` runs
   `UPDATE subscriptions SET status=?, plan=? WHERE user_id=? AND
   dashboard_key=?` with `user_id = victim_uid`. If the victim already
   has an `inactive` row for that `dashboard_key`, it gets flipped to
   `active`. If they have no row, `_grant_access` on a subsequent
   `subscription.created`-like delivery (or admin replay) UPSERTs one.

5. The attacker now grants and revokes the victim's subscription. They
   can also use this to *deny* service: `_update_plan` flips the
   victim's existing active row to `inactive` when the attacker cancels
   their own subscription with `metadata.user_id = victim_uid`.

**Why not High:** Requires either misconfigured Customer Portal or a
future checkout flow that takes metadata from the client. Today the
checkout in `billing_routes.py` pins metadata server-side. But there is
no defence here — the moment metadata becomes client-influenced, every
user becomes a one-event grant/revoke oracle for every other user.

**Fix (defence in depth):**

1. On every dispatch, look up
   `SELECT id FROM users WHERE stripe_customer_id = ?` using the event's
   `customer` field and compare it to `metadata.user_id`. Reject (200 +
   error stamp) on mismatch.

2. Backfill `stripe_customer_id` whenever the gateway sees a
   `customer.subscription.created` event (the `customer` field is
   reliably present), so the lookup table actually exists. Today the
   column is added by migration 185 but **never written to** — a free
   `UPDATE users SET stripe_customer_id = ? WHERE id = ?` in
   `_grant_access` after the subscription insert closes the loop.

3. Make `stripe_customer_id` a UNIQUE index (migration 185 currently
   creates a non-unique partial index — see Info-1). Without uniqueness,
   two narve.ai users could both end up bound to the same Stripe
   customer in a misordered event sequence, and the customer-to-user
   lookup becomes ambiguous.

---

### MED-3 — Idempotency record commits in a separate transaction from dispatch

**Location:** `stripe_webhook_hardening.py:176-211` (`mark_received`),
`stripe_webhook_routes.py:277-307` (route flow).

**What:** `mark_received` opens its own `with db.conn() as c:` block.
`db.conn()` commits on `__exit__` (`db.py:257-266`). So the row in
`processed_stripe_events` is committed *before* the dispatch branch runs
and *before* `mark_processed` updates `processed_at`. If `_grant_access`
(or any dispatch branch) raises, `mark_processed` stamps the error and
returns 200 — fine — but the idempotency row is now stuck:

- A subsequent retry of the same event by Stripe (which it will do for
  up to 3 days on a *non-2xx*, **but our handler returned 200**) is
  short-circuited as "already processed" — so we never re-attempt
  delivery.
- Operationally the admin panel surfaces the row with non-null `error`
  for the human to replay. That's the design. But for any non-human-
  attended deploy (the canonical Stripe pattern is auto-retry), one bad
  push during a flaky DB window silently drops the event.

Combined with `mark_received`'s broad `except Exception` (it logs and
returns `None`), a transient DB outage during ledger insert lets the
dispatch run *without* idempotency protection — and a subsequent retry
of the same event after the DB recovers will re-run `_grant_access`,
`_update_plan`, or `apply_subscription_cancelled`. UPSERTs are
idempotent. Email enqueue in `apply_subscription_cancelled` (lines
324-361) is **not** — duplicate cancellation emails ship.

**Attack / failure mode:**

1. SQLite write-locked (long checkpoint, backup snapshot, etc.) when
   Stripe delivers `customer.subscription.deleted`. `mark_received`
   raises -> caught -> `None` returned -> dispatch runs ->
   `apply_subscription_cancelled` enqueues the email + revokes
   sessions. Returns 200.

2. Stripe doesn't retry (we returned 200).

3. SQLite recovers. Stripe replays nothing.

Or, the inverted variant:

1. Dispatch raises (network blip enqueuing the cancellation email; the
   subproduct_access cache invalidate succeeds before the email
   enqueue at line 360 fails). `mark_processed` stamps `error`. Returns
   200.

2. Admin replays the event from the admin panel. The
   `INSERT OR IGNORE` row is already there -> `mark_received` returns
   `already_processed` -> dispatch does **not** run. The session is
   never re-revoked / the cache invalidate is never re-run.

**Why not High:** No data corruption. SQLite write locks are short.
Stripe's own 3-day retry curve is the real backstop and most failures
self-heal.

**Fix:**

- Roll `mark_received` into the same transaction as the dispatch. Open
  the connection in the route, pass the cursor down. Commit only at the
  end after `mark_processed` stamps success/error. If anything raises,
  rollback so the idempotency row goes away with the dispatch.
- Alternatively: split idempotency into a two-phase "received" / "done"
  pair where `received_at` reserves the row but the short-circuit on
  replay only fires when `processed_at IS NOT NULL` (or the row is
  older than a small grace window). Admin replays already-flagged rows
  re-run cleanly.

Race window note: SQLite serializes writers, so two concurrent webhook
deliveries of the same event_id will not both succeed at `INSERT OR
IGNORE` — the second blocks then sees `rowcount=0`. Postgres or any
future MVCC migration would lose that guarantee unless the route adds an
advisory lock or a `SELECT … FOR UPDATE`.

---

### LOW-1 — Global webhook rate limit is per-route, not per-IP

**Location:** `stripe_webhook_routes.py:214-218`:

```python
if _is_rate_limited("stripe_webhook_global", limit=100, window=60):
```

The key is a literal string — every caller shares one bucket. A noisy
neighbour (or a flood from a single misbehaving Stripe source IP, e.g.
during a backfill) immediately starves the legit Stripe traffic for the
remainder of the minute. The comment at line 214 says "Stripe normally
bursts ~3/s" — at 100/min that is exactly the steady-state rate, so
*any* burst above baseline trips 429 and Stripe sees retries from the
queued events.

**Fix:** Key on `client_ip` (well-formed Stripe traffic comes from ~12
IPs, so the bucket size stays sane). Or raise the limit substantially —
Stripe will *retry* on 429 (this is fine), but operationally we'd
rather not amplify retries during legitimate spikes (subscription churn
on a Black Friday, a webhook redelivery storm after a Stripe outage).

---

### LOW-2 — `_record_payment` and `_update_plan` silently no-op when metadata is missing

**Location:** `stripe_webhook_routes.py:166-186`, `stripe_webhook_routes.py:126-163`.

`_record_payment` early-returns when `obj.get("subscription")` is empty.
`_update_plan` early-returns when `user_id` or `dashboard_key` missing.
Both stamp `processed_at` without `error` set — the admin panel sees a
clean row. Operationally this is fine; auditably it hides the case where
metadata regressed in the checkout flow and a real customer paid for
something that the gateway can't bind to a user.

**Fix:** Stamp a soft warning into `error` (`"missing user_id metadata"`,
`"no subscription on invoice"`) so the admin panel surfaces these
without flagging them as crashes.

---

### LOW-3 — `error` truncation at 500 chars loses stack trace

**Location:** `stripe_webhook_routes.py:300-301`:

```python
error_msg = str(exc)[:500]
```

A SQLite `IntegrityError` or a `stripe.error.APIConnectionError`
typically has a useful message under 500 chars, but a wrapped traceback
or a JSON-encoded Stripe response can blow past that. The exception is
also logged via `log.exception` so the traceback isn't lost, but the
admin-panel row truncates without an ellipsis indicator, which can
confuse on-call.

**Fix:** Either store the full traceback in a separate column or append
`"…"` when truncated so the operator knows there's more in the logs.

---

### LOW-4 — Webhook accepts unbounded body size

**Location:** `stripe_webhook_routes.py:227-231`:

```python
try:
    payload = await request.body()
except Exception as exc:
    log.warning("failed to read webhook body: %s", exc)
    return JSONResponse({"error": "Bad request"}, status_code=400)
```

Stripe events are well under 1 MB in practice. The route reads the full
body before checking the signature, so an attacker who can reach the
route (off-tunnel; HIGH-1) can submit a multi-MB body and force the
gateway to allocate before rejecting on bad signature. There's a global
per-IP 600/min cap upstream (`server.py:1765-1789`) and Cloudflare body-
size caps; this is a non-issue today, but trivial to harden.

**Fix:** Stream-read and bail at 1 MB:

```python
body = b""
async for chunk in request.stream():
    body += chunk
    if len(body) > 1_048_576:
        return JSONResponse({"error": "Payload too large"}, status_code=413)
```

---

### INFO-1 — `stripe_customer_id` index is non-unique

**Location:** `migrations/185_users_stripe_customer_id.py:25-28`:

```python
c.execute(
    "CREATE INDEX IF NOT EXISTS idx_users_stripe_customer "
    "ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
)
```

`CREATE INDEX`, not `CREATE UNIQUE INDEX`. A Stripe customer can only be
bound to one Stripe account, so one `cus_…` value should map to at most
one narve.ai user — but the DB doesn't enforce it. Combined with MED-2,
this gives the lookup an ambiguity surface. Adding the unique constraint
also costs nothing (the column is otherwise unused).

**Fix:** Convert to `CREATE UNIQUE INDEX` in a new migration after
backfilling.

---

### INFO-2 — `stripe_customer_id` is never written by application code

**Location:** Grep `UPDATE users SET stripe_customer_id` across `gateway/`
returns only `tests/test_billing_portal.py`. The webhook handler reads
the column at `stripe_webhook_hardening.py:262` and the billing portal
reads it at `billing_routes.py:1194-1200`, but no production code ever
populates it.

The fallback lookup at `_user_id_from_event` (lines 256-268) is therefore
dead in practice — only the `metadata.user_id` path runs. This makes
MED-2 (metadata as authority) the *only* binding between Stripe state
and local users.

**Fix:** Backfill `stripe_customer_id` on `customer.subscription.created`
delivery. Two lines in `_grant_access`:

```python
customer = obj.get("customer") or ""
if customer and user_id:
    c.execute("UPDATE users SET stripe_customer_id = ? "
              "WHERE id = ? AND stripe_customer_id IS NULL",
              (customer, user_id))
```

That gives MED-2's mitigation a leg to stand on.

---

### INFO-3 — No timestamp / event-replay window

**Location:** Whole route.

`stripe.Webhook.construct_event` by default enforces a 5-minute tolerance
against the `t=` parameter in the signature header (the SDK's
`DEFAULT_TOLERANCE`). The route does not pass an explicit tolerance —
that's fine, the SDK default holds. But there is no additional check
against `event["created"]` (Stripe's event creation time) and no upper
bound on how stale a *previously-unseen* event can be. A signed event
captured in transit (e.g. logged by an intermediate proxy under
attacker control) 5 minutes ago is still acceptable; an event captured
4 hours ago is rejected by the 5-minute signature tolerance. Adequate.

This is informational because the SDK's tolerance is the standard
defence. Calling it out so a future SDK upgrade that loosens the
default is on the audit radar.

**Optional hardening:** explicit
`stripe.Webhook.construct_event(payload, sig_header, secret, tolerance=300)`
so the tolerance is pinned in source rather than inherited from the
SDK version.

---

## Notes on what is correct

The audit was adversarial; for balance, the following were checked and
are sound:

- **Constant-time signature comparison:** delegated to
  `stripe.Webhook.construct_event`, which the Stripe SDK implements via
  `hmac.compare_digest`. The test stub mirrors this at
  `tests/test_stripe_webhook_route.py:59`.
- **No JSON-parse-before-verify:** the route reads raw bytes, hands them
  to the SDK, and only uses the parsed `event` after verification
  succeeds. The fallback comment at `stripe_webhook_routes.py:255-258`
  explicitly refuses unsigned parsing — good.
- **No SQL injection on metadata:** all writes are parameterised
  (`?` placeholders), and `_coerce_int` rejects non-integer
  `metadata.user_id`. `dashboard_key` is interpolated into the UPSERT as
  a bound parameter.
- **Embed widgets revoked on cancellation:**
  `apply_subscription_cancelled` correctly flips `is_active=0` for all
  widgets owned by the cancelled user (line 306-311), closing the
  obvious "user cancels but their widget still serves cached responses"
  hole.
- **Cache invalidation:** `subproduct_access.invalidate_user` is called
  on both cancel and payment-failed paths, so cached access verdicts
  don't outlive the subscription change.
- **Stripe SDK presence check** before any state work — the 503 path at
  line 204-211 prevents a misdeploy from accepting unsigned events.
- **`error` field on processed_stripe_events** is the right operational
  signal for human triage.

---

## Reproduction notes

The findings above were verified by code reading; for confirmation in a
running gateway:

- **HIGH-1**: `curl -X POST http://127.0.0.1:<port>/stripe/webhook -H
  "CF-Connecting-IP: 3.18.12.63" -H "Stripe-Signature: bogus" -d '{}'`
  from any host that can reach the gateway without going through
  Cloudflare — expect a 400 (bad sig), confirming the allowlist did
  not 403 first. (The 400 is observable, but it shows the order: the
  allowlist did not stop the request.)
- **MED-1**: set `STRIPE_LIVE_MODE=true` and post a signed event with
  `livemode=False`. Expect 200 + dispatch (under current code).
- **MED-2**: replay a signed `customer.subscription.updated` event with
  the attacker's real signing secret, but `metadata.user_id` rewritten
  to a victim's id. Confirm a `subscriptions` row materialises against
  the victim.
- **MED-3**: under a synthetic SQLite-lock injection (open a long
  `BEGIN IMMEDIATE` from another process), POST a webhook -> observe
  `mark_received` swallow the exception (warning log) and dispatch run
  twice if the event is redelivered after the lock clears.

No code changes were made as part of this audit.
