"""Tests for /api/analytics/event hardening + the analytics DB layer.

Three suites:

* :class:`TestAnalyticsDb` — pure DB / helper coverage. Predates the
  hardening pass.
* :class:`TestAnalyticsScrub` — unit tests for the PII / size helpers
  in :mod:`queries.analytics` added by the 2026-05-14 security audit.
* :class:`TestAnalyticsEndpoint` — exercises ``POST /api/analytics/event``
  through ``TestClient`` to verify schema validation, PII scrub, and the
  per-principal rate limit.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest

# Pre-import env tweaks: drop the site gate so /api/analytics/event is
# reachable without a cookie, and raise the global per-IP cap so the
# rate-limit test can crank through > 60 requests under TestClient
# (which shares one host/IP for every call in this suite).
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("GLOBAL_RATE_LIMIT_PER_MIN", "10000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402


class TestAnalyticsDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_record_event(self):
        eid = db.record_analytics_event(
            event_type="page_view",
            user_id=None,
            session_id=None,
            page="/landing",
            referrer="",
            ip_hash="abc123",
            user_agent_category="desktop",
        )
        self.assertGreater(eid, 0)

    def test_ip_hash_never_raw_ip(self):
        # Spot check: a SHA-256-derived hex is way longer than a dotted IP.
        from server import _hash_ip
        h = _hash_ip("192.168.1.1")
        self.assertNotIn(".", h)
        self.assertNotIn("192", h)
        self.assertGreaterEqual(len(h), 16)

    def test_newsletter_signup_event_counted(self):
        for _ in range(3):
            db.record_analytics_event(
                "newsletter_signup", None, None, "/", "", "ip" + str(_), "desktop"
            )
        result = db.get_analytics_prerelease(since=0)
        self.assertGreaterEqual(result["newsletter_signups"], 3)

    def test_get_analytics_users_growth_series(self):
        db.create_user("growth1@test.com", "TestPass123!", username="growth1")
        db.create_user("growth2@test.com", "TestPass123!", username="growth2")
        result = db.get_analytics_users(since=0)
        self.assertGreaterEqual(result["total_users"], 2)
        self.assertIn("growth_series", result)

    def test_get_analytics_revenue_returns_breakdown(self):
        result = db.get_analytics_revenue()
        self.assertIn("mrr", result)
        self.assertIn("arr", result)
        self.assertIn("breakdown", result)
        self.assertEqual(result["arr"], result["mrr"] * 12)

    def test_get_analytics_features(self):
        db.record_analytics_event("feed_view", None, None, "/feed", "", "iphash1", "desktop")
        result = db.get_analytics_features(since=0)
        self.assertGreaterEqual(result["feed_views"], 1)


class TestAnalyticsScrub(unittest.TestCase):
    """:mod:`queries.analytics` PII / size helpers (2026-05-14 hardening)."""

    def test_drops_pii_keys(self):
        from queries.analytics import scrub_properties
        out = scrub_properties({
            "email": "user@example.com",
            "phone_number": "555-867-5309",
            "password": "hunter2",
            "ok": "value",
        })
        self.assertNotIn("email", out)
        self.assertNotIn("phone_number", out)
        self.assertNotIn("password", out)
        self.assertEqual(out.get("ok"), "value")

    def test_redacts_email_in_values(self):
        from queries.analytics import scrub_properties
        out = scrub_properties({"note": "ping me at hi@narve.ai please"})
        self.assertNotIn("hi@narve.ai", out["note"])
        self.assertIn("[redacted-email]", out["note"])

    def test_redacts_phone_in_values(self):
        from queries.analytics import scrub_properties
        out = scrub_properties({"note": "call +1 415 555 0173"})
        self.assertNotIn("415 555 0173", out["note"])
        self.assertIn("[redacted-phone]", out["note"])

    def test_value_length_capped(self):
        from queries.analytics import scrub_properties, PROPERTY_VALUE_MAX
        out = scrub_properties({"blob": "x" * 5000})
        self.assertLessEqual(len(out["blob"]), PROPERTY_VALUE_MAX)

    def test_properties_too_large(self):
        from queries.analytics import properties_too_large, PROPERTIES_MAX_CHARS
        # Enough small entries to overflow PROPERTIES_MAX_CHARS regardless
        # of where we set the constant.
        big = {f"k{i}": "v" * 200 for i in range(PROPERTIES_MAX_CHARS // 100)}
        self.assertTrue(properties_too_large(big))
        self.assertFalse(properties_too_large({"k": "v"}))
        self.assertFalse(properties_too_large(None))

    def test_none_props_returns_none(self):
        from queries.analytics import scrub_properties
        self.assertIsNone(scrub_properties(None))
        self.assertIsNone(scrub_properties({}))


# ── Endpoint-level tests ─────────────────────────────────────────────────────

# Opt-in to the shared in-memory SQLite + migrations set up by tests/_testdb.
# The conftest's `_module_uses_testdb` autouse fixture pins db.conn back to
# the shared conn for every test in this class, so our endpoint hits the
# same database we read from below.
from tests import _testdb  # noqa: E402,F401
USES_TESTDB = True

# Host the SubproductMiddleware accepts in dev; "testclient" (TestClient's
# default) is NOT in _DEV_HOSTS and would 400 before reaching the route.
_TEST_HOST = "localhost"


class TestAnalyticsEndpoint(unittest.TestCase):
    """POST /api/analytics/event — schema, PII, rate-limit."""

    @classmethod
    def setUpClass(cls):
        import server
        from fastapi.testclient import TestClient
        cls.server = server
        cls.client = TestClient(server.app, follow_redirects=False)

    def setUp(self):
        # Drop the rate-limit counters between tests so each one starts
        # fresh. TestClient always reports the same host/IP, so without
        # this the analytics:ip:* bucket leaks across tests.
        try:
            self.server._rate_store.clear()
        except Exception:
            pass

    def _post(self, body: dict):
        return self.client.post(
            "/api/analytics/event",
            json=body,
            headers={"Host": _TEST_HOST},
        )

    def _events(self):
        with db.conn() as c:
            rows = c.execute(
                "SELECT event_type, properties FROM analytics_events "
                "ORDER BY id DESC"
            ).fetchall()
        return [(r["event_type"], r["properties"]) for r in rows]

    # ── valid path ────────────────────────────────────────────────────

    def test_valid_event_accepted(self):
        r = self._post({
            "event_type": "page_view",
            "page": "/landing",
            "referrer": "",
            "user_agent_category": "desktop",
            "properties": {"variant": "A"},
        })
        self.assertEqual(r.status_code, 204)
        self.assertTrue(any(et == "page_view" for et, _ in self._events()))

    def test_missing_properties_field_accepted(self):
        # Backward-compat: legacy clients omit `properties` entirely.
        r = self._post({"event_type": "newsletter_signup"})
        self.assertEqual(r.status_code, 204)

    # ── validation ────────────────────────────────────────────────────

    def test_invalid_event_name_rejected(self):
        # Pattern is [A-Za-z0-9_]{1,64}; spaces / hyphens get rejected.
        r = self._post({"event_type": "bad event"})
        self.assertEqual(r.status_code, 400)

    def test_event_name_too_long_rejected(self):
        r = self._post({"event_type": "a" * 65})
        self.assertEqual(r.status_code, 400)

    def test_oversized_properties_rejected(self):
        # Each value is below PROPERTY_VALUE_MAX so the per-value clamp
        # doesn't shrink the dict — total still trips PROPERTIES_MAX_CHARS.
        # 16 keys × 200 chars ≈ 3.4 KB of properties — under the 4 KB body
        # cap but well past the 2 KB properties cap → 422.
        props = {f"k{i:02d}": "v" * 200 for i in range(16)}
        r = self._post({"event_type": "page_view", "properties": props})
        self.assertEqual(r.status_code, 422)

    def test_body_over_4kb_rejected_400(self):
        # One huge value blows the *body* cap (4096 raw bytes) before
        # we ever parse JSON — should be a 400, not a 422.
        r = self._post({"event_type": "page_view", "properties": {"x": "z" * 5000}})
        self.assertEqual(r.status_code, 400)

    def test_pii_fields_scrubbed(self):
        r = self._post({
            "event_type": "form_submit",
            "properties": {
                "email": "alice@example.com",
                "phone": "415-555-1212",
                "campaign": "spring",
                "comment": "reach me at bob@example.com",
            },
        })
        self.assertEqual(r.status_code, 204)
        # The newest row is ours.
        et, props_json = self._events()[0]
        self.assertEqual(et, "form_submit")
        stored = json.loads(props_json)
        self.assertNotIn("email", stored)
        self.assertNotIn("phone", stored)
        self.assertEqual(stored.get("campaign"), "spring")
        # Embedded email in a non-PII key got redacted, not dropped.
        self.assertIn("[redacted-email]", stored.get("comment", ""))
        self.assertNotIn("bob@example.com", stored.get("comment", ""))

    # ── rate limit ────────────────────────────────────────────────────

    def test_rate_limit_kicks_in_at_61st_request(self):
        # First 60 within a minute pass, the 61st gets 429.
        statuses = []
        for _ in range(60):
            r = self._post({"event_type": "page_view"})
            statuses.append(r.status_code)
        # All 60 should be 204.
        self.assertTrue(all(s == 204 for s in statuses), statuses[-5:])
        # 61st trips the bucket.
        over = self._post({"event_type": "page_view"})
        self.assertEqual(over.status_code, 429)


if __name__ == "__main__":
    unittest.main()
