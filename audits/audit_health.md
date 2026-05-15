# Audit — `/health` and `/api/v1/status` endpoints

**Scope:** verify that the gateway's two public health-facing endpoints
check the five dependencies named in the brief: DB reachable, scheduler
running, last cron tick recent, Sentry init OK, Stripe SDK present.

**Date:** 2026-05-15
**Git SHA at audit:** `7a443e0` (branch `feature/platform-build`).
**Auditor mode:** read-only.

---

## Summary

| Endpoint                | Path registered       | Probe shape                                                                                    |
|-------------------------|-----------------------|------------------------------------------------------------------------------------------------|
| `/health`               | `gateway/server.py:3251` (`@app.get("/health")`, `+/health/deep`) | JSON, `checks{}` map. Critical = `{database, gate}`. 503 on critical failure, 200 otherwise.   |
| `/api/v1/status`        | **NOT REGISTERED**     | The canonical JSON status snapshot is at `/api/status` (`gateway/status_routes.py:232`). See G1. |
| `/api/admin/health-monitor` | `gateway/admin_health_monitor_routes.py:168` | Admin-only HEAD-probe of every subproduct's `/health`. Unrelated to dependency-health.        |

### Coverage of the five required checks

| Required check          | `/health` (shallow) | `/health/deep` | `/api/v1/status` (intended) | `/api/status` (actual) | Notes                                        |
|-------------------------|---------------------|----------------|-----------------------------|------------------------|----------------------------------------------|
| DB reachable            | YES (`_check_database`, key=`db`/`database`) | YES | n/a — endpoint missing | NO — only renders incidents/uptime, never pings DB | G3 |
| Scheduler running       | YES (`_check_scheduler`, key=`scheduler`) | YES | n/a | NO | G4 |
| Last cron tick recent   | **NO**              | **NO**         | n/a                         | **NO**                 | G2. Status only checks `_scheduler.running` flag — a hung event loop with the scheduler object still alive returns `"ok"`. |
| Sentry init OK          | **NO**              | **NO**         | n/a                         | **NO**                 | G5. `init_sentry()` is **never called** from the gateway server — only the standalone scraper invokes it. |
| Stripe SDK present      | **NO**              | **NO**         | n/a                         | **NO**                 | G6. No `import stripe` probe; every Stripe call site does its own lazy import and silently no-ops if missing. |

`/health` does additionally probe **encryption key**, **gate token**,
**static dir**, **dashboards config**, **email mode**, **redis** (deep
only), and **subproduct dashboards** (deep only). Those are out of scope
for the brief but documented here for completeness.

---

## Gaps (numbered, severity-tagged)

### G1 — HIGH — `/api/v1/status` endpoint is undefined

There is no route registered for `/api/v1/status`. The closest matches
are:

- `GET /api/status` — public JSON status snapshot of the
  incidents/uptime page (`gateway/status_routes.py:232-276`). Reads
  from `status_uptime.overall_system_status()` and
  `status_db.list_recent_incidents()`. **Does not probe live
  dependencies** — it surfaces *previously recorded* uptime samples
  written by the `status_jobs.py` cron.
- `gateway/api_v1.py` declares `router = APIRouter(prefix="/api/v1")`
  but exposes only `/sources`, `/predictions`, `/markets/*`.

`gateway/static/status.html:91` ships JS that calls
`fetch('/api/v1/status/subscribe', …)` and
`gateway/tests/test_status_page.py:62` asserts the form's action is
`/api/v1/status/subscribe`. The test comment claims:

> Clients hit `/api/v1/status/subscribe` (canonical); the APIVersion
> middleware transparently rewrites it to `/api/status/subscribe`.

**But no `APIVersionMiddleware` is registered in `server.py`.**
`gateway/api/deprecation.py:20` documents it as living in `server.py`,
but `grep -n "APIVersionMiddleware\|class APIVersion" gateway/` returns
zero hits in non-test code. Subscription POSTs from the live status
page would 404 in production. This is also relevant to the audit
because anyone hitting `/api/v1/status` for a dependency snapshot gets
a 404 with no diagnostic.

**Fix:** either (a) register a real `APIVersionMiddleware` that rewrites
`/api/v1/{path}` → `/api/{path}` for the legacy aliases, **or** (b)
re-declare the status routes under `/api/v1/status*` and keep `/api/*`
as redirects.

If a *health* snapshot is wanted at `/api/v1/status` (as the brief
implies), it must be a new endpoint — `/api/status` is **not** that
endpoint despite the similar name. Recommendation: alias
`/api/v1/health` → `/health` and document that as the canonical path.

### G2 — HIGH — `_check_scheduler` does not check the last cron tick

`gateway/server.py:3090-3108`:

```python
def _check_scheduler() -> tuple[str, Optional[str]]:
    if os.environ.get("NARVE_SKIP_SCHEDULER", "").strip():
        return "disabled", None
    try:
        sched = globals().get("_scheduler") or globals().get("scheduler")
        if sched is None:
            return "disabled", None
        running = getattr(sched, "running", None)
        if running is None and hasattr(sched, "state"):
            running = bool(sched.state)
        return ("ok", None) if running else ("error", "scheduler not running")
    ...
```

This only reads the *flag* `sched.running`. It does **not** consult
`job_runs.started_at MAX()` to confirm that the scheduler has actually
fired *anything* recently. A frozen event loop, a stuck DB-writing job
holding `record_end`'s connection, or APScheduler silently swallowing
a misfired job all leave `running=True` while the cron has not ticked
for hours. The audit row table (`migration 105 → job_runs`) is the
authoritative signal and is never queried by `/health`.

**Fix:** add a check that does

```sql
SELECT MAX(started_at) FROM job_runs
```

and returns `error` if the most-recent start is older than the
expected-tick threshold (suggest 600s — the shortest interval job is
`status_jobs` at 60s, so 10× that is a generous floor).

Also note: the wrapper at `gateway/scheduler/scheduler.py:100`
references `self._impl` but the local `running` attribute lookup at
`server.py:3100-3105` looks for `sched.running` and `sched.state` —
the `Scheduler` class exposes neither directly. The actual running
flag is `self._started`. So the current check resolves
`running = None` → falsy → returns `"error", "scheduler not
running"` whenever APScheduler is actually live but the underlying
AsyncIOScheduler's `.running` is hidden behind `_impl`. Worth a
focused re-test on staging — `/health` may already be reporting
`scheduler=error` in prod and nobody has noticed because it's
non-critical.

### G3 — MEDIUM — `_check_database` only does `SELECT 1`

`gateway/server.py:3016-3025`:

```python
with db.conn() as c:
    row = c.execute("SELECT 1").fetchone()
```

Passes even when `auth.db` is open but WAL-locked, when migrations
have not run, or when the schema is at an unexpected revision. Suggest
extending to confirm a key table exists (`SELECT 1 FROM users LIMIT
1`) and that the migration revision matches a pinned floor.

### G4 — MEDIUM — `_check_scheduler` does not probe job-queue worker

The gateway has *two* schedulers in flight (server.py:482-499):

1. `jobs/__init__.py::start_worker()` — the queue-style worker that
   drives `background_jobs` (emails, pipeline kicks).
2. `scheduler/registry.py::register_all()` + `scheduler.start()` —
   the APScheduler-backed cron.

`_check_scheduler` only covers (2). A frozen (1) means no emails get
sent and no Stripe webhook side-effects fire, and `/health` reports
`ok`. Add a parallel `_check_job_worker()` that reads the worker's
heartbeat / queue depth.

### G5 — HIGH — Sentry is never initialised in the gateway

`init_sentry` is defined twice in the repo (one for the gateway, one
for the scraper) but is only **called** in:

- `gateway/scraper/main.py:35` — scraper service.
- `centralbank-dashboard/server.py:28`, `whale-dashboard/server.py:29`,
  `world-health-dashboard/server.py:31`, `love-dashboard/server.py:76`
  — subproduct dashboards.

`gateway/server.py` does **not** import `observability.sentry_setup`
nor call `init_sentry()`. Verified via:

```
grep -rn "init_sentry\|sentry_sdk.init" gateway/ --include='*.py' \
  | grep -v /tests/ | grep -v scraper
```

→ zero hits in `gateway/`.

Consequence: gateway exceptions (the **main HTTP surface**) do not
ship to Sentry. The DSN may be set in the env, the SDK is in
`requirements.lock` (`sentry-sdk==1.45.1`), the scrubber is correct —
but the SDK is never armed. `/health` does not surface this. A user
flipping `SENTRY_DSN` in the env file gets no feedback that nothing
changed.

**Fix (two parts):**
1. Call `observability.init_sentry(platform="backend")` near the top
   of `server.py` (after env loading, before `FastAPI(...)` so
   `FastApiIntegration` can hook the middleware stack).
2. Add `_check_sentry()` to `/health`:

```python
def _check_sentry() -> tuple[str, Optional[str]]:
    try:
        import sentry_sdk
    except ImportError:
        return ("disabled", "sentry-sdk not installed") if not IS_PRODUCTION \
            else ("error", "sentry-sdk not installed")
    hub = sentry_sdk.Hub.current
    client = hub.client
    if not client or not client.dsn:
        return ("disabled", None) if not IS_PRODUCTION \
            else ("error", "SENTRY_DSN not set / init never called")
    return "ok", None
```

Make it **non-critical** so a missing DSN doesn't 503 the LB, but make
it part of the JSON so monitoring sees the drift.

### G6 — HIGH — Stripe SDK presence is never verified at startup

Every Stripe touch point does its own lazy `import stripe` inside the
handler. Verified at:

- `gateway/billing_routes.py:1227`
- `gateway/stripe_webhook_routes.py:205`
- `gateway/stripe_webhook_hardening.py:435`
- `gateway/subproduct_signup_routes.py:118`
- `gateway/subproduct_access.py:179`
- `gateway/jobs/reconcile_subscriptions.py:49`

If `stripe` is not installed (or installed at the wrong version
range), the *first* paying customer to hit checkout discovers it via
an HTTP 500. Health-check coverage for this is zero.

**Fix:** add to `/health`:

```python
def _check_stripe_sdk() -> tuple[str, Optional[str]]:
    if not os.environ.get("STRIPE_SECRET_KEY"):
        # Pre-Stripe deploy / test env — don't fail health.
        return ("disabled", None) if not IS_PRODUCTION \
            else ("error", "STRIPE_SECRET_KEY not set")
    try:
        import stripe  # noqa: F401
    except ImportError as exc:
        return "error", f"stripe SDK missing: {exc}"
    if not getattr(stripe, "api_key", None):
        # Lazy-init pattern — api_key only set inside handlers. Just
        # confirm the module attribute exists.
        return "ok", "api_key set lazily per-request"
    return "ok", None
```

Mark **critical in production** (a missing Stripe SDK in a paid-tier
gateway is a P0).

### G7 — MEDIUM — `/health` exposes no scheduler-tick freshness even in deep mode

`/health?deep=1` adds Redis + per-subproduct probes but no extra
scheduler / sentry / stripe coverage. The "deep" variant is the
natural place to add the heavier checks gated on a manual or
admin-triggered probe.

### G8 — LOW — `/health` checks `database` under two keys

`server.py:3279-3282`:

```python
checks["database"] = db_status
# Tests assert ``db`` is the canonical key and ``database`` a legacy alias.
checks["db"] = db_status
```

Both keys are written; the comment says `db` is canonical but the
critical set on line 3324 reads `{"database", "gate"}`. Mismatched —
if a future cleanup drops `database`, the critical-check filter
becomes a no-op and the gateway will report 200 even when the DB is
down. Pick one canonical key; gate the criticality off the canonical
name only.

### G9 — LOW — `_check_redis` reports `"disabled"` indistinguishably from "URL set, client missing"

`server.py:3076-3087`:

```python
if not _REDIS_URL:
    return "disabled", None
if _redis_client is None:
    return "error", "redis client not initialized"
```

`"disabled"` looks like `"ok"` to a casual reader of the JSON. Worth
adding a `note` field (or returning `"unconfigured"` per the email
check's pattern at line 3054) so dashboards don't render disabled as
green.

### G10 — INFO — `/health/deep` has no auth

`@app.get("/health/deep", include_in_schema=False)` (`server.py:3252`)
is reachable by anyone. The deep probe hits every subproduct (`urllib`
with 1s timeout per probe; ~14 backends → 14s fan-out per request).
An attacker hammering `/health/deep?deep=1` from a residential IP
pool would generate non-trivial backend load. The route is excluded
from the global rate limit (`_GLOBAL_RL_SKIP_PATHS = frozenset({
"/health"})` at server.py:1891). Suggest either
(a) require the gate token cookie for `/health/deep`, or
(b) add it back into the global rate-limit allowlist with a high
per-IP cap.

### G11 — INFO — No `/health/live` vs `/health/ready` split

Kubernetes / standard LB convention separates *liveness* (process is
alive, restart if not) from *readiness* (downstreams are ok, can take
traffic). The gateway combines both into one endpoint. The current
behaviour — 503 when DB or gate token is missing — is *readiness*.
Cloudflare Health Checks reading `/health` will pull the gateway out
of rotation on a 503, which is correct for `readiness` but unusual
for `liveness`. Not a bug; flagged so the eventual `/healthz` /
`/readyz` aliases (referenced at `server.py:716` and `auth/middleware.py:40`)
are wired with explicit semantics rather than as plain aliases.

---

## Files inspected

- `gateway/server.py` (lines 3004-3361 for /health, 480-527 for
  startup wiring, 716, 1445-1467, 1891 for routing/middleware).
- `gateway/status_routes.py` (lines 1-330 for /status & /api/status).
- `gateway/api_v1.py` (lines 1-60 for prefix declaration; no status route).
- `gateway/api/deprecation.py` (lines 1-50 for documented-but-missing
  `APIVersionMiddleware`).
- `gateway/observability/sentry_setup.py` (full file; init_sentry never called from gateway).
- `gateway/observability/__init__.py` (re-exports init_sentry).
- `gateway/scheduler/scheduler.py` (full file; wrapper around AsyncIOScheduler, exposes `_started`/`_impl`).
- `gateway/admin_health_monitor_routes.py` (lines 1-200; admin-only probe of subproducts).
- `gateway/tests/test_health.py` (lines 1-148; current contract).
- `gateway/tests/test_status_page.py` (lines 50-78; broken APIVersion assumption).
- `gateway/static/status.html` (line 91; client posts to `/api/v1/status/subscribe`).
- `requirements.lock` (line 47; `sentry-sdk==1.45.1`).

---

## Counts

| Severity | Count |
|----------|-------|
| HIGH     | 4     |
| MEDIUM   | 3     |
| LOW      | 2     |
| INFO     | 2     |

Total: **11** findings.
