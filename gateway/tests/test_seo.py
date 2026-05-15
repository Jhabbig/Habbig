"""Tests for SEO head builder, sitemap, robots.txt, and OG cards."""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seo import (
    SEO,
    ROBOTS_TXT,
    build_seo_head,
    build_sitemap_xml,
    organization_schema,
    person_schema,
    website_schema,
)


class TestSEOHeadBuilder(unittest.TestCase):
    def test_default_contains_title_and_description(self):
        head = build_seo_head(SEO(
            title="Hello", description="World", canonical_path="/foo",
        ))
        self.assertIn("<title>Hello · narve.ai</title>", head)
        self.assertIn('name="description"', head)
        self.assertIn("World", head)

    def test_title_suffix_not_doubled(self):
        head = build_seo_head(SEO(title="Already narve.ai"))
        self.assertIn("<title>Already narve.ai</title>", head)

    def test_canonical_is_absolute(self):
        head = build_seo_head(SEO(canonical_path="/pricing"))
        self.assertIn('href="https://narve.ai/pricing"', head)

    def test_og_and_twitter_tags_present(self):
        head = build_seo_head(SEO())
        for prop in ("og:title", "og:description", "og:type", "og:url", "og:image",
                     "og:image:width", "og:image:height"):
            self.assertIn(f'property="{prop}"', head, prop)
        for name in ("twitter:card", "twitter:title", "twitter:description", "twitter:image"):
            self.assertIn(f'name="{name}"', head, name)

    def test_robots_default_is_index_follow(self):
        head = build_seo_head(SEO())
        self.assertIn('content="index, follow"', head)

    def test_robots_noindex_passes_through(self):
        head = build_seo_head(SEO(robots="noindex"))
        self.assertIn('content="noindex"', head)

    def test_html_escapes_description(self):
        head = build_seo_head(SEO(description='Has "quotes" and <tags>'))
        self.assertNotIn("<tags>", head)
        self.assertIn("&lt;tags&gt;", head)
        self.assertIn("&quot;quotes&quot;", head)

    def test_jsonld_is_embedded(self):
        head = build_seo_head(SEO(jsonld=[organization_schema()]))
        self.assertIn('type="application/ld+json"', head)
        self.assertIn('"@type":"Organization"', head)

    def test_jsonld_escapes_close_script(self):
        payload = {"@context": "x", "@type": "Thing", "name": "</script>evil"}
        head = build_seo_head(SEO(jsonld=[payload]))
        self.assertNotIn("</script>evil", head)
        self.assertIn("<\\/script>", head)

    def test_sentinel_marker_present(self):
        self.assertIn("narve-seo-head", build_seo_head(SEO()))


class TestSchemaBuilders(unittest.TestCase):
    def test_organization_shape(self):
        org = organization_schema()
        self.assertEqual(org["@type"], "Organization")
        self.assertEqual(org["url"], "https://narve.ai")

    def test_person_schema_shape(self):
        p = person_schema("fedwatcher", ["politics"])
        self.assertEqual(p["@type"], "Person")
        self.assertEqual(p["name"], "@fedwatcher")
        self.assertEqual(p["knowsAbout"], ["politics"])

    def test_website_schema_shape(self):
        self.assertEqual(website_schema()["@type"], "WebSite")


class TestSitemap(unittest.TestCase):
    def test_is_valid_xml_header(self):
        xml = build_sitemap_xml()
        self.assertTrue(xml.startswith('<?xml version="1.0"'))

    def test_static_entries_present(self):
        xml = build_sitemap_xml()
        for path in ("/", "/pricing", "/calendar", "/terms", "/privacy", "/dpa"):
            self.assertIn(f"<loc>https://narve.ai{path}</loc>", xml)

    def test_source_handles_rendered(self):
        xml = build_sitemap_xml(source_handles=["fedwatcher", "cryptokid"])
        self.assertIn("/sources/fedwatcher", xml)
        self.assertIn("/sources/cryptokid", xml)

    def test_priority_and_changefreq_emitted(self):
        xml = build_sitemap_xml()
        self.assertIn("<priority>1.0</priority>", xml)
        self.assertIn("<changefreq>daily</changefreq>", xml)


class TestRobots(unittest.TestCase):
    def test_allows_public_paths(self):
        for path in ("/pricing", "/sources/", "/calendar", "/terms"):
            self.assertIn(f"Allow: {path}", ROBOTS_TXT)

    def test_disallows_private_paths(self):
        # /token is no longer listed here — the invite-token gate was
        # removed in the 2026-05-15 refactor. /login is the direct
        # entry point and stays disallowed.
        for path in ("/dashboard/", "/admin/", "/api/", "/login"):
            self.assertIn(f"Disallow: {path}", ROBOTS_TXT)

    def test_sitemap_reference(self):
        self.assertIn("Sitemap: https://narve.ai/sitemap.xml", ROBOTS_TXT)


class TestOGCards(unittest.TestCase):
    def setUp(self):
        import og_cards
        og_cards.clear_cache()
        self.og_cards = og_cards

    def _assert_png_1200x630(self, data: bytes) -> None:
        from PIL import Image
        import io
        self.assertTrue(data.startswith(b"\x89PNG"))
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.size, (1200, 630))

    def test_default_card(self):
        self._assert_png_1200x630(self.og_cards.default_card())

    def test_pricing_card(self):
        self._assert_png_1200x630(self.og_cards.pricing_card())

    def test_calendar_card(self):
        self._assert_png_1200x630(self.og_cards.calendar_card())

    def test_source_card_with_full_data(self):
        self._assert_png_1200x630(self.og_cards.source_card("fedwatcher", 0.81, 0.72, 145))

    def test_source_card_with_missing_fields(self):
        self._assert_png_1200x630(self.og_cards.source_card("new_source", None, None, 0))

    def test_market_card_with_edge(self):
        self._assert_png_1200x630(self.og_cards.market_card(
            "Will the Fed hold rates at March meeting?",
            market_price=0.61, narve_price=0.74, platform="Polymarket",
        ))

    def test_market_card_without_narve_signal(self):
        self._assert_png_1200x630(self.og_cards.market_card(
            "Some future market?",
            market_price=0.5, narve_price=None, platform="Kalshi",
        ))

    def test_cache_returns_same_object(self):
        calls = {"n": 0}

        def make() -> bytes:
            calls["n"] += 1
            return self.og_cards.default_card()

        first = self.og_cards.cached("test-key", 60, make)
        second = self.og_cards.cached("test-key", 60, make)
        self.assertIs(first, second)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
