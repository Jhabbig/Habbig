# Adversarial Audit — `gateway/realtime/`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Target: `/Users/shocakarel/Habbig/gateway/realtime/`
Files reviewed:

- `gateway/realtime/__init__.py`
- `gateway/realtime/routes.py`     (WS endpoint, auth, rate limit, admin stats)
- `gateway/realtime/hub.py`        (in-process pub/sub fan-out, eviction)
- `gateway/realtime/channels.py`   (allow-list, per-channel auth)
- `gateway/realtime/broadcast.py`  (emit helpers)

Supporting layers read for cross-checks:

- `gateway/server.py`              (`_user_from_ws`, `_require_admin_user`, `_get_client_ip`, uvicorn invocation)
- `gateway/subproduct_access.py`   (`has_subproduct_access` — called from `channels.py`)
- `gateway/tests/test_realtime.py` (282 lines; covers channel allow-list with **mocked** subproduct check)
- `gateway/middleware/perf.py`     (XFF handling elsewhere in stack)
- `scripts/systemd/narve-gateway.service` (uvicorn launch — no `--ws-max-size` flag)

Scope (from brief):

1. WebSocket auth flow
2. Channel-access checks
3. Message-rate limit per connection
4. Ping/pong timeout
5. Max-message size
6. Fan-out scope

Pre-release-only findings are flagged but **not counted** in the severity table per the
hard rule. They are listed in a separate "Pre-release notes" appendix.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 4 |
| Low      | 4 |
| Info     | 3 |
| **Total**| **13** |

## Top 3 findings (ranked by exploitability × impact)

1. **HIGH-1** — `is_channel_allowed` for `subproduct:{slug}` passes the WS user
   *dict* (only `user_id`, `email`, `is_admin`, `admin_level`) to
   `has_subproduct_access`, but that helper reads `subscription_tier` and
   `subproduct_subscriptions` from the row. Non-admin pro/enterprise/
   subproduct-entitled users will be **silently denied** the WS channel
   despite having a paid entitlement on the HTTP side. The unit test
   patches `_has_subproduct_access`, so CI is green. Either: load the
   full user row in `_user_from_ws`, or refactor the helper to accept a
   user_id and re-query. (See HIGH-1.)

2. **HIGH-2** — No max-message-size cap on inbound client frames. The
   loop is `async for raw in ws.iter_text(): json.loads(raw)`. Starlette
   itself imposes no payload ceiling; uvicorn's `--ws-max-size`
   (default 16 MiB on `websockets`, unbounded on `wsproto` depending on
   version) is never set in `narve-gateway.service`. Any authenticated
   user can hold the event loop with one 100 MiB `{"op":"subscribe",
   "channel":"…"}` blob, and `json.loads` will tie up the worker for
   seconds while it parses. Combined with `MAX_CONNECTIONS_PER_USER=3`
   that's only a 3× per-account amplification, but each connection also
   gets 30 msg/s — a malicious account can wedge the loop with very few
   resources. (See HIGH-2.)

3. **MED-1** — No server-side idle/ping timeout. The endpoint accepts a
   client `{"op":"ping"}` and replies pong, but the **server never
   originates** a ping and the event loop never times out a silent
   socket. A client that authenticates, subscribes, then goes quiet
   holds a slot until the OS-level TCP keepalive eventually decides to
   reap it (typically 2+ hours on Linux defaults). At 3 conns/user that
   is a slow but unattended slot-exhaustion vector once the user count
   grows past a few thousand. (See MED-1.)

---

## Findings

### HIGH-1 — Subproduct WS channel gating breaks for non-admin paid users (auth/correctness)

**Where:** `gateway/realtime/channels.py:48-55, 102-103` + caller in
`gateway/realtime/routes.py:104-146` (`_user_from_ws`).

**What:** `_user_from_ws` builds a minimal dict:

```python
return {
    "user_id": session["user_id"],
    "email": session["email"] if "email" in session.keys() else "",
    "is_admin": bool(admin_level),
    "admin_level": admin_level,
}
```

There's **no** `subscription_tier`, **no** `subproduct_subscriptions`.
This dict is then passed all the way to `subproduct_access.has_subproduct_access`:

```python
def _has_subproduct_access(user: dict, slug: str) -> bool:
    from subproduct_access import has_subproduct_access as _check
    return bool(_check(user.get("user_id"), slug))   # <-- but the helper expects a ROW, not a user_id
```

Two layered bugs in one line:

- `_check(user.get("user_id"), slug)` passes an **int** as the `user_row`
  argument. `has_subproduct_access` then calls `_pro_or_better(int)`,
  which does `_field(row, "is_admin", 0)`. `_field` tries `int["is_admin"]`
  → `TypeError` → caught → returns 0. Then it tries
  `int["subscription_tier"]` → same. Then `_blob_entry` reads
  `subproduct_subscriptions` from the int → empty → `None`.
- Net effect: **every** subproduct channel subscription from a non-admin
  user gets denied, including legitimate Pro / Enterprise / per-slug
  buyers. Admins squeak through only because `_user_from_ws` does propagate
  `is_admin`, but admins are already a special case via the admin path.

**Why CI is green:** `test_subproduct_checks_entitlement` patches
`realtime.channels._has_subproduct_access` directly, so the real call
path is never exercised. There is no integration test that wires
`is_channel_allowed → subproduct_access.has_subproduct_access`
end-to-end.

**Why it matters:** the subproduct WS channels are the live-tick fan-out
for the paid dashboards — these are the *purchased* product surface.
A paid user buys an entitlement, opens the dashboard, the dashboard
opens `/ws`, subscribes to `subproduct:trading-intel`, and gets a
`{"op":"denied","channel":"subproduct:trading-intel"}` back. The page
silently falls back to polling (or just stops updating). The user
attributes it to "the site being slow," not to entitlement breakage.

**Fix:** either load the full row in `_user_from_ws`:

```python
row = db.get_user_by_id(session["user_id"])
return dict(row) | {"user_id": session["user_id"], ...}
```

…or change the wrapper to fetch the row inside the channel layer:

```python
def _has_subproduct_access(user: dict, slug: str) -> bool:
    from subproduct_access import has_subproduct_access as _check
    row = db.get_user_by_id(user.get("user_id"))
    return bool(_check(row, slug))
```

The latter is the smaller change and keeps `_user_from_ws` thin. Add an
integration test that does **not** mock `_has_subproduct_access`.

---

### HIGH-2 — No max-message-size cap on inbound WS frames (DoS)

**Where:** `gateway/realtime/routes.py:224-243` + uvicorn config in
`scripts/systemd/narve-gateway.service:12`.

**What:** The receive loop is:

```python
async for raw in ws.iter_text():
    now = time.time()
    recent.append(now)
    ...
    msg = json.loads(raw)
```

There is no length check on `raw` and no `--ws-max-size` flag in the
systemd unit:

```ini
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 --app-dir gateway
```

uvicorn's default for `websockets`-protocol implementation is 16 MiB
per frame; `wsproto` historically has no cap. A single 16 MiB frame
through `json.loads` blocks the event loop for hundreds of ms to
seconds. Even the rate limit doesn't help — the 30-msg/s rolling
bucket is checked *after* `iter_text` yields the frame, which means
the worker has already paid the receive + JSON-parse cost before we
reject the next message.

**Threat:** authenticated user (i.e. anyone who can register a free
account) opens 3 sockets, fires one giant frame on each every second.
Three workers' event loops are pinned. Other users see request hangs;
admin observability page itself becomes unresponsive.

**Fix:**

1. Pass `ws_max_size=65536` (64 KiB is generous — the largest legitimate
   client frame is a subscribe op which is < 200 bytes) on the
   `uvicorn.run` call in `gateway/server.py:8791` **and** in the
   systemd unit.
2. Defensive guard in the loop itself:
   ```python
   if len(raw) > 64 * 1024:
       await ws.close(code=1009, reason="Message too big")
       break
   ```

`1009` is the standard "message too big" close code.

---

### MED-1 — No server-originated ping; idle sockets hold slots until TCP keepalive (resource)

**Where:** `gateway/realtime/routes.py:213-275`.

**What:** The server responds to client pings but never sends one.
There is no `asyncio.wait_for` around `iter_text`, no
`server_heartbeat_interval`, no `--ws-ping-interval` on uvicorn. An
authenticated client that completes the upgrade, subscribes to zero
channels, and goes silent will keep its slot until:

- the client disconnects cleanly (won't happen — adversarial),
- the OS TCP keepalive timer fires (Linux default: ~2 h),
- or the process restarts.

`MAX_CONNECTIONS_PER_USER=3` caps it per account but the user table is
public-signup; a botnet of N accounts at 3 sockets each yields 3N
zombie slots. With Hub state in-process, eventually the per-process
file-descriptor table is the ceiling.

**Fix:** wrap the receive in a timeout, e.g.:

```python
PING_EVERY = 30        # seconds
PONG_DEADLINE = 60     # seconds without ANY frame from client

last_seen = time.time()
async def reader():
    nonlocal last_seen
    async for raw in ws.iter_text():
        last_seen = time.time()
        ...

async def pinger():
    while True:
        await asyncio.sleep(PING_EVERY)
        if time.time() - last_seen > PONG_DEADLINE:
            await ws.close(code=1001, reason="idle")
            return
        await ws.send_json({"op": "ping", "server_ts": int(time.time()*1000)})
```

Or simpler: pass `--ws-ping-interval=30 --ws-ping-timeout=20` to uvicorn
so the websockets library handles control-frame pings at the protocol
layer. That covers idle-disconnect on its own; the in-app `op:"ping"`
JSON ping can stay for client-side RTT measurement.

---

### MED-2 — `_user_from_ws` reads `x-forwarded-for` from any peer; spoofable IP in audit logs (logging integrity)

**Where:** `gateway/realtime/routes.py:174`:

```python
ip = (ws.client.host if ws.client else None) or ws.headers.get("x-forwarded-for", "")
```

Compare to `gateway/server.py:1774` (`_get_client_ip`):

```python
peer = (request.client.host if request.client else "") or ""
if peer in _TRUSTED_PROXY_HOSTS:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip: return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff: return xff.split(",")[0].strip()
return peer or "unknown"
```

The realtime path uses `or` between `ws.client.host` and the XFF header.
Since `ws.client.host` is almost always present (loopback 127.0.0.1
because we sit behind the Cloudflare Tunnel), the `or` short-circuits
and XFF is *usually* ignored. But:

- If a client somehow has `ws.client.host == ""`, XFF is read unconditionally
  with no trust check on the peer.
- Worse, it reads the **entire** XFF header rather than the leftmost
  entry, so the IP value logged into `realtime_connection_events` can
  be `"1.2.3.4, 5.6.7.8, 9.10.11.12"`. That breaks downstream IP-based
  abuse triage.

**Fix:** call `srv._get_client_ip(...)` instead. (It already exists; reuse
it. Note `_get_client_ip` takes a `Request`; either expose a sibling
that takes the headers + peer, or build a thin shim that does the same
trust-then-leftmost logic against `ws.headers` and `ws.client.host`.)

---

### MED-3 — `evict_oldest_for_user` returns the eviction list but the cap can race past the limit (concurrency)

**Where:** `gateway/realtime/hub.py:80-92` + `gateway/realtime/routes.py:191-206`.

**What:** The eviction sequence is:

1. `evict_oldest_for_user(user_id, MAX_CONNECTIONS_PER_USER)` reads
   `self.user_conns[user_id]` under the lock, **but does not modify it**
   — it only returns a list to close.
2. Outside the lock, the route closes each old ws and calls
   `unsubscribe_all` (which *does* take the lock and removes from
   `user_conns`).
3. After eviction, `ws.accept()` then `register_connection(ws,...)`
   (which appends to `user_conns[user_id]` under the lock).

If user U holds 3 sockets and opens two new ones concurrently (browser
re-connect + native app, say), both coroutines can:

- enter `evict_oldest_for_user` and each compute `to_close = [oldest_1]`,
- both attempt to close `oldest_1` (idempotent — fine),
- both run `unsubscribe_all(oldest_1)` (one will silently no-op),
- both call `register_connection`, so `user_conns[U]` ends at 4
  rather than the intended 3.

The window is small, but the public WS endpoint is asynchronous and
this is the kind of thing that's exploitable from a single Chrome tab
opening N sockets in a Promise.all loop.

**Fix:** make `evict_oldest_for_user` atomic — perform the slice
removal from `user_conns` *inside* the lock and return the closed
list. Or, simpler: enforce the cap inside `register_connection`
itself, returning the list of sockets the caller should now close.

---

### MED-4 — Auth check + `ws.accept()` rely on cookie session but skip the impersonation cookie unwrap (auth correctness)

**Where:** `gateway/realtime/routes.py:104-146` (`_user_from_ws`).

**What:** Docstring at line 13 says: *"Impersonation cookie respected
(admin session is still tied to the ADMIN user for the purpose of
`user:{id}` channel gating)."*

But the implementation only walks `db.get_session(token)`. It does
**not** read the `narve_impersonation` cookie, nor does it consult
the impersonation table. So the doc's claim — that an impersonating
admin would still resolve as the admin user — is actually correct
*by accident*: `db.get_session` returns the admin's session row, and
the impersonation overlay only matters in `current_user(request)`
which the HTTP middleware applies.

The good news: this is consistent with the channel rule for `user:{id}`
(only the literal user can subscribe to their own channel) — an
impersonating admin won't be able to subscribe to the target's
`user:{id}` channel via WS, which is the desired property.

The risk: any future change that does propagate impersonation into
`_user_from_ws` will silently break that property. The current code
relies on documentation rather than enforcement. Add an explicit
test that the WS endpoint never resolves a session to anything other
than the cookie-session's `user_id`, and a code comment in
`_user_from_ws` that says "DO NOT honour impersonation here."

---

### LOW-1 — `_log_event` is synchronous SQLite inside the WS hot path (latency)

**Where:** `gateway/realtime/routes.py:149-166`. Called on every
connect / disconnect / subscribe / unsubscribe / denied.

The docstring concedes "if this ever shows up as hot on the profile we
can enqueue via the job queue." Subscribe is the highest-volume of
those: a dashboard tab can subscribe to 10-20 channels at startup.
At our current single-worker SQLite gateway, a 5-10 ms write on a busy
SQLite is real. Use the existing job queue (`gateway/jobs/`) for the
audit rows; they're observability, not correctness.

---

### LOW-2 — Broadcast envelope leaks `ts` to clients but never signs or sequences it (replay)

**Where:** `gateway/realtime/hub.py:156-160`:

```python
envelope = {
    "channel": channel,
    "ts": int(time.time() * 1000),
    **message,
}
```

The comment at the top of `hub.py` says *"clients can reconcile order
across reconnects if they care to"*, but there's no per-channel
sequence number, just a wall-clock `ts`. Two messages emitted within
the same millisecond will be indistinguishable to a client trying to
de-dupe a re-subscription. Tighter: monotonic per-channel sequence
embedded in the envelope. Low because no security implication; just a
reliability foot-gun for clients that try to be clever.

---

### LOW-3 — `emit_*` helpers swallow every exception silently (debuggability)

**Where:** `gateway/realtime/broadcast.py:26-44`.

`_schedule` does `loop.create_task(coro)` and the docstring promises
"never raises into the caller." That's fine for sync write paths, but
the task itself is fire-and-forget — there is no `task.add_done_callback`
that logs exceptions from inside `hub.broadcast`. A bug that makes
broadcasts raise (e.g. a future `after_broadcast` hook that does a
forbidden DB call from inside the event loop) will be invisible.

Add `task.add_done_callback(_log_if_exc)` or equivalent.

---

### LOW-4 — `unsubscribe_all` decrements `disconnect_reasons[reason]` whether the user reached this path via clean close, eviction-rep-replace, or broadcast-send-failure (metric noise)

**Where:** `gateway/realtime/hub.py:109-125` + `routes.py:194-203`.

Eviction calls `unsubscribe_all(old_ws, reason="replaced")`, then
broadcast-send-failure calls it with `reason="broadcast_send_failed"`,
then the route finally calls it again with `close_reason` on the
normal disconnect path. For a connection that gets evicted, both
"replaced" *and* the eventual finally-block "client_closed" can fire.
Result: `disconnect_reasons` over-counts. Low because admin panel uses
this as a directional signal, not a billable metric.

Fix: short-circuit the second call when `ws_meta` has already been
popped.

---

### INFO-1 — Channel allow-list is closed, well-tested for namespace shapes

**Where:** `gateway/realtime/channels.py:33-106`.

`is_channel_allowed` is a strict allow-list with a regex-validated
subject and explicit per-namespace gating. `subscribe` on the route
side fails closed on any unknown shape. This is the right design;
keep it that way. Documenting as INFO so future maintainers know not
to relax the regex on the "lazy market channel" justification — the
`market` namespace IS already lazy, that's where the looseness lives.

---

### INFO-2 — In-process Hub is a deliberate single-worker constraint

The `__init__.py` header explicitly calls out: *"If/when we scale to
multi-worker we'll swap `Hub` for a Redis-backed pub/sub with the
same interface."*

Worth recording in this audit because the per-process `Hub` means the
`MAX_CONNECTIONS_PER_USER=3` limit, the message-rate bucket, and the
admin stats are all per-worker, not per-cluster. A horizontal-scale
deploy would silently let a user open 3×workers connections. The doc
already names this; the fix is a multi-worker migration, out of scope
for this audit.

---

### INFO-3 — Origin check in dev is bypassed

`_ws_origin_allowed` returns `True` when `IS_PRODUCTION` is false. This
is documented and intentional. Worth noting for any developer running
`PRODUCTION=1` locally for a test — they will need to fake an Origin
header that matches `ALLOWED_DOMAINS`.

---

## Cross-cutting observations

- **Test coverage gap:** `gateway/tests/test_realtime.py` exercises the
  channel allow-list, hub mechanics, and `emit_*` helpers, but never
  drives the route through a real `WebSocketTestClient` and never
  hits the real subproduct-access path. The HIGH-1 bug is invisible
  to the current suite.
- **No coverage of message-size, idle-timeout, or eviction-race:** the
  three resource-side issues above all share the property that there
  are no tests checking the bound is enforced. Add unit-level tests
  for each before relying on the limit in production.
- **Admin observability is admin-only and routed through
  `_require_admin_user`:** route registration looks correct
  (`include_in_schema=False`, GET-only). No issue found in that path.
- **`emit_credibility_update` fan-out scope:** the helper emits to
  both `market:{slug}` and `feed:global` — `feed:global` is gated by
  `feed == "global"` only, which means every authenticated user sees
  every credibility update for every source. Confirmed against the
  brief's "fan-out scope" requirement: this is by design; the payload
  contains no PII or private data.
- **`emit_capture_attempt` fan-out scope:** emits only to
  `admin:security` which is gated by `admin_level >= 1`. Good.
- **`emit_notification` fan-out scope:** emits to `user:{user_id}`,
  which is gated to that single user. Good — confirms private
  notifications aren't leaking.

---

## Pre-release notes (out of scope per hard rule, **not** counted above)

None — every finding above applies to production behaviour. The
pre-release banner is not relevant to the WS layer's security or
correctness surface.

---

## Closing

The hub design is clean and the channel allow-list is well-shaped. The
sharpest issue is the subproduct-channel auth bug (HIGH-1) — it is a
silent correctness break for paid users that CI cannot see because the
only test mocks the call. Fix that, set a `ws_max_size`, and wire a
server-originated ping; the rest are tidy-ups.
