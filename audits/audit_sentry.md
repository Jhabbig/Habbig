# Adversarial audit — Sentry init + scrubbing

- Files audited:
  - `/Users/shocakarel/Habbig/gateway/observability/sentry_setup.py` (133 LOC) — primary backend init + scrubber
  - `/Users/shocakarel/Habbig/gateway/observability/__init__.py` (44 LOC) — `detect_release()` git-SHA resolver
  - `/Users/shocakarel/Habbig/gateway/observability/sentry_api.py` (165 LOC) — admin REST polling
  - `/Users/shocakarel/Habbig/gateway/scraper/observability.py` (90 LOC) — sibling scraper init (parallel surface)
  - `/Users/shocakarel/Habbig/gateway/static/sentry-boot.js` (49 LOC) — browser-side init
  - `/Users/shocakarel/Habbig/gateway/server.py` (cross-checked for any `init_sentry` / `sentry_sdk.init` calls)
  - `/Users/shocakarel/Habbig/gateway/config.py` (validator for `SENTRY_TRACES_SAMPLE_RATE`)
  - `/Users/shocakarel/Habbig/gateway/.env.example` (env contract)
  - `/Users/shocakarel/Habbig/gateway/queries/integrations.py` (admin status row)
  - `/Users/shocakarel/Habbig/gateway/tests/test_sentry.py`, `tests/test_admin_sentry.py`
- Note: the audit brief refers to `gateway/sentry.py`; the actual location is
  `gateway/observability/sentry_setup.py`. There is no `gateway/sentry.py`.
- Date: 2026-05-15
- Auditor focus: DSN sourced from env (not hardcoded), `before_send` strips PII
  (cookies, auth tokens, password fields), release tag uses git SHA via
  `detect_release()`, `traces_sample_rate` not 1.0 in production (cost
  control). Plus adjacent risks: PII echo via `set_user`, scrubber bypass
  paths, frontend DSN segregation, dead-init wiring, send-default-pii flag.
- Method: full static read of every Sentry surface; traced each env-derived
  value end-to-end; checked every call site for `init_sentry`,
  `sentry_sdk.init`, `set_user`, `set_user_context`; ran the scrubber's
  field-name list against the SDK's standard event shape (request.headers,
  request.cookies, request.data, request.query_string, extra, breadcrumbs,
  exception.values, contexts, user); verified `detect_release()` against
  `Path(__file__).resolve().parents[2]` actually resolves to the git root;
  cross-referenced .env.example, config.py validator, and runtime defaults.

## Severity counts

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High     | 1 |
| Medium   | 3 |
| Low      | 4 |
| Info     | 3 |

Total: 11 findings.

## Top 3 issues (by exploitability × blast-radius)

1. **H1 — Gateway backend never actually calls `init_sentry()`; backend
   Sentry is silently dead.** `gateway/observability/sentry_setup.py:58`
   defines `init_sentry`, and `observability/__init__.py:9` re-exports it,
   but `grep -rn "init_sentry"` across the gateway production tree returns
   zero callers in `gateway/` (only `gateway/scraper/main.py:35` calls its
   own sibling copy, and each subproduct dashboard calls its local
   `_observability.init_sentry`). `gateway/server.py` does not import
   `observability` at all. Net effect: setting `SENTRY_DSN` produces no
   backend events from the main FastAPI app. The admin "System Health"
   row at `queries/integrations.py:376` reports "backend DSN configured"
   based on env presence, so the admin panel will show Sentry as
   "connected" while no events are being captured. This is the single
   highest-impact finding — the entire purpose of the file (error
   visibility for the production server) is currently unmet. Fix: call
   `from observability import init_sentry; init_sentry(platform="backend")`
   at the top of `gateway/server.py`, BEFORE the FastAPI app is created
   (the SDK's auto-instrumentation needs to be active before middlewares
   are added).

2. **M1 — Scrubber misses several event shapes that routinely carry PII /
   credentials.** `scrub_sensitive_data` in `sentry_setup.py:23-55` only
   examines `event["request"].headers / cookies / data / query_string`
   and `event["extra"]`. It does NOT scrub:
   - `event["breadcrumbs"]["values"][i]["data"]` — every HTTP breadcrumb
     the SDK auto-records contains a `data.url`, `data.method`, and
     (for some integrations) request bodies. URLs from Stripe/Polymarket
     callbacks routinely embed `client_secret`, `setup_intent`, `state`,
     and bearer tokens in query strings.
   - `event["exception"]["values"][i]["stacktrace"]["frames"][j]["vars"]`
     — local-variable capture is on by default and will include any
     `password`, `token`, `api_key` Python local in the failing frame.
     `send_default_pii=False` reduces but does not eliminate this; the
     SDK still serializes locals.
   - `event["contexts"]` (e.g. `runtime`, `os`, but also any custom
     contexts the codebase sets via `set_context`).
   - `event["request"]["env"]` — WSGI env dict; can contain
     `HTTP_AUTHORIZATION`, `HTTP_COOKIE`, `HTTP_X_API_KEY`.
   - Cookies are blanket-filtered (every cookie value → `[Filtered]`),
     which is correct, but the equivalent treatment is not applied to
     `request.env` or breadcrumb `data` payloads.

   Fix: extend the scrubber to walk `breadcrumbs.values[*].data`,
   `exception.values[*].stacktrace.frames[*].vars`, `request.env`, and
   to apply the same `_SENSITIVE_FIELD_HINTS` substring match recursively.
   Consider replacing the bespoke implementation with `EventScrubber` from
   `sentry_sdk.scrubber`, which handles all these shapes out of the box
   and is the SDK's documented recommendation.

3. **M2 — `sentry-boot.js` hard-codes `tracesSampleRate: 0.1` and is
   loaded by no HTML page, but is still bundled in the deploy
   payload.** `gateway/static/sentry-boot.js:21` ignores any environment
   override (the backend `SENTRY_TRACES_SAMPLE_RATE` env var has no
   parallel on the frontend), and the file is included in
   `scripts/push_to_server.sh:102` but a recursive grep across
   `gateway/static/*.html` finds no `<script src=".../sentry-boot.js">`
   reference, and no Python code injects the `__SENTRY_CONFIG__` window
   object the boot script depends on (`admin.html:979` only *reads*
   `window.__SENTRY_CONFIG__.environment` defensively). Net effect:
   frontend Sentry is dead code shipped to every production deploy. If
   it were wired up, the hardcoded 0.1 rate would prevent the runtime
   throttle but would also defeat any future need to dial it down
   without a code change. Also note `release: cfg.release || "1.0.0"`
   falls back to a literal `"1.0.0"` string rather than a git SHA, so
   even after wiring, frontend events would group under a single fake
   release.

## All findings

### High

**H1 — Backend `init_sentry()` is unreachable from the gateway.**
See top-3 #1. Setting `SENTRY_DSN` in prod does not capture backend
errors. The "platform=backend" tag has never been written.
**Severity: High** — observability gap, not a vulnerability per se,
but the audit brief specifically asks for the init's correctness.

### Medium

**M1 — Scrubber misses breadcrumbs, locals, env, and contexts.**
See top-3 #2.

**M2 — Frontend Sentry boot is dead code with hardcoded sample rate.**
See top-3 #3.

**M3 — `detect_release()` shells out at every cold start with a 2 s
timeout and falls back to the literal `"unknown"` string.**
`observability/__init__.py:30-43` runs `git rev-parse --short HEAD`
synchronously the first time `init_sentry` (or any caller) hits it.
On hosts where the deploy is a tarball / Docker image without a `.git`
directory (the recommended deploy shape), this always returns
`"unknown"`, defeating the audit's "release tag uses git SHA" check.
There is an env override (`NARVE_RELEASE`) but `.env.example` does not
mention it, and the deploy script (`scripts/push_to_server.sh`) does
not set it. Fix: have the deploy script export
`NARVE_RELEASE=$(git rev-parse --short HEAD)` before restart, and add
the var to `.env.example` so its existence is documented. Also: in a
non-git env, the 2 s `subprocess.check_output` runs every cold start
before `lru_cache` populates — for short-lived workers this adds 2 s
startup latency.

### Low

**L1 — `set_user_context` accepts an `email` parameter that it then
silently drops.** `sentry_setup.py:106` signature is
`set_user_context(user_id, email=None, tier=None)`, but the body uses
only `user_id` (hashed) and `tier`. The `email` parameter is dead —
it implies the function will send it, but the comment on line 110
says "Email is intentionally dropped". Future callers reading the
signature could reasonably assume the email is being sent. Either
drop the parameter from the signature or document the no-op behavior
in the docstring header (not just an inline comment). The function
is currently called from zero production sites, so the practical
risk is nil today, but it is a foot-gun waiting for a future caller.

**L2 — `_SENSITIVE_FIELD_HINTS` substring match is case-folded
correctly but matches benign keys.** The list includes the bare token
`"key"`, so any data field named `api_key`, `cache_key`, `lookup_key`,
`primary_key`, `chart_key`, `unique_key`, etc. is replaced with
`[Filtered]` — including non-secret values the team may want for
debugging. Trade-off is intentional (better to over-filter than
under-filter), but worth noting because it can mask the root cause of
bugs whose stack trace would have been disambiguated by the masked
`cache_key`. Consider an allowlist of known-benign suffixes or a
whole-word match for `key` (e.g. `\bkey\b`) while keeping the broader
substring match for `password`, `secret`, `token`.

**L3 — `SENTRY_DSN` is read once at process start and never refreshed.**
`init_sentry` line 63 calls `os.getenv("SENTRY_DSN", "")` exactly
once. Operators rotating the DSN (e.g. after the public-key/private-key
mix-up the file header warns about) must restart every gateway worker.
This is standard SDK behavior but the file header's warning about
public vs private keys deserves a parallel warning that DSN rotation
requires a restart.

**L4 — `traces_sample_rate` is correctly defaulted to 0.1 and validated
to ≤1.0 (`config.py:140`), but `profiles_sample_rate` has the same
0.1 default and validator — and profiling is significantly more
expensive than tracing.** Sentry profiling on a busy FastAPI app at
10 % can add measurable CPU overhead. Consider dropping the profile
sample default to 0.01 in `sentry_setup.py:95` for production cost
control. (Audit brief specifically called out `traces_sample_rate
not 1.0 in production` — that is satisfied. This is the adjacent
finding.)

### Info

**I1 — DSN sourcing: `SENTRY_DSN` is read via `os.getenv` only.**
No hardcoded DSN anywhere in `sentry_setup.py`, `sentry_api.py`,
`scraper/observability.py`, or `static/sentry-boot.js`. The frontend
boot reads from `window.__SENTRY_CONFIG__.dsn` which would be injected
server-side (if it were wired up — see M2). Backend/frontend DSNs are
deliberately split (`SENTRY_DSN` vs `SENTRY_DSN_PUBLIC` in
`.env.example:66-67`), as documented in `sentry_setup.py:3-5`. **Audit
brief item PASSES.**

**I2 — `send_default_pii=False` is set (`sentry_setup.py:99`).** This
disables the SDK's automatic capture of cookies, request bodies, and
user IPs. Combined with the custom `before_send` it provides
defense-in-depth. **Audit brief PII item PASSES** subject to M1.

**I3 — Release tag uses git SHA via `detect_release()`
(`sentry_setup.py:97`).** The function falls back to `"unknown"`
rather than crashing, which is correct. **Audit brief release-tag
item PASSES** subject to M3 (deploy script must set
`NARVE_RELEASE` for tarball deploys).

## What the brief asked, summarised

| Brief item | Verdict | Detail |
| --- | --- | --- |
| DSN sourced from env, not hardcoded | PASS | `os.getenv("SENTRY_DSN", "")` only; no literals in any audited file (I1) |
| `before_send` filter strips PII | PARTIAL PASS | Cookies/auth headers/password-fields scrubbed; breadcrumbs, locals, env, contexts not (M1). `send_default_pii=False` (I2) |
| Release tag uses git SHA | PASS in git-checkout deploys | `detect_release()` chain works; tarball deploys need `NARVE_RELEASE` exported (M3) |
| `traces_sample_rate` ≠ 1.0 in production | PASS | Defaults to 0.1, validator caps at 1.0, env default in `.env.example` is 0.10 |

## Verification commands

- DSN literal check: `grep -rEn 'https://[a-zA-Z0-9]+@[a-zA-Z0-9.]+\.sentry\.io' /Users/shocakarel/Habbig/gateway/` → expected: no matches
- Backend init call sites: `grep -rn 'init_sentry\(' /Users/shocakarel/Habbig/gateway/ --include='*.py' | grep -v tests | grep -v scraper` → expected: zero matches in the active server bootstrap (this is the H1 finding)
- Scrubber unit tests: `cd /Users/shocakarel/Habbig/gateway && python -m pytest tests/test_sentry.py -q`
- Validator: `cd /Users/shocakarel/Habbig/gateway && python -m pytest tests/test_config_validator.py -q -k sample_rate`

## Sign-off

No code changes were made. The audit recommends wiring `init_sentry()` into
`gateway/server.py` (H1), extending the scrubber to cover breadcrumbs and
local variables (M1), and either wiring or deleting `sentry-boot.js` (M2).
