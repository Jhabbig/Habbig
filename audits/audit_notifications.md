# Audit — notification + push routes

**Date:** 2026-05-15
**Auditor:** automated adversarial pass
**Scope:** every notification-bearing route file in `gateway/*_routes.py` reachable from `grep -rln "notification\|push\|alert"`. Specific focus: VAPID key handling, push-subscription endpoint host validation (SSRF), notification-preferences ownership, unsubscribe-token validation.
**Hard rules respected:** no code changes; synchronous bash only.

---

## 0. Result summary

| Severity | Count |
|----------|------:|
| Critical | 0 |
| High     | 3 |
| Medium   | 4 |
| Low      | 4 |
| Info     | 2 |

**Top 3 findings:**

1. **H1 — `notification_routes.py` is wholly broken: every handler references DB helpers that do not exist.** `db.create_notification`, `db.get_notifications`, `db.get_unread_count`, `db.mark_notification_read`, `db.mark_all_notifications_read`, `db.archive_notification`, `db.delete_notification`, `db.get_notification_preferences`, `db.set_notification_preferences`, `db.notification_type_enabled`, and the `db.NOTIFICATION_TYPES` constant are imported/called but undefined anywhere reachable from `db.py` (no direct def, no `queries/*` re-export). `feedback_routes.py:108-110` explicitly comments: *"that helper references NOTIFICATION_TYPES + a DB function that don't exist in this branch yet"*. Effect: the bell list, unread count, mark-read, archive, delete, prefs GET/PATCH, and SSE-fed broadcast path all 500 (the route catches exceptions and returns empty/0 for some, but PATCH/DELETE bubble; the SSE broadcast path never persists). User-visible: bell empty, "mark all read" silently no-ops, preference toggles never save.

2. **H2 — `PATCH /api/notifications/preferences` is currently CSRF-unprotected** (and so is `DELETE /api/notifications/{id}`). Per `server.py:1137-1139`, `CSRF_PATCH_DELETE_ENFORCE` defaults to `false`. The middleware (`server.py:1363-1375`) only logs a soft-warn on a missing/invalid CSRF token for PATCH/PUT/DELETE — it lets the request through. The notification-prefs PATCH accepts `inapp_enabled` / `push_enabled` / `email_enabled` bools and a `types` dict; a cross-origin attacker can silently disable a victim's push + email by getting them to visit a hostile page (no user interaction beyond a `fetch` from the page's JS) so long as the victim has a cookie session. The module docstring at `notification_routes.py:21-23` claims "Hard DELETE and POST operations additionally enforce CSRF via the global ``CSRFMiddleware`` (no exemption)" — that's true for POST only; PATCH and DELETE here are in Phase-1 soft-warn mode.

3. **H3 — `email_system/unsubscribe.py` falls back to the literal string `"narve-unsubscribe"` as its HMAC secret when `GATEWAY_COOKIE_SECRET` is unset, with no production guard.** Compare `auth/cookies.py:62-70` which raises `RuntimeError` in production when the env var is missing. `email_system/unsubscribe.py:21-22` has no such guard:
   ```python
   def _secret() -> bytes:
       return os.environ.get("GATEWAY_COOKIE_SECRET", "narve-unsubscribe").encode()
   ```
   If a deployment ever ships without the env var (misconfigured systemd unit, container missing the secret mount, dev container promoted to staging — common patterns), every unsubscribe token in the wild becomes forgeable: an attacker who knows any victim's email can craft `_sign(raw + email + "marketing")` with the hard-coded secret and POST `/unsubscribe?token=...`, flipping `users.email_marketing` and `email_digest` off. There is no rate-limit on `/unsubscribe` (`server_features.py:97`), so iteration over a recovered email list (e.g. from a partial dump) is unbounded.

No code changes were made.

---

## 1. Inventory

`grep -rln "notification\|push\|alert" gateway/*_routes.py` returns 17 files. Of those, three are squarely "notification routes":

| File | Lines | Purpose |
|---|---:|---|
| `gateway/notification_routes.py` | 298 | In-app bell: list, unread_count, read, read-all, archive, delete, prefs GET/PATCH, SSE stream, `/notifications` HTML page. |
| `gateway/push_routes.py` | 222 | Web Push: VAPID key, subscribe, unsubscribe, test-self, list. |
| `gateway/alerts_routes.py` | 196 | Market-mover alert rules (CRUD) + `/api/feed/movements`. Not push/in-app — these are background-job triggers. |

Supporting modules read but not directly routed:

| File | Role |
|---|---|
| `gateway/notifications.py` | Preference-gated insert + SSE fan-out. `create_notification` is the canonical entry point for other code that wants to ring the bell. |
| `gateway/push.py` | VAPID keypair management, `push_subscriptions` CRUD, pywebpush sender. |
| `gateway/email_system/unsubscribe.py` | Signed-token unsubscribe for marketing/digest emails. Reached from `server_features.py:97` `/unsubscribe`. |
| `gateway/status_routes.py` | `/api/status/(un)subscribe`, `/status/unsubscribe` — public status-page email subscription, separate token scheme (random, no HMAC). |
| `gateway/public_routes.py` | `/api/newsletter/unsubscribe` — accepts plain `?email=` query string with no token. |

Other files matched by the grep but tangential (admin cost alerts, AI chat alerts, billing receipt alerts, etc.) were skimmed and contain no auth-relevant notification logic.

---

## 2. VAPID key handling (`gateway/push.py`)

### V1 — production guard: PASS (info)
`push.py:80-88` correctly refuses to generate / read an on-disk fallback when `PRODUCTION=1`:
```python
if _is_production:
    raise RuntimeError(
        "PUSH_VAPID_PRIVATE_KEY_PEM must be set in production; "
        "the filesystem fallback at ~/.narve/vapid.key is "
        "disabled when PRODUCTION=1."
    )
```
This matches the convention in `auth/cookies.py:62-70`. Good.

### V2 — dev key file permissions: PASS (info)
`push.py:101-110` creates the dev keypair at `~/.narve/vapid.key` with `mode=0o700` parent and `0o600` file, with the explicit comment about the umask race window (`touch → chmod 0o600 → write_text → chmod 0o600 again`). Defensive and correct.

### V3 — VAPID public key cache header: LOW
`push_routes.py:104` sets `Cache-Control: public, max-age=3600` on the VAPID public-key endpoint. Fine. But the route is unauthenticated (correctly — browsers need it before `pushManager.subscribe()`) and is rate-limited at `60/min` per IP (`push_routes.py:89`). Acceptable. **Severity: Info.** Note that an attacker who scrapes the public key learns *nothing sensitive* — that's its purpose — but the unbounded `Cache-Control: public` allows CDN/intermediate caching, which is fine because the key never changes.

### V4 — VAPID subject email: PASS (info)
`push.py:39` reads `PUSH_VAPID_SUBJECT` from env, defaulting to `mailto:hello@narve.ai`. Some push services reject the request if the `sub` claim's domain doesn't match the subscriber's origin, but that's a deliverability concern, not a security one.

### V5 — private key never leaves the module: PASS (info)
`_cached_private_pem` is module-private; nothing in `push_routes.py` exposes it. The only consumer is `_send_one` (`push.py:212`) which hands it to pywebpush. Good.

---

## 3. Push subscription endpoint — SSRF / push-spam (`gateway/push_routes.py`)

### S1 — endpoint host allowlist: PASS (info)
`push_routes.py:40-77` enforces an explicit allowlist of canonical push-service hosts (FCM, Mozilla Autopush, Apple WebPush, Microsoft WNS) and rejects anything else with `422`. The wildcard logic at line 72-76 is correct (suffix match with a length check so `notify.windows.com` itself doesn't bypass via `evilnotify.windows.com.attacker.tld`, and `*.notify.windows.com` requires `host > suffix` so the bare suffix doesn't match). The scheme check at `:63` enforces `https`. The redundant `endpoint.startswith(("https://",))` check at `push_routes.py:136-137` is defensive parity.

This closes the obvious SSRF: an attacker can't get the server to fire VAPID-signed POSTs at `http://169.254.169.254/latest/meta-data/` or arbitrary internal endpoints.

### S2 — endpoint length / payload size: MEDIUM
The `endpoint` field has no length cap before it's persisted to `push_subscriptions.endpoint`. The schema (migration 034) presumably uses `TEXT` (SQLite). A 1 MB endpoint URL would happily land in the DB and then be retried on every notification send (`push.py:266`). Not a remote vector — only an authenticated user can submit one — but a 5-tier subscription user could fill the table with 10000 × 1 MB rows = 10 GB before `MAX_RULES_PER_USER`-style limits kick in (there is no per-user push-subscription cap). **Recommendation:** enforce `len(endpoint) <= 2048` and a per-user limit (~10 active subscriptions).

### S3 — keys field length: MEDIUM
Same issue, narrower scope: `p256dh` and `auth` should be base64url strings of fixed length (P-256 uncompressed = 65 raw bytes = 88 base64url chars; auth = 16 bytes = 22 chars). The route accepts any string. **Recommendation:** length-cap to e.g. ≤ 256 chars each and (optionally) regex-validate `^[A-Za-z0-9_-]+={0,2}$`.

### S4 — User-Agent reflection: PASS (info)
`push_routes.py:141` truncates UA to 500 chars before storage. Good; mitigates log-injection / DB-bloat via the `User-Agent` header.

### S5 — idempotent overwrite rebinds owner: PASS (info)
`push.py:158-175` uses `ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id`. Reasonable for the "browser handed off between accounts" case. **Subtle risk:** if a malicious authenticated user submits another user's known endpoint, they take ownership of it (and the next push for that endpoint will go to the attacker's session). But: the endpoint URL is itself an unguessable per-browser secret (FCM-style endpoints embed a random 100+ char token), and the keys are required (`p256dh`, `auth`) to actually decrypt anything; without the original `auth` key the push service still routes to the *original* browser, which would then drop the payload as undecryptable. Net effect: at most a denial of *future* pushes to the original subscriber, which the original subscriber can repair by re-subscribing. **Severity: Low.** Documenting for completeness — `push.py:148-154` comment acknowledges this design intent.

---

## 4. Notification preferences — ownership + CSRF (`gateway/notification_routes.py`)

### N1 — `before_id` cross-user enumeration check: PASS (info)
`notification_routes.py:78-98` explicitly validates that an attacker-supplied `before_id` keyset cursor belongs to the requesting user before passing it to the DB layer, with the comment noting the 404 mirrors "does not exist" to avoid an existence oracle. Good defence-in-depth pattern.

### N2 — single-notification ownership check: PASS (info)
`notification_routes.py:138-159` (mark-read), `:182-187` (archive), `:190-197` (delete) all filter by `(notif_id, user_id)` at the DB layer. The mark-read route additionally pre-checks ownership at the route layer with a 404 mirror.

### N3 — preferences GET/PATCH: BROKEN (HIGH, see H1)
The handlers call `db.get_notification_preferences` and `db.set_notification_preferences` which **do not exist** anywhere in `db.py` or `queries/*`. Both handlers will raise `AttributeError`. The PATCH catches the exception and returns `500 Failed to save preferences` (`notification_routes.py:232-234`); the GET does not catch and lets FastAPI's default 500 fire. **Top finding #1.**

### N4 — PATCH preferences CSRF: MISSING (HIGH, see H2)
Per the global middleware (`server.py:1137-1139, 1363-1375`), PATCH is in soft-warn mode and not enforced. **Top finding #2.** Practical exploit if the broken db helper were ever wired up: attacker page does
```js
fetch('https://narve.ai/api/notifications/preferences', {
  method: 'PATCH', credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({push_enabled: false, email_enabled: false})
});
```
and a logged-in victim visiting the page silently loses their notifications. The Origin check (`server.py:1348-1361`) catches *browsers that send Origin*, but for fetch initiated from a `text/html` document where `mode: 'no-cors'` or a `form` element submits, no Origin header is set on some legacy paths — and the inline middleware only blocks when `origin` is truthy *and* `IS_PRODUCTION` (`:1349`). The Origin defence is real but not bulletproof; CSRF token enforcement is the primary control and it is currently off.

### N5 — preferences body validation: PASS (info)
`notification_routes.py:217-229` validates JSON shape, accepts only the known keys, coerces bools, coerces the `types` dict's values to bools. Mass-assignment safe. (Once the underlying db helper exists.)

### N6 — SSE stream auth & cleanup: PASS (info)
`notifications.py:188-214` correctly subscribes on connect, yields heartbeats every 30 s, unsubscribes in `finally` so a disconnected client doesn't leak a queue slot. The queue is bounded to `maxsize=100` (`notifications.py:67`), and `_broadcast` drops on `QueueFull` with a log warning rather than blocking the producer. Good back-pressure design.

### N7 — SSE stream no rate-limit: PASS (info)
The route deliberately omits `@rate_limit` (commented at `notification_routes.py:244-247`). Since the per-connection queue caps memory and the connection itself is auth-gated, this is fine.

### N8 — `/notifications` HTML page: PASS (info)
Standard render. Redirects anonymous users to `/login?next=/notifications`. No template-injection vectors observed (uses `server.render_page` with named kwargs).

### N9 — rate-limit buckets are per-user: PASS (info)
The `_user_key` helper (`notification_routes.py:46-56`) buckets by user, with IP fallback for the unauth case. Standard.

---

## 5. Unsubscribe-token validation

The repo has **four distinct unsubscribe flows**, with different token schemes:

| Endpoint | Token scheme | Auth | Rate limit | Notes |
|---|---|---|---|---|
| `/unsubscribe?token=…&type=marketing` (`server_features.py:97`) | `email_system/unsubscribe.py` — HMAC-signed random | Public | **None** | Flips `users.email_marketing` / `email_digest`. Token format `raw.sig` with `sig = HMAC-SHA256(secret, raw+email+scope)[:32]` (128 bits, fine). |
| `/status/unsubscribe?token=…` + `/api/status/unsubscribe` (`status_routes.py:318, 330`) | `status_system/db.py:345-351` — 24-byte URL-safe random, no HMAC | Public, CSRF-exempt | **None** | Token is primary key. Forgery requires guessing 192 bits, infeasible. |
| `/api/push/unsubscribe` (`push_routes.py:157`) | None — accepts raw endpoint URL | Authenticated | 30/min/user | Filtered by `user_id` in DELETE. Safe. |
| `/api/newsletter/unsubscribe?email=…` (`public_routes.py:511`) | None — plain email | Public | 20/hour/IP | Always returns identical HTML. Below. |

### U1 — `email_system/unsubscribe.py` falls back to a hard-coded secret in production: HIGH (see H3)
`email_system/unsubscribe.py:21-22`:
```python
def _secret() -> bytes:
    return os.environ.get("GATEWAY_COOKIE_SECRET", "narve-unsubscribe").encode()
```
No production guard. **Top finding #3.** Compare `auth/cookies.py:62-70` which raises if the env var is missing in prod. The two HMAC users share the same env var, but the email-unsubscribe path falls open to a known fixed string while cookies fail closed. Fix: copy the `_is_production()` raise from `auth/cookies.py`.

### U2 — `/unsubscribe` has no rate limit: MEDIUM
`server_features.py:97-126` has no `@rate_limit` decorator. Combined with U1 (if it ever fired), an attacker could iterate forged tokens at line-rate. Even without U1, the absence of rate-limiting lets a token from a leaked email get replayed indefinitely — though replay just re-applies the same unsubscribe, so the practical exploit is enumeration-by-error-page. The page deliberately returns the same body for valid/invalid tokens (`server_features.py:120-124`), which limits the oracle, **but** the `?type=` parameter is reflected into the page (`scope_label` switch at `:107`) so a malformed `type=...` is observable. Low-impact but worth a `30/hour/IP` cap.

### U3 — `/api/newsletter/unsubscribe` accepts a raw email with no token: MEDIUM
`public_routes.py:511-559`. Anyone can POST any address and have it removed from the newsletter list. The handler is rate-limited at 20/hour/IP (`:527`), but a botnet or a single hostile actor with cheap IPs (residential proxies) can unsubscribe at scale. The handler intentionally returns the same HTML regardless to avoid enumeration (`:518-519`) — good — but the underlying state change (`db.unsubscribe_newsletter(email)`) is taken on the attacker's say-so. **Recommendation:** require a signed token or a double-opt-in confirmation step on unsubscribe (the signup flow already uses a confirmation token at `/api/newsletter/confirm`).

### U4 — status-page unsubscribe has no rate limit and no CSRF: LOW
`status_routes.py:318, 330` are CSRF-exempt (`server.py:1163-1164`) and have no `@rate_limit` decorator. The token is 192 bits of entropy so brute-force is infeasible; the impact of a successful unsubscribe is limited (status emails only). However, a high-volume attacker who somehow obtained a list of tokens could iterate without throttle. **Recommendation:** add a per-IP rate-limit (e.g. 30/min).

### U5 — `email_system/unsubscribe.py` HMAC compare uses `compare_digest`: PASS (info)
`email_system/unsubscribe.py:64` correctly uses `hmac.compare_digest`. Good.

### U6 — token-vs-row mismatch handled correctly: PASS (info)
`email_system/unsubscribe.py:52-66`: token lookup by exact match, then HMAC verification against the stored email + scope. Robust against token-substitution.

---

## 6. Cross-cutting

### X1 — Notification preferences table is dead code: HIGH (see H1)
Migration `026_notifications.py` creates `notification_preferences` but no production code reads or writes it (verified: grep of `set_notification_preferences` and `get_notification_preferences` finds only the broken callers in `notification_routes.py`). The whole bell feature shipped without the DB layer landing.

### X2 — `alerts_routes.py` direct DB bypass: LOW
`alerts_routes.py:37-40` uses `sqlite3.connect(_db_path())` directly rather than going through `db.conn()`. This bypasses any connection pooling, WAL configuration, busy-timeout, or PRAGMA setup performed in `db.py:258` `conn()`. Functionally works against SQLite but inconsistent with the rest of the codebase. **Recommendation:** route through `db.conn()`.

### X3 — `alerts_routes.py` no rate-limit on CRUD endpoints: LOW
The `MAX_RULES_PER_USER = 10` cap (`:26, :81-82`) limits *active* rules but not creation throughput; an attacker can churn `POST /api/alerts` → `DELETE /api/alerts/{id}` indefinitely, hammering the DB. **Recommendation:** add `@rate_limit(60/min/user)` on `create_rule`, `update_rule`, `delete_rule`.

### X4 — `alerts_routes.py` uses Form() — CSRF coverage depends on form-encoded content-type: PASS (info)
The CSRF middleware (`server.py:1329-1334`) parses `application/x-www-form-urlencoded` bodies for the `_csrf` form field, so the Form-based POST is covered. Verified.

### X5 — `notifications.create_notification` swallows broadcast failures: PASS (info)
`notifications.py:166-178` catches and logs both legacy SSE broadcast and realtime hub emit failures. The notification still persists. Good — a stuck SSE consumer can't take down a notification-trigger job. (Persistence is broken anyway per H1, but the failure-isolation pattern is correct.)

### X6 — Subscription churn delete handling: PASS (info)
`push.py:225-231` correctly deletes expired/gone push subscriptions on `404/410` from the push service. Standard hygiene.

### X7 — `push_subscriptions` per-user count is unbounded: LOW
Combined with S2/S3 — an authenticated user can submit arbitrarily many subscriptions (each with a unique `endpoint`, since the unique constraint is on `endpoint` not `(user_id, endpoint)`). **Recommendation:** cap at ~10 per user.

---

## 7. Severity rollup

| ID  | Severity | Title | File |
|-----|----------|-------|------|
| H1  | High     | All `notification_routes.py` DB helpers undefined; bell, prefs GET/PATCH, broadcast persist all 500 | `notification_routes.py`, `notifications.py`, `db.py` |
| H2  | High     | `PATCH /api/notifications/preferences` + `DELETE /api/notifications/{id}` CSRF not enforced (Phase-1 soft-warn) | `notification_routes.py`, `server.py:1137` |
| H3  | High     | `email_system/unsubscribe.py` falls back to constant `"narve-unsubscribe"` secret in production with no guard | `email_system/unsubscribe.py:21-22` |
| M1  | Medium   | Push `endpoint` length not capped; per-user subscription count unbounded | `push_routes.py:130-139` |
| M2  | Medium   | Push `p256dh`/`auth` length & charset not validated | `push_routes.py:131-135` |
| M3  | Medium   | `/unsubscribe` has no rate limit | `server_features.py:97` |
| M4  | Medium   | `/api/newsletter/unsubscribe` accepts plain email with no token | `public_routes.py:511` |
| L1  | Low      | `push.save_subscription` lets a user rebind another user's known endpoint (impact: future-push DoS for original subscriber) | `push.py:158-175` |
| L2  | Low      | `/status/unsubscribe` + `/api/status/unsubscribe` no rate limit | `status_routes.py:318, 330` |
| L3  | Low      | `alerts_routes.py` uses raw `sqlite3.connect` bypassing `db.conn()` | `alerts_routes.py:37` |
| L4  | Low      | `alerts_routes.py` CRUD endpoints have no rate-limit decorator | `alerts_routes.py:51-149` |
| I1  | Info     | VAPID prod guard, dev key file perms, public-key cache, HMAC compare_digest — all correctly implemented | `push.py`, `email_system/unsubscribe.py` |
| I2  | Info     | SSE subscriber lifecycle (lock, bounded queue, finally-unsubscribe) is correct | `notifications.py` |

---

## 8. Recommended near-term fixes (in order)

1. **Implement the missing `db.*` notification helpers** (H1) — biggest blocker. The migration exists; the schema is laid out; only the Python wrappers are missing. Until done, the bell feature is non-functional.
2. **Flip `CSRF_PATCH_DELETE_ENFORCE=true`** (H2) once telemetry from soft-warn logs confirms no first-party client still PATCHes without the header. Audit `gateway/static/` JS to ensure every PATCH/DELETE includes `x-csrf-token`.
3. **Add production guard to `email_system/unsubscribe.py:_secret()`** (H3) — mirror `auth/cookies.py:62-70`.
4. **Cap push `endpoint`, `p256dh`, `auth` lengths and per-user subscription count** (M1, M2).
5. **Rate-limit** the four public unsubscribe endpoints (M3, M4, L2).
6. **Replace `/api/newsletter/unsubscribe`'s plain email with a signed token** matching the `/unsubscribe` pattern (M4).
7. **Route `alerts_routes.py` through `db.conn()`** and add `@rate_limit` (L3, L4).

---

## 9. Files inspected (absolute paths)

- `/Users/shocakarel/Habbig/gateway/notification_routes.py`
- `/Users/shocakarel/Habbig/gateway/push_routes.py`
- `/Users/shocakarel/Habbig/gateway/alerts_routes.py`
- `/Users/shocakarel/Habbig/gateway/notifications.py`
- `/Users/shocakarel/Habbig/gateway/push.py`
- `/Users/shocakarel/Habbig/gateway/email_system/unsubscribe.py`
- `/Users/shocakarel/Habbig/gateway/server_features.py` (lines 85-150)
- `/Users/shocakarel/Habbig/gateway/public_routes.py` (lines 500-622)
- `/Users/shocakarel/Habbig/gateway/status_routes.py` (lines 300-360)
- `/Users/shocakarel/Habbig/gateway/status_system/db.py` (lines 340-460)
- `/Users/shocakarel/Habbig/gateway/server.py` (lines 1110-1395 — CSRF middleware + exempt lists)
- `/Users/shocakarel/Habbig/gateway/auth/cookies.py` (lines 55-90 — for `_secret()` comparison)
- `/Users/shocakarel/Habbig/gateway/db.py` (lines 858-970 — `queries/*` re-exports; no notification helpers found)
- `/Users/shocakarel/Habbig/gateway/feedback_routes.py` (lines 100-125 — corroborating comment about the missing db helpers)
- `/Users/shocakarel/Habbig/gateway/migrations/026_notifications.py`
