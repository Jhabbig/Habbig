"""Tests for the subscription-pause guard in gateway/queries/subscriptions.py.

AUDIT (HIGH): the access-check helpers historically only filtered on
``subscriptions.status`` + ``expires_at``, never consulting
``users.subscription_paused_until``. Paused users therefore retained
access until their next renewal arrived. This file pins the corrected
behaviour for the four affected helpers:

    * has_active_subscription
    * has_any_active_subscription
    * get_user_active_subproducts
    * get_user_subscription_tier

Three cases per helper:

    1. Paused user with subscription_paused_until=tomorrow → no access.
    2. Paused user with subscription_paused_until=yesterday (expired
       pause) → access restored.
    3. Non-paused user → returns the pre-fix value (access intact).

Uses the shared in-memory DB via tests._testdb so the test pins the
real query module rather than a hand-rolled mock.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Opt into the conftest's shared in-memory DB.
USES_TESTDB = True

from tests import _testdb  # noqa: E402,F401

import db  # noqa: E402

# Re-pin db.conn to the shared fake so sibling test files can't poison us.
_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


def _make_paying_user(
    email: str,
    username: str,
    *,
    plan: str = "pro",
    dashboard_key: str = "dash_truth",
    days_left: int = 300,
) -> int:
    """Create a user with one active subscription row. Returns user_id."""
    uid = db.create_user(email, "TestPass123!", username=username)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscriptions "
            "(user_id, dashboard_key, plan, status, started_at, expires_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (uid, dashboard_key, plan, now, now + days_left * 86400),
        )
    return uid


def _set_pause(uid: int, until_ts: int | None) -> None:
    """Set or clear users.subscription_paused_until for ``uid``.

    ``until_ts`` is a unix timestamp; ``None`` clears the column.
    """
    with db.conn() as c:
        if until_ts is None:
            c.execute(
                "UPDATE users SET subscription_paused_until = NULL WHERE id = ?",
                (uid,),
            )
        else:
            c.execute(
                "UPDATE users SET subscription_paused_until = datetime(?, 'unixepoch') "
                "WHERE id = ?",
                (until_ts, uid),
            )


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        # Clean any rows left by sibling tests so we run in isolation.
        with db.conn() as c:
            c.execute("DELETE FROM subscriptions")
            c.execute("UPDATE users SET subscription_paused_until = NULL")
        super().setUp()


class TestHasActiveSubscriptionRespectsPause(_Base):
    def test_paused_user_future_until_loses_access(self):
        uid = _make_paying_user("pause-has@t.com", "pause_has", dashboard_key="dash_truth")
        _set_pause(uid, int(time.time()) + 30 * 86400)
        self.assertFalse(db.has_active_subscription(uid, "dash_truth"))

    def test_expired_pause_restores_access(self):
        uid = _make_paying_user("expired-has@t.com", "expired_has", dashboard_key="dash_truth")
        _set_pause(uid, int(time.time()) - 86400)  # yesterday
        self.assertTrue(db.has_active_subscription(uid, "dash_truth"))

    def test_unpaused_user_retains_access(self):
        uid = _make_paying_user("active-has@t.com", "active_has", dashboard_key="dash_truth")
        # subscription_paused_until is NULL by default.
        self.assertTrue(db.has_active_subscription(uid, "dash_truth"))


class TestHasAnyActiveSubscriptionRespectsPause(_Base):
    def test_paused_user_future_until_returns_false(self):
        uid = _make_paying_user("pause-any@t.com", "pause_any")
        _set_pause(uid, int(time.time()) + 30 * 86400)
        self.assertFalse(db.has_any_active_subscription(uid))

    def test_expired_pause_returns_true(self):
        uid = _make_paying_user("expired-any@t.com", "expired_any")
        _set_pause(uid, int(time.time()) - 86400)
        self.assertTrue(db.has_any_active_subscription(uid))

    def test_unpaused_user_returns_true(self):
        uid = _make_paying_user("active-any@t.com", "active_any")
        self.assertTrue(db.has_any_active_subscription(uid))


class TestGetUserActiveSubproductsRespectsPause(_Base):
    def test_paused_user_future_until_returns_empty_set(self):
        uid = _make_paying_user(
            "pause-subs@t.com", "pause_subs", dashboard_key="dash_truth"
        )
        _set_pause(uid, int(time.time()) + 30 * 86400)
        self.assertEqual(db.get_user_active_subproducts(uid), set())

    def test_expired_pause_returns_normal_set(self):
        uid = _make_paying_user(
            "expired-subs@t.com", "expired_subs", dashboard_key="dash_truth"
        )
        _set_pause(uid, int(time.time()) - 86400)
        self.assertEqual(db.get_user_active_subproducts(uid), {"dash_truth"})

    def test_unpaused_user_returns_normal_set(self):
        uid = _make_paying_user(
            "active-subs@t.com", "active_subs", dashboard_key="dash_truth"
        )
        self.assertEqual(db.get_user_active_subproducts(uid), {"dash_truth"})


class TestGetUserSubscriptionTierRespectsPause(_Base):
    def test_paused_pro_user_future_until_returns_none(self):
        uid = _make_paying_user(
            "pause-tier@t.com", "pause_tier", plan="pro_annual"
        )
        _set_pause(uid, int(time.time()) + 30 * 86400)
        self.assertEqual(db.get_user_subscription_tier(uid), "none")

    def test_expired_pause_returns_normal_tier(self):
        uid = _make_paying_user(
            "expired-tier@t.com", "expired_tier", plan="pro_annual"
        )
        _set_pause(uid, int(time.time()) - 86400)
        self.assertEqual(db.get_user_subscription_tier(uid), "pro")

    def test_unpaused_user_returns_normal_tier(self):
        uid = _make_paying_user(
            "active-tier@t.com", "active_tier", plan="pro_annual"
        )
        self.assertEqual(db.get_user_subscription_tier(uid), "pro")


if __name__ == "__main__":
    unittest.main()
