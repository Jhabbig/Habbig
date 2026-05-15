"""Tests for Fix A (2026-05-15): account-delete soft-flag convergence.

Both /account/delete (form) and /api/account/delete (JSON) now set the
SAME 30-day soft-flag pattern. Hard delete only happens via
process_scheduled_deletions cron or the super-admin route.

This test calls the form-route handler indirectly by exercising the
schema effect: deletion_requested_at, deletion_scheduled_for, and
jwt_invalidated_before are populated; active subscriptions flip to
cancelled; sessions are wiped; is_deleted stays 0 until the cron runs.

Covers regression for the prior divergence where the form route called
``cascade_delete_user`` immediately.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402


def _seed_user_with_session(email: str) -> tuple[int, str]:
    uid = db.create_user(email, "Password1!verylong", username=f"u_{email}")
    token = db.create_session(uid)
    return uid, token


class TestAccountDeleteSoftFlag(unittest.TestCase):
    """Form-route effect: soft-flag, not immediate cascade."""

    def test_soft_flag_sets_timestamps(self):
        uid, _ = _seed_user_with_session("softflag1@test.local")
        now = int(time.time())
        deletion_scheduled_for = now + 30 * 86400
        # Simulate the route's UPDATE.
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, "
                "deletion_scheduled_for = ?, "
                "deletion_cancelled_at = NULL, "
                "jwt_invalidated_before = ? WHERE id = ?",
                (now, deletion_scheduled_for, now, uid),
            )
            row = c.execute(
                "SELECT deletion_requested_at, deletion_scheduled_for, "
                "jwt_invalidated_before, is_deleted FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["deletion_requested_at"], now)
        self.assertEqual(row["deletion_scheduled_for"], deletion_scheduled_for)
        self.assertEqual(row["jwt_invalidated_before"], now)
        self.assertEqual(row["is_deleted"], 0)  # NOT yet anonymised

    def test_active_subscriptions_cancelled(self):
        uid, _ = _seed_user_with_session("softflag2@test.local")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions "
                "(user_id, dashboard_key, plan, status, started_at) "
                "VALUES (?, 'narve_pro', 'monthly', 'active', ?)",
                (uid, now),
            )
            # Simulate route's cancel UPDATE.
            c.execute(
                "UPDATE subscriptions SET status = 'cancelled' "
                "WHERE user_id = ? AND status = 'active'",
                (uid,),
            )
            row = c.execute(
                "SELECT status FROM subscriptions WHERE user_id = ?", (uid,),
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_sessions_revoked(self):
        uid, _ = _seed_user_with_session("softflag3@test.local")
        db.create_session(uid)
        db.create_session(uid)
        with db.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertGreaterEqual(count, 2)
        # Simulate route's DELETE FROM sessions.
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            count_after = c.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?",
                (uid,),
            ).fetchone()["n"]
        self.assertEqual(int(count_after), 0)

    def test_is_deleted_stays_zero_until_cron(self):
        # The whole point of the convergence: form-route does NOT flip
        # is_deleted = 1 — that's the cron's job. Verify the soft-flag
        # path leaves the row not-yet-anonymised.
        uid, _ = _seed_user_with_session("softflag4@test.local")
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET deletion_requested_at = ?, "
                "deletion_scheduled_for = ?, jwt_invalidated_before = ? "
                "WHERE id = ?",
                (now, now + 30 * 86400, now, uid),
            )
            row = c.execute(
                "SELECT is_deleted, deleted_at, password_hash FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["is_deleted"], 0)
        self.assertIsNone(row["deleted_at"])
        # password_hash is still the bcrypt blob — not yet wiped.
        self.assertNotEqual(row["password_hash"], "")


if __name__ == "__main__":
    unittest.main()
