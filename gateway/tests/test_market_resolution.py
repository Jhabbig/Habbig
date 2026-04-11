"""Tests for Feature 8: market resolution notifications."""

from __future__ import annotations

import asyncio
import time
import unittest

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _add_view(user_id: int, market_slug: str) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO user_market_views (user_id, market_slug, first_viewed_at, last_viewed_at, view_count, notified_on_resolution) "
            "VALUES (?, ?, ?, ?, 1, 0)",
            (user_id, market_slug, now, now),
        )


class TestMarketViewTracking(unittest.TestCase):
    def test_view_row_created(self):
        uid = db.create_user("viewer@test.com", "InitialPass123!", username="viewer")
        _add_view(uid, "btc-etf-q2")
        with db.conn() as c:
            row = c.execute(
                "SELECT view_count, notified_on_resolution FROM user_market_views "
                "WHERE user_id = ? AND market_slug = ?",
                (uid, "btc-etf-q2"),
            ).fetchone()
        self.assertEqual(row["view_count"], 1)
        self.assertEqual(row["notified_on_resolution"], 0)


class TestResolutionJob(unittest.TestCase):
    def test_job_notifies_all_viewers_and_flags_them(self):
        uids = []
        for i in range(3):
            uid = db.create_user(f"resol-{i}@test.com", "TestPass123!", username=f"resol{i}")
            uids.append(uid)
            _add_view(uid, "us-prez-2024")

        from jobs.notification_jobs import send_market_resolution_notifications
        result = _run(send_market_resolution_notifications(
            market_slug="us-prez-2024",
            outcome="YES",
            market_question="Will X win the 2024 election?",
        ))
        self.assertEqual(result["notified"], 3)

        # All three rows should now have notified_on_resolution=1
        with db.conn() as c:
            rows = c.execute(
                "SELECT notified_on_resolution FROM user_market_views WHERE market_slug = ?",
                ("us-prez-2024",),
            ).fetchall()
        for r in rows:
            self.assertEqual(r["notified_on_resolution"], 1)

    def test_job_does_not_double_notify(self):
        uid = db.create_user("dedupe@test.com", "TestPass123!", username="dedupe")
        _add_view(uid, "dedupe-market")

        from jobs.notification_jobs import send_market_resolution_notifications
        first = _run(send_market_resolution_notifications(
            market_slug="dedupe-market", outcome="NO",
        ))
        second = _run(send_market_resolution_notifications(
            market_slug="dedupe-market", outcome="NO",
        ))
        self.assertEqual(first["notified"], 1)
        self.assertEqual(second["notified"], 0)

    def test_job_batches_with_more_flag(self):
        # Create 3 viewers, call with batch_size=2 → first run notifies 2 + more=True
        for i in range(3):
            uid = db.create_user(f"batch-{i}@test.com", "TestPass123!", username=f"batch{i}")
            _add_view(uid, "batch-market")

        from jobs.notification_jobs import send_market_resolution_notifications
        first = _run(send_market_resolution_notifications(
            market_slug="batch-market", outcome="YES", batch_size=2,
        ))
        self.assertEqual(first["notified"], 2)
        self.assertTrue(first["more"])

    def test_job_skips_deleted_users(self):
        uid = db.create_user("deleted-viewer@test.com", "TestPass123!", username="delvwr")
        _add_view(uid, "skip-deleted")
        with db.conn() as c:
            c.execute("UPDATE users SET is_deleted = 1 WHERE id = ?", (uid,))

        from jobs.notification_jobs import send_market_resolution_notifications
        result = _run(send_market_resolution_notifications(
            market_slug="skip-deleted", outcome="YES",
        ))
        self.assertEqual(result["notified"], 0)


if __name__ == "__main__":
    unittest.main()
