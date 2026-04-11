"""Tests for the onboarding flow — DB layer and API contract."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class _DBHarness:
    """Reusable in-memory DB swap for tests."""

    @classmethod
    def install(cls):
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

    @classmethod
    def teardown(cls):
        db.conn = cls._orig
        cls._conn.close()


class TestOnboardingDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _DBHarness.install()
        cls._counter = 0

    @classmethod
    def tearDownClass(cls):
        _DBHarness.teardown()

    def _new_user(self) -> int:
        TestOnboardingDb._counter += 1
        n = TestOnboardingDb._counter
        return db.create_user(f"ob{n}@test.com", "TestPass123!", username=f"obuser{n}")

    def test_initial_status_not_completed(self):
        user_id = self._new_user()
        status = db.get_onboarding_status(user_id)
        self.assertFalse(status["completed"])
        self.assertEqual(status["categories"], [])

    def test_save_categories(self):
        user_id = self._new_user()
        db.set_onboarding_categories(user_id, ["politics", "crypto"])
        status = db.get_onboarding_status(user_id)
        self.assertEqual(set(status["categories"]), {"politics", "crypto"})

    def test_save_notifications(self):
        user_id = self._new_user()
        db.set_onboarding_notifications(user_id, push=True, email=False, ev_threshold=0.15, cred_threshold=0.6)
        status = db.get_onboarding_status(user_id)
        self.assertTrue(status["notify_push"])
        self.assertFalse(status["notify_email"])
        self.assertAlmostEqual(status["notify_ev_threshold"], 0.15)
        self.assertAlmostEqual(status["notify_cred_threshold"], 0.6)

    def test_complete_onboarding_sets_flag(self):
        user_id = self._new_user()
        db.complete_onboarding(user_id)
        status = db.get_onboarding_status(user_id)
        self.assertTrue(status["completed"])
        self.assertIsNotNone(status["completed_at"])

    def test_complete_is_idempotent(self):
        user_id = self._new_user()
        db.complete_onboarding(user_id)
        db.complete_onboarding(user_id)
        status = db.get_onboarding_status(user_id)
        self.assertTrue(status["completed"])


if __name__ == "__main__":
    unittest.main()
