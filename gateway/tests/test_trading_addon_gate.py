"""Trading Add-on gate coverage for state-mutating portfolio routes.

The audit found that the access contract for the £25/mo Trading add-on
hangs entirely off ``portfolio.routes._require_trading_addon`` — if any
mutating handler skips that helper, free users can hit a paid surface.
This file is the per-route safety net.

For every state-mutating handler in ``gateway/portfolio/routes.py``:

  * an authenticated free-tier user (no add-on, not admin) MUST see 402
    Payment Required and never reach the side-effect (no credential
    upsert, no Kalshi login proxy, no sync write, no bankroll write).
  * an authenticated add-on user MUST be allowed through. The upstream
    call is stubbed so the test isolates the gate.
  * the read-only ``/api/portfolio/status`` endpoint stays 200 for both
    cohorts so the dashboard can render the upsell card.

Mirrors the cookie / CSRF / DB-pin scaffolding used by
``test_kalshi_throttle.py`` so this suite slots into the same pytest
session without re-patching ``db.conn``.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

# Pin db.conn BEFORE importing server (same ordering as the
# test_kalshi_throttle / test_portfolio_integration peers — required so
# the in-memory DB sees migrations before any module-level lookups).
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
# Global per-IP middleware (600/min in prod) would otherwise eat our
# back-to-back requests; the gate logic is what we're asserting, not
# throttling, so push the global cap well clear.
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault(
        "CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode(),
    )
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

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portfolio import kalshi as _kalshi_mod  # noqa: E402
from portfolio import polymarket as _poly_mod  # noqa: E402


client = TestClient(server.app)


# ── Helpers ────────────────────────────────────────────────────────────────


_unique_ctr = 0


def _unique(prefix: str) -> str:
    global _unique_ctr
    _unique_ctr += 1
    return f"{prefix}{_unique_ctr}_{int(time.time())}"


def _make_user(*, with_addon: bool) -> tuple[int, str]:
    """Create a user + hardened session. ``narve_session`` is the cookie
    portfolio.routes reads via auth.middleware.SessionMiddleware; the
    legacy ``pm_gateway_session`` flow doesn't reach those handlers."""
    slug = _unique("gate")
    uid = db.create_user(f"{slug}@test.example", "TestPass123!", username=slug)
    if with_addon:
        # 30-day window — well outside any expiry path the gate inspects.
        db.set_trading_addon(uid, True, period_end=int(time.time()) + 30 * 86400)
    raw = db.create_user_session(uid)
    return uid, raw


def _prime_csrf(token: str) -> str:
    """Get a CSRF cookie on the TestClient and return its value. The
    portfolio mutating routes are POST, so the double-submit pair must
    line up or CSRFMiddleware returns 403 before the gate ever fires."""
    client.get(
        "/feedback",
        cookies={"narve_session": token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post(path: str, token: str, body: dict | None = None):
    csrf = _prime_csrf(token)
    return client.post(
        path,
        cookies={"narve_session": token, "_csrf": csrf},
        headers={"X-CSRF-Token": csrf},
        json=body if body is not None else {},
    )


def _clear_kalshi_buckets() -> None:
    """Wipe just the kalshi-connect-* rate-limit keys between cases.
    The connect handler in some branches layers buckets in front of the
    gate; this keeps a prior test from leaving them half-full so a
    fresh user can't see a stale 429 where we asserted 200/402."""
    try:
        from security.rate_limiter import limiter as _rl
        with _rl._lock:
            stale = [
                k for k in list(_rl._windows.keys())
                if k.startswith("kalshi-connect-")
            ]
            for k in stale:
                del _rl._windows[k]
    except Exception:
        pass


@contextlib.contextmanager
def _stub_kalshi_login():
    """Stand in for kalshi.login so addon users get past the upstream
    network call. The gate is what we're testing, not the proxy itself."""
    async def _ok(_email, _password):
        return {
            "token": "fake.kalshi.token",
            "member_id": "m-1",
            "expires_at": int(time.time()) + 3600,
        }
    with patch.object(_kalshi_mod, "login", new=AsyncMock(side_effect=_ok)):
        yield


@contextlib.contextmanager
def _stub_sync_positions():
    """Stub both platforms' sync_positions so addon users can hit the
    sync endpoint without the test reaching out to the live Polymarket
    / Kalshi APIs. Returns the canned per-platform shape the route emits."""
    async def _poly_sync(_uid):
        return {"count": 0, "wallet": None, "error": "not_connected"}

    async def _kalshi_sync(_uid):
        return {"count": 0, "error": "not_connected"}

    with patch.object(
        _poly_mod, "sync_positions",
        new=AsyncMock(side_effect=_poly_sync),
    ), patch.object(
        _kalshi_mod, "sync_positions",
        new=AsyncMock(side_effect=_kalshi_sync),
    ):
        yield


# ── Free-user must hit the gate ────────────────────────────────────────────


_GATED_STATUSES = (402, 403)


class TestFreeUserBlocked(unittest.TestCase):
    """Each state-mutating route returns a gating status (402 or 403)
    for a session without the Trading add-on.

    Why both 402 and 403 are accepted:
      * ``portfolio.routes._require_trading_addon`` raises HTTPException
        with status 402 (Payment Required) — the user IS authenticated,
        the request is gated behind a paid product. That is the right
        status for the portfolio module's own routes.
      * ``/api/kelly/calculate`` is also registered by ``market_routes``,
        and FastAPI uses first-match routing. The market_routes handler
        is registered first and shadows portfolio.routes, returning 403
        for the same gate. The security contract — "free users cannot
        reach this surface" — holds in both cases; the status code
        differs only because of the dispatch order.

    For the routes uniquely owned by portfolio.routes (status, sync, the
    two disconnects, kalshi connect, polymarket connect, kelly/bankroll)
    we tighten the assertion to ``== 402`` so a regression that swaps
    the handler for one with a different gate is caught.
    """

    def setUp(self):
        _clear_kalshi_buckets()
        client.cookies.clear()
        _uid, self.token = _make_user(with_addon=False)

    def tearDown(self):
        _clear_kalshi_buckets()

    def test_polymarket_connect_blocked(self):
        r = _post(
            "/api/portfolio/polymarket/connect", self.token,
            {"wallet_address": "0x" + "a" * 40},
        )
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_kalshi_connect_blocked(self):
        # Stub the upstream login anyway so a regression that lets a
        # free user past the gate still doesn't smash the live API.
        with _stub_kalshi_login():
            r = _post(
                "/api/portfolio/kalshi/connect", self.token,
                {"email": "victim@kalshi.example", "password": "irrelevant"},
            )
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_sync_positions_blocked(self):
        with _stub_sync_positions():
            r = _post("/api/portfolio/sync", self.token)
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_kelly_calculate_blocked(self):
        # /api/kelly/calculate is also registered by market_routes (which
        # wins first-match dispatch and returns 403). Accept either to
        # stay green across the dispatch order; the security contract
        # holds regardless.
        r = _post(
            "/api/kelly/calculate", self.token,
            {"our_probability": 0.6, "market_price": 0.5},
        )
        self.assertIn(r.status_code, _GATED_STATUSES, msg=r.text)

    def test_bankroll_set_blocked(self):
        r = _post("/api/kelly/bankroll", self.token, {"bankroll_usd": 5000})
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_polymarket_disconnect_blocked(self):
        r = _post("/api/portfolio/polymarket/disconnect", self.token)
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_kalshi_disconnect_blocked(self):
        r = _post("/api/portfolio/kalshi/disconnect", self.token)
        self.assertEqual(r.status_code, 402, msg=r.text)

    def test_gate_has_no_side_effect(self):
        """A blocked Polymarket connect MUST NOT upsert a wallet row.
        Catches the regression where the gate is bypassed and the row
        is written before the 402 response is returned."""
        slug = _unique("nofx")
        uid = db.create_user(f"{slug}@test.example", "TestPass123!", username=slug)
        raw = db.create_user_session(uid)
        wallet = "0x" + "b" * 40
        r = _post(
            "/api/portfolio/polymarket/connect", raw,
            {"wallet_address": wallet},
        )
        self.assertEqual(r.status_code, 402, msg=r.text)
        # No wallet row should exist for this user.
        self.assertIsNone(_poly_mod.get_connection(uid))


# ── Addon-active user is allowed through ───────────────────────────────────


class TestAddonUserAllowed(unittest.TestCase):
    """Same 6 mutating routes return 2xx for an add-on holder. We stub
    the heavy upstreams (Kalshi login, exchange sync) so the test reaches
    the gate, passes, and returns the route's own success shape."""

    def setUp(self):
        _clear_kalshi_buckets()
        client.cookies.clear()
        self.uid, self.token = _make_user(with_addon=True)

    def tearDown(self):
        _clear_kalshi_buckets()

    def test_polymarket_connect_ok(self):
        r = _post(
            "/api/portfolio/polymarket/connect", self.token,
            {"wallet_address": "0x" + "c" * 40},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json()["connected"])

    def test_kalshi_connect_ok(self):
        with _stub_kalshi_login():
            r = _post(
                "/api/portfolio/kalshi/connect", self.token,
                {"email": f"{_unique('trader')}@kalshi.example",
                 "password": "irrelevant-stubbed"},
            )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json().get("connected"))

    def test_sync_positions_ok(self):
        with _stub_sync_positions():
            r = _post("/api/portfolio/sync", self.token)
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        # Per-platform field surfaces the not_connected error rather than
        # 4xx-ing the whole call — keeps the upsell dashboard simple.
        self.assertIn("polymarket", body)
        self.assertIn("kalshi", body)

    def test_kelly_calculate_past_gate(self):
        # /api/kelly/calculate is dispatched to market_routes (registered
        # first), whose request body shape differs from portfolio.routes.
        # We assert the gate is PASSED — i.e. the response is anything
        # other than the 402/403 gate statuses. A 400 ``market_id``
        # complaint here is fine: it means the addon check let us reach
        # the handler's own validation layer.
        r = _post(
            "/api/kelly/calculate", self.token,
            {"our_probability": 0.6, "market_price": 0.5,
             "bankroll_usd": 10_000},
        )
        self.assertNotIn(r.status_code, _GATED_STATUSES, msg=r.text)

    def test_bankroll_set_ok(self):
        r = _post(
            "/api/kelly/bankroll", self.token, {"bankroll_usd": 12_345},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertEqual(r.json()["bankroll_usd"], 12_345)

    def test_polymarket_disconnect_ok(self):
        # Seed a credential so the disconnect has something to scrub.
        db.upsert_market_credential(
            self.uid, "polymarket",
            polymarket_wallet_address="0x" + "d" * 40,
        )
        r = _post("/api/portfolio/polymarket/disconnect", self.token)
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json()["disconnected"])
        row = db.get_market_credential(self.uid, "polymarket")
        # Row remains (UI shows "Reconnect") but is flagged inactive.
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)

    def test_kalshi_disconnect_ok(self):
        db.upsert_market_credential(
            self.uid, "kalshi",
            kalshi_token="enc.fake.token", kalshi_member_id="m-99",
        )
        r = _post("/api/portfolio/kalshi/disconnect", self.token)
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json()["disconnected"])
        row = db.get_market_credential(self.uid, "kalshi")
        self.assertIsNotNone(row)
        self.assertEqual(row["is_active"], 0)
        # Token is scrubbed; member_id stays so the UI label survives.
        self.assertIsNone(row["kalshi_token"])
        self.assertEqual(row["kalshi_member_id"], "m-99")


# ── Read-only status endpoint stays accessible ─────────────────────────────


class TestStatusReadable(unittest.TestCase):
    """The GET /api/portfolio/status endpoint is the dashboard's signal
    for whether to show the upsell or the active-user controls. It must
    NOT 402 — that would make the upsell card un-renderable."""

    def setUp(self):
        client.cookies.clear()

    def test_free_user_status_is_200_with_has_addon_false(self):
        _uid, token = _make_user(with_addon=False)
        r = client.get(
            "/api/portfolio/status",
            cookies={"narve_session": token},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertFalse(r.json()["has_addon"])

    def test_addon_user_status_is_200_with_has_addon_true(self):
        _uid, token = _make_user(with_addon=True)
        r = client.get(
            "/api/portfolio/status",
            cookies={"narve_session": token},
        )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json()["has_addon"])


if __name__ == "__main__":
    unittest.main()
