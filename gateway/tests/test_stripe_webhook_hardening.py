"""Stripe webhook hardening tests (idempotency + mode check).

The helpers don't hit real Stripe — they only need a DB + event dicts.
We ingest two events with the same id and verify the second one short-
circuits as ``already_processed``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        # Every module that does a top-level ``import db`` caches the
        # *old* DB path via closure. Reload them so their module-level
        # ``import db`` picks up the new GATEWAY_DB_PATH.
        for mod in ("db", "migrations", "stripe_webhook_hardening"):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        os.environ.pop("PRODUCTION", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _event(self, **over):
        base = {
            "id": "evt_test_1",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": {"metadata": {}}},
        }
        base.update(over)
        return base

    def test_first_event_not_already_processed(self):
        from stripe_webhook_hardening import mark_received
        resp = mark_received(self._event())
        self.assertIsNone(resp, "first ingest must not short-circuit")

    def test_second_event_already_processed(self):
        from stripe_webhook_hardening import mark_received
        evt = self._event()
        self.assertIsNone(mark_received(evt))
        resp = mark_received(evt)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 200)
        # Body is JSON {"status": "already_processed"}
        import json
        body = json.loads(bytes(resp.body))
        self.assertEqual(body.get("status"), "already_processed")

    def test_mode_mismatch_in_production(self):
        os.environ["PRODUCTION"] = "1"
        from stripe_webhook_hardening import reject_mode_mismatch
        # Production but event.livemode=False → reject.
        resp = reject_mode_mismatch(self._event(livemode=False))
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 400)

    def test_mode_match_in_production(self):
        os.environ["PRODUCTION"] = "1"
        from stripe_webhook_hardening import reject_mode_mismatch
        resp = reject_mode_mismatch(self._event(livemode=True))
        self.assertIsNone(resp)

    def test_mark_processed_stamps_row(self):
        from stripe_webhook_hardening import mark_processed, mark_received
        evt = self._event()
        mark_received(evt)
        mark_processed(evt)
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT processed_at, error FROM processed_stripe_events "
                "WHERE event_id = ?",
                (evt["id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["processed_at"])
        self.assertIsNone(row["error"])

    def test_mark_processed_records_error(self):
        from stripe_webhook_hardening import mark_processed, mark_received
        evt = self._event(id="evt_test_error")
        mark_received(evt)
        mark_processed(evt, error="boom")
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT error FROM processed_stripe_events WHERE event_id = ?",
                (evt["id"],),
            ).fetchone()
        self.assertEqual(row["error"], "boom")


if __name__ == "__main__":
    unittest.main()
