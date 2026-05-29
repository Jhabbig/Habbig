"""Tests for Feature 7: public source profiles + sitemap + robots.txt."""

from __future__ import annotations

import os
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402 — registers the routes
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


class TestSourceProfileGating(unittest.TestCase):
    def test_unknown_source_returns_404(self):
        r = client.get("/sources/never-seen-handle")
        self.assertEqual(r.status_code, 404)

    def test_unrated_source_returns_404(self):
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('unrated_user', 0.5, 0, 2, 1, 1, ?)",
                (int(time.time()),),
            )
        r = client.get("/sources/unrated_user")
        self.assertEqual(r.status_code, 404)

    def test_rated_source_returns_200(self):
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('rated_user', 0.81, 1, 47, 34, 3, ?)",
                (int(time.time()),),
            )
        r = client.get("/sources/rated_user")
        self.assertEqual(r.status_code, 200)
        self.assertIn("@rated_user", r.text)
        self.assertIn("0.81", r.text)


class TestSEOTags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('seo_user', 0.74, 1, 40, 28, 3, ?)",
                (int(time.time()),),
            )

    def test_canonical_url_present(self):
        r = client.get("/sources/seo_user")
        self.assertIn("<link rel='canonical'", r.text)
        self.assertIn("/sources/seo_user", r.text)

    def test_opengraph_tags_present(self):
        r = client.get("/sources/seo_user")
        self.assertIn("og:type", r.text)
        self.assertIn("og:title", r.text)
        self.assertIn("og:url", r.text)

    def test_meta_description_includes_score_and_accuracy(self):
        r = client.get("/sources/seo_user")
        # Either quote style is fine — both render identically.
        self.assertTrue(
            "<meta name='description'" in r.text
            or '<meta name="description"' in r.text,
            "missing meta description tag",
        )
        self.assertIn("0.74", r.text)
        # 28/40 = 70% accuracy
        self.assertIn("70%", r.text)

    def test_schema_org_person_json_ld(self):
        r = client.get("/sources/seo_user")
        self.assertIn("application/ld+json", r.text)
        # Accept either spaced or compact JSON serialisation.
        self.assertTrue(
            '"@type": "Person"' in r.text or '"@type":"Person"' in r.text,
            "missing schema.org Person @type",
        )

    def test_robots_index_follow(self):
        r = client.get("/sources/seo_user")
        self.assertIn("index, follow", r.text)


# Sitemap is served at an obscure, non-guessable path (server._SITEMAP_PATH),
# submitted directly to Search Console and never advertised at /sitemap.xml or
# in robots.txt. Its contents are restricted to fixed public pages — dynamic
# /sources/<handle> profiles are intentionally excluded.
_SITEMAP_PATH = "/497951413996680578.xml"


class TestSitemap(unittest.TestCase):
    def test_guessable_sitemap_path_404s(self):
        r = client.get("/sitemap.xml")
        self.assertEqual(r.status_code, 404)

    def test_sitemap_xml_headers(self):
        r = client.get(_SITEMAP_PATH)
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/xml", r.headers["content-type"])
        self.assertTrue(r.text.startswith("<?xml"))
        self.assertIn("<urlset", r.text)

    def test_sitemap_includes_static_pages(self):
        r = client.get(_SITEMAP_PATH)
        self.assertIn("/terms", r.text)
        self.assertIn("/privacy", r.text)

    def test_sitemap_excludes_source_profiles(self):
        # Rated sources used to be enumerated in the sitemap; the public-only
        # policy now keeps the dynamic /sources/ graph out entirely.
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('sitemap_user', 0.7, 1, 30, 21, 3, ?)",
                (int(time.time()),),
            )
        r = client.get(_SITEMAP_PATH)
        self.assertNotIn("/sources/", r.text)


class TestRobotsTxt(unittest.TestCase):
    def test_robots_disallows_admin_and_api(self):
        r = client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("User-agent: *", r.text)
        self.assertIn("Disallow: /admin/", r.text)
        self.assertIn("Disallow: /api/", r.text)
        # /token was removed in the 2026-05-15 auth refactor; /login is
        # the direct entry point and stays disallowed for crawlers.
        self.assertIn("Disallow: /login", r.text)
        # No Sitemap: line — the obscure sitemap URL is not advertised.
        self.assertNotIn("Sitemap:", r.text)

    def test_robots_allows_public_pages(self):
        # robots.txt puts public pages under an unqualified "Allow: /" (plus
        # named whitelists for /pricing, /terms, /privacy, /dpa, etc.).
        r = client.get("/robots.txt")
        self.assertIn("Allow: /", r.text)
        self.assertNotIn("Disallow: /sources", r.text)


if __name__ == "__main__":
    unittest.main()
