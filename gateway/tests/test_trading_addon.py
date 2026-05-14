"""Tests for Trading Add-on gating, admin toggle, and settings display."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestTradingAddon(unittest.TestCase):
    """Tests for trading add-on database operations."""

    @classmethod
    def setUpClass(cls):
        cls._test_conn = sqlite3.connect(":memory:")
        cls._test_conn.row_factory = sqlite3.Row
        cls._test_conn.execute("PRAGMA foreign_keys = ON")
        cls._test_conn.executescript(db.SCHEMA)
        cls._test_conn.commit()

        @contextlib.contextmanager
        def test_conn():
            try:
                yield cls._test_conn
                cls._test_conn.commit()
            except Exception:
                cls._test_conn.rollback()
                raise

        cls._orig_conn = db.conn
        db.conn = test_conn

        # Run migrations (adds trading_addon columns)
        db.init_db()

        # Create test users
        cls.user_id = db.create_user("trader@test.com", "TestPass123!", username="trader")
        cls.admin_id = db.create_user("admin@test.com", "TestPass123!", username="testadmin", admin_level=1)

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def test_addon_default_inactive(self):
        # Use a fresh user to avoid state from other tests
        fresh = db.create_user("fresh_addon@test.com", "TestPass123!", username="freshaddon")
        status = db.get_trading_addon_status(fresh)
        self.assertFalse(status["active"])
        self.assertIsNone(status["period_end"])

    def test_activate_addon(self):
        period_end = int(time.time()) + 30 * 86400
        db.set_trading_addon(self.user_id, True, period_end)
        status = db.get_trading_addon_status(self.user_id)
        self.assertTrue(status["active"])
        self.assertEqual(status["period_end"], period_end)

    def test_deactivate_addon(self):
        db.set_trading_addon(self.user_id, False, None)
        status = db.get_trading_addon_status(self.user_id)
        self.assertFalse(status["active"])

    def test_has_trading_addon_false_by_default(self):
        new_user = db.create_user("noaddon@test.com", "TestPass123!", username="noaddon")
        self.assertFalse(db.has_trading_addon(new_user))

    def test_has_trading_addon_true_when_active(self):
        db.set_trading_addon(self.user_id, True, int(time.time()) + 86400)
        self.assertTrue(db.has_trading_addon(self.user_id))
        # Clean up
        db.set_trading_addon(self.user_id, False, None)

    def test_admin_always_has_trading_addon(self):
        self.assertTrue(db.has_trading_addon(self.admin_id))

    def test_expired_addon_returns_inactive(self):
        # Set to past expiry
        db.set_trading_addon(self.user_id, True, int(time.time()) - 100)
        status = db.get_trading_addon_status(self.user_id)
        self.assertFalse(status["active"])  # Should be inactive due to expiry
        self.assertFalse(db.has_trading_addon(self.user_id))
        # Clean up
        db.set_trading_addon(self.user_id, False, None)

    def test_locked_state_shows_correct_pricing(self):
        """Verify the pricing page references the Trading Access tier
        and the canonical £25/mo monthly price somewhere on the page.

        The legacy template used ``data-monthly="25"`` data attributes
        for the JS price flipper; the redesigned page renders the
        amount inline (``<span class="pr-currency">&pound;</span>25``).
        Either rendering carries the same contract — the page must
        show the canonical price next to the Trading Access label.
        """
        with open(os.path.join(os.path.dirname(__file__), "..", "static", "pricing.html")) as f:
            html = f.read()
        self.assertIn("Trading Access", html)
        # Accept either the legacy data attribute or the inline £25 amount.
        has_legacy_data_attr = 'data-monthly="25"' in html
        has_inline_price = ">25<" in html or "&pound;</span>25" in html
        self.assertTrue(
            has_legacy_data_attr or has_inline_price,
            "pricing.html does not advertise the £25/mo Trading Access price",
        )

    def test_settings_template_has_billing_section(self):
        """Verify settings.html includes the billing section placeholder."""
        with open(os.path.join(os.path.dirname(__file__), "..", "static", "settings.html")) as f:
            html = f.read()
        self.assertIn("raw_billing_section", html)


if __name__ == "__main__":
    unittest.main()
