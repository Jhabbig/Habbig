"""Tests for Feature 1: unified email system.

Covers:
  - EmailService dry-run logs but does not send
  - Template renderer resolves {{ vars }}, {% for %}, {% if %}
  - base.html extension pipeline (child block inserted into base)
  - Unsubscribe tokens are signed and single-use-reusable
  - Transactional templates render without an unsubscribe_url
  - Marketing / digest templates render WITH an unsubscribe_url
"""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401 — sets up in-memory DB + migrations
import db  # noqa: E402

from email_system.service import EmailService  # noqa: E402
from email_system.renderer import render, render_text_fallback  # noqa: E402
from email_system.unsubscribe import UnsubscribeManager  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestRenderer(unittest.TestCase):
    def test_base_extension(self):
        html = render("welcome", {
            "display_name": "Alice",
            "tier": "Pro",
            "app_url": "https://narve.ai",
            # Welcome template now has three mutually-exclusive variants;
            # the generic block matches the pre-subproduct copy.
            "is_generic_welcome": True,
        })
        self.assertIn("Welcome, Alice.", html)
        self.assertIn("Pro", html)
        self.assertIn("narve.ai · Prediction market intelligence", html)  # from base.html footer

    def test_for_loop(self):
        html = render("weekly_digest", {
            "display_name": "Bob",
            "week_start": "Apr 1", "week_end": "Apr 7, 2026",
            "top_predictions": [
                {"source": "@alice", "category": "politics", "content": "Foo", "credibility": 0.82},
                {"source": "@bob", "category": "crypto", "content": "Bar", "credibility": 0.71},
            ],
            "top_sources": [{"handle": "alice", "credibility": 0.82, "accuracy": "80%"}],
            "app_url": "https://narve.ai",
        })
        self.assertIn("@alice", html)
        self.assertIn("@bob", html)
        self.assertIn("Foo", html)

    def test_html_escape(self):
        html = render("welcome", {
            "display_name": "<script>",
            "tier": "Pro",
            "app_url": "https://narve.ai",
            "is_generic_welcome": True,
        })
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html[: html.find("narve.ai")])

    def test_text_fallback(self):
        plain = render_text_fallback("<p>Hello <strong>world</strong></p><br>line 2")
        self.assertIn("Hello world", plain)
        self.assertIn("line 2", plain)
        self.assertNotIn("<p>", plain)


class TestEmailServiceDryRun(unittest.TestCase):
    def test_dry_run_send_returns_true(self):
        service = EmailService()
        self.assertTrue(service.dry_run)
        ok = _run(service.send(
            to="alice@test.com",
            subject="Test",
            html="<p>Hi</p>",
            text="Hi",
        ))
        self.assertTrue(ok)

    def test_dry_run_template_send(self):
        service = EmailService()
        ok = _run(service.send_template(
            to="alice@test.com",
            template="welcome",
            context={"display_name": "Alice", "tier": "Pro"},
        ))
        self.assertTrue(ok)


class TestUnsubscribeTokens(unittest.TestCase):
    def test_token_is_signed(self):
        t1 = UnsubscribeManager.generate_token("alice@test.com", None, "marketing")
        self.assertIn(".", t1)  # raw.signature format
        # Second call returns the same token (not a new row).
        t2 = UnsubscribeManager.generate_token("alice@test.com", None, "marketing")
        self.assertEqual(t1, t2)

    def test_unsubscribe_applies_to_user(self):
        uid = db.create_user("unsub@test.com", "TestPass123!", username="unsubtest")
        # Both flags start True (default 1 from migration 002)
        with db.conn() as c:
            row = c.execute("SELECT email_digest, email_marketing FROM users WHERE id = ?", (uid,)).fetchone()
        self.assertEqual(row["email_digest"], 1)
        self.assertEqual(row["email_marketing"], 1)

        token = UnsubscribeManager.generate_token("unsub@test.com", uid, "marketing")
        result = UnsubscribeManager.unsubscribe(token)
        self.assertIsNotNone(result)

        with db.conn() as c:
            row = c.execute("SELECT email_digest, email_marketing FROM users WHERE id = ?", (uid,)).fetchone()
        self.assertEqual(row["email_digest"], 1)  # digest still on
        self.assertEqual(row["email_marketing"], 0)  # marketing flipped off

    def test_invalid_token_returns_none(self):
        result = UnsubscribeManager.unsubscribe("not-a-real-token")
        self.assertIsNone(result)

    def test_tampered_token_rejected(self):
        token = UnsubscribeManager.generate_token("tamper@test.com", None, "digest")
        raw, _ = token.rsplit(".", 1)
        bad_token = raw + ".0000000000000000000000000000000000000000000000000000000000000000"
        result = UnsubscribeManager.unsubscribe(bad_token)
        self.assertIsNone(result)


class TestTemplatesHaveUnsubscribeFooter(unittest.TestCase):
    def test_weekly_digest_has_unsubscribe(self):
        html = render("weekly_digest", {
            "display_name": "Test",
            "week_start": "Apr 1", "week_end": "Apr 7",
            "top_predictions": [], "top_sources": [],
            "app_url": "https://narve.ai",
            "unsubscribe_url": "https://narve.ai/unsubscribe?token=abc",
        })
        self.assertIn("Unsubscribe", html)
        self.assertIn("https://narve.ai/unsubscribe?token=abc", html)

    def test_token_delivery_has_no_unsubscribe_when_context_omitted(self):
        # Transactional emails don't pass unsubscribe_url. The base.html
        # footer's {% if %} guard hides the link.
        html = render("token_delivery", {
            "display_name": "Test",
            "raw_token": "TOKEN123",
            "app_url": "https://narve.ai",
        })
        self.assertNotIn("Unsubscribe", html)


if __name__ == "__main__":
    unittest.main()
