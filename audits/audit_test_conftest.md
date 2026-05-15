# Adversarial audit — `gateway/tests/conftest.py` + shared fixture files

**Scope:**
- `gateway/tests/conftest.py` (462 LoC — root conftest, autouse fixtures)
- `gateway/tests/_testdb.py` (56 LoC — shared in-memory sqlite + migration boot)
- `gateway/tests/helpers.py` (211 LoC — factories, CSRF helpers, Stripe signing)
- `gateway/tests/e2e/conftest.py` (291 LoC — gate bypass, e2e cleanup)
- `gateway/tests/qa/conftest.py` (337 LoC — qa walks, playwright fixtures)
- `gateway/tests/browser/conftest.py` (184 LoC — browser engine fixtures)

**Date:** 2026-05-15
**Auditor focus:** test-DB isolation (shared in-mem sqlite, per-test wipe, FK ordering), fixture cleanup between tests (cookies, rate-limit dicts, TTL cache, module globals), leaked `SITE_ACCESS_TOKEN` env (autouse scrub fixture), TestClient HTTPS setup (`base_url`, secure-cookie behaviour).

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0     |
| High     | 3     |
| Medium   | 5     |
| Low      | 4     |
| Info     | 2     |

Headline: the autouse `_scrub_site_access_token` and `_clear_module_testclient_cookies` fixtures are doing real work and are correctly authored. The harder problems are **shared in-memory DB with no per-test rollback wrapper**, **FK-violating cleanup order in `_e2e_clean_slate`**, and **HTTPS-cookie behaviour that is never exercised because every TestClient uses the default `http://testserver`**.

---

## Top 3 findings (ranked)

### 1. [HIGH] Shared in-memory sqlite has no per-test SAVEPOINT/ROLLBACK isolation — promised in docstring, never implemented

**Location:** `gateway/tests/_testdb.py:28-47` + `gateway/tests/conftest.py:241-260` (docstring "guarantee isolation with a SAVEPOINT/ROLLBACK wrapper").

```python
# _testdb.py
_conn = sqlite3.connect(":memory:", check_same_thread=False)
# ...
@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise
```

```python
# conftest.py:241-260 (docstring claim — no matching implementation anywhere)
# * The suite has ONE shared in-memory sqlite connection (tests/_testdb.py).
#   Migrating to per-test tmp-path DBs would break >1000 tests that share
#   state across cases, so the fixtures below ride the shared conn and
#   guarantee isolation with a SAVEPOINT/ROLLBACK wrapper.
```

The docstring advertises SAVEPOINT/ROLLBACK isolation; **grep shows zero `SAVEPOINT` and zero `ROLLBACK TO` usages in any fixture file**. The `_fake_conn` only does normal `commit()` on success and `rollback()` on exception — both at the outer connection, not nested. Per-test isolation is provided opportunistically by `_reset_global_test_state` (function-scoped autouse) which deletes from a hand-picked list of only **three** "pollution tables": `rate_limits`, `audit_log`, `engagement_events`. Every other table (users, sessions, predictions, source_credibility, subscriptions, invite_tokens, saved_predictions, saved_markets, followed_sources, notifications, …) accumulates rows for the whole test session.

Consequences observed in code:
- `make_user` uses a global monotonic counter (`_user_ctr`) to dodge UNIQUE collisions on `users.email/username` — masking the underlying leak. A test that uses `email="fixed@x.test"` (and many do — `test_token_first_auth`, `test_password_reset`, `test_auth_flow`) will 409 on the second run within the same pytest invocation unless they manually clear users first.
- `seed_basic` does `INSERT OR REPLACE` on `source_credibility('seed_source', …)` and unconditionally inserts into `predictions(source_handle='seed_source', market_id='poly:seed-market', …)` (lines 367-394). The predictions row accumulates — every test that pulls `seed_basic` adds another duplicate, so any later test that asserts `count == 1` for that market is brittle by test order.
- `clear_tables` in `helpers.py:203-211` exists as an escape hatch — but it has no FK awareness and silently swallows errors, so a caller passing tables in the wrong order gets no signal.

**Fix sketch:** wrap each function-scoped test in a real SAVEPOINT/ROLLBACK around the shared connection (the docstring's promise). Pseudocode:

```python
@pytest.fixture(autouse=True)
def _txn_savepoint(request):
    if not _module_uses_testdb(request.node.module.__name__):
        yield
        return
    _conn.execute("SAVEPOINT _per_test")
    yield
    _conn.execute("ROLLBACK TO SAVEPOINT _per_test")
    _conn.execute("RELEASE SAVEPOINT _per_test")
```

…but a careful audit of every TestCase.setUpClass call site is needed first — many class-scoped seeds (e.g. `test_api_public.TestAuth`) currently depend on data persisting across the function-scoped wipe, which is why the conftest carries a separate **class-scoped** `_maybe_force_shared_testdb_class` fixture (lines 48-60). A naïve SAVEPOINT wrapper would invert that contract.

**Why HIGH:** the discrepancy between docstring ("guarantee isolation") and reality (3-table best-effort wipe) is a footgun. Anyone reading the conftest will assume cross-test isolation that doesn't exist, leading to test-order-dependent failures that look like flakes.

---

### 2. [HIGH] `_e2e_clean_slate` deletes `users` after children, but children list is incomplete → silent FK violations or stale rows

**Location:** `gateway/tests/e2e/conftest.py:260-290`.

```python
wipeable = (
    "sessions", "invite_tokens", "password_resets",
    "user_market_views", "user_market_alerts",
    "market_movement_events",
    "saved_predictions", "saved_markets", "followed_sources",
    "subscriptions", "user_onboarding",
    "impersonation_actions",
)
yield
try:
    with db.conn() as c:
        for table in wipeable:
            try:
                c.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        # users last — FKs from the tables above
        try:
            c.execute("DELETE FROM users WHERE email LIKE '%@test.example' OR email LIKE '%@e2e.test'")
        except Exception:
            pass
```

Three problems:

1. **`PRAGMA foreign_keys = ON`** is enabled in `_testdb.py:30`. The `wipeable` list is **not exhaustive** for users-as-parent. Spot-checking the schema (per existing audits) shows at least the following tables reference `users.id`: `api_keys`, `subscriptions` (covered), `webhook_endpoints`, `user_features`, `user_predictions`, `audit_log`, `user_dashboard_views`, `user_preferences`, `user_notifications`, `user_collections`, `user_saved_views`, `user_referrals`, `user_gifts`, `user_sessions_management`, possibly `affiliate_referrals`. Any of those that hold a row for an `@e2e.test` user will FK-block the final `DELETE FROM users`. The blanket `try/except: pass` swallows the failure silently, **leaving the user row in place** — and because `make_user` is counter-suffixed, the next test seeds a fresh row, so the leak isn't visible until you run the whole suite and notice the in-memory DB has thousands of orphan user rows by the end.
2. **`'%@test.example' OR '%@e2e.test'`** — `make_user` defaults to `f"{uname}@test.example"`, but e2e fixtures and explicit `email=…` overrides in tests use other domains (`@test.local`, `qa-walks-*`, etc.). Anything outside those two patterns is never cleaned up.
3. **Ordering** within `wipeable` is alphabetic-ish, not FK-topological. With `foreign_keys=ON`, deleting `subscriptions` after rows that reference it (if any) is fine, but more importantly, deleting `users` before, say, `audit_log` rows that reference the user_id would be a hard FK violation. The current ordering only works because `audit_log` (which references `users`) isn't in the wipeable list — meaning audit_log just accumulates forever, which feeds back into finding #1.

**Why HIGH:** silent `except: pass` around the FK-bound `DELETE FROM users` means failures aren't visible in test output. The cleanup looks like it works.

**Fix:**
- Replace the silent except with `log.warning` on failure so leaks become visible.
- Drive the cleanup order from `PRAGMA foreign_key_list` or use `PRAGMA defer_foreign_keys = ON` for the duration of the transaction.
- Expand the email-LIKE filter to a single canonical test-domain suffix and require all fixtures to use it.

---

### 3. [HIGH] TestClient never uses HTTPS `base_url` — every secure-cookie path is silently untested

**Location:** `gateway/tests/conftest.py:295-302` and `gateway/tests/qa/conftest.py:61-64`, plus every module-level `client = TestClient(server.app)` in the suite.

```python
# conftest.py:295-302
@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    tc = TestClient(app)
    tc.cookies.clear()
    yield tc
```

`TestClient(app)` defaults to `base_url="http://testserver"`. Server code at `server.py:1225, 1569, 2140, 2203` sets cookie `secure=IS_PRODUCTION`. `IS_PRODUCTION` is forced off in the test harness (`_os.environ.pop("PRODUCTION", None)` at `conftest.py:267`), so `secure=False` is what tests exercise — and any logic that conditions on the request scheme (`request.url.scheme == "https"`, `X-Forwarded-Proto`, HSTS) is never hit by the in-process suite.

Concrete impact:
- HSTS header coverage in `test_security_headers.py` is incomplete by construction — the middleware can short-circuit HSTS emission on non-HTTPS requests, and the test never sees what production sees.
- The Stripe webhook hardening tests sign payloads using a fixture-set secret but TestClient never proves the cookie/header pipeline survives the Cloudflare HTTPS termination model that prod uses.
- The CSRF middleware (`server.py::CSRFMiddleware`) parses the body for `_csrf` on form posts; that path is exercised, but the `__Host-` cookie prefix variant (which **requires** HTTPS) is not.
- Grep confirms only **one** test file in the entire suite uses HTTPS: `test_changelog.py:252` (`base_url="https://narve.ai"`). All other ~150 test files inherit the HTTP default.

**Why HIGH:** the gateway runs behind Cloudflare; the request scheme it actually serves under is HTTPS. A test suite that never sees the HTTPS code path is missing real coverage of HSTS, secure-cookie attribute, SameSite=None behaviour under cross-site, and any middleware that branches on `request.url.scheme`.

**Fix:** add a `base_url="https://testserver"` to the canonical `client` fixture in `conftest.py:300`, and a second fixture `http_client` for the rare tests that need to assert the HTTP-redirect path. Then add a regression test that asserts the session cookie carries `secure=True` when `PRODUCTION=1` is set for one focused test.

---

## Other findings (Medium / Low / Info)

### [MED] `_scrub_site_access_token` correctly handles e2e but leaks env value into other tests' subprocesses

**Location:** `gateway/tests/conftest.py:73-100`.

The fixture flips `server.SITE_ACCESS_TOKEN = ""` on the **module attribute** for non-e2e tests, then restores it. It does **not** touch `os.environ["SITE_ACCESS_TOKEN"]`. This is fine for in-process FastAPI handlers because the module-level constant is what the middleware reads. But:

- `tests/e2e/conftest.py:46` sets `_os.environ["SITE_ACCESS_TOKEN"] = E2E_GATE_TOKEN` **at import time** — and that environ value persists for the rest of the pytest process even after e2e tests finish.
- Any test that spawns a subprocess (e.g. `tests/qa` if it forks uvicorn into a thread that re-reads env, or any test that shells out) inherits the e2e gate token.
- Conversely, `tests/conftest.py:266` does `_os.environ.pop("SITE_ACCESS_TOKEN", None)` **at module import** — which races with `tests/e2e/conftest.py:46`. Whichever module is collected last wins.

This is a **real ordering hazard**: under pytest's default file-ordered collection, the root `conftest.py` is imported before `tests/e2e/conftest.py`, so the env ends up *set* to the e2e value after e2e fixtures load. The autouse fixture correctly resets the **module attribute** for non-e2e tests, but `os.environ` is left dirty.

**Fix:** also pop `os.environ["SITE_ACCESS_TOKEN"]` inside `_scrub_site_access_token` for non-e2e tests (and restore in teardown). Or — better — never set the env var at all in e2e tests; set the module attribute directly the same way the scrub fixture does.

---

### [MED] `_clear_module_testclient_cookies` walks `mod.__dict__` per test — O(modules × globals) every test, only protects function-scoped tests

**Location:** `gateway/tests/conftest.py:103-148`.

The fixture iterates every name in the test module's `__dict__`, type-checks for `TestClient`, and clears cookies. For each test in a module with N globals + M class attributes, this is O(N+M). Over a 150-file × 50-test/file suite that's a measurable overhead. More importantly:

- It only fires for **the test's own module**. If module A's tests leave cookies on `tests.test_b.client`, the next module-A test won't clean it.
- It clears `tc.cookies` but does **not** reset `tc.headers`, `tc.base_url`, or `tc.auth`. A test that monkey-patches `client.headers["Authorization"] = …` leaks that into the next test.
- It does not catch `TestClient` instances stored on module-level dicts or lists (e.g. `clients = {"a": TestClient(app), …}`).

**Why MEDIUM, not LOW:** the function exists specifically because httpx 0.27 silently persists cookies (per the in-line comment). The fix is correct in spirit but partial.

**Fix:** track `TestClient` instances explicitly via a registry decorator, or replace the housekeeping with a per-test `client` fixture that nukes everything. The latter would also force the suite to stop creating module-level TestClients, which is the underlying issue.

---

### [MED] `_reset_global_test_state` runs *after* yield, but module-level `_rate_store` cleanup happens *before* the next test's setup — fragile

**Location:** `gateway/tests/conftest.py:191-234`.

The fixture is `autouse=True` (function-scoped) and does its wipes **post-yield** (i.e. after the test finishes). Problem: if a test fails and pytest moves on, the post-yield wipe still runs — but a **class-scoped** setUpClass for the next module already ran *before* this fixture's post-yield. setUpClass-seeded rows can therefore be wiped before the class's first test runs.

The conftest mitigates this for the **DB connection patch** via the separate class-scoped `_maybe_force_shared_testdb_class` (lines 48-60), but there is no class-scoped equivalent for the `_rate_store` / `_login_failures` / TTL-cache wipe. So:

- A class that seeds rate-limit state in setUpClass to test rate-limit-aware behaviour can have it wiped between tests.
- Conversely, a class that *doesn't* clean up its `_rate_store` writes leaks into the next class.

**Fix:** split this fixture into a pre-yield (clear state before test) and post-yield (clean up after) variant, or add a class-scoped sibling that mirrors `_maybe_force_shared_testdb_class`.

---

### [MED] `make_user` factory swallows `set_trading_addon` / subscriptions seeding errors silently — pro users sometimes aren't pro

**Location:** `gateway/tests/conftest.py:327-355`.

```python
if trading:
    try:
        db.set_trading_addon(uid, True, int(_time.time()) + 30 * 86400)
    except Exception:
        pass
if pro:
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, "
                "plan, status, started_at) "
                "VALUES (?, '__plan__', 'pro_monthly', 'active', ?)",
                (uid, int(_time.time())),
            )
    except Exception:
        pass
```

Tests calling `make_user(pro=True)` or `make_user(trading=True)` expect those flags to hold post-construction. If the migration that creates `subscriptions` hasn't run for a specific build, or if the dashboard_key UNIQUE constraint fires on a re-run, the insert silently fails and the fixture returns a `pro_user` that is functionally identical to a free user. Downstream test then 403's on a paywalled route and reports "auth bypass regression" when actually the fixture broke.

**Fix:** drop the `except Exception: pass` — let pytest fail loudly if the migration state can't support the fixture, or assert post-condition (`assert db.get_user_subscription(uid)['plan'] == 'pro_monthly'`).

---

### [MED] `_testdb._fake_conn` rolls back on any raised exception — including `StopIteration` / cancel signals — without a savepoint to scope the rollback

**Location:** `gateway/tests/_testdb.py:33-40`.

```python
@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise
```

This is a **process-wide** sqlite connection. A single `_conn.rollback()` rolls back **every uncommitted write across every fixture that's currently inside its own `with db.conn() as c:` block**. If two fixtures or two threads (test thread + APScheduler thread, even with `NARVE_SKIP_SCHEDULER=1` set, since not every code path honours that) are mid-transaction and one raises, the other loses unrelated work.

**Why MEDIUM:** the scheduler is supposed to be off (`NARVE_SKIP_SCHEDULER=1` is set in conftest), and httpx's TestClient is single-threaded by default, so the actual concurrent-rollback risk in steady state is low. But the `check_same_thread=False` flag is on (line 28), so any future background thread that lands a write at the wrong moment causes silent data loss.

**Fix:** wrap each `_fake_conn()` call in a SAVEPOINT (named per-call) and roll back to *that* savepoint on exception instead of the whole connection. This dovetails with finding #1.

---

### [LOW] `seed_basic` builds rows but never removes them — re-seeding accumulates duplicate predictions

**Location:** `gateway/tests/conftest.py:358-395`.

```python
cur = c.execute(
    "INSERT INTO predictions (source_handle, market_id, category, "
    " direction, predicted_probability, content, extracted_at) "
    "VALUES ('seed_source', 'poly:seed-market', 'other', 'yes', "
    " 0.7, 'Seed prediction for fixtures', ?)",
    (int(_time.time()),),
)
```

`predictions` has no UNIQUE constraint visible from this insert. Every call to the `seed_basic` fixture inserts a *new* row with the same source/market — a test that uses `seed_basic` twice (different test cases) ends up with two seed predictions for `poly:seed-market`. Tests that count predictions for that market silently break later in the suite.

**Fix:** use `INSERT OR IGNORE` keyed on a deterministic `(source_handle, market_id)` UNIQUE, or remove the seed inserts and let tests call `make_prediction` from `helpers.py` directly.

---

### [LOW] `_user_ctr` global counter resets per process, not per session — relying on it for uniqueness is fragile under xdist

**Location:** `gateway/tests/conftest.py:305-311`.

If the suite ever moves to `pytest-xdist`, every worker has its own process with its own `_user_ctr=0`, but they share the same in-memory DB **only if** the testdb is moved off `:memory:` (per-process in-memory DBs don't share anything). The current `:memory:` setup is xdist-incompatible by construction. The counter pattern is fine as-is, but it's a forward-compatibility wart worth knowing.

**Fix (if migrating to xdist):** suffix with `os.getpid()` or `secrets.token_hex(4)`.

---

### [LOW] `qa/conftest.py::live_server` writes to the dev DB at `repo_root/auth.db`, not to the in-memory testdb

**Location:** `gateway/tests/browser/conftest.py:137`.

```python
os.environ.setdefault("GATEWAY_DB_PATH", str(repo_root / "auth.db"))
```

This is the **real on-disk dev database**. Browser tests boot uvicorn, which then writes test users/sessions to the developer's working `auth.db`. Subsequent local `python3 server.py` runs see those users. Not a security issue (it's the dev machine), but a hygiene one — a `pytest gateway/tests/browser/` run quietly mutates dev state.

**Fix:** point at `tmp_path / "auth.db"` so each session gets a fresh file, or honour `GATEWAY_DB_PATH=":memory:"` if the FastAPI app supports it cleanly.

---

### [LOW] `_testdb` calls `migrations.upgrade_to_head()` once at process import — no way to test partial migration state

**Location:** `gateway/tests/_testdb.py:49-50`.

The fact that migrations always run to head is fine for the bulk of the suite, but `test_migrations.py` and `test_migration_188.py` exist specifically to test individual migrations. Those tests have to spin up their own connections, which they do — meaning the conftest doesn't help them and the `_testdb`-pinning logic (`_maybe_force_shared_testdb`) actively gets in their way. Currently they work because they don't import `_testdb` and they don't set `USES_TESTDB`, so the fixture's `_module_uses_testdb` check returns False and leaves them alone — but the contract is fragile.

**Info-level note:** document this in the conftest docstring so future migrations tests don't accidentally add `USES_TESTDB=True` and break themselves.

---

### [INFO] `helpers.csrf_headers` and conftest `csrf_headers` fixture overlap — two ways to do the same thing

**Location:** `gateway/tests/helpers.py:151-163` and `gateway/tests/conftest.py:441-444`.

Both return `{"Cookie": "_csrf=t", "x-csrf-token": "t"}`-shaped dicts. The helper accepts an optional session_token; the fixture is hard-coded to no-session. Tests pick arbitrarily. Not broken, just two patterns for one job.

**Fix:** keep the fixture (composable), retire the helper function — or vice versa.

---

### [INFO] `EMAIL_DRY_RUN`, `NARVE_SKIP_SCHEDULER`, `RATE_LIMIT_ENABLED`, `GLOBAL_RATE_LIMIT_PER_MIN`, `CREDENTIALS_ENCRYPTION_KEY` are all set at **import time** in `conftest.py:263-284`

These env tweaks happen at module import, before any test or fixture runs. That's correct for things `server.py` reads at module top, but it means **any test process that imports `tests.conftest` for any reason gets these overrides**. If a dev runs `python -c "from tests import conftest"` to debug a fixture, their shell now has `RATE_LIMIT_ENABLED=true` and `NARVE_SKIP_SCHEDULER=1` polluted into the process. Minor footgun; the conftest could push these into a `pytest_configure` hook so they only fire under pytest itself.

---

## Gaps summary

| # | Gap                                                                                                                              | Severity |
|---|----------------------------------------------------------------------------------------------------------------------------------|----------|
| 1 | Docstring promises SAVEPOINT/ROLLBACK per-test isolation; reality is a 3-table wipe                                              | High     |
| 2 | `_e2e_clean_slate` silently swallows FK-violation deletes; `users` cleanup is partial-coverage                                   | High     |
| 3 | No TestClient uses HTTPS `base_url`; `secure` cookie + HSTS + scheme-branching code paths untested                               | High     |
| 4 | `SITE_ACCESS_TOKEN` env var is left dirty across e2e ↔ non-e2e module boundaries (scrub only resets module attr, not env)        | Medium   |
| 5 | `_clear_module_testclient_cookies` only handles cookies — leaves `headers`/`auth`/`base_url` and cross-module TestClients alone  | Medium   |
| 6 | `_reset_global_test_state` is function-scoped post-yield only; class-scoped seeds aren't matched by class-scoped state resets    | Medium   |
| 7 | `make_user(pro=True / trading=True)` silently swallows seeding failures — fixture returns a free user disguised as a pro         | Medium   |
| 8 | `_fake_conn` rolls back the whole shared connection on any exception — fragile if any thread ever writes concurrently            | Medium   |
| 9 | `seed_basic` inserts predictions without dedup → accumulating rows                                                               | Low      |
| 10 | `_user_ctr` global is xdist-incompatible                                                                                         | Low      |
| 11 | `browser/conftest.py::live_server` writes to dev's real on-disk `auth.db`                                                        | Low      |
| 12 | `_testdb` runs migrations to head unconditionally — migration-specific tests work by accident                                    | Low      |
| 13 | Two parallel `csrf_headers` implementations (helper fn + fixture)                                                                | Info     |
| 14 | Env mutations at conftest import time leak into any process that imports the module                                              | Info     |

---

## What's already correct (worth noting)

- The autouse `_scrub_site_access_token` fixture **is** restoring the previous value cleanly in teardown (lines 96-99) and correctly detects e2e modules by name prefix.
- `_module_uses_testdb` (lines 34-45) checks both the explicit marker and the implicit import, so opt-in is well-bounded.
- `EMAIL_DRY_RUN=true` and `NARVE_SKIP_SCHEDULER=1` are set at import — both correct for hermetic runs.
- The class-scoped `_maybe_force_shared_testdb_class` (lines 48-60) is a subtle but correct fix for the setUpClass-vs-function-scoped-fixture ordering bug the comment describes.
- The `_testdb` module uses `getattr(db.conn, "_is_test_fake", False)` to make the patch idempotent — a real production-grade touch.
- Stripe webhook signing in `helpers.signed_stripe_event` matches the canonical format (`t=<ts>,v1=<hmac_sha256>`).

---

*Generated 2026-05-15 — `audit_test_conftest.md`.*
