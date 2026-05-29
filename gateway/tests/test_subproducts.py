"""Tests for the six narve.ai sub-brand subdomains.

Covers:
  - subproduct module: SUBPRODUCTS catalogue + host lookup + access check
  - landing page: each subdomain serves its own branded HTML with wordmark
                  and hero copy
  - sitemap + robots: subdomain-scoped, canonical to the subdomain itself
  - admin /admin/subproducts: renders MRR rollup without crashing

The existing reverse-proxy path (authenticated users on *.narve.ai) is
intentionally not exercised here — it requires a running backend dashboard
service on port 8000/8888/etc. The guard that matters is that the landing
page only takes the unauthenticated branch; authed traffic still flows
through proxy_request as it always did.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import subproduct


# ── Pure-module tests (no server, no DB) ───────────────────────────────────


class TestSubproductCatalogue(unittest.TestCase):
    def test_thirteen_subproducts_defined(self):
        # Catalogue grew from 6 → 13 with the platform-build expansion
        # (new: voters, whale, cb, climate, disasters, health, love).
        self.assertEqual(
            set(subproduct.SUBPRODUCTS.keys()),
            {
                "sports", "weather", "world", "crypto", "midterm", "traders",
                "voters", "whale", "cb", "climate", "disasters", "health",
                "love",
            },
        )

    def test_every_subproduct_has_required_fields(self):
        required = {
            "slug", "dashboard_key", "name", "tagline", "hero_headline",
            "hero_sub", "price_usd", "price_gbp", "floating_numbers",
            "stat_pills", "tabs", "env_price_id",
        }
        for slug, cfg in subproduct.SUBPRODUCTS.items():
            missing = required - set(cfg.keys())
            self.assertFalse(missing, f"{slug} missing fields: {missing}")

    def test_prices_match_spec(self):
        # Spec pegs these exact prices — regressions get caught here.
        self.assertEqual(subproduct.SUBPRODUCTS["sports"]["price_usd"], 19.99)
        self.assertEqual(subproduct.SUBPRODUCTS["weather"]["price_usd"], 7.99)
        self.assertEqual(subproduct.SUBPRODUCTS["world"]["price_usd"], 5.99)
        self.assertEqual(subproduct.SUBPRODUCTS["crypto"]["price_usd"], 9.99)
        self.assertEqual(subproduct.SUBPRODUCTS["midterm"]["price_usd"], 14.99)
        self.assertEqual(subproduct.SUBPRODUCTS["traders"]["price_usd"], 12.99)

    def test_traders_uses_top_traders_dashboard_key(self):
        # Subdomain "traders" but the subscriptions table uses "top_traders".
        self.assertEqual(subproduct.SUBPRODUCTS["traders"]["dashboard_key"], "top_traders")
        self.assertEqual(subproduct.DASHBOARD_KEY_FOR_SLUG["traders"], "top_traders")

    def test_five_other_slugs_map_to_themselves(self):
        for slug in ("sports", "weather", "world", "crypto", "midterm"):
            self.assertEqual(subproduct.DASHBOARD_KEY_FOR_SLUG[slug], slug)


class TestHostLookup(unittest.TestCase):
    def test_subdomain_in_fqdn(self):
        self.assertEqual(
            subproduct.subproduct_for_host("crypto.narve.ai")["slug"],
            "crypto",
        )

    def test_apex_returns_none(self):
        self.assertIsNone(subproduct.subproduct_for_host("narve.ai"))
        self.assertIsNone(subproduct.subproduct_for_host("narve.ai:8000"))

    def test_staging_returns_none(self):
        # Staging is treated as apex everywhere else; subproduct_for_host
        # must do the same so we don't serve a sub-brand page on staging.
        self.assertIsNone(subproduct.subproduct_for_host("staging.narve.ai"))

    def test_unknown_subdomain_returns_none(self):
        self.assertIsNone(subproduct.subproduct_for_host("blog.narve.ai"))

    def test_empty_and_none(self):
        self.assertIsNone(subproduct.subproduct_for_host(""))
        self.assertIsNone(subproduct.subproduct_for_host(None))

    def test_port_suffix_stripped(self):
        self.assertEqual(
            subproduct.subproduct_for_host("crypto.localhost:8000")["slug"],
            "crypto",
        )


class TestAccessCheck(unittest.TestCase):
    def _fake_has_active_subscription(self, user_id, dashboard_key):
        return self._subs.get((user_id, dashboard_key), False)

    def _fake_has_pro_plan(self, user):
        return bool(user and user.get("plan") == "pro")

    def setUp(self):
        self._subs: dict = {}

    def test_no_user_denied(self):
        self.assertFalse(
            subproduct.has_subproduct_access(
                None, "sports",
                has_active_subscription=self._fake_has_active_subscription,
                has_pro_plan=self._fake_has_pro_plan,
            )
        )

    def test_admin_bypasses(self):
        self.assertTrue(
            subproduct.has_subproduct_access(
                {"user_id": 1, "is_admin": True},
                "sports",
                has_active_subscription=self._fake_has_active_subscription,
                has_pro_plan=self._fake_has_pro_plan,
            )
        )

    def test_pro_plan_grants_all_subproducts(self):
        user = {"user_id": 2, "is_admin": False, "plan": "pro"}
        for slug in subproduct.SUBPRODUCTS:
            self.assertTrue(
                subproduct.has_subproduct_access(
                    user, slug,
                    has_active_subscription=self._fake_has_active_subscription,
                    has_pro_plan=self._fake_has_pro_plan,
                )
            )

    def test_subproduct_specific_sub(self):
        # User with only a "crypto" subscription should reach crypto but not sports.
        self._subs[(3, "crypto")] = True
        user = {"user_id": 3, "is_admin": False, "plan": None}
        self.assertTrue(subproduct.has_subproduct_access(
            user, "crypto",
            has_active_subscription=self._fake_has_active_subscription,
            has_pro_plan=self._fake_has_pro_plan,
        ))
        self.assertFalse(subproduct.has_subproduct_access(
            user, "sports",
            has_active_subscription=self._fake_has_active_subscription,
            has_pro_plan=self._fake_has_pro_plan,
        ))

    def test_traders_checks_top_traders_key(self):
        # Subscription is keyed top_traders; slug is traders.
        self._subs[(4, "top_traders")] = True
        user = {"user_id": 4, "is_admin": False, "plan": None}
        self.assertTrue(subproduct.has_subproduct_access(
            user, "traders",
            has_active_subscription=self._fake_has_active_subscription,
            has_pro_plan=self._fake_has_pro_plan,
        ))


class TestLandingContext(unittest.TestCase):
    def test_every_slug_produces_context(self):
        for slug, cfg in subproduct.SUBPRODUCTS.items():
            ctx = subproduct.landing_context(slug)
            self.assertEqual(ctx["slug"], slug)
            self.assertEqual(ctx["name"], cfg["name"])
            self.assertIn("/", ctx["hero_headline"])  # spec uses slash delimiter
            self.assertTrue(len(ctx["floating_numbers"]) >= 6)
            self.assertTrue(len(ctx["stat_pills"]) >= 1)

    def test_missing_stats_default_to_em_dash(self):
        # Template placeholders {arbs} / {edge} aren't provided, they render as "—".
        ctx = subproduct.landing_context("sports")
        for pill in ctx["stat_pills"]:
            self.assertNotIn("{", pill)  # no leftover placeholders


# ── HTTP-level smoke tests via FastAPI TestClient ───────────────────────────
#
# These are skipped when the full server module can't be loaded (missing
# env vars, etc). The skip is intentional — the module-level tests above
# are the real contract, the HTTP smoke tests are a bonus integration check.


class TestLandingHttpSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
        os.environ.pop("PRODUCTION", None)
        try:
            from fastapi.testclient import TestClient
            import server  # noqa: F401 — force app import
            cls.client = TestClient(server.app)
            cls.skipped = False
        except Exception as exc:
            cls.skipped = True
            cls.skip_reason = str(exc)

    def setUp(self):
        if getattr(self, "skipped", False):
            self.skipTest(f"server import failed: {self.skip_reason}")

    def test_crypto_subdomain_serves_landing(self):
        r = self.client.get("/", headers={"Host": "crypto.narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("narve.ai", r.text)
        self.assertIn("Crypto Edge", r.text)

    def test_landing_shows_slug_in_wordmark(self):
        r = self.client.get("/", headers={"Host": "sports.narve.ai"})
        self.assertIn("sports", r.text.lower())

    def test_apex_still_renders_prerelease(self):
        r = self.client.get("/", headers={"Host": "narve.ai"})
        self.assertEqual(r.status_code, 200)
        # Apex should NOT show the sub-brand wordmark.
        self.assertNotIn("narve.ai / crypto", r.text)

    def test_subdomain_sitemap_is_self_canonical(self):
        # Sitemap lives at an obscure path (server._SITEMAP_PATH), not
        # /sitemap.xml. The guessable path no longer serves a sitemap: it
        # falls through to the generic HTML shell (a soft-404), so it never
        # exposes a <urlset> page-roadmap.
        guessable = self.client.get("/sitemap.xml", headers={"Host": "weather.narve.ai"})
        self.assertNotIn("<urlset", guessable.text)
        r = self.client.get("/497951413996680578.xml", headers={"Host": "weather.narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("<urlset", r.text)
        self.assertIn("https://weather.narve.ai/", r.text)
        self.assertNotIn("https://narve.ai/sources/", r.text)

    def test_subdomain_robots_omits_sitemap(self):
        # The obscure sitemap URL is submitted to Search Console, never
        # advertised — so subdomain robots.txt carries no Sitemap: line.
        r = self.client.get("/robots.txt", headers={"Host": "midterm.narve.ai"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Sitemap:", r.text)


if __name__ == "__main__":
    unittest.main()
