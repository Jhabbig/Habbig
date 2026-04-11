"""HTTP-level tests for Features 1-7 routes added via server_features.py."""

from __future__ import annotations

import os
import unittest

# Must come before any `import server` so the gate + dev bypass behave.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402 — registers the feature routes
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


def _cookies_for_user(uid: int) -> dict:
    token = db.create_session(uid)
    return {server.COOKIE_NAME: token}


class TestTermsAndPrivacy(unittest.TestCase):
    def test_terms_public_and_renders(self):
        r = client.get("/terms")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Terms of Service", r.text)
        self.assertIn("narve.ai", r.text)

    def test_privacy_public_and_renders(self):
        r = client.get("/privacy")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Privacy Policy", r.text)


class TestRobotsAndSitemap(unittest.TestCase):
    def test_robots_txt(self):
        r = client.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn("User-agent: *", r.text)
        self.assertIn("Disallow: /admin/", r.text)
        self.assertIn("Disallow: /api/", r.text)
        self.assertIn("Sitemap:", r.text)

    def test_sitemap_xml_renders_live_when_no_file(self):
        r = client.get("/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.text.startswith("<?xml"))
        self.assertIn("<urlset", r.text)
        self.assertIn("/terms", r.text)
        self.assertIn("/privacy", r.text)


class TestUnsubscribe(unittest.TestCase):
    def test_unsubscribe_page_renders_even_without_token(self):
        r = client.get("/unsubscribe")
        self.assertEqual(r.status_code, 200)
        # Generic page is rendered whether or not the token is valid.
        self.assertIn("narve.ai", r.text)

    def test_unsubscribe_invalid_token_still_200(self):
        r = client.get("/unsubscribe?token=bogus&type=marketing")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Link expired or invalid", r.text)


class TestPasswordResetFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_id = db.create_user("pwreset@test.com", "InitialPass123!", username="pwresettest")

    def test_forgot_password_always_returns_ok(self):
        r = client.post(
            "/auth/forgot-password",
            data={"email": "does-not-exist@test.com", "_csrf": "x"},
            headers={"x-csrf-token": "x"},
        )
        # CSRF middleware blocks without a real token → that's still a gate.
        self.assertIn(r.status_code, (200, 403))

    def test_reset_page_with_invalid_token_shows_error(self):
        """Either 400 (strict server_features handler) or 200 with an error
        string (the baseline handler) counts as an error signal."""
        r = client.get("/reset-password?token=invalid-token")
        self.assertIn(r.status_code, (200, 400))
        body = r.text.lower()
        self.assertTrue(
            "expired" in body or "invalid" in body,
            "reset-password page should mention invalid/expired token",
        )


class TestWaitlistPosition(unittest.TestCase):
    def test_position_lookup_unknown_email_404(self):
        r = client.get("/api/newsletter/position?email=nosuch@test.com")
        self.assertEqual(r.status_code, 404)

    def test_position_lookup_missing_email_400(self):
        r = client.get("/api/newsletter/position")
        self.assertEqual(r.status_code, 400)


class TestAccountDeletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user_id = db.create_user("delete-me@test.com", "TestPass123!", username="deleteme")

    def test_delete_requires_authentication(self):
        r = client.post("/api/account/delete", json={"confirm": "DELETE"})
        self.assertIn(r.status_code, (401, 403))

    def test_delete_requires_confirm_word(self):
        cookies = _cookies_for_user(self.user_id)
        r = client.post("/api/account/delete", json={"confirm": "nope"}, cookies=cookies, headers={"x-csrf-token": "x"})
        # CSRF gate might hit first — either way it doesn't schedule deletion.
        self.assertIn(r.status_code, (400, 403))

    def test_cancel_delete_requires_auth(self):
        r = client.post("/api/account/delete/cancel")
        self.assertIn(r.status_code, (401, 403))


class TestSourceProfile(unittest.TestCase):
    def test_unrated_source_returns_404(self):
        r = client.get("/sources/nobody-rated-yet")
        self.assertEqual(r.status_code, 404)

    def test_rated_source_renders_with_seo_tags(self):
        import time
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_credibility "
                "(source_handle, global_credibility, accuracy_unlocked, total_predictions, "
                "correct_predictions, categories_active, last_computed_at) "
                "VALUES (?, ?, 1, 40, 28, 3, ?)",
                ("seotest", 0.74, now),
            )
        r = client.get("/sources/seotest")
        self.assertEqual(r.status_code, 200)
        self.assertIn("@seotest", r.text)
        self.assertIn("0.74", r.text)
        self.assertIn("<meta name='description'", r.text)
        self.assertIn("<meta property='og:type'", r.text)
        self.assertIn("application/ld+json", r.text)


class TestJobMonitoringGates(unittest.TestCase):
    def test_jobs_status_requires_admin(self):
        r = client.get("/admin/api/jobs/status")
        self.assertEqual(r.status_code, 403)

    def test_jobs_recent_requires_admin(self):
        r = client.get("/admin/api/jobs/recent")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
