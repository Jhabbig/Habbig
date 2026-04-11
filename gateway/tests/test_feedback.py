"""Tests for the feedback submission DB layer."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestFeedbackDb(unittest.TestCase):
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
        cls.user_id = db.create_user("fb@test.com", "TestPass123!", username="fbuser")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_create_feedback_returns_id(self):
        fid = db.create_feedback(
            user_id=self.user_id, type_="bug", message="Login broke",
            priority="high", page_url="/login", user_tier="pro",
        )
        self.assertIsInstance(fid, int)
        self.assertGreater(fid, 0)

    def test_list_feedback_includes_user_email(self):
        db.create_feedback(self.user_id, "feature", "Add dark mode toggle", None, "/settings", "pro")
        rows = db.list_feedback()
        self.assertTrue(any(r["user_email"] == "fb@test.com" for r in rows))

    def test_filter_by_status(self):
        fid = db.create_feedback(self.user_id, "bug", "Crash", "critical", "/dash", "pro")
        db.update_feedback_status(fid, "resolved")
        resolved = db.list_feedback(status_filter="resolved")
        self.assertTrue(any(r["id"] == fid for r in resolved))
        opens = db.list_feedback(status_filter="open")
        self.assertFalse(any(r["id"] == fid for r in opens))

    def test_count_open_feedback(self):
        before = db.count_feedback_by_status("open")
        db.create_feedback(self.user_id, "general", "Looks great", None, "/", "pro")
        after = db.count_feedback_by_status("open")
        self.assertEqual(after, before + 1)

    def test_update_status_sets_resolved_at(self):
        fid = db.create_feedback(self.user_id, "bug", "Slow", "low", "/dash", "trader")
        db.update_feedback_status(fid, "closed", admin_notes="duplicate")
        with db.conn() as c:
            row = c.execute("SELECT * FROM feedback_submissions WHERE id = ?", (fid,)).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertIsNotNone(row["resolved_at"])
        self.assertEqual(row["admin_notes"], "duplicate")


if __name__ == "__main__":
    unittest.main()
