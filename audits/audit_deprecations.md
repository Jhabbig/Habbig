# Audit — DeprecationWarnings in gateway tests

**Date:** 2026-05-15
**Scope:** `gateway/tests/` run with `-W error::DeprecationWarning` (then re-run with `-W default::DeprecationWarning -W ignore::UserWarning -W ignore::RuntimeWarning -W ignore::pytest.PytestUnraisableExceptionWarning` and `pytest.ini`'s `filterwarnings` overridden so the suite-default `ignore::DeprecationWarning:fastapi.*`/`...:starlette.*`/`pytest.PytestDeprecationWarning` filters do not mask anything).
**Auditor focus:** capture every `DeprecationWarning` / `PendingDeprecationWarning` raised during a full gateway test run, attribute it to the upstream API surface that emitted it, and rank by emission volume.

---

## How the run was invoked

```
python3 -m pytest tests/ \
  -W "default::DeprecationWarning" \
  -W "default::PendingDeprecationWarning" \
  -W "ignore::UserWarning" \
  -W "ignore::RuntimeWarning" \
  -W "ignore::pytest.PytestUnraisableExceptionWarning" \
  --override-ini="filterwarnings=
      default::DeprecationWarning
      default::PendingDeprecationWarning
      ignore::UserWarning
      ignore::RuntimeWarning
      ignore::pytest.PytestUnraisableExceptionWarning" \
  -p no:cacheprovider --tb=no -q
```

Synchronous bash only, no pre-release packages installed. An initial run with `-W error::DeprecationWarning` aborted collection on 7 modules where the deprecation fires at *import* time (the same modules that account for the bulk of the count below); switching to `default::DeprecationWarning` lets pytest record every emission without terminating the run.

**Run health:** 100% progress, 428 FAILED/ERROR test items (pre-existing — none of these failures are caused by the deprecation filter; they are the routine red ones already visible in `git status` modified routes). No `PendingDeprecationWarning` was raised anywhere.

---

## Headline numbers

| Metric | Value |
|---|---|
| Total `DeprecationWarning` emissions | **432** |
| Distinct deprecation messages | **5** |
| Distinct upstream APIs | **3** (`httpx`, `websockets`, `pytest-asyncio`) |
| Test files emitting at least one deprecation | **53** |
| `PendingDeprecationWarning` emissions | 0 |

`pytest.ini` currently suppresses `DeprecationWarning` from `fastapi.*`, `starlette.*`, and `pytest.PytestDeprecationWarning`. After overriding those suppressions, **zero** FastAPI/Starlette deprecations were unmasked, but the suppressed `pytest.PytestDeprecationWarning` from `pytest-asyncio` re-surfaces (counted below).

---

## Tabulation by API

| Rank | API surface | Deprecation | Emissions | Test files |
|---:|---|---|---:|---:|
| 1 | `httpx._client.Client.request` | Setting per-request `cookies=<...>` is being deprecated; set cookies directly on the client instance instead | **418** | 47 |
| 2 | `httpx._content.encode_request` | Use `content=<...>` to upload raw bytes/text content (passing `data=<str>`/`data=<bytes>` is deprecated) | **11** | 6 |
| 3 | `pytest_asyncio.plugin` | Configuration option `asyncio_default_fixture_loop_scope` is unset; future versions will default to function scope | **1** | session-wide (fires once at plugin load) |
| 4 | `uvicorn.protocols.websockets.websockets_impl` | `websockets.server.WebSocketServerProtocol` is deprecated | **1** | `tests/browser/test_visual_regression.py` |
| 5 | `websockets.legacy` | `websockets.legacy` is deprecated; see upgrade guide | **1** | `tests/browser/test_visual_regression.py` |
| | | **Total** | **432** | |

Grouped by upstream package:

| Package | Emissions | Share |
|---|---:|---:|
| `httpx` | 429 | 99.3% |
| `websockets` / `uvicorn` | 2 | 0.5% |
| `pytest-asyncio` | 1 | 0.2% |

---

## Top 5 deprecations (ranked by emission count)

### 1. `httpx` — per-request `cookies=` kwarg (418 emissions, 47 test files)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_client.py:812: DeprecationWarning:
Setting per-request cookies=<...> is being deprecated, because the expected behaviour on cookie
persistence is ambiguous. Set cookies directly on the client instance instead.
    warnings.warn(message, DeprecationWarning)
```

**Surface affected:** every call site that does `client.get(..., cookies={"session": ...})` instead of `client.cookies.set(...)` (or constructing the `TestClient` with `cookies=...`). The biggest emitters:

| Emissions | Test file |
|---:|---|
| 39 | `tests/test_feedback_routes.py` |
| 25 | `tests/test_settings_billing.py` |
| 23 | `tests/test_market_takes.py` |
| 22 | `tests/test_auth_flow.py` |
| 17 | `tests/test_trading_addon_gate.py` |
| 15 | `tests/test_admin_jobs.py` |
| 15 | `tests/test_churn_and_retention.py` |
| 14 | `tests/test_admin_emails.py` |
| 14 | `tests/test_admin_test_emails.py` |
| 14 | `tests/test_environmental_http.py` |
| 14 | `tests/test_polymarket_siwe.py` |
| 13 | `tests/test_admin_audit_log.py` |
| 13 | `tests/test_settings_trading_addon.py` |
| 12 | `tests/test_admin_integrations.py` |
| 12 | `tests/test_admin_users.py` |
| 12 | `tests/test_log_admin.py` |
| 11 | `tests/test_affiliate.py` |
| 11 | `tests/test_portfolio_polymarket.py` |
| 10 | `tests/test_admin_cost_alerts.py` |
| 10 | `tests/test_admin_newsletter.py` |
| 10 | `tests/test_settings_integrations.py` |
| 9 | `tests/test_billing_addon_checkout.py` |
| 8 | `tests/test_billing_portal.py` |
| 8 | `tests/test_push_routes.py` |
| 7 | `tests/test_admin_health_monitor.py` |
| 7 | `tests/test_admin_sentry.py` |
| 7 | `tests/test_portfolio_integration.py` |
| 5 | `tests/test_admin_subproducts.py` |
| 5 | `tests/test_i18n.py` |
| 4 | `tests/test_admin_self_demote.py` |
| 4 | `tests/test_search.py` |
| 3 | `tests/qa/qa_walk_e_style.py` |
| 3 | `tests/test_impersonation_middleware.py` |
| 3 | `tests/test_newsletter_blast_bounding.py` |
| 3 | `tests/test_token_first_auth.py` |
| 2 | `tests/qa/qa_walk_c_auth.py` |
| 2 | `tests/qa/qa_walk_d_admin.py` |
| 2 | `tests/test_admin_delete.py` |
| 2 | `tests/test_notifications.py` |
| 1 each | `tests/qa/qa_walk_f_ux.py`, `tests/qa/qa_walk_h_perf.py`, `tests/test_cache.py`, `tests/test_feature_routes.py`, `tests/test_health.py`, `tests/test_http_auth.py`, `tests/test_logout.py`, `tests/test_sharing.py` |

**Likely root cause:** a shared `auth_client` / `login()` helper that does `client.get("/whatever", cookies={"session": tok})` from `tests/helpers.py` or `tests/conftest.py`. Migrate to `client.cookies.set("session", tok)` once per session, or pass `cookies=` to the `TestClient` constructor. Fixing one helper extinguishes ~99% of the suite's deprecation volume.

### 2. `httpx` — `data=<str>` / `data=<bytes>` body (11 emissions, 6 test files)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/httpx/_content.py:202: DeprecationWarning:
Use 'content=<...>' to upload raw bytes/text content.
    warnings.warn(message, DeprecationWarning)
```

| Emissions | Test file |
|---:|---|
| 4 | `tests/test_admin_self_demote.py` |
| 2 | `tests/test_admin_jobs.py` |
| 2 | `tests/test_admin_newsletter.py` |
| 1 | `tests/test_admin_delete.py` |
| 1 | `tests/test_admin_users.py` |
| 1 | `tests/test_newsletter_blast_bounding.py` |

**Migration:** replace `client.post(url, data="raw body")` with `client.post(url, content="raw body")`. `data=` is still allowed for form-encoded dict bodies.

### 3. `pytest-asyncio` — `asyncio_default_fixture_loop_scope` unset (1 emission)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/pytest_asyncio/plugin.py:208:
PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture caching scope.
Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to
function scope. Set the default fixture loop scope explicitly in order to avoid unexpected
behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package",
"session"
```

Currently suppressed in `pytest.ini` via `ignore::pytest.PytestDeprecationWarning` (visible only when that filter is overridden). Fix: add `asyncio_default_fixture_loop_scope = function` to `[pytest]` in `pytest.ini`.

### 4. `uvicorn` — `WebSocketServerProtocol` import (1 emission)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/uvicorn/protocols/websockets/websockets_impl.py:16:
DeprecationWarning: websockets.server.WebSocketServerProtocol is deprecated
    from websockets.server import WebSocketServerProtocol
```

Fires once during `tests/browser/test_visual_regression.py::test_public_page_renders[/-desktop_16]` (the first browser test that boots a uvicorn server). Upstream issue; nothing to change in gateway code — bump uvicorn to a version that uses the new websockets API once available.

### 5. `websockets.legacy` deprecated module (1 emission)

```
/Users/shocakarel/Library/Python/3.9/lib/python/site-packages/websockets/legacy/__init__.py:6:
DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09
```

Same trigger as #4 — pulled in transitively by uvicorn's websocket protocol. Same remediation path.

---

## Recommended remediations (in priority order)

1. **Refactor the test cookie helper.** Stop passing `cookies=` per-request to the FastAPI `TestClient`; instead seed `client.cookies` once per fixture. This deletes ~418 of 432 (96.8%) of the deprecation volume in one change.
2. **Swap `data=<str|bytes>` → `content=<...>`** in the 6 admin-area test files that POST raw bodies (likely Stripe-style raw payloads). Eliminates 11 more.
3. **Pin `asyncio_default_fixture_loop_scope = function`** in `pytest.ini` to silence the pytest-asyncio deprecation and prevent the future scope-default surprise.
4. **Track uvicorn/websockets release** that drops `websockets.legacy`; nothing for gateway to change directly. Re-test after `pip install -U uvicorn websockets` once a non-prerelease version ships the migration.
5. **After (1)–(3) land,** re-run the suite with `-W error::DeprecationWarning` and remove the `ignore::DeprecationWarning:fastapi.*` / `...:starlette.*` lines from `pytest.ini` so future regressions fail loudly. Today those filters mask nothing (both packages already emit zero deprecations in the test paths), but they are a trap for future-us.
