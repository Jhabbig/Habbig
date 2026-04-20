"""Tests for the market movement detection system.

Covers: MarketMovementDetector, db CRUD, alert rules, API routes.
"""

from __future__ import annotations

USES_TESTDB = True

import json
import time
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import patch

from tests import _testdb  # noqa: F401 — shared in-memory DB

import db


# ── Fake UnifiedMarket for detector tests ────────────────────────────────────


@dataclass
class FakeMarket:
    id: str = "poly:test-market"
    source: str = "polymarket"
    title: str = "Will it rain tomorrow?"
    category: str = "weather"
    yes_price: float = 0.60
    no_price: float = 0.40
    volume_usd: float = 50000.0
    liquidity_usd: float = 10000.0
    close_time: Optional[str] = None
    status: str = "active"
    outcome: Optional[str] = None
    url: str = "https://polymarket.com/test"
    betyc_ev_score: Optional[float] = None
    betyc_avg_credibility: Optional[float] = None
    betyc_prediction_count: int = 0
    betyc_consensus: Optional[str] = None
    false_consensus: bool = False
    false_consensus_direction: Optional[str] = None


# ── Detector tests ───────────────────────────────────────────────────────────


class TestMarketMovementDetector(unittest.TestCase):

    def setUp(self):
        self.now = int(time.time())
        # Clean up tables
        with db.conn() as c:
            c.execute("DELETE FROM market_movement_events")
            c.execute("DELETE FROM market_snapshots")

    def test_new_market_detected(self):
        """Market with no prior snapshot → new_market event."""
        from backend.markets.movement_detector import MarketMovementDetector

        m = FakeMarket(id="poly:brand-new-market", yes_price=0.55)
        detector = MarketMovementDetector()
        events = detector.detect([m], now=self.now)

        new_events = [e for e in events if e.event_type == "new_market"]
        self.assertEqual(len(new_events), 1)
        self.assertEqual(new_events[0].market_slug, "brand-new-market")
        self.assertEqual(new_events[0].severity, "low")

    def test_odds_movement_detected(self):
        """Price swing exceeding threshold → odds_movement event."""
        from backend.markets.movement_detector import MarketMovementDetector

        # Insert an old snapshot
        slug = "odds-test"
        old_ts = self.now - 7200  # 2 hours ago
        db.insert_market_snapshot(slug, 0.40, snapshotted_at=old_ts)

        m = FakeMarket(id=f"poly:{slug}", yes_price=0.65)  # +25pp
        detector = MarketMovementDetector(price_threshold=0.08)
        events = detector.detect([m], now=self.now)

        odds_events = [e for e in events if e.event_type == "odds_movement"]
        self.assertEqual(len(odds_events), 1)
        self.assertAlmostEqual(odds_events[0].price_change, 0.25, places=2)
        self.assertEqual(odds_events[0].severity, "high")

    def test_no_event_below_threshold(self):
        """Small price change below threshold → no event."""
        from backend.markets.movement_detector import MarketMovementDetector

        slug = "small-move"
        old_ts = self.now - 7200
        db.insert_market_snapshot(slug, 0.50, snapshotted_at=old_ts)

        m = FakeMarket(id=f"poly:{slug}", yes_price=0.53)  # +3pp
        detector = MarketMovementDetector(price_threshold=0.08)
        events = detector.detect([m], now=self.now)

        odds_events = [e for e in events if e.event_type == "odds_movement"]
        self.assertEqual(len(odds_events), 0)

    def test_volume_spike_detected(self):
        """Volume jump ≥ 3x → volume_spike event."""
        from backend.markets.movement_detector import MarketMovementDetector

        slug = "vol-spike"
        old_ts = self.now - 7200
        db.insert_market_snapshot(slug, 0.50, snapshotted_at=old_ts, volume=10000)

        m = FakeMarket(id=f"poly:{slug}", yes_price=0.51, volume_usd=40000)
        detector = MarketMovementDetector(volume_spike_mult=3.0)
        events = detector.detect([m], now=self.now)

        vol_events = [e for e in events if e.event_type == "volume_spike"]
        self.assertEqual(len(vol_events), 1)
        self.assertEqual(vol_events[0].severity, "medium")

    def test_approaching_resolution_detected(self):
        """Market closing within approaching_hours → event."""
        from backend.markets.movement_detector import MarketMovementDetector

        slug = "closing-soon"
        old_ts = self.now - 7200
        db.insert_market_snapshot(slug, 0.80, snapshotted_at=old_ts)

        close_ts = self.now + 3600 * 6  # 6 hours from now
        m = FakeMarket(
            id=f"poly:{slug}",
            yes_price=0.81,
            close_time=str(close_ts),
        )
        detector = MarketMovementDetector(approaching_hours=24)
        events = detector.detect([m], now=self.now)

        approaching = [e for e in events if e.event_type == "approaching_resolution"]
        self.assertEqual(len(approaching), 1)
        self.assertAlmostEqual(approaching[0].hours_to_close, 6.0, places=0)

    def test_reversal_detected(self):
        """Price reverses direction after a significant move → reversal event."""
        from backend.markets.movement_detector import MarketMovementDetector

        slug = "reversal-test"
        # Two snapshots showing an up-then-down
        even_older_ts = self.now - 14400  # 4h ago
        old_ts = self.now - 7200          # 2h ago
        db.insert_market_snapshot(slug, 0.40, snapshotted_at=even_older_ts)
        db.insert_market_snapshot(slug, 0.55, snapshotted_at=old_ts)  # +15pp

        m = FakeMarket(id=f"poly:{slug}", yes_price=0.40)  # -15pp reversal
        detector = MarketMovementDetector(
            price_threshold=0.08,
            reversal_min_swing=0.10,
        )
        events = detector.detect([m], now=self.now)

        reversals = [e for e in events if e.event_type == "reversal"]
        self.assertEqual(len(reversals), 1)
        self.assertEqual(reversals[0].severity, "high")

    def test_inactive_markets_skipped(self):
        """Closed/resolved markets should not produce events."""
        from backend.markets.movement_detector import MarketMovementDetector

        m = FakeMarket(id="poly:closed", status="closed")
        detector = MarketMovementDetector()
        events = detector.detect([m], now=self.now)
        self.assertEqual(len(events), 0)


# ── Persistence + deduplication ──────────────────────────────────────────────


class TestPersistAndDedup(unittest.TestCase):

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM market_movement_events")

    def test_persist_events(self):
        from backend.markets.movement_detector import MovementEvent, persist_events

        ev = MovementEvent(
            event_type="odds_movement",
            market_slug="persist-test",
            market_question="Test?",
            old_price=0.40,
            new_price=0.60,
            price_change=0.20,
            severity="high",
            detected_at=int(time.time()),
        )
        ids = persist_events([ev])
        self.assertEqual(len(ids), 1)
        row = db.get_movement_event(ids[0])
        self.assertIsNotNone(row)
        self.assertEqual(row["event_type"], "odds_movement")
        self.assertEqual(row["market_slug"], "persist-test")

    def test_deduplicate_within_cooldown(self):
        from backend.markets.movement_detector import MovementEvent, deduplicate, persist_events

        now = int(time.time())
        ev = MovementEvent(
            event_type="odds_movement",
            market_slug="dedup-test",
            severity="medium",
            detected_at=now,
        )
        # First persist
        persist_events([ev])

        # Try to detect same event again — should be deduped
        ev2 = MovementEvent(
            event_type="odds_movement",
            market_slug="dedup-test",
            severity="medium",
            detected_at=now + 60,
        )
        result = deduplicate([ev2], cooldown_seconds=1800)
        self.assertEqual(len(result), 0)

    def test_deduplicate_after_cooldown(self):
        from backend.markets.movement_detector import MovementEvent, deduplicate, persist_events

        now = int(time.time())
        ev = MovementEvent(
            event_type="odds_movement",
            market_slug="dedup-ok",
            severity="medium",
            detected_at=now - 3600,  # 1h ago
        )
        persist_events([ev])

        ev2 = MovementEvent(
            event_type="odds_movement",
            market_slug="dedup-ok",
            severity="medium",
            detected_at=now,
        )
        result = deduplicate([ev2], cooldown_seconds=1800)
        self.assertEqual(len(result), 1)


# ── DB CRUD tests ────────────────────────────────────────────────────────────


class TestMovementEventsCRUD(unittest.TestCase):

    def setUp(self):
        self.now = int(time.time())
        with db.conn() as c:
            c.execute("DELETE FROM market_movement_events")

    def test_list_filter_by_type(self):
        db.insert_movement_event("odds_movement", "m1", self.now, severity="high")
        db.insert_movement_event("volume_spike", "m2", self.now, severity="medium")

        odds = db.list_movement_events(event_type="odds_movement")
        self.assertEqual(len(odds), 1)
        self.assertEqual(odds[0]["market_slug"], "m1")

    def test_list_filter_by_severity(self):
        db.insert_movement_event("odds_movement", "m1", self.now, severity="high")
        db.insert_movement_event("odds_movement", "m2", self.now, severity="low")

        high = db.list_movement_events(severity="high")
        self.assertEqual(len(high), 1)

    def test_list_since(self):
        db.insert_movement_event("odds_movement", "old", self.now - 86400, severity="medium")
        db.insert_movement_event("odds_movement", "new", self.now, severity="medium")

        recent = db.list_movement_events(since=self.now - 3600)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["market_slug"], "new")

    def test_mark_notified(self):
        eid = db.insert_movement_event("odds_movement", "notify-test", self.now)
        row = db.get_movement_event(eid)
        self.assertEqual(row["notified"], 0)

        db.mark_events_notified([eid])
        row = db.get_movement_event(eid)
        self.assertEqual(row["notified"], 1)


# ── Alert rules CRUD ────────────────────────────────────────────────────────


class TestAlertRulesCRUD(unittest.TestCase):

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM user_market_alerts")
        # Ensure a test user exists
        with db.conn() as c:
            row = c.execute("SELECT id FROM users WHERE email = 'mover@test.com'").fetchone()
        if row:
            self.user_id = row["id"]
        else:
            self.user_id = db.create_user("mover@test.com", "pass123", "mover_tester")

    def test_create_and_list(self):
        rid = db.create_alert_rule(
            self.user_id,
            "odds_movement",
            min_severity="high",
            delivery="email",
        )
        self.assertIsInstance(rid, int)
        rules = db.list_alert_rules(self.user_id)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["event_type"], "odds_movement")
        self.assertEqual(rules[0]["delivery"], "email")

    def test_get_by_id(self):
        rid = db.create_alert_rule(self.user_id, "volume_spike")
        rule = db.get_alert_rule(rid, self.user_id)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["event_type"], "volume_spike")

    def test_update(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        ok = db.update_alert_rule(rid, self.user_id, min_severity="critical", delivery="both")
        self.assertTrue(ok)
        rule = db.get_alert_rule(rid, self.user_id)
        self.assertEqual(rule["min_severity"], "critical")
        self.assertEqual(rule["delivery"], "both")

    def test_delete(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        ok = db.delete_alert_rule(rid, self.user_id)
        self.assertTrue(ok)
        rule = db.get_alert_rule(rid, self.user_id)
        self.assertIsNone(rule)

    def test_cannot_access_other_users_rule(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        rule = db.get_alert_rule(rid, self.user_id + 999)
        self.assertIsNone(rule)
        ok = db.delete_alert_rule(rid, self.user_id + 999)
        self.assertFalse(ok)

    def test_get_rules_for_event(self):
        db.create_alert_rule(self.user_id, "odds_movement", delivery="email")
        db.create_alert_rule(self.user_id, "volume_spike", delivery="in_app")
        rows = db.get_alert_rules_for_event("odds_movement")
        types = [r["event_type"] for r in rows]
        self.assertIn("odds_movement", types)
        self.assertNotIn("volume_spike", types)

    def test_disabled_rule_excluded(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        db.update_alert_rule(rid, self.user_id, enabled=0)
        rows = db.get_alert_rules_for_event("odds_movement")
        self.assertEqual(len(rows), 0)


# ── API route tests ──────────────────────────────────────────────────────────


class TestMovementAPIRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server
        import server_features  # noqa: F401
        from starlette.testclient import TestClient

        cls.app = server.app
        cls.client = TestClient(cls.app)

        # Create a test user and get a session
        with db.conn() as c:
            row = c.execute("SELECT id FROM users WHERE email = 'mv_api@test.com'").fetchone()
        if row:
            cls.user_id = row["id"]
        else:
            cls.user_id = db.create_user("mv_api@test.com", "TestPass1!", "mv_api_user")

        token = db.create_session(cls.user_id)
        cls.cookies = {server.COOKIE_NAME: token, "_csrf": "test_csrf"}
        cls.csrf_headers = {"x-csrf-token": "test_csrf"}

    def setUp(self):
        with db.conn() as c:
            c.execute("DELETE FROM market_movement_events")
            c.execute("DELETE FROM user_market_alerts")

    def test_list_movements_requires_auth(self):
        r = self.client.get("/api/movements")
        self.assertEqual(r.status_code, 401)

    def test_list_movements_empty(self):
        r = self.client.get("/api/movements", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 0)

    def test_list_movements_returns_events(self):
        now = int(time.time())
        db.insert_movement_event(
            "odds_movement", "api-test", now,
            market_question="Test market?", severity="high",
            metadata_json='{"direction": "up"}',
        )
        r = self.client.get("/api/movements", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["movements"][0]["event_type"], "odds_movement")
        self.assertEqual(data["movements"][0]["metadata"]["direction"], "up")

    def test_get_single_movement(self):
        now = int(time.time())
        eid = db.insert_movement_event("volume_spike", "single-test", now)
        r = self.client.get(f"/api/movements/{eid}", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["market_slug"], "single-test")

    def test_get_movement_404(self):
        r = self.client.get("/api/movements/99999", cookies=self.cookies)
        self.assertEqual(r.status_code, 404)

    def test_create_alert_rule(self):
        r = self.client.post(
            "/api/alerts/rules",
            json={"event_type": "odds_movement", "min_severity": "high", "delivery": "email"},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 201)
        self.assertIn("id", r.json())

    def test_create_rule_invalid_type(self):
        r = self.client.post(
            "/api/alerts/rules",
            json={"event_type": "invalid_type"},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_list_alert_rules(self):
        db.create_alert_rule(self.user_id, "odds_movement")
        r = self.client.get("/api/alerts/rules", cookies=self.cookies)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["rules"]), 1)

    def test_update_alert_rule(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        r = self.client.put(
            f"/api/alerts/rules/{rid}",
            json={"min_severity": "critical", "enabled": False},
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        rule = db.get_alert_rule(rid, self.user_id)
        self.assertEqual(rule["min_severity"], "critical")
        self.assertEqual(rule["enabled"], 0)

    def test_delete_alert_rule(self):
        rid = db.create_alert_rule(self.user_id, "odds_movement")
        r = self.client.delete(
            f"/api/alerts/rules/{rid}",
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(db.get_alert_rule(rid, self.user_id))

    def test_delete_rule_404(self):
        r = self.client.delete(
            "/api/alerts/rules/99999",
            cookies=self.cookies,
            headers=self.csrf_headers,
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
