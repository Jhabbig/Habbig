"""Tests for the SQLite subscriber DAO + signed unsubscribe tokens."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SubscribersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["VOTER_PULSE_DB"] = str(Path(self.tmp.name) / "test.db")
        os.environ["ALERT_SIGNING_SECRET"] = "test-secret"
        # Reimport so the module picks up the env vars
        import importlib
        from ingestion import subscribers as subs_mod
        importlib.reload(subs_mod)
        self.subs = subs_mod

    def tearDown(self):
        self.tmp.cleanup()

    def test_email_validation(self):
        self.assertTrue(self.subs.is_valid_email("a@b.com"))
        self.assertTrue(self.subs.is_valid_email("first.last+tag@example.co.uk"))
        self.assertFalse(self.subs.is_valid_email(""))
        self.assertFalse(self.subs.is_valid_email("not-an-email"))
        self.assertFalse(self.subs.is_valid_email("a@b"))
        self.assertFalse(self.subs.is_valid_email("a b@c.com"))

    def test_subscribe_normalises_case(self):
        a = self.subs.subscribe("Alice@Example.COM")
        self.assertEqual(a["email"], "alice@example.com")
        self.assertEqual(self.subs.list_active(), ["alice@example.com"])

    def test_resubscribe_after_unsubscribe(self):
        self.subs.subscribe("u@v.com")
        self.subs.unsubscribe("u@v.com")
        self.assertEqual(self.subs.count_active(), 0)
        self.subs.subscribe("u@v.com")
        self.assertEqual(self.subs.count_active(), 1)

    def test_token_roundtrip(self):
        token = self.subs.token_for("a@b.com")
        self.assertEqual(len(token), 16)
        self.assertTrue(self.subs.verify_token("a@b.com", token))
        self.assertFalse(self.subs.verify_token("a@b.com", "wrong"))
        self.assertFalse(self.subs.verify_token("other@b.com", token))

    def test_token_case_insensitive_email(self):
        # Token derivation lower-cases the email so the URL works regardless
        # of how the user typed their address.
        self.assertEqual(
            self.subs.token_for("a@b.com"),
            self.subs.token_for("A@B.COM"),
        )

    def test_alert_state_roundtrip(self):
        self.assertIsNone(self.subs.get_alert_state("last_sent_mood"))
        self.subs.set_alert_state("last_sent_mood", "47.5")
        self.assertEqual(self.subs.get_alert_state("last_sent_mood"), "47.5")
        # Overwrite
        self.subs.set_alert_state("last_sent_mood", "41.0")
        self.assertEqual(self.subs.get_alert_state("last_sent_mood"), "41.0")


if __name__ == "__main__":
    unittest.main()
