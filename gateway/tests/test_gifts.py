"""Tests for the gifted subscriptions DB layer."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestGiftsDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()
        cls.admin = db.create_user("giftadmin@test.com", "TestPass123!", username="giftadmin", admin_level=2)
        cls.target = db.create_user("gifttarget@test.com", "TestPass123!", username="gifttarget")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_create_gift_returns_id(self):
        gid = db.create_gift(
            user_id=self.target,
            gifted_by_admin_id=self.admin,
            subscription_type="pro_monthly",
            ends_at=int(time.time()) + 30 * 86400,
            is_permanent=False,
        )
        self.assertGreater(gid, 0)

    def test_enterprise_config_persisted(self):
        cfg = {
            "max_api_calls_per_day": 5000,
            "max_topics": 25,
            "trading_addon_included": True,
            "intelligence_addon_included": True,
            "api_rate_limit": "high",
            "allowed_features": "all",
        }
        gid = db.create_gift(
            user_id=self.target,
            gifted_by_admin_id=self.admin,
            subscription_type="enterprise",
            ends_at=None,
            is_permanent=True,
            is_enterprise=True,
            enterprise_config=cfg,
            internal_notes="press partnership",
        )
        with db.conn() as c:
            row = c.execute("SELECT * FROM gifted_subscriptions WHERE id = ?", (gid,)).fetchone()
        import json
        stored = json.loads(row["enterprise_config"])
        self.assertEqual(stored["max_api_calls_per_day"], 5000)
        self.assertEqual(stored["api_rate_limit"], "high")
        self.assertTrue(row["is_enterprise"])
        self.assertTrue(row["is_permanent"])

    def test_revoke_gift_marks_revoked(self):
        gid = db.create_gift(
            user_id=self.target,
            gifted_by_admin_id=self.admin,
            subscription_type="trader_monthly",
            ends_at=int(time.time()) + 30 * 86400,
            is_permanent=False,
        )
        db.revoke_gift(gid, admin_id=self.admin)
        with db.conn() as c:
            row = c.execute("SELECT * FROM gifted_subscriptions WHERE id = ?", (gid,)).fetchone()
        self.assertTrue(row["revoked"])
        self.assertIsNotNone(row["revoked_at"])
        self.assertEqual(row["revoked_by_admin_id"], self.admin)

    def test_intelligence_addon_active_via_gift(self):
        db.create_gift(
            user_id=self.target,
            gifted_by_admin_id=self.admin,
            subscription_type="intelligence_addon",
            ends_at=int(time.time()) + 30 * 86400,
            is_permanent=False,
        )
        self.assertTrue(db.get_user_intelligence_addon_active(self.target))

    def test_user_subscription_tier_admin_is_pro(self):
        self.assertEqual(db.get_user_subscription_tier(self.admin), "pro")

    def test_list_active_gifts_excludes_revoked(self):
        gid = db.create_gift(self.target, self.admin, "pro_annual", None, True)
        db.revoke_gift(gid, self.admin)
        gifts = db.list_active_gifts()
        self.assertFalse(any(g["id"] == gid for g in gifts))


if __name__ == "__main__":
    unittest.main()
