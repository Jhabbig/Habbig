"""Tests for the subproduct-aware welcome email.

Three branches matter:

  1. No active subscription          → generic variant.
  2. One subproduct subscription     → subproduct variant, scoped to the
                                       flagship slug (highest price wins).
  3. Pro plan (all dashboards)       → "all 12 dashboards" variant.

Also covers the ``get_user_primary_subscription`` DB helper directly so the
flagship-picking logic is locked in even if the template copy churns later.
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401 — sets up in-memory DB

import db  # noqa: E402
from email_system.renderer import render  # noqa: E402
from email_system.welcome import build_welcome_context  # noqa: E402


def _make_user(email: str) -> int:
    return db.create_user(email=email, password="TestPass123!", username=email.split("@")[0])


class TestPrimarySubscriptionPicker(unittest.TestCase):
    def test_no_subscription_returns_none(self):
        uid = _make_user("nosub@narve.test")
        self.assertIsNone(db.get_user_primary_subscription(uid))

    def test_single_subproduct_returns_that_subproduct(self):
        uid = _make_user("crypto@narve.test")
        db.upsert_subscription(
            user_id=uid, dashboard_key="crypto",
            plan="crypto_monthly", duration_days=30, source="test",
        )
        primary = db.get_user_primary_subscription(uid)
        self.assertIsNotNone(primary)
        self.assertEqual(primary["slug"], "crypto")
        self.assertEqual(primary["display_name"], "Crypto Edge")
        self.assertEqual(primary["subdomain"], "crypto")

    def test_multiple_subproducts_returns_highest_priced(self):
        # Sports (19.99) should beat Voters (5.99) on price.
        uid = _make_user("multi@narve.test")
        db.upsert_subscription(
            user_id=uid, dashboard_key="voters",
            plan="voters_monthly", duration_days=30, source="test",
        )
        db.upsert_subscription(
            user_id=uid, dashboard_key="sports",
            plan="sports_monthly", duration_days=30, source="test",
        )
        primary = db.get_user_primary_subscription(uid)
        self.assertIsNotNone(primary)
        self.assertEqual(primary["slug"], "sports")

    def test_skips_internal_plan_row(self):
        # Trader bundle uses dashboard_key="__plan__" with no real subproduct.
        uid = _make_user("trader-only@narve.test")
        db.upsert_subscription(
            user_id=uid, dashboard_key="__plan__",
            plan="trader_monthly", duration_days=30, source="test",
        )
        self.assertIsNone(db.get_user_primary_subscription(uid))

    def test_traders_subdomain_maps_to_top_traders_key(self):
        # subdomain "traders" → dashboard_key "top_traders" (only special-case).
        uid = _make_user("traders@narve.test")
        db.upsert_subscription(
            user_id=uid, dashboard_key="top_traders",
            plan="traders_monthly", duration_days=30, source="test",
        )
        primary = db.get_user_primary_subscription(uid)
        self.assertIsNotNone(primary)
        self.assertEqual(primary["slug"], "traders")
        self.assertEqual(primary["dashboard_key"], "top_traders")


class TestWelcomeContextBuilder(unittest.TestCase):
    def test_no_subscription_falls_to_generic(self):
        uid = _make_user("ctx-nosub@narve.test")
        ctx = build_welcome_context(uid, display_name="Sam")
        self.assertTrue(ctx.get("is_generic_welcome"))
        self.assertFalse(ctx.get("is_pro_welcome"))
        self.assertNotIn("subproduct_name", ctx)
        self.assertEqual(ctx["display_name"], "Sam")

    def test_single_subproduct_sets_subproduct_branch(self):
        uid = _make_user("ctx-crypto@narve.test")
        db.upsert_subscription(
            user_id=uid, dashboard_key="crypto",
            plan="crypto_monthly", duration_days=30, source="test",
        )
        ctx = build_welcome_context(uid, display_name="Sam")
        self.assertEqual(ctx["subproduct_name"], "Crypto Edge")
        self.assertIn("crypto.narve.ai", ctx["subproduct_url"])
        self.assertTrue(ctx["subproduct_tagline"])
        self.assertFalse(ctx.get("is_pro_welcome"))
        self.assertFalse(ctx.get("is_generic_welcome"))

    def test_pro_plan_sets_pro_branch(self):
        uid = _make_user("ctx-pro@narve.test")
        # Pro stamps every dashboard_key — replicate that here.
        from server import DASHBOARDS as _DASHBOARDS
        for key in _DASHBOARDS:
            db.upsert_subscription(
                user_id=uid, dashboard_key=key,
                plan="pro_monthly", duration_days=30, source="test",
            )
        ctx = build_welcome_context(uid, display_name="Sam")
        self.assertTrue(ctx.get("is_pro_welcome"))
        self.assertEqual(ctx["tier"], "Pro")
        self.assertFalse(ctx.get("subproduct_name"))
        self.assertFalse(ctx.get("is_generic_welcome"))


class TestWelcomeRender(unittest.TestCase):
    """Render the actual template and verify the right copy reaches HTML."""

    def test_generic_render_keeps_legacy_copy(self):
        html = render("welcome", {
            "display_name": "Alice",
            "tier": "Free",
            "app_url": "https://narve.ai",
            "is_generic_welcome": True,
        })
        self.assertIn("Welcome, Alice.", html)
        self.assertIn("Go to your feed", html)
        self.assertNotIn("12 dashboards", html)
        self.assertNotIn("subscription is active", html)

    def test_subproduct_render_shows_brand_name_and_link(self):
        html = render("welcome", {
            "display_name": "Bob",
            "tier": "Crypto Edge",
            "app_url": "https://narve.ai",
            "subproduct_name": "Crypto Edge",
            "subproduct_url": "https://crypto.narve.ai/",
            "subproduct_tagline": "BTC and crypto ensemble signals",
        })
        self.assertIn("Crypto Edge", html)
        self.assertIn("https://crypto.narve.ai/", html)
        self.assertIn("BTC and crypto ensemble signals", html)
        self.assertIn("subscription is active", html)
        # Generic block must not also render.
        self.assertNotIn("Go to your feed", html)
        self.assertNotIn("12 dashboards", html)

    def test_pro_render_mentions_all_dashboards(self):
        html = render("welcome", {
            "display_name": "Carol",
            "tier": "Pro",
            "app_url": "https://narve.ai",
            "is_pro_welcome": True,
        })
        self.assertIn("all 12 dashboards", html)
        self.assertIn("Pro", html)
        # No subproduct or generic copy leaks through.
        self.assertNotIn("subscription is active", html)
        self.assertNotIn("Go to your feed", html)

    def test_subproduct_url_is_html_escaped_when_dangerous(self):
        # The renderer auto-escapes non-raw_ vars; a hostile slug shouldn't
        # break out of the href. (Defence-in-depth — the SUBPRODUCTS catalog
        # is static, but the welcome enqueue site could in future read
        # user-supplied data.)
        html = render("welcome", {
            "display_name": "Eve",
            "tier": "x",
            "app_url": "https://narve.ai",
            "subproduct_name": "<img onerror=x>",
            "subproduct_url": "javascript:alert(1)",
            "subproduct_tagline": "ok",
        })
        self.assertNotIn("<img onerror=x>", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
