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


class TestSubproductFiltering(unittest.TestCase):
    """Each subscriber's digest is restricted to the dashboards they pay for.

    Ticketed to the email audit HIGH #5 — pre-fix, a single-subproduct
    subscriber received content from the other 11 subproducts.
    """

    def _make_user_with_sub(self, email: str, username: str, dashboard_key: str | None) -> int:
        import time as _t
        uid = db.create_user(email, "TestPass123!", username=username)
        if dashboard_key is not None:
            with db.conn() as c:
                c.execute(
                    "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (uid, dashboard_key, "monthly", int(_t.time())),
                )
        return uid

    def _seed_predictions_for_categories(self, *categories: str) -> None:
        # Drop a recent prediction in each requested category so the
        # digest builder has something to filter on.
        now = int(__import__("time").time())
        with db.conn() as c:
            for cat in categories:
                c.execute(
                    "INSERT INTO predictions (source_handle, market_id, category, direction, "
                    "content, source_url, extracted_at) VALUES (?, ?, ?, 'YES', ?, '', ?)",
                    (f"@src_{cat}", f"m-{cat}-{now}", cat, f"signal in category {cat}", now),
                )

    def test_helper_returns_active_dashboards(self):
        uid = self._make_user_with_sub("filter-helper@test.com", "filterhelper", "crypto")
        self.assertEqual(db.get_user_active_subproducts(uid), {"crypto"})

    def test_helper_skips_plan_synthetic_row(self):
        uid = self._make_user_with_sub("filter-plan@test.com", "filterplan", "__plan__")
        # __plan__ is the Pro/Trader plan marker, not a real subproduct.
        self.assertEqual(db.get_user_active_subproducts(uid), set())

    def test_crypto_only_subscriber_gets_only_crypto_predictions(self):
        from jobs.email_jobs import _resolve_subproduct_filter
        uid = self._make_user_with_sub("filter-crypto@test.com", "filtercrypto", "crypto")
        cats, labels, should_send = _resolve_subproduct_filter(uid)
        self.assertTrue(should_send)
        self.assertIsNotNone(cats)
        # Crypto whitelist includes the 'crypto' category.
        self.assertIn("crypto", cats)
        # Categories outside the crypto subproduct must NOT be in the filter.
        self.assertNotIn("nfl", cats)
        self.assertNotIn("weather", cats)
        # Header label is set so the email shows "Your digest for: …".
        self.assertEqual(len(labels), 1)

    def test_pro_tier_returns_no_filter(self):
        import time as _t
        uid = db.create_user("filter-pro@test.com", "TestPass123!", username="filterpro")
        with db.conn() as c:
            c.execute(
                "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                "VALUES (?, '__plan__', 'pro_monthly', 'active', ?)",
                (uid, int(_t.time())),
            )
        from jobs.email_jobs import _resolve_subproduct_filter
        cats, labels, should_send = _resolve_subproduct_filter(uid)
        self.assertTrue(should_send)
        self.assertIsNone(cats)  # None = show everything
        self.assertEqual(labels, [])

    def test_no_active_subscription_skips_send(self):
        from jobs.email_jobs import _resolve_subproduct_filter
        uid = db.create_user("filter-none@test.com", "TestPass123!", username="filternone")
        cats, labels, should_send = _resolve_subproduct_filter(uid)
        self.assertFalse(should_send)
        # And the digest batch must NOT enqueue an email job for this user.
        from jobs.email_jobs import send_weekly_digest_batch
        _run(send_weekly_digest_batch())
        with db.conn() as c:
            rows = c.execute(
                "SELECT 1 FROM background_jobs WHERE name = 'send_email' AND payload LIKE ?",
                ("%filter-none@test.com%",),
            ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_digest_template_renders_subproduct_header(self):
        from email_system.renderer import render
        html = render("weekly_digest", {
            "display_name": "Test",
            "week_start": "Apr 1", "week_end": "Apr 7, 2026",
            "top_predictions": [], "top_sources": [],
            "subproduct_labels": ["Crypto Edge", "Sharpe Sports"],
            "subproduct_labels_str": "Crypto Edge, Sharpe Sports",
            "app_url": "https://narve.ai",
            "unsubscribe_url": "https://narve.ai/unsubscribe?token=x",
        })
        self.assertIn("Your digest for:", html)
        self.assertIn("Crypto Edge", html)
        self.assertIn("Sharpe Sports", html)

    def test_single_subproduct_send_filters_predictions(self):
        # A crypto-only subscriber should see only crypto predictions in
        # the rendered context — not the weather / NFL signals.
        self._seed_predictions_for_categories("crypto", "weather", "nfl", "weather")
        self._make_user_with_sub(
            "filter-flow@test.com", "filterflow", "crypto",
        )
        from jobs.email_jobs import send_weekly_digest_batch
        result = _run(send_weekly_digest_batch())
        self.assertGreaterEqual(result["enqueued"], 1)
        with db.conn() as c:
            row = c.execute(
                "SELECT payload FROM background_jobs WHERE name = 'send_email' AND payload LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                ("%filter-flow@test.com%",),
            ).fetchone()
        self.assertIsNotNone(row)
        import json as _json
        payload = _json.loads(row["payload"])
        ctx = payload.get("kwargs", payload).get("context", {})
        cats_seen = {p.get("category") for p in ctx.get("top_predictions", [])}
        # Only crypto-category predictions should have made it through.
        self.assertTrue(cats_seen.issubset({"crypto"}) or not cats_seen,
                        f"unexpected categories in digest: {cats_seen}")
        self.assertIn("Crypto Edge", ctx.get("subproduct_labels", []))


if __name__ == "__main__":
    unittest.main()
