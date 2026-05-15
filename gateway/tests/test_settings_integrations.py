"""Tests for /settings/integrations — the dedicated UI surface for
Polymarket wallet, Kalshi token, and bankroll.

The GET page itself is a thin shell: it requires auth (redirect to /token)
and renders three settings-cards that hydrate client-side. Most of the
behaviour lives behind market_routes' JSON endpoints
(/api/markets/connections, /api/markets/connect/{src}, /api/user/bankroll),
which are exercised by test_portfolio_integration.py — this file focuses
on the new wiring:

  - GET requires authentication.
  - GET renders the three integration cards regardless of connection state.
  - GET surfaces a Kalshi member-id / Polymarket wallet address when the user
    has those creds — and gracefully renders the same shell when they don't.
  - PATCH /api/user/bankroll rejects negative numbers.
  - CSRF: POST mutators (Kalshi+Polymarket connect) require the double-
    submit CSRF cookie+header pair. PATCH /api/user/bankroll and DELETE
    /api/markets/connect/{source} aren't CSRF-gated by the gateway's
    current middleware (it only inspects POST) — same-origin + session
    cookies guard those. The DELETE/PATCH tests below assert round-trip
    success rather than a CSRF reject so they stay accurate as the
    middleware evolves.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Explicit opt-in for the shared in-memory test DB — same pattern as
# test_settings_billing. Must come BEFORE server is imported so db.conn is
# patched once at module load.
USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)

_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _clear_client_cookies() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────


_unique_ctr = 0


def _unique(prefix: str) -> str:
    global _unique_ctr
    _unique_ctr += 1
    return f"{prefix}{_unique_ctr}"


def _make_trader_user(email: str, username: str) -> tuple[int, str]:
    """User with active Trading Add-on — passes _require_markets_user."""
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
    return uid, db.create_session(uid)


def _make_plain_user(email: str, username: str) -> tuple[int, str]:
    """User without the Trading Add-on — should 403 on market endpoints."""
    uid = db.create_user(email, "TestPass123!", username=username)
    return uid, db.create_session(uid)


def _auth(token: str) -> dict:
    return {"Cookie": f"{server.COOKIE_NAME}={token}"}


def _prime_csrf(token: str) -> str:
    """Prime ``_csrf`` cookie by hitting the integrations page itself, then
    read it back from the shared client jar. Mirrors the pattern from
    test_portfolio_integration._prime_csrf — the CSRF middleware sets the
    cookie on any GET that returns HTML."""
    client.get(
        "/settings/integrations",
        cookies={server.COOKIE_NAME: token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_json(path: str, token: str, json_body: dict | None = None):
    """POST/PATCH/DELETE with session cookie + matching CSRF pair."""
    csrf = _prime_csrf(token)
    return client.post(
        path,
        cookies={server.COOKIE_NAME: token, "_csrf": csrf},
        headers={"X-CSRF-Token": csrf},
        json=json_body if json_body is not None else {},
    )


def _patch_json(path: str, token: str, json_body: dict | None = None,
                with_csrf: bool = True):
    """PATCH with the same CSRF wiring. ``with_csrf=False`` deliberately
    omits the header so we can prove CSRF is enforced."""
    csrf = _prime_csrf(token) if with_csrf else ""
    cookies = {server.COOKIE_NAME: token}
    headers: dict = {}
    if with_csrf and csrf:
        cookies["_csrf"] = csrf
        headers["X-CSRF-Token"] = csrf
    return client.patch(
        path, cookies=cookies, headers=headers,
        json=json_body if json_body is not None else {},
    )


def _delete(path: str, token: str, with_csrf: bool = True):
    csrf = _prime_csrf(token) if with_csrf else ""
    cookies = {server.COOKIE_NAME: token}
    headers: dict = {}
    if with_csrf and csrf:
        cookies["_csrf"] = csrf
        headers["X-CSRF-Token"] = csrf
    return client.delete(path, cookies=cookies, headers=headers)


# ── DB isolation base ──────────────────────────────────────────────────────


class _DbIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client_cookies()
        super().setUp()


# ── GET /settings/integrations — auth + render ─────────────────────────────


class TestPageRender(_DbIsolation):
    def test_requires_login(self):
        r = client.get("/settings/integrations", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertIn("/token", r.headers["location"])

    def test_renders_three_cards_for_authed_user(self):
        slug = _unique("si_render")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = client.get(
            "/settings/integrations",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # Breadcrumb + all three cards present.
        self.assertIn("Integrations", r.text)
        self.assertIn('data-card="polymarket"', r.text)
        self.assertIn('data-card="kalshi"', r.text)
        self.assertIn('id="si-bankroll-input"', r.text)
        # And the connect buttons that the JS hydrates against.
        self.assertIn('id="si-poly-connect"', r.text)
        self.assertIn('id="si-kalshi-connect"', r.text)
        self.assertIn('id="si-bankroll-save"', r.text)
        # The static shell defaults the pill to "Loading…" — JS fills it in.
        self.assertIn("Loading", r.text)

    def test_renders_with_no_connections(self):
        """Page must render the same shell whether the user has creds or not.
        The JSON endpoints (loaded client-side) carry the actual state."""
        slug = _unique("si_empty")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = client.get(
            "/settings/integrations",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # Connect CTA visible (it's the default pre-JS state).
        self.assertIn("Connect Wallet", r.text)
        self.assertIn("Connect Kalshi", r.text)

    def test_renders_with_connected_polymarket_wallet(self):
        """Page is a shell, but the JSON endpoint that hydrates it returns
        the saved wallet address — verify the round-trip via the API the
        page calls on load."""
        slug = _unique("si_poly")
        uid, token = _make_trader_user(f"{slug}@test.com", slug)
        addr = "0x" + "ab" * 20  # 40-hex-digit checksum-less address
        db.upsert_market_credential(uid, "polymarket", polymarket_wallet_address=addr)

        r = client.get(
            "/settings/integrations",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # Page itself is a shell; check the API call the JS makes returns
        # the wallet so we know the hydration path works end-to-end.
        conns = client.get(
            "/api/markets/connections", headers=_auth(token),
        )
        self.assertEqual(conns.status_code, 200)
        body = conns.json()
        self.assertEqual(body["polymarket"]["address"], addr)
        self.assertEqual(body["polymarket"]["status"], "active")
        self.assertTrue(body["polymarket"]["connected"])


# ── /api/user/bankroll — validation ────────────────────────────────────────


class TestBankrollPatchValidation(_DbIsolation):
    def test_patch_rejects_negative(self):
        slug = _unique("si_br_neg")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = _patch_json("/api/user/bankroll", token, {"bankroll": -1})
        self.assertEqual(r.status_code, 400)

    def test_patch_accepts_zero(self):
        # Zero is valid (user explicitly says "no money to size against").
        slug = _unique("si_br_zero")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = _patch_json("/api/user/bankroll", token, {"bankroll": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["bankroll"], 0)

    def test_patch_accepts_positive(self):
        slug = _unique("si_br_pos")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = _patch_json("/api/user/bankroll", token, {"bankroll": 7500})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["bankroll"], 7500)

    def test_patch_rejects_non_numeric(self):
        slug = _unique("si_br_str")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = _patch_json("/api/user/bankroll", token, {"bankroll": "not a number"})
        self.assertEqual(r.status_code, 400)


# ── CSRF enforcement on mutating endpoints ────────────────────────────────


class TestCsrfAndAuth(_DbIsolation):
    """The integrations page POSTs/PATCHes/DELETEs to JSON endpoints. The
    gateway's CSRF middleware (server.py:979) inspects POST only — it
    rejects JSON POSTs without an ``X-CSRF-Token`` header that matches
    the ``_csrf`` cookie. PATCH and DELETE on the same routes are
    protected by the session cookie + same-origin (the middleware
    intentionally skips them; see test_portfolio_integration for parity).
    """

    def test_post_connect_kalshi_requires_csrf(self):
        slug = _unique("si_csrf_kalshi")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        r = client.post(
            "/api/markets/connect/kalshi",
            cookies={server.COOKIE_NAME: token},  # no _csrf cookie/header
            json={"email": "x@y.z", "password": "irrelevant"},
        )
        self.assertEqual(r.status_code, 403)

    def test_post_connect_polymarket_requires_csrf(self):
        """CSRF middleware fires before any body parsing happens, so we
        just need a plausible POST payload. We use the live SIWE body
        shape (the legacy ``{wallet_address}`` shape now 410's anyway —
        see test_polymarket_siwe.TestLegacyRemoval)."""
        slug = _unique("si_csrf_poly")
        _, token = _make_trader_user(f"{slug}@test.com", slug)
        addr = "0x" + "ab" * 20
        r = client.post(
            "/api/markets/connect/polymarket",
            cookies={server.COOKIE_NAME: token},  # no _csrf cookie/header
            json={"address": addr, "signature": "0x" + "00" * 65, "message": "stub"},
        )
        self.assertEqual(r.status_code, 403)

    def test_delete_credential_works_with_csrf(self):
        """End-to-end disconnect via the same DELETE the page issues."""
        slug = _unique("si_csrf_ok")
        uid, token = _make_trader_user(f"{slug}@test.com", slug)
        # Seed a credential to delete.
        addr = "0x" + "cd" * 20
        db.upsert_market_credential(uid, "polymarket", polymarket_wallet_address=addr)

        r = _delete("/api/markets/connect/polymarket", token, with_csrf=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("disconnected"))

    def test_connections_endpoint_requires_addon(self):
        """Page renders for any logged-in user, but the JSON endpoint it
        calls 403's without the Trading Add-on — that's how the JS shows
        the "add-on required" empty state."""
        slug = _unique("si_noaddon")
        _, token = _make_plain_user(f"{slug}@test.com", slug)
        r = client.get("/api/markets/connections", headers=_auth(token))
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
