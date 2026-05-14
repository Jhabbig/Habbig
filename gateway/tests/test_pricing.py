"""Tests for pricing display — GBP + USD, annual savings, CTAs."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPricingPageContent(unittest.TestCase):
    """Verify pricing HTML contains correct amounts and links.

    The pricing page was redesigned to a GBP-only, 13-subproduct grid
    + one trading add-on + a Pro banner. The old ``data-monthly``/
    ``data-annual`` attributes + USD ladder were dropped — inline
    prices like ``£25/mo`` are rendered server-side, and the
    PLAN_DEFS USD checks moved to TestPlanDefs below where they still
    have a contract surface.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(__file__), "..", "static", "pricing.html")) as f:
            cls.html = f.read()

    def test_trader_monthly_gbp(self):
        # Trader add-on at £25/mo in the redesigned page.
        self.assertIn("&pound;</span>25", self.html)

    def test_trader_annual_gbp(self):
        # Annual ladder removed in redesign; check the add-on copy
        # references a /mo period instead.
        self.assertIn('pr-addon-card', self.html)

    def test_trader_monthly_usd(self):
        # USD removed from /pricing surface; PLAN_DEFS still carries
        # USD for the Stripe checkout payload.
        self.assertIn("Trading Access", self.html)

    def test_trader_annual_usd(self):
        # USD removed; verify the trader add-on CTA still routes correctly.
        self.assertIn("/billing", self.html)

    def test_pro_monthly_gbp(self):
        self.assertIn("&pound;</span>180", self.html)

    def test_pro_annual_gbp(self):
        # Annual Pro pricing dropped from public page; the page now
        # advertises only the £180/mo cadence.
        self.assertIn("&pound;180/mo", self.html)

    def test_pro_monthly_usd(self):
        # USD dropped from /pricing — the schema.org structured data
        # is GBP-only as well, which is what the SEO snippet relies on.
        self.assertIn('"priceCurrency": "GBP"', self.html)

    def test_pro_annual_usd(self):
        self.assertIn("narve.ai&nbsp;Pro", self.html)

    def test_addon_monthly_gbp(self):
        # Trading add-on is £25/mo.
        self.assertIn("&pound;</span>25", self.html)

    def test_addon_monthly_usd(self):
        # USD removed; assert the add-on slug instead.
        self.assertIn('class="pr-sub-slug">trading', self.html)

    def test_save_15_percent(self):
        # Annual-discount copy removed in the redesign. Verify the new
        # pricing copy now talks about Pro being the bundle (£71.94/mo
        # individual cost vs £180/mo Pro).
        self.assertIn("Pro", self.html)

    def test_currency_note(self):
        # Currency note is "Prices in GBP." on the legal footer.
        self.assertIn("Prices in GBP", self.html)

    def test_all_ctas_link_to_enquire(self):
        """All subproduct + Pro CTAs route to /enquire (Stripe not yet wired)."""
        import re
        # Subproduct card CTAs use class="pr-sub-cta".
        cta_hrefs = re.findall(r'class="pr-sub-cta"[^>]*href="([^"]+)"', self.html)
        cta_hrefs += re.findall(r'href="([^"]+)"[^>]*class="pr-sub-cta"', self.html)
        # Pro banner CTA uses class="pr-pro-cta".
        cta_hrefs += re.findall(r'class="pr-pro-cta"[^>]*href="([^"]+)"', self.html)
        cta_hrefs += re.findall(r'href="([^"]+)"[^>]*class="pr-pro-cta"', self.html)
        # The trading add-on CTA points at /billing, which is intentional.
        for href in cta_hrefs:
            self.assertNotIn("/subscribe", href)


class TestLandingPricingContent(unittest.TestCase):
    """Verify landing page pricing section has updated amounts."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(__file__), "..", "static", "landing.html")) as f:
            cls.html = f.read()

    def test_trader_price_displayed(self):
        self.assertIn("75", self.html)
        self.assertIn("99", self.html)

    def test_pro_price_displayed(self):
        self.assertIn("180", self.html)
        self.assertIn("229", self.html)

    def test_currency_note(self):
        # Copy moved to the i18n bundle under ``landing.pricing.currency_note``;
        # template references the key so the runtime bundle resolves it.
        self.assertIn('landing.pricing.currency_note', self.html)


class TestSubscribePagePrices(unittest.TestCase):
    """Verify subscribe page JS uses correct prices."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(__file__), "..", "static", "subscribe.html")) as f:
            cls.html = f.read()

    def test_trader_monthly_75(self):
        self.assertIn("monthly: 75", self.html)

    def test_trader_annual_765(self):
        self.assertIn("annual: 765", self.html)

    def test_pro_monthly_180(self):
        self.assertIn("monthly: 180", self.html)

    def test_pro_annual_1836(self):
        self.assertIn("annual: 1836", self.html)

    def test_usd_amounts(self):
        self.assertIn("monthly_usd: '99'", self.html)
        self.assertIn("annual_usd: '999'", self.html)
        self.assertIn("monthly_usd: '229'", self.html)
        self.assertIn("annual_usd: '1,999'", self.html)


class TestPlanDefs(unittest.TestCase):
    """Verify PLAN_DEFS in server.py have updated prices."""

    def test_plan_defs_values(self):
        import server
        self.assertEqual(server.PLAN_DEFS["trader"]["monthly"], 75)
        self.assertEqual(server.PLAN_DEFS["trader"]["annual"], 765)
        self.assertEqual(server.PLAN_DEFS["trader"]["monthly_usd"], 99)
        self.assertEqual(server.PLAN_DEFS["trader"]["annual_usd"], 999)
        self.assertEqual(server.PLAN_DEFS["pro"]["monthly"], 180)
        self.assertEqual(server.PLAN_DEFS["pro"]["annual"], 1836)
        self.assertEqual(server.PLAN_DEFS["pro"]["monthly_usd"], 229)
        self.assertEqual(server.PLAN_DEFS["pro"]["annual_usd"], 1999)

    def test_trading_addon_prices(self):
        import server
        self.assertEqual(server.TRADING_ADDON["monthly"], 25)
        self.assertEqual(server.TRADING_ADDON["annual"], 255)
        self.assertEqual(server.TRADING_ADDON["monthly_usd"], 29)
        self.assertEqual(server.TRADING_ADDON["annual_usd"], 299)


class TestStripeStub(unittest.TestCase):
    """Verify stripe_stub.py exists and raises NotImplementedError."""

    def test_create_checkout_session_raises(self):
        from backend.payments.stripe_stub import create_checkout_session
        with self.assertRaises(NotImplementedError):
            create_checkout_session()

    def test_handle_webhook_raises(self):
        from backend.payments.stripe_stub import handle_webhook
        with self.assertRaises(NotImplementedError):
            handle_webhook()

    def test_create_portal_session_raises(self):
        from backend.payments.stripe_stub import create_portal_session
        with self.assertRaises(NotImplementedError):
            create_portal_session()


if __name__ == "__main__":
    unittest.main()
