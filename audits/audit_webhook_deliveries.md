# Adversarial Audit — Outbound Webhook Delivery System

Date: 2026-05-15
Auditor: Claude (Opus 4.7)
Targets:
- `/Users/shocakarel/Habbig/gateway/webhooks.py` — delivery engine
- `/Users/shocakarel/Habbig/gateway/webhooks_routes.py` — settings + admin routes
- `/Users/shocakarel/Habbig/gateway/migrations/129_webhooks.py` — base schema
- `/Users/shocakarel/Habbig/gateway/migrations/179_webhook_hardening.py` — DLQ + circuit-breaker columns
- `/Users/shocakarel/Habbig/gateway/migrations/182_webhook_dlq_index.py` — DLQ index
- `/Users/shocakarel/Habbig/gateway/db.py` — CRUD + DLQ helpers (lines 1224-1453)
- `/Users/shocakarel/Habbig/gateway/tests/test_webhooks.py` — unit coverage

Commits anchored:
- `397e79c` — `feat(webhooks): retries + DLQ + circuit-breaker + anti-replay` (2026-05-14)
- `f98cdf6` — `perf(webhooks): partial index for DLQ list — first_failed_at DESC where unreqeued` (2026-05-14)

Scope as specified in the brief:
1. HMAC signing per-customer secret
2. Retry policy and exponential backoff
3. Dead-letter queue (migration 179 → `webhook_dead_letter`)
4. SSRF protection on user-supplied callback URLs (private-IP block, hostname allowlist)
5. Maximum delivery body size

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 3 |
| Medium   | 5 |
| Low      | 6 |
| Info     | 4 |
| **Total**| **18** |

## Top 3 findings (ranked by exploitability x impact)

1. **HIGH-1** — The SSRF guard in `_validate_url`
   (`webhooks_routes.py:75-114`) is **only enforced when
   `os.environ["PRODUCTION"] == "1"` exactly**, but the canonical
   `config.is_production()` helper accepts `"1"`, `"true"`, `"yes"` (any
   case). A deploy with `PRODUCTION=true` (which `config.py` happily
   treats as production) skips every private-IP/loopback/metadata check
   and accepts `http://127.0.0.1`, `http://169.254.169.254/latest/meta-data/`,
   etc. as legitimate webhook destinations. The check is also pure
   regex over the parsed `hostname` string — no DNS resolution — so even
   in correctly-configured prod the gateway honors *names* that resolve
   to RFC1918 / link-local / loopback at delivery time (`internal.foo.io`
   → 10.0.0.42 is fine by the regex). See HIGH-1.

2. **HIGH-2** — Per-subscription HMAC secrets are stored in
   `webhook_subscriptions.secret` as **plain TEXT** (migration
   `129_webhooks.py:34`, never re-encrypted by `179`). The column is
   served back to authenticated owners directly in the settings page
   listing and is readable by any admin via the admin webhooks view
   query (`db.list_all_webhooks` at `db.py:1252`). Any read access to
   `db.db` (backup leak, `admin_shell` query inspector, replication
   target) yields the signing key needed to forge any past/future
   delivery to that endpoint. The schema already has a precedent for
   Fernet-at-rest (`db.set_system_secret` at `db.py:1456+`), so the gap
   is intentional design rather than missing primitive. See HIGH-2.

3. **HIGH-3** — `_deliver_once` (`webhooks.py:182-185`) calls
   `httpx.AsyncClient.post()` with **no `max_redirects=0`, no
   `follow_redirects` override (httpx default is False, which is good),
   but also no `limits=` ceiling on response body size, no
   `transport=` restricted resolver, and a 10s timeout that is
   per-attempt (so a slow-loris sink can hold 3 concurrent goroutines
   for 30s + the 12s backoff per subscription)**. The biggest concrete
   issue: httpx's default is to fully buffer the response body, so a
   subscriber returning a gigabyte 200 OK forces the gateway to allocate
   a gigabyte and discard it. Combined with HIGH-1 above, a user who
   sets their webhook URL to a local-network sink can trivially DoS the
   gateway from inside the trust boundary. See HIGH-3.

---

## Findings

### HIGH-1 — SSRF guard bypassed by env-var case sensitivity, plus DNS-name SSRF in any environment

**Location:** `webhooks_routes.py:75-114` (`_validate_url`).

**What:**

```python
import os as _os
if _os.environ.get("PRODUCTION", "0") == "1":
    for pat in blocked_host_patterns:
        if re.match(pat, host):
            raise HTTPException(400, f"URL host not allowed: {host}")
if parsed.scheme == "http" and _os.environ.get("PRODUCTION", "0") == "1":
    raise HTTPException(400, "Production webhooks must use https://")
```

Two failures in one block.

(a) **`== "1"` is wrong.** The canonical helper `config.is_production()`
at `config.py:189-190` says:

```python
return os.environ.get("PRODUCTION", "0").strip().lower() in ("1", "true", "yes")
```

A deploy whose process env has `PRODUCTION=true` (the long-form
documented in `ENV_DEFAULTS_AUDIT.md` and accepted by every other
production gate in the codebase) is in production by every other
measure, but `_validate_url` will treat it as dev and accept
`http://localhost`, `http://169.254.169.254/`, `http://10.0.0.5/`, etc.

(b) **The blocklist is regex-on-hostname, never a DNS lookup.** The
pattern list at lines 98-106 only catches IP-literal hostnames. A user
can register `http://internal.acme.io/` where the A record resolves to
10.0.0.42 — `re.match("^10\\.", "internal.acme.io")` is False, validation
passes, and at delivery time httpx resolves and connects to RFC1918.
DNS rebinding is the explicit acknowledgement in the code comment at
line 96 ("we want users to notice their own mistake immediately, not
race a DNS rebinding attack later") — but the comment is exactly
backwards: the code chose the *weaker* defense, not the stronger one.
A real SSRF guard resolves at *delivery* time and aborts on private IP.

(c) **The pattern list itself is incomplete.** Missing host classes:

| Class | Example | Blocked? |
|---|---|---|
| AWS IMDS literal | `169.254.169.254` | yes (`^169\.254\.`) |
| GCP IMDS hostname | `metadata.google.internal` | **no** |
| Azure IMDS | `169.254.169.254` | yes (collides w/ AWS) |
| Docker daemon | `host.docker.internal` | **no** |
| Kubernetes API | `kubernetes.default.svc` | **no** |
| Decimal IP | `2130706433` (= 127.0.0.1) | **no** |
| Hex IP | `0x7f000001` | **no** |
| Octal IP | `0177.0.0.1` | **no** |
| IPv4-mapped IPv6 | `::ffff:127.0.0.1` | **no** (regex looks for `::1` exactly) |
| IPv6 link-local | `fe80::1` | **no** (regex covers `fc/fd` ULAs only) |
| IPv6 loopback w/ zone | `[::1%lo0]` | **no** |
| `0.0.0.0` | covered | yes |
| Trailing dot | `127.0.0.1.` | **no** |

httpx will follow most of these forms (it delegates to the OS
resolver/socket layer, which accepts the int/hex/octal forms on Linux).

**Attack:**

1. Attacker creates an account on production narve.ai.
2. Goes to `/settings/webhooks`, picks any event, and sets URL to
   `http://metadata.google.internal/computeMetadata/v1/instance/
   service-accounts/default/token` (or `http://2130706433/` on AWS).
3. Triggers any qualifying event (`best_bet.new` fires from the
   forecast pipeline routinely; the attacker can also use
   `POST /settings/webhooks/{id}/test` to fire `test.ping` immediately
   without waiting).
4. Gateway POSTs the signed envelope to the metadata service. The
   request body is the attacker's known payload (signed with their own
   secret, so the attacker can verify). The response is **not** logged
   by `_deliver_once` (`webhooks.py:182-189` only stores
   `status_code`), so the attacker doesn't get the response bytes back
   directly.

5. **But:** even without response readback, this is enough to (a)
   confirm gateway IAM scope (200 vs 401 distinguishes a server with
   instance creds), (b) **enumerate internal services** (the
   `status_code` and `error` columns on `webhook_deliveries` reveal
   whether `http://10.0.0.5:8500/v1/agent/services` is reachable — a
   Consul or k8s API discovery), and (c) **trigger state-changing
   internal calls** if any internal HTTP service accepts POST without
   auth (a TODO endpoint, a forecast cache flusher, a Prometheus
   pushgateway). All without ever needing the response body.

6. With HIGH-3 below stacked on: the attacker can also stand up an
   external sink that returns 10 MB of zeros and force allocation.

**Why not Critical:** Without response readback to the attacker, this
is a write-only / blind SSRF for most cases. The IMDS-token-exfil path
specifically needs the response body, which the gateway does not
forward. But (a) the response status is enough to fingerprint internal
services, and (b) any internal POST-acting service is fully reachable.

**Fix:** four changes.

1. Replace the env check with `config.is_production()` (one import).
2. Add a **delivery-time** SSRF check, not just a validation-time one.
   Resolve the URL host immediately before `await client.post(...)` and
   refuse if any A/AAAA record is in
   `ipaddress.ip_address(...).is_private` /
   `.is_loopback` / `.is_link_local` / `.is_multicast` /
   `.is_reserved`. Use `socket.getaddrinfo` and iterate; pass the
   resolved IP to httpx via a custom transport so the connect target
   matches what was validated (defeats the rebinding race the current
   comment claims to be afraid of). Python's `ipaddress` module handles
   the int/hex/octal/IPv4-mapped/v6-loopback normalization the regex
   list misses.
3. Maintain a hostname denylist for the named internal services
   (`metadata.google.internal`, `host.docker.internal`,
   `kubernetes.default.svc.cluster.local`, `127.0.0.1.nip.io`-style
   patterns).
4. Disable the use of `http://` in any non-dev environment — and gate
   that on `config.is_production()` consistently.

---

### HIGH-2 — Per-customer HMAC secrets stored as plain TEXT

**Location:**

- Schema: `migrations/129_webhooks.py:30-41` —
  `secret TEXT NOT NULL` in `webhook_subscriptions`.
- Write path: `db.create_webhook_subscription` at `db.py:1224-1240` —
  stores raw input string.
- Read path: `db.get_webhook_subscription` at `db.py:1264-1269`, then
  `webhooks._deliver_once` at `webhooks.py:160`, then admin pages
  via `db.list_all_webhooks` at `db.py:1252-1261`.
- Existing primitive for at-rest secrets: `db.set_system_secret` /
  `db.get_system_secret` (Fernet, see `db.py:1456+`) — already used
  for `STRIPE_WEBHOOK_SIGNING_SECRET`, `SENTRY_DSN`, etc.

**What:**

Per the design (acknowledged in the module docstring at
`webhooks.py:43-45`), the HMAC secret is **the** root of trust for the
receiver. Any holder of `secret` can mint a signed delivery indistinguishable
from a genuine narve.ai webhook, including to receivers who deduplicate
based on payload content (Slack-style ChatOps, internal CI triggers,
or any state-changing automation hung off a webhook).

The secret is auto-generated (`secrets.token_urlsafe(32)` at
`webhooks_routes.py:226`) — fine entropy — and offered to the user
verbatim in the settings page so they can paste it into their own
infra. **But it is also stored in cleartext** in `auth.db` /`db.db`
(whichever schema lives there — see `gateway/db.py` `conn()` for which
DB hosts `webhook_subscriptions`).

Surfaces of compromise:

- **Backup leak.** The DB file is in `/Users/shocakarel/Habbig/gateway/`
  alongside `*-wal` and `*-shm` artifacts. Any rsync, S3 backup, or
  database snapshot containing this file leaks every webhook secret.
  No code in the repo references encrypting backups.
- **Admin shell.** `gateway/admin_shell.py` is a developer-facing
  query inspector. A trusted-but-curious admin can run
  `SELECT url, secret FROM webhook_subscriptions` and exfiltrate keys
  for every paying customer.
- **Side-channel via the admin webhooks page.** Currently the admin
  page (`webhooks_routes.py:289-333`) does *not* render `secret`. Good.
  But the row dict returned from `db.list_all_webhooks` *does* include
  `secret` (it's `SELECT w.*`), and a future template change that adds
  a "show secret" affordance would inherit cleartext.
- **SQL log enabled.** Any deployment that turns on SQLite query
  logging (or a future Postgres migration with query logs) captures
  the `INSERT … VALUES (?, ?, ?, "<secret>", …)` payload.

The `replay_dead_letter` admin action at `webhooks.py:551-577` reads
`secret` to re-sign the replay, so the secret has to be reachable to
the worker — but Fernet decrypt-at-use solves that without changing the
delivery path. There is no fundamental reason this is plaintext.

**Attack:**

1. Adversary obtains read access to `db.db` via any of the above
   channels (cited in the codebase audits: backup misconfiguration in
   `RUNBOOK.md`, admin abuse in `audit_admin_jobs_routes.md`).
2. `SELECT id, url, secret FROM webhook_subscriptions WHERE is_active=1`.
3. For each row, the attacker can now mint signed deliveries to the
   subscriber's `url`. Receivers that trust the HMAC do whatever the
   payload dictates — `market.resolved` events drive automated trades
   in some integrations (described in `gateway/integrations/`); a
   forged "best_bet.new" can be wired to a Slack channel and execute
   a downstream bot action.

**Why not Critical:** Requires DB read access — not directly remote-
exploitable from an unauthenticated network position. But the impact
once compromised is per-tenant total compromise of the webhook channel,
and the fix is a small change to an already-existing crypto helper.

**Fix:**

1. New migration that adds `webhook_subscriptions.secret_encrypted BLOB`,
   backfills from `secret` using `set_system_secret`'s Fernet key,
   nulls the cleartext column.
2. `db.create_webhook_subscription` writes `secret_encrypted`; the
   plaintext is returned to the user once at create-time only.
3. `webhooks._deliver_once` reads via a new
   `db.get_webhook_signing_secret(webhook_id)` that decrypts in
   memory.
4. `webhooks_routes.py:226-228` already takes user-supplied secrets as
   an option — that path also needs to encrypt before insert.
5. Drop `secret` from `SELECT *` in `list_all_webhooks` and
   `list_webhooks_for_user`. Explicit column list.

---

### HIGH-3 — Outbound delivery has no max-response-body, no resolved-IP pinning, and a per-attempt timeout that compounds across retries

**Location:** `webhooks.py:156-191` (`_deliver_once`), `webhooks.py:80-92`
(constants).

**What:**

```python
DELIVERY_TIMEOUT_S = 10.0
...
async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
    resp = await client.post(url, content=body_bytes, headers=headers)
```

Three missing controls:

(a) **No response body limit.** httpx by default reads the entire
response into memory for `.status_code` access. A malicious subscriber
returning `Content-Length: 1073741824` and slowly trickling a gig of
zeros forces the gateway to allocate a gig per attempt. With
`MAX_ATTEMPTS=3` and the 5xx-retry path, that's 3 GB allocations per
event. With `broadcast_event` fanning out via `asyncio.gather`
(`webhooks.py:461`) and no concurrency cap, **N subscribers can each
DoS one goroutine simultaneously**. The combination of HIGH-1
(register an attacker-controlled internal URL) and this finding is the
full-stack memory-exhaustion attack vector.

(b) **No resolved-IP pinning.** As noted in HIGH-1, httpx resolves the
hostname at connect time. A subscriber URL `evil.example.com` that
resolves to a public IP at validation time but switches to 10.0.0.5
between validation and delivery (or between two attempts) is a classic
TOCTOU SSRF rebinding.

(c) **Per-attempt timeout, not per-delivery.** `DELIVERY_TIMEOUT_S =
10.0` per attempt × 3 attempts = 30s of wall time spent on one
hostile subscriber's connection, plus 2s + 4s = 6s of backoff
(`RETRY_DELAYS = (2, 4, 8)` — the third value is dead, see Info-2).
Across the `asyncio.gather` fan-out, a fleet of slow subscribers
holds open one task per sub for ~36s. Combine with the *un-capped fan-
out concurrency* (no `asyncio.Semaphore`) and a malicious user with
10 webhook subscriptions (the MAX_WEBHOOKS_PER_USER limit) can hold
10 in-flight tasks per event for 36s each.

(d) **`httpx.AsyncClient` is constructed inside the function, not
reused.** Every attempt opens a brand-new TCP+TLS handshake. Even
ignoring DoS, this is a small efficiency loss; under spike load it
multiplies the connection-time portion of the timeout budget.

**Attack:**

1. Register a webhook subscription with URL pointing to an
   attacker-controlled HTTP server that responds 200 with
   `Content-Length: 1073741824` and slow-trickles zeros (1 byte/s).
2. Subscribe to a high-frequency event (`best_bet.new` is emitted
   from the forecast pipeline; in production this fires hundreds of
   times a day).
3. Per event, the gateway allocates up to 1 GB and holds it for ~36s.
4. Repeat across 10 subscriptions (the per-user cap), 10 GB of
   peak allocation per event. The Python process eats it; depending
   on the host, OOM kill or sustained swap.
5. Note that `_deliver_with_retries` short-circuits on 2xx, so the
   200-OK trick stops further retries — but the *first* attempt
   allocation has already happened.

If the 200 path is replaced with 503 the impact halves (no body to
read on a typed 5xx error from httpx with no content) but the
connection-hold attack still works for the 30s timeout window per
attempt.

**Why not Critical:** Requires a paying-tier user with webhook
access (the feature is registered at `/settings/webhooks` which
requires `_current_user` — gated behind login). The per-user cap of
10 limits one attacker's blast radius. Still, "any paying user can
spike the gateway memory at will" is a real outage vector.

**Fix:**

```python
import httpx
LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
MAX_RESPONSE_BYTES = 64 * 1024  # we don't need the body, just status

# Resolver pinning — refuse RFC1918 / loopback / link-local at delivery time.
# Resolve once, attach as transport, then httpx won't re-resolve under us.
```

Add a per-fan-out semaphore (`asyncio.Semaphore(50)` shared across
`_one()` tasks in `broadcast_event`), and add `event_hooks` or a
custom transport that aborts the response read past
`MAX_RESPONSE_BYTES`. Practically: `client.stream("POST", url,
content=...)`, peek at the status code, then close — never read body.

---

### MED-1 — Circuit breaker is checked at fan-out only, not before each retry

**Location:** `webhooks.py:241-298` (`_deliver_with_retries`) vs.
`webhooks.py:421-462` (`broadcast_event` with `_circuit_open` filter).

**What:** `broadcast_event` filters out subscriptions with an open
breaker before fanning out (line 440). But once
`_deliver_with_retries` is running, it does **not** re-check the
breaker between attempts. If a different event lands on the same
subscription and trips the breaker (10 consecutive failures across
events) mid-flight, the in-progress retry loop still finishes its
2s/4s/8s ladder against a known-broken endpoint.

More concretely: `_bump_and_maybe_break` (line 330) opens the breaker
*at the end* of the current delivery. So within a single delivery,
attempts 1, 2, 3 all run even though attempt 1 already burned the
threshold. That's by design.

The cross-event case is the real bug. Two events arrive 100 ms apart
for the same subscription. Both `broadcast_event` calls observe the
breaker as closed (before either has terminated). Both fan out, both
run 3 attempts, both DLQ. The breaker opens twice (idempotent — just
overwrites `disabled_until`). The 2x amplification doesn't matter for
the cooldown logic but does for the hammering count on the broken
endpoint and for the volume of DLQ writes.

**Fix:** Re-read `disabled_until` before each attempt inside
`_deliver_with_retries`. Or move the per-attempt check inline:

```python
sub = db.get_webhook_subscription(webhook_id)
if _circuit_open(sub):
    return False
```

at the top of the for-loop body.

---

### MED-2 — DLQ replay bypasses circuit breaker (admin-only, but no rate limit)

**Location:** `webhooks.py:551-577` (`replay_dead_letter`),
`webhooks_routes.py:403-428` (`admin_webhooks_dlq_requeue`).

**What:** `replay_dead_letter` reads `subscription_id` from the DLQ
row, fetches `secret`/`url`, and calls `_deliver_with_retries`. No
breaker check. By design — the admin is overriding the breaker to
unstick a backlog after the subscriber has supposedly recovered.

But:

1. There is **no rate limit** on the admin replay endpoint. An admin
   can click "Re-queue" 500 times in a row (the DLQ list shows up to
   500 rows; mass-replay is a likely operator action). If the
   subscriber is still down, that's 500 * 3 = 1500 outbound POSTs.

2. The action is admin-only (`_require_admin` at line 408), and the
   audit log records each replay (line 419) — so this is operational
   risk, not a vuln. But the per-event retry+DLQ design assumes a
   subscriber is broken when ≥10 deliveries fail in a row. Bulk-replay
   of a 200-event DLQ when the endpoint is half-broken (say, 90%
   timeout) generates 540 outbound POSTs with no breaker to stop them.

3. The `requeued_at` stamp is written *regardless of replay outcome*
   (line 574-575). A failed replay still gets stamped requeued. The
   admin UI's "open backlog" view drops the row, so a recurring
   failure becomes invisible after the replay click. The
   `?include_requeued=1` query param surfaces it again but is not
   discoverable without reading the source.

**Why not High:** Admin-only, audit-logged, recoverable.

**Fix:** (a) rate-limit `admin_webhooks_dlq_requeue` per admin (5/min
should suffice). (b) Only stamp `requeued_at` on successful 2xx — let
failed replays sit in the DLQ for re-inspection. (c) Optionally, expose
a "bulk replay" action that processes 10 at a time with a 100 ms gap
and aborts on 3 consecutive failures.

---

### MED-3 — No total delivery body cap on outbound payload

**Location:** `webhooks.py:235` (`body_bytes = json.dumps(payload,
**_JSON_ARGS).encode()`).

**What:** The payload going *out* is built from `envelope` at
`broadcast_event` (line 444), which wraps the internal `payload` dict
the caller passed. There is no cap on payload size. Internal callers
controlling `payload` should self-limit, but:

- `hub_bridge` at line 481 forwards whatever `message` was published
  on the hub channel directly into the envelope `data`. If any
  realtime hub publisher ever forwards a large dict (a big batch of
  market data, a backfilled scenario tree), the entire blob is
  serialized, signed, and POSTed to *every active subscriber*.
- The subscriber sees the body. The same body is **also stored
  verbatim in `webhook_deliveries`** (line 251-254) and, on terminal
  failure, in `webhook_dead_letter` (line 321). A 5 MB payload that
  fails 3x produces 3 rows in the delivery log plus 1 row in DLQ —
  20 MB of TEXT in SQLite per failed delivery, per subscriber.
- `db.record_webhook_dead_letter` (line 1394) does not truncate the
  payload. `last_error[:1000]` is truncated but the payload column
  is not.

**Attack:** Not directly attacker-controlled — `payload` is set by
internal callers. But a logic bug or a future scaling event that
amplifies hub message size silently amplifies storage cost and
delivery latency. Operationally, this also means the DLQ admin page
(`webhooks_routes.py:339-400`) is paginated by `LIMIT 500` but each
row can be arbitrarily large — rendering the page can OOM under
pathological data.

**Fix:** Cap `body_bytes` length at, say, 256 KB; if exceeded, write a
delivery-log row with `error="payload too large (N bytes)"` and skip
the POST. Same cap in `record_webhook_dead_letter` — store only
`payload[:65_536] + "…"` in DLQ to keep admin views fast.

---

### MED-4 — Test endpoint `/settings/webhooks/{id}/test` is unauthenticated against URL re-validation

**Location:** `webhooks_routes.py:270-283` (`webhooks_test`),
`webhooks.py:515-545` (`fire_test_payload`).

**What:** Owner POSTs to `/settings/webhooks/{id}/test`. The handler
checks `sub["user_id"] == user["user_id"]` (line 275) — good. Then it
calls `fire_test_payload(webhook_id)` which fetches `sub["url"]` from
the DB and POSTs directly. **The URL is not re-validated** against
the current `_validate_url` policy.

This matters in two cases:

1. A subscription created when `PRODUCTION != "1"` (the env-string bug
   in HIGH-1) was saved with `http://localhost:9000`. After the env
   was fixed and `PRODUCTION=1` set, the row is grandfathered —
   `/test` re-fires against localhost.

2. A future tightening of `_validate_url` (e.g. adding GCP IMDS
   hostname blocking after HIGH-1 is fixed) won't affect existing rows.

The same critique applies to scheduled `broadcast_event` deliveries
— they read `sub["url"]` and trust it. There's no "re-validate URL
on use" step anywhere.

**Fix:** Either (a) re-run `_validate_url` against `sub["url"]` before
each delivery (cheap, small CPU cost) and on failure mark the
subscription `is_active=0` + email the owner; or (b) on policy
changes, run a one-shot migration that re-validates every row and
flags violations.

---

### MED-5 — `_enqueue_disabled_email` falls back to `_a.get_event_loop().create_task` (deprecated, race-prone)

**Location:** `webhooks.py:395-404`.

**What:**

```python
import asyncio as _a
_a.get_event_loop().create_task(
    svc.send_template(owner["email"], "webhook_disabled", ctx)
)
```

`asyncio.get_event_loop()` is deprecated in Python 3.12+ when no loop
is running and raises `DeprecationWarning` (and in 3.14 it will
raise). The fallback fires on the path where `jobs.enqueue_email` is
not available — which is the case in test runs, ad-hoc scripts, and
any deployment that hasn't wired the worker. In those environments
the breaker-trip email is silently lost (or crashes the trip itself).

Also: the fallback creates a task on the *current* loop. If
`_bump_and_maybe_break` is called from a sync context (it's not, but
could become so under refactor), the task is orphaned.

**Fix:** Replace with `asyncio.get_running_loop().create_task(...)`
inside a try/except that synthesizes an "email_send_failed" log entry
on miss. Better: drop the email fallback entirely and require the
jobs worker — surface a `log.error` if missing rather than silently
no-op.

---

### LOW-1 — `MAX_WEBHOOKS_PER_USER` enforced via `len(list)`, vulnerable to TOCTOU

**Location:** `webhooks_routes.py:219-221`.

```python
rows = db.list_webhooks_for_user(user["user_id"])
if len(rows) >= MAX_WEBHOOKS_PER_USER:
    raise HTTPException(409, "At webhook limit for this account")
...
wid = db.create_webhook_subscription(...)
```

Two concurrent create requests from the same user both see `len=9`,
both insert, user ends with 11 rows. The cap is 10 and the impact of
+1 is essentially nil, but the pattern is sloppy. A motivated user
could script up to ~2x the cap with parallel curl.

**Fix:** `CREATE UNIQUE INDEX … (user_id, url) WHERE is_active=1` and
let the DB reject duplicates, or do the count + insert in one
`INSERT … SELECT WHERE (SELECT COUNT(*) …) < 10`.

---

### LOW-2 — `RETRY_DELAYS = (2, 4, 8)` has an unused third element

**Location:** `webhooks.py:80-81`, used at line 284.

`MAX_ATTEMPTS = 3` means the loop sleeps after attempt 1 and after
attempt 2 (between attempts), but never after attempt 3 (it returns).
So `RETRY_DELAYS[2] = 8` is dead code. Not a bug, but signals that the
constants are off-by-one in the author's head — easy source of
future error. Documented at line 79 as "the final attempt's slot is
never read" so the author knows.

**Fix:** `RETRY_DELAYS = (2, 4)` and reword the doc.

---

### LOW-3 — `verify_signature` accepts any signature prefix "sha256="

**Location:** `webhooks.py:119-150`.

```python
if not signature_header or not signature_header.startswith("sha256="):
    return False
expected = _sign(secret, body, timestamp=ts)
return hmac.compare_digest(expected, signature_header[len("sha256="):])
```

The header parsing is permissive of trailing whitespace and case:
`"sha256= abc"` (note leading space after `=`) passes the prefix
check but `compare_digest` fails on the byte mismatch (the space is
preserved in the slice). Not exploitable — but it does mean the
function tolerates malformed headers silently rather than rejecting
them.

Also: the function only accepts `sha256=` (no other algorithms). Fine
today; if the team ever wants to roll signatures (`sha512=`,
`ed25519=`), they need a multi-alg parser. Not a current bug.

**Fix:** Normalize: strip the post-`=` portion, reject if non-hex,
reject if not exactly 64 chars. Tiny robustness win.

---

### LOW-4 — Delivery log payload column has no retention

**Location:** `migrations/129_webhooks.py:51-62` (`webhook_deliveries`
schema), `db.record_webhook_delivery` (`db.py:1313-1329`).

Every attempt writes a full payload string into `webhook_deliveries`.
With 3 retries per failed delivery and N events/day, this table grows
unbounded. There's no migration that prunes it, no scheduled job in
`gateway/jobs/` to vacuum it (verified by
`ls /Users/shocakarel/Habbig/gateway/jobs/` — no `webhook_*` file
exists).

For a small site this is fine; at scale (the `polymarket-bot` and
`forecast_sync` jobs emit hub messages on a sub-minute cadence),
this is the highest-write SQLite table after `audit_log`.

**Fix:** Add a cron in `gateway/jobs/db_maintenance.py` to delete
`webhook_deliveries` older than 30 days. Add `idx_whd_delivered_at`
to keep the prune query fast (only `(webhook_id, delivered_at DESC)`
and `(event_type, delivered_at DESC)` exist today).

---

### LOW-5 — `events` field is JSON-encoded TEXT; no schema validation at write time

**Location:** `db.create_webhook_subscription` (`db.py:1224-1240`).

```python
"INSERT INTO webhook_subscriptions "
"(user_id, url, events, secret, created_at, is_active) "
"VALUES (?, ?, ?, ?, ?, 1)",
(user_id, url, _json.dumps(events or []), secret, int(time.time())),
```

The `events` list arrives validated from `webhooks_routes._parse_events`
(line 117-126, filtered against `_VALID_EVENTS`). But:

- Any other caller of `create_webhook_subscription` — e.g. a future
  admin "create on behalf of" tool, the test suite, an import script
  — bypasses the route-level filter.
- The `list_active_webhooks_for_event` function at `db.py:1291-1310`
  parses JSON in Python and filters in-memory; malformed JSON rows
  are silently skipped (`except: continue`). A subscription created
  with garbage `events` is invisible forever, no error.

**Fix:** Validate `events` in `create_webhook_subscription` against
the canonical `EVENT_TYPES` tuple in `webhooks.py` (move it to
`webhooks.py` and import here, to keep one source of truth — currently
duplicated as `_VALID_EVENTS` at `webhooks_routes.py:40`).

---

### LOW-6 — Hub bridge forwards arbitrary hub messages without sanitization

**Location:** `webhooks.py:472-488` (`hub_bridge`).

```python
_CHANNEL_TO_EVENT: dict[str, str] = {
    "best_bets": "best_bet.new",
    ...
}

async def hub_bridge(channel: str, message: dict) -> None:
    event_type = _CHANNEL_TO_EVENT.get(channel)
    if event_type is None:
        return
    try:
        await broadcast_event(event_type, message)
```

Whatever dict the internal hub publishes on `best_bets` is forwarded
verbatim as the public webhook envelope. If a hub publisher ever
includes an internal field (a user's email, a partial portfolio, a
draft scenario) in the same message it forwards to internal
subscribers, that field leaks to external webhook receivers.

The schemes used by current hub publishers are out-of-scope for this
audit, but the architectural absence of a per-channel "public field
allowlist" is the issue. The `_CHANNEL_TO_EVENT` mapping should be
paired with a `_CHANNEL_TO_PUBLIC_FIELDS` filter.

**Fix:**

```python
_CHANNEL_TO_PUBLIC_FIELDS = {
    "best_bets": {"market_id", "side", "prob", "edge", "asof"},
    ...
}
public = {k: message[k] for k in _CHANNEL_TO_PUBLIC_FIELDS[channel]
          if k in message}
await broadcast_event(event_type, public)
```

---

### INFO-1 — DLQ `last_error` truncated to 1000, payload not

**Location:** `db.record_webhook_dead_letter` (`db.py:1411-1418`),
`webhooks._drop_to_dlq` (line 322).

`last_error[:1000]` is enforced; payload is stored verbatim.
Consistency would suggest both fields should have the same retention
policy. As noted in MED-3, the payload is the bigger storage cost.

---

### INFO-2 — Retry-on-failure does not differentiate between connect/read/write timeout

**Location:** `webhooks.py:187` (`except httpx.TimeoutException:`).

All timeout sub-types collapse to error string `"timeout"`. A
connect-timeout (the subscriber's host is unreachable) is operationally
different from a read-timeout (subscriber accepted the request but
didn't respond) — the former retries are pointless, the latter might
genuinely recover. Both currently retry 3x. Minor inefficiency.

---

### INFO-3 — `failure_count` is bumped only on terminal failure, not per attempt

**Location:** `db.bump_webhook_failure` (`db.py:1342-1358`), called
once from `_bump_and_maybe_break` at end of `_deliver_with_retries`.

A 503-retried-3x failure increments `consecutive_failures` by exactly
1, not 3. The variable name suggests "every failed attempt" — the
behavior is "every failed delivery". Consistent with the circuit
breaker semantics (10 deliveries, not 10 attempts) but the name is
misleading.

`failure_count` (cumulative) is also bumped once per delivery, not
per attempt. The all-time analytics counter undercounts attempts.

---

### INFO-4 — `record_webhook_delivery` is best-effort but errors are silently swallowed

**Location:** `webhooks.py:249-256`.

```python
try:
    db.record_webhook_delivery(...)
except Exception:
    pass  # delivery log is best-effort — don't block retries on it
```

Comment is correct on intent. But a sustained DB write failure (disk
full, lock contention) silently disappears the entire delivery log
without any operator signal. Should at least `log.warning("delivery
log write failed")` so a flapping DB shows up in Sentry / journald.

---

## Notes on what is correct

For balance, these were checked and are sound:

- **HMAC over timestamp+body**, not body alone. Defeats the standard
  swap-the-timestamp-header attack. `webhooks.py:105-116`, exercised
  by `tests/test_webhooks.py:149-160`.
- **`hmac.compare_digest`** for constant-time signature comparison
  (`webhooks.py:150`). Correct primitive.
- **`asyncio.gather(..., return_exceptions=True)`** in
  `broadcast_event` — one slow/failing subscriber doesn't block the
  others (`webhooks.py:461`).
- **`secrets.token_urlsafe(32)`** as default secret — 256 bits of
  entropy, URL-safe (`webhooks_routes.py:226`).
- **`ON DELETE CASCADE`** on DLQ and deliveries — clean orphan
  handling when a subscription is removed (`migrations/129_webhooks.py:54`,
  `migrations/179_webhook_hardening.py:43`).
- **Idempotent migrations.** `CREATE TABLE IF NOT EXISTS` and
  `PRAGMA table_info` gating on the ALTER (`migrations/179:61-72`).
- **Owner-only delete.** `db.delete_webhook_subscription` includes
  `AND user_id = ?` in the WHERE clause (`db.py:1276`). No IDOR.
- **`is_active` filtering.** `list_active_webhooks_for_event` honors
  the manual disable flag (`db.py:1300`), so user-disabled
  subscriptions are skipped even pre-circuit-breaker.
- **Audit log on create/delete** (`webhooks_routes.py:239-247,
  259-266`) and on DLQ replay (line 419-426). The latter is
  particularly important given MED-2.
- **Anti-replay window of 5 minutes** matches Stripe's documented
  practice and is what most receiver SDKs default to
  (`webhooks.py:96`).
- **JSON serialization stable** (`_JSON_ARGS = {"separators": (",", ":"),
  "sort_keys": True, "default": str}` at line 99) — byte-identical
  re-signing is possible. Caveat: `default=str` coerces non-JSON
  types to their `repr`, which could be unstable for some types
  (`datetime`, `Decimal`); see INFO-2 for the related point on hub
  forwarding.

---

## Reproduction notes

The findings above were verified by code reading; for confirmation
in a running gateway:

- **HIGH-1 (a)**: set `PRODUCTION=true`, POST `/settings/webhooks`
  with `url=http://127.0.0.1:8000/`. Expect 302 (created), not 400.
- **HIGH-1 (c)**: with `PRODUCTION=1`, POST same form with
  `url=http://2130706433/` (decimal form of 127.0.0.1). Expect 302.
  Then `/test` the webhook and watch the gateway POST itself.
- **HIGH-2**: `sqlite3 /Users/shocakarel/Habbig/gateway/auth.db
  "SELECT id, url, substr(secret,1,8) FROM webhook_subscriptions
  LIMIT 5"`. The secret column contains cleartext.
- **HIGH-3**: stand up `python3 -m http.server` modified to
  trickle-respond with `Content-Length: 1073741824`. Register webhook
  pointing at it, fire `/test`. Observe gateway RSS climb.
- **MED-1**: register one subscription, bump `consecutive_failures`
  to 9 in the DB, fire two `broadcast_event` calls 50 ms apart from
  a script — observe both run the 3-attempt ladder before the
  breaker is consulted.
- **MED-3**: write a hub publisher that emits a 1 MB dict on
  `best_bets`. Fire one event. Inspect `webhook_deliveries.payload`
  size — it'll be 1 MB times (subs * attempts).

No code changes were made as part of this audit.
