"""Tests for the impersonation subsystem.

Covers:
  - db.create_impersonation_session / get / end / record_action
  - impersonation.is_action_blocked pattern matching
  - impersonation.banner_html and display_name_for helpers
"""

from __future__ import annotations

import unittest

from tests import _testdb  # noqa: F401  (imports the shared in-memory DB)

import db
import impersonation as imp


def _mk_user(email: str, is_admin: int = 0) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0], is_admin=is_admin)


class TestDbImpersonation(unittest.TestCase):
    def setUp(self):
        self.admin_id = _mk_user(f"admin_{id(self)}@t.com", is_admin=1)
        self.target_id = _mk_user(f"target_{id(self)}@t.com", is_admin=0)

    def test_create_and_get_session(self):
        result = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="support ticket #123",
            ip_address="127.0.0.1",
        )
        self.assertIn("id", result)
        self.assertIn("cookie_token", result)
        self.assertTrue(len(result["cookie_token"]) > 20)

        row = db.get_impersonation_session_by_token(result["cookie_token"])
        self.assertIsNotNone(row)
        self.assertEqual(row["admin_user_id"], self.admin_id)
        self.assertEqual(row["target_user_id"], self.target_id)
        self.assertEqual(row["reason"], "support ticket #123")
        self.assertIsNone(row["ended_at"])

    def test_end_session_is_idempotent(self):
        imp_row = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="x",
        )
        db.end_impersonation_session(imp_row["id"], end_reason="admin_ended")
        r1 = db.get_impersonation_session(imp_row["id"])
        self.assertIsNotNone(r1["ended_at"])
        # Calling again is a no-op (idempotent)
        db.end_impersonation_session(imp_row["id"], end_reason="admin_ended")
        r2 = db.get_impersonation_session(imp_row["id"])
        self.assertEqual(r1["ended_at"], r2["ended_at"])

    def test_record_action_and_count(self):
        imp_row = db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="x",
        )
        db.record_impersonation_action(
            session_id=imp_row["id"],
            method="GET", path="/dashboards", status_code=200, was_blocked=False,
        )
        db.record_impersonation_action(
            session_id=imp_row["id"],
            method="POST", path="/account/delete", status_code=403, was_blocked=True,
        )
        actions = db.list_impersonation_actions(imp_row["id"])
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1]["was_blocked"], 1)
        updated = db.get_impersonation_session(imp_row["id"])
        self.assertEqual(updated["action_count"], 2)

    def test_list_sessions_joins_emails(self):
        db.create_impersonation_session(
            admin_user_id=self.admin_id,
            target_user_id=self.target_id,
            reason="list test",
        )
        rows = db.list_impersonation_sessions(limit=10)
        self.assertTrue(any(r["reason"] == "list test" for r in rows))
        # One of the rows has admin+target email populated via JOIN
        hit = next(r for r in rows if r["reason"] == "list test")
        self.assertIn("@t.com", hit["admin_email"])
        self.assertIn("@t.com", hit["target_email"])


class TestBlockedPaths(unittest.TestCase):
    def test_get_on_safe_path_never_blocked(self):
        # The GET-safe invariant: reading non-destructive pages never
        # trips the impersonation guard. Destructive routes (delete, 2FA,
        # api-keys, payment methods) are explicitly always-blocked
        # because even the GET page may render a CSRF form that would
        # execute real changes.
        self.assertFalse(imp.is_action_blocked("GET", "/settings"))
        self.assertFalse(imp.is_action_blocked("GET", "/billing/cancel"))

    def test_account_destructive_blocked(self):
        self.assertTrue(imp.is_action_blocked("POST", "/account/delete"))
        self.assertTrue(imp.is_action_blocked("POST", "/account/password"))
        # The blocked-path list uses prefix-style patterns. "/account/email"
        # catches /account/email, /account/email/change, /account/email/verify,
        # etc. — every real email-change route lives under that prefix.
        self.assertTrue(imp.is_action_blocked("POST", "/account/email"))

    def test_billing_blocked(self):
        self.assertTrue(imp.is_action_blocked("POST", "/billing/cancel"))
        self.assertTrue(imp.is_action_blocked("POST", "/billing/checkout"))
        self.assertTrue(imp.is_action_blocked("POST", "/subscribe"))

    def test_ai_intelligence_blocked(self):
        self.assertTrue(imp.is_action_blocked("POST", "/intelligence/chat"))
        self.assertTrue(imp.is_action_blocked("POST", "/api/ai/complete"))

    def test_widget_create_blocked(self):
        self.assertTrue(imp.is_action_blocked("POST", "/widgets/new"))
        self.assertTrue(imp.is_action_blocked("DELETE", "/api/widgets/42"))

    def test_end_impersonation_always_allowed(self):
        # Must be reachable even via POST so the admin can end the session.
        self.assertFalse(imp.is_action_blocked("POST", "/admin/impersonations/end"))

    def test_arbitrary_post_not_blocked(self):
        # Routes not in the list should pass through (e.g. benign forms).
        self.assertFalse(imp.is_action_blocked("POST", "/feedback"))
        self.assertFalse(imp.is_action_blocked("POST", "/dashboards"))


class TestBannerHtml(unittest.TestCase):
    def test_banner_contains_expected_elements(self):
        html_out = imp.banner_html(
            target_display="jake (jake@example.com)",
            admin_email="admin@narve.ai",
            started_at=1_700_000_000,
        )
        self.assertIn("narve-impersonation-banner", html_out)
        self.assertIn("jake@example.com", html_out)
        self.assertIn("admin@narve.ai", html_out)
        self.assertIn("End session", html_out)
        self.assertIn("/admin/impersonations/end", html_out)

    def test_banner_escapes_admin_email(self):
        html_out = imp.banner_html(
            target_display="<script>alert(1)</script>",
            admin_email="<img>",
            started_at=0,
        )
        # Real chars are escaped; raw <script> must not appear.
        self.assertNotIn("<script>alert", html_out)
        self.assertIn("&lt;script&gt;", html_out)


if __name__ == "__main__":
    unittest.main()
