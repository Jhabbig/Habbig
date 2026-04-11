"""Tests for Feature 9: weekly digest email."""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402


def _run(coro):
    """Run a coroutine on a fresh event loop.

    Using `asyncio.get_event_loop()` leaks closed loops when pytest runs this
    file alongside pytest-asyncio test files — grabbing a new one every call
    sidesteps that entirely.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestWeeklyDigestBatch(unittest.TestCase):
    def test_job_registered_and_cron_scheduled(self):
        from jobs.registry import job_registry, cron_jobs
        self.assertIn("send_weekly_digest_batch", job_registry)
        # Monday 08:00 UTC
        crons = [c for c in cron_jobs if c["name"] == "send_weekly_digest_batch"]
        self.assertEqual(len(crons), 1)
        self.assertEqual(crons[0]["weekday"], 0)
        self.assertEqual(crons[0]["hour"], 8)
        self.assertEqual(crons[0]["minute"], 0)

    def test_skips_users_without_active_subscription(self):
        # Create a user with no subscription. email_digest defaults True.
        uid = db.create_user("no-sub@test.com", "TestPass123!", username="nosub")
        self.assertEqual(db.get_user_subscription_tier(uid), "none")

        from jobs.email_jobs import send_weekly_digest_batch
        result = _run(send_weekly_digest_batch())
        # This user has tier='none' → should be in `skipped` not `enqueued`.
        self.assertGreaterEqual(result["skipped"], 1)

    def test_sends_to_user_with_active_pro(self):
        import time as _time
        uid = db.create_user("digest-pro@test.com", "TestPass123!", username="digestpro")
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                "VALUES (?, '__plan__', 'pro_monthly', 'active', ?)",
                (uid, int(_time.time())),
            )
        from jobs.email_jobs import send_weekly_digest_batch
        result = _run(send_weekly_digest_batch())
        self.assertGreaterEqual(result["enqueued"], 1)

    def test_skips_users_who_unsubscribed_from_digest(self):
        import time as _time
        uid = db.create_user("digest-off@test.com", "TestPass123!", username="digestoff")
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                "VALUES (?, '__plan__', 'pro_monthly', 'active', ?)",
                (uid, int(_time.time())),
            )
            c.execute("UPDATE users SET email_digest = 0 WHERE id = ?", (uid,))
        from jobs.email_jobs import send_weekly_digest_batch
        result = _run(send_weekly_digest_batch())
        # This specific user should NOT be in the enqueued count.
        # We can't assert exact counts because the fixture has other users
        # from earlier tests, but we can at least check our user is skipped
        # by checking the email isn't in the job queue audit.
        with db.conn() as c:
            # No job row targeting this email should exist.
            rows = c.execute(
                "SELECT payload FROM background_jobs WHERE name = 'send_email' AND payload LIKE ?",
                ("%digest-off@test.com%",),
            ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_digest_template_has_unsubscribe_link_context(self):
        # Verify the template context includes the unsubscribe_url key so
        # the footer {% if unsubscribe_url %} guard triggers.
        from email_system.renderer import render
        html = render("weekly_digest", {
            "display_name": "Test",
            "week_start": "Apr 1", "week_end": "Apr 7, 2026",
            "top_predictions": [], "top_sources": [],
            "app_url": "https://narve.ai",
            "unsubscribe_url": "https://narve.ai/unsubscribe?token=abc.def",
        })
        self.assertIn("Unsubscribe", html)
        self.assertIn("/unsubscribe?token=abc.def", html)


if __name__ == "__main__":
    unittest.main()
