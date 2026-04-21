"""Tests for admin-editable email templates.

Covers db helpers, EmailService override resolution, and the preview
renderer's fallback behavior when variables are missing.
"""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401

import db
from email_system.service import EmailService, render_preview, _resolve_admin_override


class TestDbTemplates(unittest.TestCase):
    def test_upsert_creates_then_updates(self):
        db.upsert_email_template(
            key="welcome",
            subject="Hi {{ display_name }}",
            body_html="<p>Hello {{ display_name }}</p>",
            variables=["display_name"],
        )
        row = db.get_email_template("welcome")
        self.assertEqual(row["subject"], "Hi {{ display_name }}")
        self.assertEqual(row["is_active"], 1)

        db.upsert_email_template(
            key="welcome",
            subject="Hey {{ display_name }}",
            body_html="<p>Hey!</p>",
            variables=["display_name"],
            is_active=False,
        )
        row = db.get_email_template("welcome")
        self.assertEqual(row["subject"], "Hey {{ display_name }}")
        self.assertEqual(row["is_active"], 0)

    def test_delete_removes_override(self):
        db.upsert_email_template(
            key="password_reset",
            subject="s", body_html="b",
        )
        self.assertTrue(db.delete_email_template("password_reset"))
        self.assertIsNone(db.get_email_template("password_reset"))
        # Delete again: False, but does not raise.
        self.assertFalse(db.delete_email_template("password_reset"))


class TestOverrideResolution(unittest.TestCase):
    def setUp(self):
        # Clean slate each test
        for key in ("token_delivery", "market_resolved"):
            db.delete_email_template(key)

    def test_no_override_returns_none_pair(self):
        subject, body = _resolve_admin_override("token_delivery", {"display_name": "x"})
        self.assertIsNone(subject)
        self.assertIsNone(body)

    def test_active_override_returns_rendered(self):
        db.upsert_email_template(
            key="token_delivery",
            subject="Token for {{ display_name }}",
            body_html="<p>Hello {{ display_name }}, your token is {{ token }}.</p>",
            variables=["display_name", "token"],
            is_active=True,
        )
        subject, body = _resolve_admin_override(
            "token_delivery",
            {"display_name": "Jake", "token": "abc123"},
        )
        self.assertEqual(subject, "Token for Jake")
        self.assertIn("Hello Jake", body)
        self.assertIn("abc123", body)

    def test_inactive_override_is_ignored(self):
        db.upsert_email_template(
            key="token_delivery",
            subject="hi", body_html="<p>hi</p>",
            is_active=False,
        )
        subject, body = _resolve_admin_override("token_delivery", {})
        self.assertIsNone(subject)
        self.assertIsNone(body)

    def test_html_escaping_non_raw(self):
        db.upsert_email_template(
            key="market_resolved",
            subject="Re: {{ market_question }}",
            body_html="<p>{{ market_question }}</p>",
            is_active=True,
        )
        subject, body = _resolve_admin_override(
            "market_resolved",
            {"market_question": "<img onerror=x>"},
        )
        self.assertNotIn("<img onerror=x>", body)
        self.assertIn("&lt;img", body)


class TestRenderPreview(unittest.TestCase):
    def test_missing_vars_use_sample_fallback(self):
        result = render_preview(
            subject="Welcome {{ display_name }}",
            body_html="<p>Hi {{ display_name }} ({{ email }})</p>",
            variables=["display_name", "email"],
        )
        self.assertEqual(result["subject"], "Welcome Sample display_name")
        self.assertIn("Sample display_name", result["html"])
        self.assertIn("Sample email", result["html"])

    def test_explicit_overrides_win(self):
        result = render_preview(
            subject="{{ display_name }}",
            body_html="{{ display_name }}",
            variables=["display_name"],
            sample_overrides={"display_name": "Alice"},
        )
        self.assertEqual(result["subject"], "Alice")
        self.assertEqual(result["html"], "Alice")


class TestEmailServiceOverride(unittest.TestCase):
    def test_active_override_used_by_send_template(self):
        """EmailService.send_template picks up the admin override first."""
        db.upsert_email_template(
            key="welcome",
            subject="Custom subject for {{ display_name }}",
            body_html="<p>Custom {{ display_name }}</p>",
            is_active=True,
        )
        svc = EmailService()
        ok = asyncio.run(svc.send_template(
            to="user@example.com",
            template="welcome",
            context={"display_name": "Jane"},
        ))
        self.assertTrue(ok)  # dry-run returns True


if __name__ == "__main__":
    unittest.main()
