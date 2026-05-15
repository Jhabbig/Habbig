"""Tests for the notification quiet-hours gate (audit HIGHx4).

Four hourly user-impacting jobs were firing push/email at 03:00 UK time:

  - poll_market_resolutions          (resolution_jobs.py)
  - check_market_movers              (notification_jobs.py)
  - send_saved_prediction_resolution_notifications (notification_jobs.py)
  - detect_market_movements          (movement_jobs.py)

This file verifies:

1. ``_within_quiet_hours`` returns True for UTC hours in ``[22, 6)`` and
   False otherwise.
2. At 03:00 UTC, the notification-emit step of each affected job
   short-circuits — no push, no email, no in-app.
3. At 10:00 UTC, normal fan-out runs.
4. ``poll_market_resolutions`` still resolves predictions in the DB
   during the quiet window (data pass survives) but skips notification
   fan-out.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
class TestWithinQuietHours(unittest.TestCase):
    def test_quiet_at_22(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 22, 0, 0)
        self.assertTrue(_within_quiet_hours(now=now))

    def test_quiet_at_03(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 3, 0, 0)
        self.assertTrue(_within_quiet_hours(now=now))

    def test_quiet_at_0559(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 5, 59, 59)
        self.assertTrue(_within_quiet_hours(now=now))

    def test_not_quiet_at_06(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 6, 0, 0)
        self.assertFalse(_within_quiet_hours(now=now))

    def test_not_quiet_at_10(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 10, 0, 0)
        self.assertFalse(_within_quiet_hours(now=now))

    def test_not_quiet_at_2159(self):
        from jobs.quiet_hours import _within_quiet_hours
        now = _dt.datetime(2026, 5, 15, 21, 59, 59)
        self.assertFalse(_within_quiet_hours(now=now))

    def test_default_now_calls_utcnow(self):
        """When ``now`` is omitted the helper must use UTC, not local time."""
        from jobs import quiet_hours as qh
        fixed_quiet = _dt.datetime(2026, 5, 15, 3, 0, 0)

        class _FakeDT:
            @staticmethod
            def utcnow():
                return fixed_quiet

        with mock.patch.object(qh._dt, "datetime", _FakeDT):
            self.assertTrue(qh._within_quiet_hours())


# ---------------------------------------------------------------------------
# gate behaviour at 03:00 UTC and 10:00 UTC for each affected job
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) is False else asyncio.new_event_loop().run_until_complete(coro)


def _await(coro):
    """Drive a coroutine to completion in a fresh loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestDetectMarketMovementsGate(unittest.TestCase):
    """``detect_market_movements`` runs detection always but skips delivery
    inside the quiet window."""

    def test_skips_delivery_at_03_utc(self):
        from jobs import movement_jobs

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=True), \
             mock.patch("backend.markets.movement_detector.run_detection_once",
                        return_value={"events_written": 0}) as mock_detect, \
             mock.patch.object(movement_jobs, "_deliver_pending_events",
                               new=mock.AsyncMock(return_value={"unreachable": True})) as mock_deliver:
            result = _await(movement_jobs.detect_market_movements())

        # Detection still ran
        mock_detect.assert_called_once()
        # Delivery did NOT run
        mock_deliver.assert_not_called()
        self.assertEqual(result["delivery"], {"skipped": "quiet_hours"})

    def test_runs_delivery_at_10_utc(self):
        from jobs import movement_jobs

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=False), \
             mock.patch("backend.markets.movement_detector.run_detection_once",
                        return_value={"events_written": 0}) as mock_detect, \
             mock.patch.object(movement_jobs, "_deliver_pending_events",
                               new=mock.AsyncMock(return_value={"pending": 0, "delivered": 0})) as mock_deliver:
            result = _await(movement_jobs.detect_market_movements())

        mock_detect.assert_called_once()
        mock_deliver.assert_called_once()
        self.assertEqual(result["delivery"], {"pending": 0, "delivered": 0})


class TestSavedPredictionResolutionGate(unittest.TestCase):
    """``send_saved_prediction_resolution_notifications`` is pure-emit: it
    early-exits before touching the DB or enqueueing email/push."""

    def test_skips_at_03_utc(self):
        from jobs import notification_jobs

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=True), \
             mock.patch("db.conn") as mock_conn:
            result = _await(notification_jobs.send_saved_prediction_resolution_notifications())

        # DB never opened — gate fires before any query
        mock_conn.assert_not_called()
        self.assertEqual(result, {"notified": 0, "more": False, "skipped": "quiet_hours"})

    def test_runs_at_10_utc(self):
        from jobs import notification_jobs

        # At 10:00 the function must reach the SQL query. Stub the DB so
        # we don't need real saved_predictions rows — we only assert that
        # the gate did NOT short-circuit.
        fake_cursor = mock.MagicMock()
        fake_cursor.fetchall.return_value = []
        fake_conn_obj = mock.MagicMock()
        fake_conn_obj.execute.return_value = fake_cursor
        fake_conn_cm = mock.MagicMock()
        fake_conn_cm.__enter__.return_value = fake_conn_obj
        fake_conn_cm.__exit__.return_value = False

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=False), \
             mock.patch("db.conn", return_value=fake_conn_cm) as mock_conn:
            result = _await(notification_jobs.send_saved_prediction_resolution_notifications())

        mock_conn.assert_called()  # passed the gate
        self.assertEqual(result, {"notified": 0, "more": False})


class TestCheckMarketMoversGate(unittest.TestCase):
    """``check_market_movers`` is pure-emit: early-exit before fetching
    upstream APIs or scanning users."""

    def test_skips_at_03_utc(self):
        from jobs import notification_jobs

        # If the gate works, neither unified_markets nor db.conn is hit.
        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=True), \
             mock.patch("backend.markets.unified_markets.fetch_unified_markets",
                        new=mock.AsyncMock()) as mock_fetch, \
             mock.patch("db.conn") as mock_conn:
            result = _await(notification_jobs.check_market_movers())

        mock_fetch.assert_not_called()
        mock_conn.assert_not_called()
        self.assertEqual(result, {"alerts_sent": 0, "movers_found": 0, "skipped": "quiet_hours"})


class TestPollMarketResolutionsGate(unittest.TestCase):
    """``poll_market_resolutions`` is special: the data pass (resolve
    predictions in the DB, fire credibility recompute) ALWAYS runs.
    Only the user-facing send_market_resolution_notifications enqueue
    is gated.
    """

    def test_data_pass_runs_at_03_utc_but_notifications_skipped(self):
        from jobs import resolution_jobs

        # Seed one unresolved poly market with a YES outcome upstream.
        market_id = "poly:quiet-hours-test"
        db.create_prediction("src_qh", "qh pred", market_id=market_id, direction="YES")

        fake_poly_client = mock.MagicMock()
        fake_poly_client.get_market = mock.AsyncMock(return_value={
            "resolved": True,
            "outcome": "Yes",
            "question": "QH test market",
        })
        fake_poly_client.close = mock.AsyncMock()
        fake_kalshi_client = mock.MagicMock()
        fake_kalshi_client.get_market = mock.AsyncMock(return_value=None)
        fake_kalshi_client.close = mock.AsyncMock()

        enqueued: list[str] = []

        async def _capture_enqueue(name, **kwargs):
            enqueued.append(name)

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=True), \
             mock.patch("backend.markets.polymarket_client.PolymarketClient",
                        return_value=fake_poly_client), \
             mock.patch("backend.markets.kalshi_client.KalshiClient",
                        return_value=fake_kalshi_client), \
             mock.patch("jobs.enqueue_job", new=_capture_enqueue):
            result = _await(resolution_jobs.poll_market_resolutions())

        # Data pass DID run — prediction is now resolved in the DB.
        with db.conn() as c:
            row = c.execute(
                "SELECT resolved, resolved_correct FROM predictions WHERE market_id = ?",
                (market_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["resolved"], 1)
        self.assertEqual(row["resolved_correct"], 1)  # YES direction × YES outcome
        self.assertGreaterEqual(result["resolved_predictions"], 1)

        # Notification enqueue was SKIPPED during quiet hours.
        self.assertNotIn("send_market_resolution_notifications", enqueued)
        # Credibility recompute still fires — it's data infrastructure, not user-facing.
        self.assertIn("recompute_credibilities", enqueued)

    def test_data_pass_and_notifications_at_10_utc(self):
        from jobs import resolution_jobs

        # Different market id so the previous test's row doesn't already-resolve us.
        market_id = "poly:non-quiet-test"
        db.create_prediction("src_nq", "nq pred", market_id=market_id, direction="YES")

        fake_poly_client = mock.MagicMock()
        fake_poly_client.get_market = mock.AsyncMock(return_value={
            "resolved": True,
            "outcome": "Yes",
            "question": "NQ test market",
        })
        fake_poly_client.close = mock.AsyncMock()
        fake_kalshi_client = mock.MagicMock()
        fake_kalshi_client.get_market = mock.AsyncMock(return_value=None)
        fake_kalshi_client.close = mock.AsyncMock()

        enqueued: list[str] = []

        async def _capture_enqueue(name, **kwargs):
            enqueued.append(name)

        with mock.patch("jobs.quiet_hours._within_quiet_hours", return_value=False), \
             mock.patch("backend.markets.polymarket_client.PolymarketClient",
                        return_value=fake_poly_client), \
             mock.patch("backend.markets.kalshi_client.KalshiClient",
                        return_value=fake_kalshi_client), \
             mock.patch("jobs.enqueue_job", new=_capture_enqueue):
            result = _await(resolution_jobs.poll_market_resolutions())

        self.assertGreaterEqual(result["resolved_predictions"], 1)
        self.assertIn("send_market_resolution_notifications", enqueued)
        self.assertIn("recompute_credibilities", enqueued)


if __name__ == "__main__":
    unittest.main()
