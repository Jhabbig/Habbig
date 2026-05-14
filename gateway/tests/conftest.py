"""Pytest conftest — scoped patch for tests that opt into the shared DB.

Test files in this directory fall into two camps:

1. **New feature tests** (e.g. `test_email_system`, `test_feature_routes`)
   import `from tests import _testdb` to pick up an in-memory sqlite3
   connection with all migrations applied.

2. **Legacy tests from the previous session** (`test_user_features`,
   `test_sentry`, `test_http_auth`, etc.) use their own `contextlib`
   fakes that patch `db.conn` directly at import time.

If both camps load in the same pytest collection phase, whichever
monkey-patches last wins, silently breaking the other. This conftest
re-installs the `_testdb._fake_conn` patch **only** for test modules
that explicitly declare `USES_TESTDB = True` or import `_testdb`. Other
tests are left alone, so the orphan fixtures keep working.
"""

from __future__ import annotations

import sys

import pytest

from tests import _testdb  # noqa: F401 — always sets up the shared conn
import db


_SHARED_CONN_CM = _testdb._fake_conn
_TESTDB_MODULE_NAMES = {"tests._testdb", "_testdb"}


def _module_uses_testdb(test_file_module: str) -> bool:
    mod = sys.modules.get(test_file_module)
    if mod is None:
        return False
    # Explicit marker — a test file can set `USES_TESTDB = True`.
    if getattr(mod, "USES_TESTDB", False):
        return True
    # Inferred — if the module imported _testdb, it implicitly opts in.
    for name in _TESTDB_MODULE_NAMES:
        if name in getattr(mod, "__dict__", {}):
            return True
    return False


@pytest.fixture(autouse=True, scope="class")
def _maybe_force_shared_testdb_class(request):
    """Class-scoped pin — fires BEFORE setUpClass so user/key seeding
    done in setUpClass lands in the shared in-memory DB. Cleaning this
    up at function scope only meant any test that set up state in
    setUpClass (e.g. test_api_public.TestAuth) lost it the moment another
    module's import-time monkey-patch had repointed db.conn at its own
    per-file fake.
    """
    module_name = request.node.module.__name__ if hasattr(request.node, "module") else ""
    if _module_uses_testdb(module_name):
        db.conn = _SHARED_CONN_CM
    yield


@pytest.fixture(autouse=True)
def _maybe_force_shared_testdb(request):
    """Re-apply the shared-conn patch for tests that use _testdb, and
    leave other tests alone so their own db.conn monkey-patches stick."""
    module_name = request.node.module.__name__ if hasattr(request.node, "module") else ""
    if _module_uses_testdb(module_name):
        db.conn = _SHARED_CONN_CM
    yield


@pytest.fixture(autouse=True)
def _scrub_site_access_token(request):
    """Ensure ``server.SITE_ACCESS_TOKEN`` is empty for non-e2e tests.

    ``tests/e2e/conftest.py`` sets the env var at module-import time so
    its flow tests can walk past the gate. If e2e tests run first, the
    gate middleware then redirects every subsequent admin/api request
    to /gate (200 HTML) instead of letting CSRF + auth middleware run
    — the test sees ``200 != 403`` and reports a CSRF failure that's
    actually a gate-redirect. We override server.py's module constant
    on every non-e2e test; e2e tests fix it back inside ``pass_gate``.
    """
    mod_name = request.node.module.__name__ if hasattr(request.node, "module") else ""
    is_e2e = mod_name.startswith("tests.e2e")
    if not is_e2e:
        try:
            import server as _server
            _prev = getattr(_server, "SITE_ACCESS_TOKEN", "")
            _server.SITE_ACCESS_TOKEN = ""
        except Exception:
            _prev = None
            _server = None
    yield
    if not is_e2e and _server is not None and _prev is not None:
        try:
            _server.SITE_ACCESS_TOKEN = _prev
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _clear_module_testclient_cookies(request):
    """Clear cookies on any module-level TestClient between tests.

    25+ test modules define ``client = TestClient(server.app)`` at
    module scope and pass per-request ``cookies={...}`` to authed calls.
    httpx 0.27 silently persists those cookies on the underlying client
    (the suite emits a DeprecationWarning about exactly this). The next
    "anonymous request should 401" test inherits the previous test's
    session cookie. Walk the module's globals AND any TestCase class
    attributes, find any TestClient, wipe state before the test runs.
    """
    import sys as _sys
    mod_name = request.node.module.__name__ if hasattr(request.node, "module") else ""
    mod = _sys.modules.get(mod_name)
    if mod is None:
        yield
        return
    try:
        from fastapi.testclient import TestClient as _TC
    except Exception:
        yield
        return
    seen = set()

    def _clear(obj):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, _TC):
            try:
                obj.cookies.clear()
            except Exception:
                pass

    # Module-level TestClient instances (the common pattern).
    for name in list(mod.__dict__):
        obj = getattr(mod, name, None)
        _clear(obj)
        # Class-level TestClient set in setUpClass (e.g.
        # ``cls.client = TestClient(server.app)`` — admin suites use this
        # pattern so the class TestClient persists cookies across tests).
        if isinstance(obj, type):
            for attr_name in list(vars(obj)):
                _clear(getattr(obj, attr_name, None))
    yield


# ──────────────────────────────────────────────────────────────────────────
# Cross-test state hygiene.
#
# The full-suite-only failure cohort traces back to module-level state
# that leaks between tests when many request-driven tests (notably
# gateway/tests/qa/qa_walk_*.py) run in-process:
#
#   1. **Module-level rate-limit / lockout dicts in server.py**
#      (``_rate_store``, ``_login_failures``) — defaultdicts that never
#      get cleared mid-process. After heavy walks run, every later test
#      that depends on "this IP can still log in / hit /admin" trips a
#      429 or the lockout threshold.
#
#   2. **In-process TTLCache** (``cache.ttl.ttl_cache``) — walks hit
#      dashboard / admin routes and populate market / feed / source
#      caches with rows that existed at walk time. Tests that
#      subsequently mutate those rows and assert the fresh value get
#      the stale cached value.
#
#   3. **Shared in-memory sqlite tables** (rate_limits, audit_log,
#      engagement_events) — rows pile up. ``rate_limits`` is the worst
#      because the persistent rate limiter cross-references it on
#      every authenticated route.
#
# Surgical state-wipe after each function-scoped test gives us the
# speed of in-process pytest with the isolation of a subprocess. Each
# wipe is wrapped in try/except so a missing migration / different DB
# layout never breaks a legacy test. The fixture only fires for tests
# on the shared _testdb connection — legacy contextlib-fake modules
# manage their own state.
# ──────────────────────────────────────────────────────────────────────────


_POLLUTION_TABLES = (
    "rate_limits",
    "audit_log",
    "engagement_events",
)


@pytest.fixture(autouse=True)
def _reset_global_test_state(request):
    """Reset module-level globals + transient DB rows between tests."""
    yield
    module_name = (
        request.node.module.__name__
        if hasattr(request.node, "module") else ""
    )
    if not _module_uses_testdb(module_name):
        return

    # ── Module-level rate-limit + lockout dicts in server.py ────────────
    try:
        import server as _server
        try:
            _server._rate_store.clear()
        except Exception:
            pass
        try:
            _server._login_failures.clear()
        except Exception:
            pass
    except Exception:
        pass

    # ── In-process TTL cache ────────────────────────────────────────────
    try:
        from cache.ttl import ttl_cache as _ttl_cache
        _ttl_cache.clear()
    except Exception:
        pass

    # ── Transient DB rows that accumulate from request-driven tests ────
    try:
        with _SHARED_CONN_CM() as _c:
            for _t in _POLLUTION_TABLES:
                try:
                    _c.execute(f"DELETE FROM {_t}")
                except Exception:
                    # Table may not exist in this build (e.g. early
                    # migration state during a focused single-file run).
                    pass
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Canonical fixtures — opt-in. Existing test files keep doing whatever they
# do; new tests can lean on these to avoid re-writing boilerplate.
#
# Design notes
# ------------
# * The suite has ONE shared in-memory sqlite connection (tests/_testdb.py).
#   Migrating to per-test tmp-path DBs would break >1000 tests that share
#   state across cases, so the fixtures below ride the shared conn and
#   guarantee isolation with a SAVEPOINT/ROLLBACK wrapper.
#
# * User creation goes through db.create_user() — the spec example used
#   role/subscription_tier kwargs that this codebase doesn't accept;
#   the admin_user / pro_user fixtures here call the real signature and
#   then promote via db.set_trading_addon / set_pro_addon where needed.
#
# * CSRF: the gateway's middleware accepts a _csrf cookie + matching
#   x-csrf-token header. The `csrf_headers` fixture returns that pair
#   pre-wired for TestClient.
#
# The fixtures are deliberately thin — a Factory-like `helpers` module
# (tests/helpers.py) carries the heavier factories so fixtures stay
# composable.
# ──────────────────────────────────────────────────────────────────────────


import os as _os
import time as _time

_os.environ.pop("SITE_ACCESS_TOKEN", None)
_os.environ.pop("PRODUCTION", None)
_os.environ["RATE_LIMIT_ENABLED"] = "true"
_os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")
# The APScheduler-backed recurring jobs (health_check, etc.) run on a
# background thread and write to the same in-memory sqlite3 connection
# tests use. sqlite3 with check_same_thread=False is documented as
# unsafe under concurrent use — the statement cache corrupts and
# subsequent c.execute() calls raise KeyError: ('SELECT 1',) at
# arbitrary points during the suite. Disable the scheduler for tests;
# anything that needs to exercise scheduled-job logic invokes the job
# function directly.
_os.environ.setdefault("NARVE_SKIP_SCHEDULER", "1")
try:
    from cryptography.fernet import Fernet as _Fernet
    _os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY",
                           _Fernet.generate_key().decode())
except Exception:
    pass


@pytest.fixture(scope="session")
def app():
    """Return the FastAPI app. Session-scoped — importing `server` is
    expensive (pulls intelligence, markets, jobs, etc.)."""
    import server as _server
    return _server.app


@pytest.fixture
def client(app):
    """Fresh TestClient per test. Cookies reset between tests; CSRF
    + session cookies are set explicitly by the fixtures below."""
    from fastapi.testclient import TestClient
    tc = TestClient(app)
    tc.cookies.clear()
    yield tc


_user_ctr = 0


def _next_slug(prefix: str) -> str:
    global _user_ctr
    _user_ctr += 1
    return f"{prefix}{_user_ctr}_{int(_time.time())}"


@pytest.fixture
def make_user():
    """Factory — call to get a fresh user dict each time.

    Usage::

        def test_x(make_user, client):
            owner = make_user()
            admin = make_user(admin_level=1)
            pro   = make_user(pro=True)

    Returns dict with: user_id, email, username, session_token.
    """
    def _factory(*, admin_level: int = 0, pro: bool = False,
                 trading: bool = False, email: str | None = None,
                 username: str | None = None, password: str = "TestPass123!"):
        uname = username or _next_slug("u")
        mail = email or f"{uname}@test.example"
        uid = db.create_user(mail, password, username=uname,
                             admin_level=admin_level)
        if trading:
            try:
                db.set_trading_addon(uid, True, int(_time.time()) + 30 * 86400)
            except Exception:
                pass
        if pro:
            # Different codebases have either a subscriptions table or a
            # dedicated set_pro_addon helper — try both, swallow failures.
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
        token = db.create_session(uid)
        return {"user_id": uid, "email": mail, "username": uname,
                "session_token": token, "password": password}
    return _factory


@pytest.fixture
def seed_basic(make_user):
    """Seed a minimum viable graph: one user, one source, one market,
    one prediction. Returns a dict keyed by entity type. Tests that
    need richer data should build it themselves — this is the smallest
    useful set for "does anything render" style checks."""
    user = make_user()
    out = {"user": user}

    # Source — source_credibility is the canonical handle table.
    try:
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, total_predictions, "
                " correct_predictions, categories_active, last_computed_at) "
                "VALUES ('seed_source', 0.72, 10, 7, 2, ?)",
                (int(_time.time()),),
            )
        out["source"] = {"handle": "seed_source"}
    except Exception:
        pass

    # Prediction.
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO predictions (source_handle, market_id, category, "
                " direction, predicted_probability, content, extracted_at) "
                "VALUES ('seed_source', 'poly:seed-market', 'other', 'yes', "
                " 0.7, 'Seed prediction for fixtures', ?)",
                (int(_time.time()),),
            )
            out["prediction"] = {"id": int(cur.lastrowid)}
    except Exception:
        pass

    return out


@pytest.fixture
def authed_user(make_user):
    """User with a session cookie ready to attach. Use together with
    ``auth_headers`` to fire authenticated requests."""
    return make_user()


@pytest.fixture
def admin_user(make_user):
    return make_user(admin_level=1)


@pytest.fixture
def super_admin(make_user):
    return make_user(admin_level=2)


@pytest.fixture
def pro_user(make_user):
    return make_user(pro=True, trading=True)


def _auth_and_csrf_cookie(session_token: str) -> str:
    """Compose the Cookie header for both session + CSRF in one go."""
    return f"pm_gateway_session={session_token}; _csrf=t"


@pytest.fixture
def auth_headers():
    """``auth_headers(user)`` → dict ready to pass to client.{get,post,...}.

    Sends both the session cookie and the CSRF cookie/header pair so
    mutating requests sail past the CSRF middleware without each test
    having to re-derive the pattern.
    """
    def _build(user: dict) -> dict:
        return {
            "Cookie": _auth_and_csrf_cookie(user["session_token"]),
            "x-csrf-token": "t",
        }
    return _build


@pytest.fixture
def csrf_headers():
    """For unauthenticated CSRF-protected requests (e.g. public signup)."""
    return {"Cookie": "_csrf=t", "x-csrf-token": "t"}


@pytest.fixture
def clear_rate_limits():
    """Purge the in-memory rate-limit store. Useful for tests that hit
    the same endpoint more than N times and would otherwise trip the
    sync / admin / login limiter from a prior test's residue."""
    import server as _server
    try:
        _server._rate_store.clear()
    except Exception:
        pass
    yield
    try:
        _server._rate_store.clear()
    except Exception:
        pass
