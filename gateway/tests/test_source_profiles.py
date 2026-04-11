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
        self.assertIn("<meta name='description'", r.text)
        self.assertIn("0.74", r.text)
        # 28/40 = 70% accuracy
        self.assertIn("70%", r.text)

    def test_schema_org_person_json_ld(self):
        r = client.get("/sources/seo_user")
        self.assertIn("application/ld+json", r.text)
        self.assertIn('"@type": "Person"', r.text)

    def test_robots_index_follow(self):
        r = client.get("/sources/seo_user")
        self.assertIn("index, follow", r.text)


class TestSitemap(unittest.TestCase):
    def test_sitemap_xml_headers(self):
        r = client.get("/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/xml", r.headers["content-type"])
        self.assertTrue(r.text.startswith("<?xml"))
        self.assertIn("<urlset", r.text)

    def test_sitemap_includes_static_pages(self):
        r = client.get("/sitemap.xml")
        self.assertIn("/terms", r.text)
        self.assertIn("/privacy", r.text)

    def test_sitemap_includes_rated_sources(self):
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('sitemap_user', 0.7, 1, 30, 21, 3, ?)",
                (int(time.time()),),
            )
        r = client.get("/sitemap.xml")
        self.assertIn("/sources/sitemap_user", r.text)

    def test_sitemap_excludes_unrated_sources(self):
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES ('hidden_user', 0.5, 0, 3, 2, 1, ?)",
                (int(time.time()),),
            )
        r = client.get("/sitemap.xml")
        self.assertNotIn("/sources/hidden_user", r.text)


class TestRobotsTxt(unittest.TestCase):
    def test_robots_disallows_admin_and_api(self):
        r = client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("User-agent: *", r.text)
        self.assertIn("Disallow: /admin/", r.text)
        self.assertIn("Disallow: /api/", r.text)
        self.assertIn("Disallow: /gate", r.text)
        self.assertIn("Sitemap:", r.text)

    def test_robots_allows_sources(self):
        r = client.get("/robots.txt")
        self.assertIn("Allow: /sources/", r.text)


if __name__ == "__main__":
    unittest.main()
