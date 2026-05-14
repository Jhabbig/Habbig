"""Tests for morning_briefing subproduct filtering (email audit HIGH #5).

The morning briefing job pulls signals from a global pool of markets +
predictions; pre-fix, a single-product subscriber received every market
in the system regardless of which dashboard they actually paid for.
These tests pin the filter logic so we can't regress.
"""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMorningBriefingSubproductFilter(unittest.TestCase):
    def _make_user_with_sub(self, email: str, username: str, dashboard_key: str | None) -> int:
        import time as _t
        uid = db.create_user(email, "TestPass123!", username=username)
        with db.conn() as c:
            c.execute("UPDATE users SET morning_briefing_enabled = 1 WHERE id = ?", (uid,))
            if dashboard_key is not None:
                c.execute(
                    "INSERT INTO subscriptions (user_id, dashboard_key, plan, status, started_at) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (uid, dashboard_key, "monthly", int(_t.time())),
                )
        return uid

    def test_template_renders_subproduct_header(self):
        from email_system.renderer import render
        html = render("morning_briefing", {
            "app_url": "https://narve.ai",
            "date": "May 14, 2026",
            "display_name": "Test",
            "top_edge_markets": [],
            "new_signals": [],
            "approaching_resolutions": [],
            "subproduct_labels": ["Crypto Edge"],
            "unsubscribe_url": "https://narve.ai/unsubscribe?type=digest",
        })
        self.assertIn("Your briefing for:", html)
        self.assertIn("Crypto Edge", html)

    def test_template_no_header_when_pro(self):
        from email_system.renderer import render
        html = render("morning_briefing", {
            "app_url": "https://narve.ai",
            "date": "May 14, 2026",
            "display_name": "Test",
            "top_edge_markets": [],
            "new_signals": [],
            "approaching_resolutions": [],
            "subproduct_labels": [],
            "unsubscribe_url": "https://narve.ai/unsubscribe?type=digest",
        })
        self.assertNotIn("Your briefing for:", html)

    def test_no_active_subscription_skips_send(self):
        """User opted in to morning briefings but their sub lapsed → no email."""
        uid = self._make_user_with_sub(
            "morning-none@test.com", "morningnone", dashboard_key=None,
        )
        self.assertEqual(db.get_user_active_subproducts(uid), set())
        self.assertEqual(db.get_user_subscription_tier(uid), "none")

    def test_helper_returns_user_dashboards(self):
        uid = self._make_user_with_sub(
            "morning-crypto@test.com", "morningcrypto", "crypto",
        )
        self.assertEqual(db.get_user_active_subproducts(uid), {"crypto"})


if __name__ == "__main__":
    unittest.main()
