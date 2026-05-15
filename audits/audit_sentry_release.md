# Adversarial audit — Sentry release / sample rate / before_send / env tag

- Scope: gateway-side Sentry initialisation only. Four verification points
  from the audit brief:
    1. `release` tag uses the git SHA at boot.
    2. `traces_sample_rate` ≤ 0.1 in production.
    3. `before_send` filter strips PII.
    4. Sentry `environment=` tag matches the actual `ENVIRONMENT` env var.
- Search invoked: `grep -rn "sentry_sdk.init\|detect_release" gateway/ --include='*.py'`
- Hits:
    - `gateway/observability/sentry_setup.py:91` — backend `sentry_sdk.init(...)`
    - `gateway/observability/sentry_setup.py:89` — `from observability import detect_release`
    - `gateway/observability/__init__.py:13` — `def detect_release() -> str:`
    - `gateway/scraper/observability.py:68` — scraper `sentry_sdk.init(...)`
      (parallel surface; uses `APP_VERSION`, not `detect_release()`)
- Files audited (read end-to-end):
    - `/Users/shocakarel/Habbig/gateway/observability/sentry_setup.py`
    - `/Users/shocakarel/Habbig/gateway/observability/__init__.py`
    - `/Users/shocakarel/Habbig/gateway/scraper/observability.py`
    - `/Users/shocakarel/Habbig/gateway/server.py` (lines 234, 540-560, 1015-1030
      for `IS_PRODUCTION`, `APP_ENVIRONMENT`, lifespan wiring)
    - `/Users/shocakarel/Habbig/gateway/config.py` (`OPTIONAL_SHAPES`, lines 137-147)
    - `/Users/shocakarel/Habbig/gateway/.env.example` (lines 5-10, 66-73, 206-219)
    - `/Users/shocakarel/Habbig/gateway/tests/test_sentry.py`
- Method: traced every env-derived value to its declaration; cross-referenced
  every call site of `init_sentry`; verified `detect_release()` precedence,
  filesystem path, and timeout; confirmed scrubber field coverage against the
  Sentry SDK event shape; checked `environment=` matches the rest of the
  app's `APP_ENVIRONMENT` binding; ran `git status` / `git log` to confirm
  branch state.
- Date: 2026-05-15
- Branch: `feature/platform-build` @ commit `0627dc8`
- Repo root SHA at audit time: `45d3d47`

## Severity counts

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High     | 2 |
| Medium   | 2 |
| Low      | 3 |
| Info     | 2 |

Total: 9 findings.

## Top 3 (by exploitability × blast-radius)

1. **H1 — Backend `init_sentry()` is never invoked from the gateway process.**
   `gateway/observability/sentry_setup.py:58` defines `init_sentry`, and
   `observability/__init__.py:9` re-exports it, but
   `grep -rn "init_sentry" gateway/ --include='*.py'` returns ZERO calls
   inside the gateway server tree — only `gateway/scraper/main.py:35` calls
   its sibling copy. `gateway/server.py` has no `init_sentry`, no
   `sentry_sdk.init`, no `from observability import init_sentry`. Net
   effect: every other Sentry property audited below (release SHA,
   sample-rate, before_send, env tag) is **dead code on the main API
   server** — exceptions in `server.py`, routes, middleware, and the
   admin shell are never reported. The release-tagging and PII-scrubbing
   logic is correct in isolation but unreachable. This dwarfs every
   other finding because none of the others can fire until init runs.

2. **H2 — `environment=` defaults to `"production"` when `ENVIRONMENT` is
   unset, regardless of `PRODUCTION=0`.**
   `gateway/observability/sentry_setup.py:96` reads
   `os.getenv("ENVIRONMENT", "production")`. The shipped
   `gateway/.env.example` sets `PRODUCTION=0` (line 5) but
   `ENVIRONMENT=production` (line 9), so any clone-and-run dev box that
   leaves `ENVIRONMENT` at its default and supplies a `SENTRY_DSN`
   uploads dev/test events tagged `environment=production`. Worse,
   the rest of the app derives its env from
   `APP_ENVIRONMENT = os.environ.get("ENVIRONMENT", "production" if IS_PRODUCTION else "dev")`
   (`server.py:553`) — which falls back to `"dev"` when `PRODUCTION` is
   off — so the Sentry env tag can disagree with the value `/health`
   reports and the value used by the staging proxy at `server.py:1026`.
   The brief asked us to verify the env tag "matches the actual
   `ENVIRONMENT` env var": it matches the env var when set, but the
   *default* paths diverge from the rest of the app and from
   developer intuition. Use the same fallback chain as `APP_ENVIRONMENT`,
   or share the constant.

3. **M1 — Scrubber misses `request.query_string` when it is a `bytes`
   object, and never walks `breadcrumbs`, `exception.values[*].value`,
   `contexts`, or `user`.**
   `gateway/observability/sentry_setup.py:45-47` only filters
   `query_string` when `isinstance(query, str)` — the FastAPI/Starlette
   integration commonly passes `bytes`. The function also does not
   recurse into `event["breadcrumbs"]["values"][i]["data"]`, which is
   where the SDK plants HTTP-client request/response bodies (e.g.
   the Stripe SDK and the Polymarket scraper both produce breadcrumbs
   carrying API tokens), nor into `event["exception"]["values"][i]["value"]`
   where a raised `ValueError("token=whsec_…")` keeps the secret
   verbatim. The scrubber's hint list is solid; the traversal isn't.

## All findings

### H1 — Backend Sentry init is never called (release/sample/scrub are unreachable)
- **Where:** `gateway/observability/sentry_setup.py:58`, re-exported at
  `gateway/observability/__init__.py:9`. No production caller anywhere
  in `gateway/` except tests at `gateway/tests/test_sentry.py:62-70`,
  which only assert the *no-DSN* return-False path.
- **Why this matters for the brief:** the audit asked whether the release
  tag uses the git SHA at boot. The code that would do so (`detect_release()`
  at `gateway/observability/__init__.py:13`) is correct, but it never
  runs in the gateway process — the only caller of `detect_release()` in
  the whole tree is `gateway/observability/sentry_setup.py:97`, which is
  inside the un-invoked `init_sentry`. So the *current* gateway boot
  attaches no release tag because it attaches no Sentry at all.
- **Fix:** wire `init_sentry(platform="backend")` into `server.py` lifespan
  startup before the first request is served. Add a test that imports
  `server.app` and asserts `sentry_sdk.Hub.current.client is not None`
  when `SENTRY_DSN` is set, otherwise this regresses silently.
- **Severity:** High.

### H2 — Default `environment=production` disagrees with `APP_ENVIRONMENT` default
- **Where:** `gateway/observability/sentry_setup.py:96` vs `server.py:553`.
- **Detail:** when neither `ENVIRONMENT` nor `PRODUCTION` is exported,
  Sentry tags the event `production` while `APP_ENVIRONMENT` resolves
  to `"dev"`. When `PRODUCTION=1` but `ENVIRONMENT` is unset, the two
  agree by coincidence. When `ENVIRONMENT=staging` and `PRODUCTION=0`,
  they agree. The failure mode is the most common one: a contributor
  copies `.env.example` (which sets `PRODUCTION=0` and
  `ENVIRONMENT=production`) without changing `ENVIRONMENT`, exports
  `SENTRY_DSN`, and pollutes the production Sentry project from a
  laptop.
- **Fix options (pick one):**
    - `environment=APP_ENVIRONMENT` (import from `server`) — single source
      of truth.
    - Change `.env.example` line 9 to `ENVIRONMENT=dev` so the shipped
      template never tags dev as prod.
    - Both — defense in depth.
- **Severity:** High.

### M1 — Scrubber traversal gaps (query_string bytes, breadcrumbs, exception value, contexts)
- **Where:** `gateway/observability/sentry_setup.py:30-54`.
- **Gaps:**
    - `query_string` only filtered when `isinstance(str)`; SDK sometimes
      sends `bytes`.
    - No traversal of `event["breadcrumbs"]["values"][i]["data"]`
      (where HTTP-client integrations log request bodies / URLs / auth
      headers).
    - No traversal of `event["exception"]["values"][i]["value"]` — a
      raised exception with `str(e)` containing a token is forwarded
      verbatim.
    - No traversal of `event["contexts"]` (e.g. `contexts.runtime`,
      `contexts.trace.data` sometimes carries SQL parameters).
    - No traversal of `event["user"]` — `send_default_pii=False` is set
      at line 99, so the SDK won't auto-populate `user.email`, but
      anything dropped via `set_user({...})` (see `set_user_context` at
      line 106) is not re-checked here.
- **Fix:** lift the scrub helper to walk the event dict recursively up
  to a depth cap; key match on `_SENSITIVE_FIELD_HINTS`; value match on
  a regex for high-shape secrets (`whsec_`, `sk_live_`, `sk-ant-`).
- **Severity:** Medium.

### M2 — Scraper init uses `APP_VERSION` for `release`, not git SHA
- **Where:** `gateway/scraper/observability.py:74` →
  `release=os.getenv("APP_VERSION", "1.0.0")`.
- **Impact:** when the audit brief says "release tag uses git SHA at boot",
  the gateway sibling does (via `detect_release()`), but the scraper does
  not. The default `APP_VERSION=1.0.0` (also the default in
  `gateway/.env.example:10` and `server.py:552`) means every scraper
  deploy reports release `1.0.0` until somebody remembers to bump the
  env var. Sentry's release-tracking, regression detection, and source-map
  upload all key on release — they're effectively disabled for scraper
  events.
- **Fix:** import the same `detect_release()` helper into
  `scraper/observability.py` (it has no gateway-package dependency
  beyond the function itself) or duplicate the git-rev-parse logic.
- **Severity:** Medium.

### L1 — `traces_sample_rate` default 0.1 is fine, but no upper-bound enforcement at init
- **Where:** `gateway/observability/sentry_setup.py:94`,
  `gateway/scraper/observability.py:71`.
- **Status against brief:** the default value is `0.1`, which satisfies
  the "≤ 0.1 in prod" rule. `gateway/config.py:137-147` validates the env
  var is a float in [0.0, 1.0] when set, but does not enforce a
  production-specific cap.
- **Risk:** an operator sets `SENTRY_TRACES_SAMPLE_RATE=1.0` in prod —
  legitimate per the validator — and quietly burns through the Sentry
  quota. The audit brief defines the policy ("≤ 0.1 in prod") more
  tightly than the code does.
- **Fix:** in `init_sentry`, clamp to `min(float(env), 0.1)` when
  `environment == "production"`, or add a `VarSpec` that validates
  `< 0.1` whenever `ENVIRONMENT == production`.
- **Severity:** Low.

### L2 — `detect_release()` is cached for the process lifetime but never refreshed across deploys without restart
- **Where:** `gateway/observability/__init__.py:12-43`.
- **Detail:** `@lru_cache(maxsize=1)` is correct for performance — the
  git shell-out runs once. The risk is a long-lived process surviving
  a deploy (e.g. uvicorn reload disabled) and reporting stale SHAs to
  Sentry. Operationally rare for this stack (Cloudflare → uvicorn,
  restarted on deploy), but worth noting.
- **Severity:** Low.

### L3 — `detect_release()` falls back to `"unknown"` silently
- **Where:** `gateway/observability/__init__.py:43`.
- **Detail:** if neither `NARVE_RELEASE` is set nor the working tree is
  a git checkout (e.g. shipped tarball / Docker without `.git`),
  release becomes the literal string `"unknown"`. Sentry will accept
  this, but release-health and regression detection collapse. No
  emission of a warning log, no failure to boot. Recommend logging a
  WARNING at init time when `release == "unknown"` so operators
  notice in stderr.
- **Severity:** Low.

### I1 — `before_send=scrub_sensitive_data` is wired correctly
- **Where:** `gateway/observability/sentry_setup.py:98`.
- **Status against brief:** confirmed. Hook is registered. Test
  coverage at `gateway/tests/test_sentry.py:13-58` exercises the seven
  cases the scrubber's current traversal covers (Authorization,
  CSRF, Cookie, password, api_token/client_secret, card_number,
  cookies-dict). See M1 for the gaps the tests don't yet cover.
- **Severity:** Info.

### I2 — `send_default_pii=False` is set
- **Where:** `gateway/observability/sentry_setup.py:99` and
  `gateway/scraper/observability.py:76`.
- **Status:** good. This keeps the SDK from auto-populating
  `request.cookies`, `user.email`, `user.ip_address`, etc., which the
  scrubber would otherwise have to catch case-by-case.
- **Severity:** Info.

## Verification matrix (what the brief asked, what the code does)

| Brief check | Result | Where |
| --- | --- | --- |
| `release` uses git SHA at boot | Gateway: function correct, **never invoked** (H1). Scraper: uses `APP_VERSION` instead (M2). | `sentry_setup.py:97`, `observability/__init__.py:13-43`, `scraper/observability.py:74` |
| `traces_sample_rate ≤ 0.1` in prod | Default 0.1; not clamped at runtime (L1). | `sentry_setup.py:94`, `scraper/observability.py:71` |
| `before_send` filter strips PII | Wired (I1), but traversal has gaps (M1). | `sentry_setup.py:23-55, 98` |
| `environment=` matches `ENVIRONMENT` env var | Matches when set; defaults diverge from `APP_ENVIRONMENT` (H2). | `sentry_setup.py:96` vs `server.py:553` |

## Recommended remediations, in order

1. Wire `init_sentry(platform="backend")` into `server.py` lifespan
   startup — resolves H1 and is a prerequisite for the rest to matter.
2. Align `environment=` with `APP_ENVIRONMENT`, and fix the
   `.env.example` default — resolves H2.
3. Recursively scrub events with depth cap; add a regex pass for
   `whsec_*`, `sk_live_*`, `sk-ant-*`; cover `breadcrumbs`,
   `exception.values[*].value`, `contexts`, and bytes-typed
   `query_string` — resolves M1.
4. Replace `APP_VERSION` in `scraper/observability.py` with the same
   `detect_release()`-style git-SHA resolver (or import it) — resolves M2.
5. Clamp `traces_sample_rate` to ≤ 0.1 whenever
   `environment == "production"` at init — resolves L1.
6. Log a WARNING when `detect_release()` returns `"unknown"` — resolves L3.
