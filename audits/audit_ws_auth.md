# Adversarial audit — WebSocket auth flow

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Constraints: synchronous bash only; pre-release page (`gateway/static/prerelease.html`, `gateway/static/pages/prerelease.css`, `gateway/pwa_middleware.py` critical CSS) out of scope and not read or touched.

## Scope

Two WebSocket entry points exist in production:

| Endpoint | File | Purpose |
|---|---|---|
| `/ws` (realtime hub) | `gateway/realtime/routes.py:172` (`ws_endpoint`) | Single multiplexed pub/sub socket: market ticks, predictions, notifications, credibility, admin security events |
| `/{full_path:path}` (subdomain proxy) | `gateway/server.py:8539` (`websocket_proxy`) | Catch-all reverse-proxy WS to dashboard upstream services (sports, weather, crypto, …) on subdomains |

Focus, per brief:

1. Cookie-based auth across origins
2. Channel allowlist
3. Message-payload validation
4. Server-side scoping by user

Supporting modules read: `gateway/realtime/__init__.py`, `gateway/realtime/channels.py`, `gateway/realtime/hub.py`, `gateway/realtime/broadcast.py`, `gateway/tests/test_realtime.py`, `gateway/subproduct_access.py`, `gateway/subproduct.py`, `gateway/queries/auth.py` (`get_session`, `SESSION_TTL`), `gateway/auth/cookies.py`, `gateway/auth/guards.py`, `gateway/auth/middleware.py`, `gateway/server.py` (`current_user`, `_require_admin_user`, `set_session_cookie`, impersonation middleware, `_gate_cookie_is_valid`, `ALLOWED_DOMAINS`, `COOKIE_NAME`, dev-bypass helpers, `websocket_proxy`).

## What's right (so the findings have context)

- Both endpoints reject cross-origin upgrades in production. `routes.py:70` and `server.py:8568` parse `Origin`, lowercase the hostname, and require `host == apex or host.endswith("." + apex)` for at least one `apex in ALLOWED_DOMAINS`. The leading `.` is correct — `evilnarve.ai` does **not** match `.narve.ai`.
- Production also rejects missing-Origin upgrades (`server.py:8583`, `routes.py:79` returns `False` on empty origin so the caller closes the socket).
- Session cookies are `HttpOnly; SameSite=Lax; Secure` in production (`server.py:2273` for `pm_gateway_session`, `auth/cookies.py:122` for `narve_session` which adds `SameSite=Strict`).
- Subscribe-side allowlist is reachable: every channel goes through `is_channel_allowed(user, channel)` (`channels.py:58`) before `hub.subscribe`. The handler explicitly rejects unknown namespaces (`channels.py:106` — `return False`).
- `user:{user_id}` channel uses strict integer equality (`channels.py:92` — `int(subject) == int(user["user_id"])`), not a regex or prefix match. Owner-only enforcement is sound.
- `admin:security` requires `admin_level >= 1` (`channels.py:100`).
- Hub fan-out attaches the channel name server-side (`hub.py:156` — `envelope = {"channel": channel, ...}`), so a client cannot fake which channel a message came from.
- Per-user concurrency cap (3), per-conn channel cap (50), per-conn 30 msg/sec rate cap implemented in `routes.py:191-203, 251, 226-233`.
- Auth tokens are NEVER read from query string. `routes.py:11` docstring spells this out and the implementation honours it — only `ws.cookies` are inspected. So `wss://narve.ai/ws?token=…` cannot be used to bypass cookie auth.
- Hardened-session migration: legacy `pm_gateway_session` is the cookie the WS code resolves. The hardened `narve_session` middleware doesn't run on WS upgrades (FastAPI only runs HTTP middleware on HTTP requests), but the HTTP `current_user()` flow still prefers `narve_session` and the legacy cookie remains valid until the rollout window ends.
- `_log_event` (`routes.py:149`) captures every connect/disconnect/subscribe/denied event into `realtime_connection_events` — gives the admin panel + the audit log a record of every channel attempt, including denials.

## Findings

Ranked by exploitable impact. No CRITICAL findings; the two HIGH items are correctness-with-security-fallout, not direct compromise paths.

### HIGH 1 — `subproduct:*` channel allowlist is **silently broken** (always denies)

**File:** `gateway/realtime/channels.py:48-55`

```python
def _has_subproduct_access(user: dict, slug: str) -> bool:
    try:
        from subproduct_access import has_subproduct_access as _check
        return bool(_check(user.get("user_id"), slug))   # passes int
    except Exception:
        return False
```

But `subproduct_access.has_subproduct_access(user_row, subproduct_slug)` (`gateway/subproduct_access.py:100`) expects a **sqlite3 Row (or dict) with `subproduct_subscriptions` JSON**, not a user_id. Walk through the called code with `user_row=7`:

- `_pro_or_better(7)` → calls `_field(7, "plan", "")` → the `row[name]` access on an `int` raises `TypeError` → caught → returns default `""` → not pro/enterprise → `False`.
- `_blob_entry(7, slug)` → `_field(7, "subproduct_subscriptions", "")` → same TypeError → `""` → `if not raw: return None`.
- `entry = None` → `return False`.

Result: **every subscribe to `subproduct:*` is denied**, including for users with valid active subscriptions. The denial is logged as `reason="auth"` in `realtime_connection_events`, not as a server bug, so it looks like a normal auth failure.

The wrong helper is being imported. There are **two** `has_subproduct_access` symbols in the codebase:

- `subproduct.py:355` — `has_subproduct_access(user: dict, slug, *, has_active_subscription, has_pro_plan)` — takes a **dict** and dependency-injected checkers. This is what `channels.py` thinks it's calling.
- `subproduct_access.py:100` — `has_subproduct_access(user_row, slug)` — takes a **row**. This is what it actually gets.

The test that should have caught this (`tests/test_realtime.py:86-90`) patches `realtime.channels._has_subproduct_access` directly, so the broken wrapper is never exercised. Coverage gap as much as a logic bug.

**Severity rationale:** denial of service for paying subproduct subscribers — they can't get realtime updates on the dashboards they paid for. Not a confidentiality/integrity break; rated HIGH for revenue impact + the silent-failure mode (denial counter ticks like a normal auth fail, no monitoring alarm).

**Fix sketch:**

```python
def _has_subproduct_access(user: dict, slug: str) -> bool:
    try:
        import db
        from subproduct_access import has_subproduct_access as _check
        row = db.get_user_by_id(user.get("user_id"))
        return bool(_check(row, slug)) if row else False
    except Exception:
        return False
```

Plus a real test that calls `is_channel_allowed` with a fixture user that has an active subproduct in `subproduct_subscriptions` JSON, no mocking.

---

### HIGH 2 — Impersonation comment in `channels.py:86-92` describes a property the code does not enforce

**File:** `gateway/realtime/channels.py:84-92`

```python
if ns == "user":
    # Only the user themselves. Impersonators are logged in as the
    # ADMIN user (current_user returns the admin row when impersonating
    # via the narve_impersonation cookie), so this check correctly
    # prevents an impersonator from listening on the target user's
    # private channel.
```

The comment is wrong. `current_user(request)` at `server.py:2100-2127` returns the **TARGET user** dict during impersonation (`"user_id": t["id"]`, `"_impersonating": True`), not the admin. The reason an admin impersonating user 7 cannot subscribe to `user:7` over the WS is unrelated to this comment: it's that **the impersonation cookie + middleware never run on WebSocket upgrades**, so `_user_from_ws()` (`routes.py:104`) reads only the admin's own `pm_gateway_session` and resolves to the admin's row — not the target's.

This means the **invariant the comment claims doesn't actually hold** in the HTTP layer, where `current_user` is used for channel decisions in any future hub-over-HTTP fallback or admin tooling. Today the WS scoping is correct **by accident** (WS bypasses HTTP middleware so impersonation can't take effect at all). The moment someone:

- adds an HTTP SSE/long-poll fallback that uses `current_user` and `is_channel_allowed`,
- or fixes the WS path to honour the impersonation cookie for parity with HTTP,

an admin who impersonates user N can subscribe to `user:N` and read their private notifications. The comment makes this look safe; the next refactor will inherit a false security assumption.

**Severity:** HIGH because the misstatement is in security-load-bearing code and is durable across future refactors. Today the gap is theoretical; the moment WS auth is unified with HTTP it becomes real.

**Fix:** rewrite the comment to describe the *actual* protection ("WS upgrades skip HTTP middleware, so the impersonation cookie has no effect; an admin's own session is used") and either (a) leave a `# TODO: parity check needed if HTTP fallback is added` flag, or (b) add a defensive check that rejects `is_channel_allowed` for `user.get("_impersonating")` truthy unless the channel subject is the admin's own id.

---

### MEDIUM 1 — Hardened `narve_session` cookie is **not** read by either WS endpoint

**Files:** `gateway/realtime/routes.py:115-126`, `gateway/server.py:8600`

Both WS endpoints read only `COOKIE_NAME` (`pm_gateway_session`). The HTTP path prefers the hardened `narve_session` first (`server.py:2128-2141`, `auth/guards.py:32`) and falls back to the legacy cookie. There's no equivalent ordering on the WS side.

Consequence: any user whose only session cookie is `narve_session` (i.e., a user who logged in after the hardened-cookie rollout completes, or any browser that lost the legacy cookie) will be unable to open `/ws` or a dashboard WS in production. Today the legacy cookie is still set on every login (`set_session_cookie`, `server.py:2273`), so live traffic isn't broken — but the hardening migration is set up to drop the legacy cookie at some point, and the WS path will silently regress.

**Severity:** MEDIUM. Operational/regression risk, not a security break — the failure mode is "auth required" close on 4401, fail-closed.

**Fix:** in `_user_from_ws` and `websocket_proxy`, try `narve_session` (`db.validate_user_session`) before falling back to `pm_gateway_session` + `db.get_session`.

---

### MEDIUM 2 — `realtime_connection_events` write is synchronous sqlite inside the WS hot path

**File:** `gateway/realtime/routes.py:149-166`

`_log_event` calls `db.conn().execute(INSERT ...)` synchronously on every connect, disconnect, subscribe, and denied event. Inside an async WS handler, this blocks the event loop for the duration of the sqlite transaction. With the per-user cap of 3 concurrent WS and 50 subscribes per conn, a single client can issue 150 sync DB writes during a normal session, and a malicious client driving the message rate ceiling (30 msg/s) of which N are `subscribe`/`unsubscribe` can drive non-trivial event-loop stalls that throttle every other WS user.

**Severity:** MEDIUM. Not an auth bypass — but a DoS amplifier in a code path that already has cooperative rate limits. The docstring acknowledges the concern ("Kept synchronous because sqlite3 is already blocking and the row is tiny; if this ever shows up as hot on the profile we can enqueue via the job queue").

**Fix:** wrap with `asyncio.to_thread(...)` or post to the job queue. The fix is mentioned in the existing docstring — nothing controversial.

---

### MEDIUM 3 — `feed:global` carries source/market/content to every authenticated user with no per-tenant scoping

**Files:** `gateway/realtime/channels.py:94-96`, `gateway/realtime/broadcast.py:78-83, 121`

Every authenticated user can subscribe to `feed:global`. `emit_new_prediction` and `emit_credibility_update` (when `market_slug` is None) fan to `feed:global` with `source_handle`, `market_slug`, `category`, `direction`, `predicted_probability`, and `content` truncated to 280 chars.

Whether this is acceptable depends on the product intent: if "global feed" is a public-by-design firehose, fine. But the audit_subproduct_dashboards.md model has dashboards gated by per-subproduct subscription. A user with a `weather` subscription can still subscribe to `feed:global` and read predictions surfaced by sources/markets outside their entitlement, including any sensitive market the credibility engine emits a recompute for.

**Severity:** MEDIUM. Conditional — this is a product/spec question, not a code bug. If the intent is "all paying users see all predictions", the auth allowlist already encodes it. If not, `feed:global` needs either splitting (`feed:market:{slug}`) or an entitlement filter on the broadcast side.

**Fix:** decide with product. If scoping is required, gate broadcast on the receiver's entitlements at fan-out time, not just at subscribe time — `hub.broadcast` currently has no awareness of who the subscriber is.

---

### LOW 1 — Message-payload validation is type-only, not size-bound

**File:** `gateway/realtime/routes.py:235-275`

The endpoint does:

```python
try:
    msg = json.loads(raw)
except json.JSONDecodeError:
    await ws.send_json({"op": "error", "error": "invalid_json"})
    continue
if not isinstance(msg, dict):
    await ws.send_json({"op": "error", "error": "invalid_shape"})
    continue
```

Good — every client frame must be a JSON object. But:

- `raw` is whatever `ws.iter_text()` yields; there's no upstream WebSocket frame-size limit configured on the FastAPI/Starlette/Uvicorn stack. A client at 29 msg/s with multi-MB frames can drive CPU on `json.loads` and on the channel-string regex.
- `msg.get("channel")` is passed directly to `_SLUG_RE.match` (max 120 chars). Channel strings longer than 120 are rejected, but the JSON parse runs first.
- No `payload`-shape validation on `op`/`channel`/anything else. Unknown fields silently ignored, which is fine; the concern is the unbounded read.

**Severity:** LOW. The 30-msg/sec cap bounds frame rate; the missing piece is a per-frame size cap (e.g., 32 KB) and possibly a per-channel-name length pre-check before `json.loads`.

**Fix:** set `ws_max_size` on the uvicorn config (default is 16 MB) to ~32 KB, and short-circuit on `len(raw) > 32 * 1024` before `json.loads`.

---

### LOW 2 — `_log_event` failure path silently degrades the audit trail to debug-level

**File:** `gateway/realtime/routes.py:165-166`

```python
except Exception as exc:
    log.debug("realtime event log failed: %s", exc)
```

If the audit insert fails (DB locked, schema drift), the event is lost without surfacing as a warning. The audit trail is used as the forensic record for connection patterns + denied subscribes. Logging at `warning` would let the admin observability panel notice DB pressure.

**Severity:** LOW. Audit-trail completeness, not auth.

**Fix:** `log.warning(...)` instead of `log.debug(...)`. One-line change.

---

### LOW 3 — Subdomain WS proxy forwards arbitrary `full_path` and query string verbatim to the upstream dashboard

**File:** `gateway/server.py:8619-8623`

```python
target_port = dash_cfg["target"]
query = ws.url.query
upstream_url = f"ws://127.0.0.1:{target_port}/{full_path}"
if query:
    upstream_url += f"?{query}"
```

`full_path` is whatever the client put after the subdomain root (auth already passed at this point, including subscription check). The upstream dashboards trust this path. If any dashboard has an unsafe WS handler keyed on `full_path` (e.g., admin endpoints reachable via WS), the gateway's session check is the only gate.

**Severity:** LOW. The subscription check (`db.has_active_subscription(user_id, key)`) ensures the connecting user is entitled to the subdomain at all. Risk is a per-dashboard rather than gateway concern — flagged for the per-dashboard audits to track.

**Fix:** out of scope for this audit; ensure each dashboard's WS handler treats `full_path` as untrusted and does its own user-id scoping based on a header forwarded by the proxy (the proxy currently forwards none).

---

## Cross-cutting observations

- **No CSRF on WS upgrades.** Standard for WebSockets — the Origin check + SameSite=Lax cookie does the job. Both endpoints implement Origin validation in production correctly.
- **Dev bypass is bounded to localhost.** `_user_from_ws` and `websocket_proxy` both check `not IS_PRODUCTION` + `host in ("localhost", "127.0.0.1") or .endswith(".localhost")` before falling back to `ensure_dev_user`. Production cannot hit this branch even with a faked Host header (production has `IS_PRODUCTION=1`).
- **Per-IP rate limit is missing.** Per-user (3 concurrent) + per-conn (30 msg/s) caps exist, but a single IP can fan out across many newly-registered accounts. Not in the brief's scope (auth, allowlist, payload, scoping), flagged for completeness.
- **Hub broadcast is fire-and-forget through `_schedule` in `broadcast.py:26`.** A coroutine-creation race on `loop.create_task` outside the event loop could leak, but `try/except` wraps the failure and only `log.debug`s it. Same audit-trail caveat as LOW 2.

## Severity counts

- CRITICAL: 0
- HIGH: 2
- MEDIUM: 3
- LOW: 3

## Top 3 (act first)

1. **HIGH 1 — `subproduct:*` is silently broken.** Wrong import: `realtime/channels.py:50-52` passes an `int` to a helper that expects a sqlite Row. Every subproduct subscribe is denied. Paying users can't get realtime updates on their dashboards, and the test suite mocks the wrapper so this never gets exercised.
2. **HIGH 2 — Misleading impersonation comment.** `channels.py:86-92` describes a protection (`current_user` returns the admin during impersonation) that the codebase does not provide; `current_user` returns the target. WS auth is currently correct only because HTTP middleware doesn't run on upgrades — a coincidence, not a property. Next refactor that unifies WS/HTTP auth or adds an HTTP fallback inherits a private-channel-leak vector.
3. **MEDIUM 1 — Hardened `narve_session` cookie isn't honoured by either WS endpoint.** Production traffic is fine today because the legacy `pm_gateway_session` is still issued; the moment the migration drops the legacy cookie, every WS authenticates as anonymous and closes with 4401.
