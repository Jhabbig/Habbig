# Adversarial audit — Sentry initialization & scrubber

**Scope (per ask):** `gateway/sentry.py` + the `sentry_init` call site in `server.py`.

**Actual scope (per repo):** `gateway/sentry.py` does **not exist**. Sentry
code lives at:

- `gateway/observability/sentry_setup.py` (133 LoC) — backend `init_sentry`,
  `scrub_sensitive_data`, `set_user_context`, `tag_request`.
- `gateway/observability/__init__.py` (44 LoC) — `detect_release()` (git SHA
  resolver, used by `init_sentry`).
- `gateway/observability/sentry_api.py` (165 LoC) — admin-panel Sentry-API
  fetcher (read-only HTTP client, not the SDK init).
- `gateway/scraper/observability.py` (90 LoC) — **second** parallel
  `init_sentry`/`scrub_sensitive_data` implementation used by the scraper
  service.

**Date:** 2026-05-15
**Git SHA at audit:** `22cccb6` (branch `feature/platform-build`).
**Auditor focus (per ask):** `traces_sample_rate` cost control in prod;
`before_send` strips cookies/passwords/session tokens; release tag is a
git SHA.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 1     |
| High     | 2     |
| Medium   | 3     |
| Low      | 4     |
| Info     | 2     |

Total: **12** findings.

The three requested checks resolve as:

- **`traces_sample_rate` < 1.0 in prod:** PASS by default (0.1), but the
  env var is read at every `init_sentry` call with no upper bound — a
  typo (`1.0` instead of `0.1`) sends 10x traffic to Sentry and is
  silently accepted. Worth a hard cap. See M2.
- **`before_send` filter strips cookies/passwords/session-tokens:** PARTIAL
  PASS. Cookies are wiped wholesale; password/token/secret/card field
  hints are stripped from `request.data` and `extra`. **Gaps:** session
  tokens in `request.url` / `request.full_url` / breadcrumbs / log
  messages / exception strings are not scrubbed; JSON bodies parsed by
  Sentry into `request.data` lists (not dicts) are missed; the gateway's
  actual session-cookie name (`pm_gateway_session`) and CSRF token name
  (`_csrf`) are caught by the generic `cookie`/`x-csrf-token` header rule
  but **not** by the `_SENSITIVE_FIELD_HINTS` tuple (no `session` or
  `csrf` entry) — meaning a `session=` form field or `csrf` body param
  ships in cleartext. See C1 and H1.
- **Release tag uses git SHA:** PASS for the **gateway** path
  (`detect_release()` → `git rev-parse --short HEAD` with `NARVE_RELEASE`
  env override, `lru_cache`-d, falls back to `"unknown"`). FAIL for the
  **scraper** path: `gateway/scraper/observability.py:74` uses
  `os.getenv("APP_VERSION", "1.0.0")` — a hard-coded version string that
  almost certainly never gets bumped. Stack frames in Sentry will be tied
  to the wrong release. See H2.

**The single largest finding, however, is not in the requested files at all:**
the gateway server.py NEVER calls `init_sentry()`. The function exists,
the env vars are documented, the README claims the startup log line
"Sentry initialised platform=backend" confirms it's wired up — but the
call site is missing. See C1.

---

## Top 3 findings

### 1. [CRITICAL] Backend Sentry never initializes — `init_sentry()` is unused in `gateway/server.py`
**Location:** `gateway/server.py` (8609 LoC, no reference to
`init_sentry` anywhere) and `gateway/observability/sentry_setup.py:58`
(function defined but uncalled by the gateway).

`grep -n "init_sentry" gateway/server.py` returns no matches. `grep -rn
"sentry_sdk.init"` returns only `gateway/observability/sentry_setup.py:91`
(the definition) and `gateway/scraper/observability.py:68` (the scraper's
own copy). The lifespan handler at `server.py:357` runs config
validation, opens `HTTP_CLIENT`, runs migrations, starts the job worker
and scheduler — but never imports `observability` or calls
`init_sentry`. The only `observability` import in the gateway request
path is `admin_routes.py:1164` (`from observability.sentry_api import
fetch_sentry_summary` — that's the admin-panel HTTP fetcher, not the
SDK).

Consequence: **every uncaught exception in the gateway is logged locally
and lost.** The whole audit subject — `traces_sample_rate`,
`before_send`, release tag — is moot because the SDK is never bound to
the FastAPI app. The admin "System Health → Sentry" tab will keep
showing a count because it queries the Sentry REST API directly via
`SENTRY_AUTH_TOKEN`, so the panel **looks healthy** while the gateway
is in fact emitting zero events. This is the worst possible failure
mode for an observability tool: silent.

`README.md:131` claims `"Restart the gateway. The startup log line
'Sentry initialised platform=backend' confirms it's wired up."` —
that log line is never emitted because `init_sentry` never runs. The
README is wrong, or the call site was deleted in a refactor (this
repo's commit history shows recent observability/admin re-organization).

The scraper path is fine (`scraper/main.py:35` calls `init_sentry`
before FastAPI is constructed, exactly as the dashboards do — see
`centralbank-dashboard/server.py:28`, `whale-dashboard/server.py:29`,
etc.). Only the gateway is broken.

**Fix:** add `from observability import init_sentry` near the top of
`server.py` and `init_sentry(platform="backend")` immediately before
`app = FastAPI(...)` at `server.py:486` — exactly as the scraper does
at `scraper/main.py:31–35`. Returning bool from the function means the
caller can log if init was skipped (DSN unset).

**Why CRITICAL and not HIGH:** this isn't a quality regression in a
hardened pipeline — it's the entire feature being absent in prod. Every
unhandled 500, every database-locked, every payment-webhook failure
since this regressed has gone unreported.

---

### 2. [HIGH] `before_send` scrubber misses session/csrf-named body fields and JSON-list bodies; cookie value is the only thing protecting `pm_gateway_session` token from leak via Sentry
**Location:** `gateway/observability/sentry_setup.py:16–55`,
`gateway/scraper/observability.py:14–45`.

The two `before_send` implementations are byte-for-byte duplicates
modulo the `query_string` clause and `extra` clause (the scraper copy
keeps `extra` but drops the `query_string` cleanup at line 45–47 of the
gateway version). Both share these gaps:

1. **`_SENSITIVE_FIELD_HINTS` is missing `session`, `csrf`, `xsrf`,
   `auth`, `cookie`, `jwt`.** The gateway's session cookie is named
   `pm_gateway_session` (`server.py:236`); the CSRF cookie/form field is
   named `_csrf` / `csrf_token` (`server.py:1074`). A request that
   submits these as **form fields** rather than cookies (any AJAX call
   that mirrors them into the body for double-submit CSRF, or a misused
   `session=` query param) will ship them to Sentry verbatim. The
   `cookies` dict scrubber on line 38 wipes **all** cookies wholesale —
   that's defensible — but the field-name match is hint-substring-based
   and `session`, `csrf`, `cookie`, `jwt` aren't in the list.
2. **`request.data` is only walked if `isinstance(data, dict)`.** When
   the request body is a JSON array (e.g. `POST /api/forecasts/bulk`
   with a list of dicts) or a top-level string/blob, the entire body
   is included in the event untouched. Sentry's Python SDK does parse
   JSON bodies into `request.data`, and the scrubber must descend into
   nested structures. As written it is a single-level shallow pass.
3. **`request.url` / `request.full_url` / breadcrumbs / log message
   strings / exception `args` are never scanned.** A path like
   `/api/login?token=abc123` will be partially handled by the
   `query_string` clause (gateway only — scraper drops it), but only
   if `query_string` is a string. Sentry's `breadcrumbs` array — which
   carries the last ~100 logged events including outgoing HTTP calls —
   is not touched at all. Any `httpx` outbound request that included
   `Authorization` will end up here unscrubbed.
4. **No recursion into nested dicts.** `extra={"meta": {"password":
   "..."}}` is not redacted because the inner dict isn't walked.

**Fix:**
- Add `"session", "csrf", "xsrf", "auth", "jwt", "cookie"` to
  `_SENSITIVE_FIELD_HINTS` in both files (or, better, replace the two
  files with one shared module — see L3).
- Recursively walk `event["request"]["data"]` and `event["extra"]`
  (limit depth to ~6 to avoid pathological inputs).
- Scrub `event["request"]["url"]` query string fragments the same way
  the existing `query_string` clause does.
- Walk `event["breadcrumbs"]` and redact any `data.headers` /
  `data.url` query string in HTTP breadcrumbs.
- Replace the shallow `data` check with `_walk_and_redact(...)` shared
  between data/extra/breadcrumbs.

**Why HIGH and not CRITICAL:** the cookie wipe and header wipe still
catch the dominant leak path (anything Sentry parses out of headers
and the `cookies` dict). The HIGH cases above require an unusual call
shape — JSON-list bodies, double-submit CSRF in form fields, log
breadcrumbs carrying auth headers. But since C1 means Sentry isn't
running at all today, the moment C1 is fixed, the gateway will start
shipping these blind spots immediately.

---

### 3. [HIGH] Scraper release tag is `APP_VERSION` env var (default `"1.0.0"`) — not the git SHA
**Location:** `gateway/scraper/observability.py:74`.

```python
release=os.getenv("APP_VERSION", "1.0.0"),
```

`APP_VERSION` is documented in `gateway/README.md:110` as "Release tag
attached to Sentry events for source-map and regression tracking" with
default `1.0.0`. There is no automation in `start_dashboards.sh` or any
deploy script that bumps `APP_VERSION` per release. Every scraper
release for the lifetime of this code base will therefore tag every
event with `release: 1.0.0`, defeating regression tracking entirely —
Sentry's "first seen in" and "regressed in" features rely on monotonic
release identifiers, and a static string makes those panels useless.

The gateway path does this correctly: `sentry_setup.py:97` calls
`detect_release()` which prefers `NARVE_RELEASE` env, falls back to
`git rev-parse --short HEAD`, then `"unknown"`. The asymmetry is the
problem — the scraper sidecar should not have its own divergent
release-resolution policy.

**Fix:** replace the `os.getenv("APP_VERSION", ...)` line with the same
`detect_release()` helper. Easiest path is to import from the gateway
observability package; if package layout truly must stay independent
(per the docstring at `scraper/observability.py:3`), duplicate the
4-line `git rev-parse --short HEAD` block from `observability/__init__.py`
into `scraper/observability.py`.

**Why HIGH:** observability data with the wrong release tag is worse
than no observability data — it lets the team **believe** they have
regression tracking while every event lands in the same `1.0.0`
bucket.

---

## All findings

### CRITICAL

**C1.** Gateway never calls `init_sentry()`. See "Top 3 #1" above.

### HIGH

**H1.** `before_send` scrubber misses session/csrf field names, JSON
lists, breadcrumbs, and nested dicts. See "Top 3 #2" above.

**H2.** Scraper `release=os.getenv("APP_VERSION", "1.0.0")` defeats
regression tracking. See "Top 3 #3" above.

### MEDIUM

**M1.** Two duplicate `before_send` implementations. `gateway/observability/sentry_setup.py:23`
and `gateway/scraper/observability.py:21` are byte-for-byte identical
except for the `query_string` clause (gateway has it, scraper doesn't).
Drift is inevitable — the next time someone adds a hint to one copy,
the other will silently ship secrets. Fix: have the scraper import from
the gateway package, or factor a shared `_sentry_scrub.py` consumed by
both.

**M2.** `traces_sample_rate` and `profiles_sample_rate` are read from
env vars (`SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`,
default `0.1` each) with **no upper-bound clamp**. A misconfigured
deploy that sets `SENTRY_TRACES_SAMPLE_RATE=1.0` (or, worse, `10`,
which the SDK silently treats as 1.0) sends every transaction. Sentry's
event quota is the typical bottleneck for cost. Recommend `min(0.5,
float(os.getenv(...)))` or a startup assertion that warns on values >
0.2 outside `ENVIRONMENT=development`.

**M3.** `environment=os.getenv("ENVIRONMENT", "production")` defaults
to `production`. That's the safe-by-default direction (a forgotten
staging env shows up as prod and is louder than the reverse), but it
also means **every dev/CI run that has `SENTRY_DSN` set will pollute
the prod environment in Sentry.** README.md:109 documents
`ENVIRONMENT` as `production | staging | dev`. Recommend documenting
in `RUNBOOK.md` that all non-prod hosts must set `ENVIRONMENT` or
unset `SENTRY_DSN`.

### LOW

**L1.** `set_user_context` (`sentry_setup.py:106`) hashes user id but
truncates the SHA-256 to **16 hex chars (8 bytes / 64 bits)**. Truncating
SHA-256 to 64 bits is fine for an opaque user pseudonym, but the comment
on line 110 says "raw internal ids never leave the server" which
overstates the safeguard — 64 bits at narve.ai's user-count scale will
not collide, but a determined Sentry-data-recipient (vendor, SRE) can
trivially brute-force the user_id → hash mapping because the full
user_id space is small (sequential ints). If the goal is privacy, use
a server-side secret salt: `hmac.new(SECRET, f"narve:{uid}".encode(),
sha256)`. If the goal is just stable correlation, the current behavior
is fine — but rewrite the comment.

**L2.** `set_user_context` silently drops the `email` parameter (the
docstring says "Email is intentionally dropped"). The parameter is still
in the signature, so callers may believe it's being sent. Remove the
parameter entirely, or accept it and `hashlib.sha256` it too.

**L3.** `_SENSITIVE_HEADER_NAMES` is missing common auth header
variants: `proxy-authorization`, `x-api-key`, `x-auth-token`. Any
embedded widget request (the public API exposes `X-API-Key` per
`server.py:489`-ish OpenAPI description) will land in Sentry events
unredacted.

**L4.** `detect_release()` (`observability/__init__.py:31`) runs `git
rev-parse --short HEAD` with `timeout=2`. On a container where the
working tree is *not* a git checkout (the deploy pipeline likely
docker-COPYs without the `.git` directory), this falls through to
`"unknown"`. The `NARVE_RELEASE` env override exists for exactly this
reason, but it's not documented in README's env-var table and there is
no deploy automation setting it. Either document it in README or have
the Dockerfile / `start_dashboards.sh` set
`NARVE_RELEASE=$(git rev-parse --short HEAD)` before exec.

### INFO

**I1.** `before_send` swallows all exceptions with a bare `except`
(`sentry_setup.py:53`). Comment says "never crash while scrubbing"
which is defensible — but it also means if the scrubber itself has a
bug (`AttributeError`, etc.), unscrubbed events ship through. Logging
the exception at DEBUG would help during development without changing
prod behavior.

**I2.** `sentry_api.py:115` builds `Authorization: Bearer <token>` and
passes it to `httpx.AsyncClient` — fine — but the `summary["error"]`
field at line 161 stringifies the exception (`str(e)[:200]`). If httpx
ever raises with the auth header in `__str__` (it doesn't currently, but
the contract isn't documented), the token would leak through `/admin/api/sentry`
back to the admin browser. Recommend scrubbing the error string against
the token before storing.

---

## What was NOT in scope but is adjacent and worth a separate audit

- The **frontend Sentry DSN** (`SENTRY_DSN_PUBLIC`) at
  `gateway/static/sentry-boot.js` was not audited here. README.md:118
  claims it lives in a separate Sentry project so a leaked public key
  cannot read backend errors — verify this is true in the Sentry org
  config.
- `admin_routes.py:1164` exposes a `/admin/api/sentry` endpoint backed
  by `fetch_sentry_summary`. Confirmed it never returns the
  `SENTRY_AUTH_TOKEN` (good), but the issue **permalinks** it returns
  are user-controlled strings the admin browser will render — at line
  140–145 of `sentry_api.py` there is a guard forcing the prefix to
  `http://` / `https://` to block `javascript:` URIs. That guard is
  correct. No finding.
- The CSP / static `sentry-boot.js` integration is out of scope here.

---

## Repro / commands

```
grep -n "init_sentry\|sentry_sdk.init" gateway/server.py
# (no output — C1)

grep -rn "init_sentry\|sentry_sdk.init" gateway/ --include='*.py' \
  | grep -v /tests/
# gateway/scraper/observability.py:68:    sentry_sdk.init(
# gateway/scraper/main.py:35:_SENTRY_ACTIVE = init_sentry(platform="scraper")
# gateway/observability/sentry_setup.py:91:    sentry_sdk.init(
# (no gateway/server.py hit — C1 confirmed)

diff gateway/observability/sentry_setup.py gateway/scraper/observability.py
# Two near-identical files (M1)

grep -n "release=" gateway/observability/sentry_setup.py gateway/scraper/observability.py
# sentry_setup.py:97:        release=detect_release(),
# scraper/observability.py:74:        release=os.getenv("APP_VERSION", "1.0.0"),
# (H2 confirmed — asymmetry)
```

---

## Suggested fix order

1. **C1** — add `init_sentry(platform="backend")` in `gateway/server.py`
   before `app = FastAPI(...)`. One-line fix, restores observability.
2. **H1** — expand `_SENSITIVE_FIELD_HINTS`, add nested-dict walker,
   scrub breadcrumbs. Pair with C1 so the first events landing in
   Sentry are already clean.
3. **H2** — point scraper `release=` at `detect_release()` (or a local
   copy of the git-SHA helper).
4. **M1** — collapse the two scrubber implementations into one shared
   module.
5. **M2** — clamp `traces_sample_rate` to ≤ 0.5 in production.
6. **L1–L4, I1–I2** — opportunistic cleanup, no urgency.

---

**Auditor's note on requested vs actual scope:** the ask said
"`gateway/sentry.py`". There is no such file. The closest matches
(`gateway/observability/sentry_setup.py`, `sentry_api.py`,
`observability/__init__.py`) were audited in full, plus the
parallel scraper implementation (which mirrors the requested checks)
and the gateway `server.py` for the missing call site. All four
files are covered above.
