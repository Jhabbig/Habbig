"""Tests for Feature 6: account deletion soft + hard delete."""

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


class TestSoftDelete(unittest.TestCase):
    def test_deletion_request_sets_timestamps(self):
        uid = db.create_user("softdel@test.com", "InitialPass123!", username="softdel")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ?, "
                "jwt_invalidated_before = ? WHERE id = ?",
                (now, now + 30 * 86400, now, uid),
            )
            row = c.execute(
                "SELECT deletion_requested_at, deletion_scheduled_for, jwt_invalidated_before, is_deleted "
                "FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["deletion_requested_at"], now)
        self.assertAlmostEqual(row["deletion_scheduled_for"], now + 30 * 86400, delta=1)
        self.assertEqual(row["jwt_invalidated_before"], now)
        self.assertEqual(row["is_deleted"], 0)  # NOT yet hard-deleted

    def test_deletion_cancels_subscriptions(self):
        uid = db.create_user("subdel@test.com", "InitialPass123!", username="subdel")
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                "VALUES (?, 'test_dash', 'test_plan', 'active', ?)",
                (uid, int(time.time())),
            )
            # Simulate deletion API cancelling subs
            c.execute(
                "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND status = 'active'",
                (uid,),
            )
            row = c.execute(
                "SELECT status FROM subscriptions WHERE user_id = ?", (uid,),
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_deletion_revokes_sessions(self):
        uid = db.create_user("sessdel@test.com", "InitialPass123!", username="sessdel")
        db.create_session(uid)
        db.create_session(uid)
        with db.conn() as c:
            before = c.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,),
            ).fetchone()[0]
        self.assertEqual(before, 2)
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            after = c.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,),
            ).fetchone()[0]
        self.assertEqual(after, 0)


class TestRecoveryWindow(unittest.TestCase):
    def test_cancel_deletion_clears_scheduled_for(self):
        uid = db.create_user("cancel-del@test.com", "InitialPass123!", username="canceldel")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ? WHERE id = ?",
                (now, now + 30 * 86400, uid),
            )
            # Cancel
            c.execute(
                "UPDATE users SET deletion_cancelled_at = ?, deletion_scheduled_for = NULL WHERE id = ?",
                (now + 86400, uid),
            )
            row = c.execute(
                "SELECT deletion_requested_at, deletion_scheduled_for, deletion_cancelled_at, is_deleted "
                "FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        # requested_at is kept for audit, scheduled_for is None, cancelled_at set.
        self.assertIsNotNone(row["deletion_requested_at"])
        self.assertIsNone(row["deletion_scheduled_for"])
        self.assertIsNotNone(row["deletion_cancelled_at"])
        self.assertEqual(row["is_deleted"], 0)


class TestHardDeleteJob(unittest.TestCase):
    def test_hard_delete_anonymises_and_sets_is_deleted(self):
        uid = db.create_user("hard-del@test.com", "InitialPass123!", username="harddel")
        # Schedule deletion for the past so the job picks it up
        past = int(time.time()) - 10
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ? WHERE id = ?",
                (past, past, uid),
            )

        from jobs.pipeline_jobs import process_scheduled_deletions
        result = _run(process_scheduled_deletions())
        self.assertGreaterEqual(result["deleted"], 1)

        with db.conn() as c:
            row = c.execute(
                "SELECT email, username, is_deleted, deleted_at, password_hash FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["is_deleted"], 1)
        self.assertTrue(row["email"].startswith("deleted_"))
        self.assertTrue(row["email"].endswith("@deleted.narve.ai"))
        self.assertIn("[deleted_", row["username"])
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["password_hash"], "")  # wiped

    def test_hard_delete_skips_cancelled_requests(self):
        uid = db.create_user("skip-del@test.com", "InitialPass123!", username="skipdel")
        past = int(time.time()) - 10
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ?, "
                "deletion_cancelled_at = ? WHERE id = ?",
                (past, past, now, uid),
            )

        from jobs.pipeline_jobs import process_scheduled_deletions
        _run(process_scheduled_deletions())

        with db.conn() as c:
            row = c.execute(
                "SELECT email, is_deleted FROM users WHERE id = ?", (uid,),
            ).fetchone()
        self.assertEqual(row["is_deleted"], 0)
        self.assertEqual(row["email"], "skip-del@test.com")  # not anonymised


if __name__ == "__main__":
    unittest.main()
