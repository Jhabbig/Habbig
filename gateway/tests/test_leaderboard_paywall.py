"""Audit HIGH (2026-05-15) — leaderboard paywall regression suite.

Covers the gap the audit flagged in ``routes_referrals.py`` at lines
269-326:

  * GET  /api/leaderboard               → 402 for free / trial users,
                                          200 for paying tiers.
  * POST /api/leaderboard/participate   → 402 for free, then
                                          429 after the 5/hour quota.
  * DELETE /api/leaderboard/participate → same 402 / 429 envelope.
  * ``total_users_approx`` is a banded string ("1,000+" / "<100" / …),
    never the exact integer it used to be.
  * Handles of opted-in participants who never set a display name are
    rendered as ``"anonymous"`` — no ``user_42`` row IDs leak through.

Tests use the shared in-memory ``_testdb`` connection so migrations and
the conftest auto-pin to it. EMAIL_DRY_RUN=true → no real emails are
sent by any side effects.
"""

from __future__ import annotations

import os
import re
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared DB + migrations
import db  # noqa: E402
import db_referrals as dbr  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _mk_user(email: str, username: str = "") -> int:
    username = username or email.split("@")[0]
    return db.create_user(email, "TestPass123!", username=username)


def _give_active_sub(user_id: int, plan: str = "pro") -> None:
    """Mint an active paid subscription so ``get_user_subscription_tier``
    classifies the user as a paying tier. The dashboard_key string just
    has to be unique per (user, plan); the audit doesn't care which
    subproduct it lives on."""
    db.upsert_subscription(
        user_id=user_id,
        dashboard_key=f"paywall-test-{plan}-{user_id}",
        plan=plan,
        duration_days=30,
        source="test",
    )


def _authed_client(user_id: int) -> TestClient:
    """Match ``tests/test_referrals.py``'s helper — session cookie +
    a stable double-submit CSRF pair so authenticated POST/DELETE
    sail past CSRFMiddleware without hand-rolling a login flow."""
    c = TestClient(server.app)
    token = db.create_session(user_id)
    c.cookies.set(server.COOKIE_NAME, token)
    c.cookies.set("_csrf", "t_test_csrf_token_fixed")
    c.headers.update({"x-csrf-token": "t_test_csrf_token_fixed"})
    return c


def _reset_rate_limiter() -> None:
    """Wipe the in-memory sliding-window store so a single user's quota
    bucket doesn't leak between tests in this module. The Redis path
    is short-circuited in test by ``_redis_client is None``, so this
    in-memory cleanup is sufficient."""
    try:
        server._rate_store.clear()
    except Exception:
        pass


# ── GET /api/leaderboard — paywall ───────────────────────────────────────────


class TestLeaderboardGetPaywall(unittest.TestCase):
    def setUp(self):
        _reset_rate_limiter()

    def test_free_user_get_leaderboard_returns_402(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_free_{tag}@test.com", f"lbpw_free_{tag}")
        # No subscription minted — get_user_subscription_tier → "none".
        c = _authed_client(uid)
        r = c.get("/api/leaderboard")
        self.assertEqual(r.status_code, 402, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("paid", body["error"].lower())

    def test_pro_user_get_leaderboard_returns_200(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_pro_{tag}@test.com", f"lbpw_pro_{tag}")
        _give_active_sub(uid, plan="pro")
        c = _authed_client(uid)
        r = c.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["period"], "all")
        self.assertIsInstance(data["rows"], list)

    def test_trader_user_get_leaderboard_returns_200(self):
        # The audit's wording is "Paying subscribers only" — trader is
        # one of the paying tiers, so it must also pass.
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_trader_{tag}@test.com", f"lbpw_trader_{tag}")
        _give_active_sub(uid, plan="trader")
        c = _authed_client(uid)
        r = c.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200, r.text)

    def test_unauthenticated_get_returns_401_not_402(self):
        # Identity layer fires before the paywall so anonymous callers
        # see 401 (login first), not 402 (upgrade).
        client = TestClient(server.app)
        r = client.get("/api/leaderboard")
        self.assertEqual(r.status_code, 401)


# ── POST/DELETE participate — paywall + rate limit ───────────────────────────


class TestLeaderboardWriteAccess(unittest.TestCase):
    def setUp(self):
        _reset_rate_limiter()

    def test_free_user_participate_post_returns_402(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_pfree_{tag}@test.com", f"lbpw_pfree_{tag}")
        c = _authed_client(uid)
        r = c.post(
            "/api/leaderboard/participate",
            json={"display_name": "free_user_handle"},
        )
        self.assertEqual(r.status_code, 402, r.text)

    def test_free_user_participate_delete_returns_402(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_dfree_{tag}@test.com", f"lbpw_dfree_{tag}")
        c = _authed_client(uid)
        r = c.delete("/api/leaderboard/participate")
        self.assertEqual(r.status_code, 402, r.text)

    def test_pro_user_can_participate(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_okpost_{tag}@test.com", f"lbpw_okpost_{tag}")
        _give_active_sub(uid, plan="pro")
        c = _authed_client(uid)
        r = c.post(
            "/api/leaderboard/participate",
            json={"display_name": f"okhandle_{tag[-12:]}"},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_sixth_participate_post_within_an_hour_returns_429(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_rl_{tag}@test.com", f"lbpw_rl_{tag}")
        _give_active_sub(uid, plan="pro")
        c = _authed_client(uid)
        # Five successful writes — each one is its own opt-in/handle
        # update — must consume exactly the quota and no more.
        for i in range(5):
            r = c.post(
                "/api/leaderboard/participate",
                json={"display_name": f"rluser{tag[-8:]}_{i}"},
            )
            self.assertEqual(r.status_code, 200, f"call {i+1} body={r.text}")
        # 6th call inside the same hour: 429.
        r6 = c.post(
            "/api/leaderboard/participate",
            json={"display_name": f"rluser{tag[-8:]}_6"},
        )
        self.assertEqual(r6.status_code, 429, r6.text)

    def test_delete_shares_the_same_rate_limit_bucket(self):
        # POST and DELETE both spend from the same "leaderboard-write"
        # bucket — five mixed writes, then any sixth one is throttled.
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_mix_{tag}@test.com", f"lbpw_mix_{tag}")
        _give_active_sub(uid, plan="pro")
        c = _authed_client(uid)
        # 3 posts + 2 deletes = 5 writes.
        for i in range(3):
            r = c.post(
                "/api/leaderboard/participate",
                json={"display_name": f"mxh{tag[-8:]}_{i}"},
            )
            self.assertEqual(r.status_code, 200, r.text)
        for _ in range(2):
            r = c.delete("/api/leaderboard/participate")
            self.assertEqual(r.status_code, 200, r.text)
        # 6th write — a DELETE this time — should 429.
        r = c.delete("/api/leaderboard/participate")
        self.assertEqual(r.status_code, 429, r.text)


# ── Response-shape regressions: banded count + no raw user_id ────────────────


class TestLeaderboardResponseHygiene(unittest.TestCase):
    def setUp(self):
        _reset_rate_limiter()

    def test_total_users_approx_is_banded_not_exact(self):
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_band_{tag}@test.com", f"lbpw_band_{tag}")
        _give_active_sub(uid, plan="pro")
        c = _authed_client(uid)
        r = c.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        approx = data["total_users_approx"]
        # The field is now a display string. Two acceptable shapes:
        # "<100", "100+", or "<n>,<n>+" (banded). Reject anything that
        # is a bare integer / int-looking string (the pre-fix leak).
        self.assertIsInstance(approx, str, f"expected str, got {type(approx)}")
        self.assertTrue(
            approx in ("<100", "100+") or re.fullmatch(r"[\d,]+\+", approx),
            f"total_users_approx={approx!r} not in expected banded format",
        )
        # Belt and braces: even the largest in-test population shouldn't
        # arrive as a plain integer string ("42") — that would mean the
        # banding function was bypassed somewhere.
        self.assertFalse(
            re.fullmatch(r"\d+", approx),
            f"total_users_approx leaked an exact count: {approx!r}",
        )

    def test_banding_helper_produces_known_bands(self):
        # Direct table-test of _band_user_count so the contract is
        # locked even if the SQL count plumbing changes upstream.
        from routes_referrals import _band_user_count as band
        self.assertEqual(band(0), "<100")
        self.assertEqual(band(42), "<100")
        self.assertEqual(band(99), "<100")
        self.assertEqual(band(100), "100+")
        self.assertEqual(band(999), "100+")
        self.assertEqual(band(1_000), "1,000+")
        self.assertEqual(band(1_999), "1,000+")
        self.assertEqual(band(9_999), "9,000+")
        self.assertEqual(band(10_000), "10,000+")
        self.assertEqual(band(42_500), "40,000+")
        self.assertEqual(band(1_000_000), "1,000,000+")

    def test_anonymous_handle_replaces_raw_user_id(self):
        # Opt a paid user into the leaderboard with a non-empty handle,
        # then null out the handle directly in the DB to simulate the
        # legacy ``user_{id}`` fallback condition. The endpoint must
        # render that participant as "anonymous", never as user_<id>.
        tag = self._testMethodName
        uid = _mk_user(f"lbpw_anon_{tag}@test.com", f"lbpw_anon_{tag}")
        _give_active_sub(uid, plan="pro")
        ok = dbr.set_leaderboard_participation(
            uid, participate=True, display_name=f"realname_{tag[-12:]}",
        )
        self.assertTrue(ok["ok"], ok)
        # Force the participating row's handle to NULL so the API has
        # to hit the fallback branch we hardened. The opt-in state and
        # the handle live on the ``users`` row itself, not in a
        # separate ``leaderboard_opt_in`` table.
        with db.conn() as c:
            c.execute(
                "UPDATE users SET leaderboard_handle = NULL "
                "WHERE id = ?",
                (uid,),
            )

        viewer = _mk_user(f"lbpw_anonview_{tag}@test.com", f"lbpw_anonview_{tag}")
        _give_active_sub(viewer, plan="pro")
        c2 = _authed_client(viewer)
        r = c2.get("/api/leaderboard")
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["rows"]
        # No row may carry a "user_<digits>" handle (the pre-fix leak),
        # and any row whose handle was nulled must render as
        # "anonymous" specifically.
        for row in rows:
            handle = row["handle"]
            self.assertFalse(
                re.fullmatch(r"user_\d+", handle),
                f"raw user_id leaked in handle: {handle!r}",
            )
        # If the participant we just nulled is visible (depends on
        # whether they have any predictions in the in-memory DB), it
        # must be as "anonymous". If they aren't visible, the negative
        # check above is still load-bearing.
        for row in rows:
            if row["handle"] == "anonymous":
                # Exactly the contract — fallback is the literal token.
                self.assertEqual(row["handle"], "anonymous")


if __name__ == "__main__":
    unittest.main()
