"""Route-level coverage for the path-traversal guard on market_id.

Audit MED (2026-05-15): ``_safe_market_id`` is the choke point that
prevents an attacker-controlled ``market_id`` from being interpolated
into an upstream URL template. The unit-level tests for the helper
itself live in ``test_markets.py::TestSafeMarketIdPathTraversal``.
This module exists to prove the guard actually fires at every HTTP
route that takes ``market_id`` and uses it against the upstream
clients:

  - GET  /api/markets/unified/{market_id:path}            (api_market_detail)
  - GET  /api/markets/poly/order-params/{market_id:path}  (api_poly_order_params)
  - POST /api/kelly/calculate                             (api_kelly_calculate)

Threat model: ``poly:foo/../v1/internal`` lands in
``f"{gamma_base}/markets/{slug}"`` and pivots the upstream request to
gamma-api's internal admin surface. The guard must reject such a value
with 400 before any upstream HTTP call is made — and it must keep
letting legitimate slugs through with 200.

Mirrors the pin-db-then-import-server pattern used by
``test_environmental_http.py`` so we don't need a real Polymarket /
Kalshi network.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest

# Pin db.conn BEFORE importing server. The market routes look up
# subscription state and the trading add-on per request, so we need a
# real (in-memory) schema, not a stub.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

import market_routes  # noqa: E402
import server  # noqa: E402
from backend.markets import unified_markets  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_trader_user(email: str, username: str) -> tuple[int, str]:
    """Create a user with the Trader plan + trading add-on enabled. The
    market routes gate on the trading add-on, not the plan tier, so any
    plan + add-on is enough to clear ``_require_markets_user``."""
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
            "VALUES (?, '__plan__', 'trader_monthly', 'active', ?)",
            (uid, now),
        )
    db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    db.set_user_bankroll(uid, 1000.0)
    return uid, db.create_session(uid)


def _stub_unified_market(market_id: str = "poly:legit-slug") -> unified_markets.UnifiedMarket:
    return unified_markets.UnifiedMarket(
        id=market_id, source="polymarket",
        title="Will BTC hit 100k in 2026?", category="crypto",
        yes_price=0.42, no_price=0.58,
        volume_usd=12345.0, liquidity_usd=6789.0,
        close_time="2026-12-31T00:00:00Z", status="active",
        outcome=None, url="https://polymarket.com/event/legit-slug",
        poly_yes_token_id="0xyes", poly_no_token_id="0xno",
        poly_neg_risk=False,
    )


def _patch_market_fetchers(test_market):
    """Replace fetch_single_market so route handlers never hit the
    network. The whole point of this test is that the *guard* runs
    before the upstream call — so a non-mocked fetcher would either
    hang on DNS or, worse, succeed against a legitimate slug because
    the URL got percent-encoded into a benign form. Mocking lets us
    assert the guard's behaviour deterministically."""
    orig = unified_markets.fetch_single_market

    async def _stub_single(*a, **kw):
        return test_market

    unified_markets.fetch_single_market = _stub_single
    return orig


def _restore_market_fetchers(orig):
    unified_markets.fetch_single_market = orig


def _attach_polymarket_credential(user_id: int) -> None:
    """``api_poly_order_params`` returns 400 unless the user has a
    connected Polymarket wallet. Plant one directly so the route can
    progress past the credential check and exercise the path guard."""
    db.upsert_market_credential(
        user_id,
        "polymarket",
        polymarket_wallet_address="0x" + "a" * 40,
    )


def _prime_csrf(session_token: str) -> str:
    """Prime the CSRF cookie pair the gateway's middleware enforces on
    every mutating request (double-submit pattern). Same trick used by
    ``test_kelly_bankroll.py`` — hit any cheap authenticated GET so the
    response sets ``_csrf``, then pull that cookie value to feed back
    on the POST as both cookie + ``X-CSRF-Token`` header."""
    client.get(
        "/feedback",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


# ── HELPER (unit-level) coverage ───────────────────────────────────────────

class TestSafeMarketIdHelperLives(unittest.TestCase):
    """Smoke check: the helper must exist and be importable. The
    sibling-commit audit (5c27678) added it; this guards against a
    regression where it gets renamed / deleted in a future refactor.
    Detailed semantic coverage of the helper lives in
    ``test_markets.py::TestSafeMarketIdPathTraversal``."""

    def test_helper_exists_and_is_callable(self):
        self.assertTrue(hasattr(market_routes, "_safe_market_id"))
        self.assertTrue(callable(market_routes._safe_market_id))

    def test_helper_rejects_canonical_attack(self):
        self.assertIsNone(market_routes._safe_market_id("poly:foo/../v1/internal"))

    def test_helper_accepts_canonical_legitimate_id(self):
        out = market_routes._safe_market_id("poly:will-btc-hit-100k-2026")
        self.assertIsNotNone(out)
        self.assertEqual(out[0], "poly:")
        self.assertEqual(out[1], "will-btc-hit-100k-2026")


# ── ROUTE: GET /api/markets/unified/{market_id:path} ───────────────────────

class TestMarketDetailPathGuard(unittest.TestCase):
    """``api_market_detail`` is the canonical surface — it's the route
    cited in the audit comment at market_routes.py:466 (now line 467
    after the fix landed). The traversal payload reaches it via the
    ``{market_id:path}`` catch-all converter, so a ``/`` in the slug
    is preserved end-to-end."""

    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.uid, cls.token = _make_trader_user(
            "pathsafe-detail@test.com", "pathsafedetail",
        )
        cls.market = _stub_unified_market("poly:legit-slug")
        cls._orig = _patch_market_fetchers(cls.market)

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig)

    def setUp(self):
        db.conn = _fake_conn
        try:
            server._rate_store.clear()
        except Exception:
            pass

    def test_path_traversal_returns_400(self):
        """The canonical proof-of-attack from the audit: a ``../`` in
        the slug must be rejected by the guard, NOT silently
        percent-encoded into a legitimate-looking upstream call.

        We percent-encode the slashes on the client side so httpx
        doesn't normalize ``../`` away before the request ever leaves
        the test harness — the goal is to exercise the server-side
        guard against the exact byte sequence an attacker would send.
        Starlette decodes the path before handing it to the handler,
        so the helper sees the raw ``poly:foo/../v1/internal``."""
        r = client.get(
            "/api/markets/unified/poly:foo%2F..%2Fv1%2Finternal",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(
            r.status_code, 400,
            f"Expected 400 for path-traversal payload, got "
            f"{r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        # Gateway's error_handlers wraps HTTPException(detail=…) into
        # {"error": "bad_request", "message": <detail>, "request_id": ...}
        self.assertEqual(body.get("message"), "Invalid market id")

    def test_legitimate_market_id_returns_200(self):
        """A clean slug in the allowlist must round-trip to 200 so we
        don't false-positive on every real market detail request."""
        r = client.get(
            "/api/markets/unified/poly:legit-slug",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(
            r.status_code, 200,
            f"Expected 200 for legitimate slug, got "
            f"{r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        # The handler rewrites market_id to f"{prefix}{encoded_slug}"
        # before returning — for a slug with only safe chars the
        # encoded form is identical, so the round-trip is lossless.
        self.assertEqual(body.get("id"), "poly:legit-slug")

    def test_unknown_prefix_returns_400(self):
        """A different prefix (no ``poly:`` / ``kalshi:``) is also a
        guard miss and should be rejected at the same boundary."""
        r = client.get(
            "/api/markets/unified/evil:something",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400)

    def test_overlong_slug_returns_400(self):
        """The length cap (128) protects upstream URL parsers from
        DoS-by-megabyte. A 129-char slug must fail the guard."""
        evil = "poly:" + ("a" * 129)
        r = client.get(
            f"/api/markets/unified/{evil}",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400)


# ── ROUTE: GET /api/markets/poly/order-params/{market_id:path} ─────────────

class TestPolyOrderParamsPathGuard(unittest.TestCase):
    """``api_poly_order_params`` is the second surface — same upstream
    fetcher, same threat model. It has an extra prefix check
    (``poly:`` only) that runs BEFORE the guard, so the guard payload
    has to keep the ``poly:`` prefix to exercise the right codepath."""

    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.uid, cls.token = _make_trader_user(
            "pathsafe-order@test.com", "pathsafeorder",
        )
        _attach_polymarket_credential(cls.uid)
        cls.market = _stub_unified_market("poly:legit-slug")
        cls._orig = _patch_market_fetchers(cls.market)

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig)

    def setUp(self):
        db.conn = _fake_conn
        try:
            server._rate_store.clear()
        except Exception:
            pass

    def test_path_traversal_returns_400(self):
        r = client.get(
            "/api/markets/poly/order-params/poly:foo%2F..%2Fv1%2Finternal",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(
            r.status_code, 400,
            f"Expected 400 for path-traversal payload, got "
            f"{r.status_code}: {r.text[:200]}",
        )

    def test_legitimate_market_id_returns_200(self):
        r = client.get(
            "/api/markets/poly/order-params/poly:legit-slug",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(
            r.status_code, 200,
            f"Expected 200 for legitimate slug, got "
            f"{r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        self.assertEqual(body.get("market_id"), "poly:legit-slug")
        self.assertEqual(body.get("yes_token_id"), "0xyes")

    def test_kalshi_prefix_via_poly_route_returns_400(self):
        """The route has its own ``poly:``-only prefix check at the
        top, and the guard then asserts the same. A kalshi-prefixed
        id reaching this route is a misuse and must 400."""
        r = client.get(
            "/api/markets/poly/order-params/kalshi:TICKER",
            cookies={server.COOKIE_NAME: self.token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 400)


# ── ROUTE: POST /api/kelly/calculate ────────────────────────────────────────

class TestKellyCalculatePathGuard(unittest.TestCase):
    """``api_kelly_calculate`` is the third surface. ``market_id``
    arrives in the JSON body, not the URL path, but it still gets
    interpolated into the same ``fetch_single_market`` call against
    upstream gamma-api / kalshi — so the same guard applies."""

    @classmethod
    def setUpClass(cls):
        db.conn = _fake_conn
        cls.uid, cls.token = _make_trader_user(
            "pathsafe-kelly@test.com", "pathsafekelly",
        )
        cls.market = _stub_unified_market("poly:legit-slug")
        cls._orig = _patch_market_fetchers(cls.market)

    @classmethod
    def tearDownClass(cls):
        _restore_market_fetchers(cls._orig)

    def setUp(self):
        db.conn = _fake_conn
        try:
            server._rate_store.clear()
        except Exception:
            pass

    def _post_kelly(self, payload: dict):
        """POST with the double-submit CSRF pattern fully wired —
        cookie + matching header, mirrored from
        ``test_kelly_bankroll.py::_post_json``."""
        csrf = _prime_csrf(self.token)
        return client.post(
            "/api/kelly/calculate",
            cookies={server.COOKIE_NAME: self.token, "_csrf": csrf},
            headers={"X-CSRF-Token": csrf},
            json=payload,
            follow_redirects=False,
        )

    def test_path_traversal_returns_400(self):
        # The payload is in the JSON body so httpx doesn't normalize it.
        # The exact ``poly:foo/../v1/internal`` byte sequence reaches
        # the handler and must be rejected.
        r = self._post_kelly({"market_id": "poly:foo/../v1/internal"})
        self.assertEqual(
            r.status_code, 400,
            f"Expected 400 for path-traversal payload, got "
            f"{r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        # Kelly returns plain JSON {"error": ...} via JSONResponse
        # (it doesn't raise HTTPException for the invalid-id case),
        # so the error_handlers middleware never rewrites the shape.
        self.assertEqual(body.get("error"), "Invalid market id")

    def test_legitimate_market_id_returns_200(self):
        r = self._post_kelly({"market_id": "poly:legit-slug", "bankroll": 1000})
        self.assertEqual(
            r.status_code, 200,
            f"Expected 200 for legitimate slug, got "
            f"{r.status_code}: {r.text[:200]}",
        )
        body = r.json()
        self.assertEqual(body.get("market_id"), "poly:legit-slug")


if __name__ == "__main__":
    unittest.main()
