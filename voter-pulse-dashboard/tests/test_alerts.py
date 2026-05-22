"""Tests for the mood-move alert dispatch logic (threshold + rate-limit)."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["VOTER_PULSE_DB"] = str(Path(self.tmp.name) / "alerts.db")
        os.environ["ALERT_SIGNING_SECRET"] = "test-secret"
        os.environ.pop("SMTP_HOST", None)
        # Make rate-limit window tiny so we can test cleanly
        os.environ["ALERT_MIN_INTERVAL_SECONDS"] = "1"
        from ingestion import subscribers as subs_mod
        from analysis import alerts as alerts_mod
        importlib.reload(subs_mod)
        importlib.reload(alerts_mod)
        self.subs = subs_mod
        self.alerts = alerts_mod
        self.subs.subscribe("alice@example.com")
        self.subs.subscribe("bob@example.com")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_send_with_no_prior_fires(self):
        out = self.alerts.check_and_send(current_mood=48.0, narrative_text="hi")
        self.assertEqual(out["sent"], 2)
        self.assertEqual(out["prior"], None)
        self.assertEqual(out["current"], 48.0)

    def test_small_move_does_not_fire(self):
        # Seed the last-sent value
        self.subs.set_alert_state("last_sent_mood", "48.0")
        self.subs.set_alert_state("last_sent_at", "0")
        out = self.alerts.check_and_send(current_mood=50.0, narrative_text="hi")
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["reason"], "mood move below threshold")

    def test_rate_limited_after_recent_send(self):
        import time
        self.subs.set_alert_state("last_sent_mood", "48.0")
        self.subs.set_alert_state("last_sent_at", str(int(time.time())))
        # 10-point move would normally fire but we're inside the window
        out = self.alerts.check_and_send(current_mood=38.0, narrative_text="hi")
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["reason"], "rate-limited")

    def test_force_overrides_rate_limit(self):
        import time
        self.subs.set_alert_state("last_sent_mood", "48.0")
        self.subs.set_alert_state("last_sent_at", str(int(time.time())))
        out = self.alerts.check_and_send(current_mood=38.0, narrative_text="hi", force=True)
        self.assertEqual(out["sent"], 2)

    def test_no_subscribers(self):
        # Unsubscribe everyone
        self.subs.unsubscribe("alice@example.com")
        self.subs.unsubscribe("bob@example.com")
        out = self.alerts.check_and_send(current_mood=50.0, narrative_text="hi", force=True)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["reason"], "no active subscribers")

    def test_none_mood_skips(self):
        out = self.alerts.check_and_send(current_mood=None, narrative_text="hi")
        self.assertEqual(out["reason"], "no current mood")

    def test_subject_line_describes_direction(self):
        self.subs.set_alert_state("last_sent_mood", "48.0")
        self.subs.set_alert_state("last_sent_at", "0")
        out = self.alerts.check_and_send(current_mood=38.0, narrative_text="hi", force=True)
        self.assertIn("down", out["subject"])
        self.assertIn("48", out["subject"])
        self.assertIn("38", out["subject"])


if __name__ == "__main__":
    unittest.main()
