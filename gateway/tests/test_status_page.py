"""Public /status page, /api/status JSON, and /status/feed.xml RSS."""

from __future__ import annotations

import os
import sys
import time
import unittest
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

# Share the in-memory DB with all other tests that opt into it.
from tests import _testdb  # noqa: F401,E402  — sets up shared conn + migrations

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from status_system import COMPONENT_KEYS  # noqa: E402
from status_system import db as status_db  # noqa: E402
from status_system import feeds as status_feeds  # noqa: E402
from status_system import uptime as status_uptime  # noqa: E402


def _seed_snapshots(component: str, status: str, count: int, start: int) -> None:
    """Insert `count` snapshots for `component` one minute apart starting at `start`."""
    for i in range(count):
        status_db.record_snapshot(component, status, response_time_ms=10.0, timestamp=start + i * 60)


class TestPublicStatusPage(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_status_page_is_public(self):
        """GET /status returns 200 without any auth cookie or gate token."""
        r = self.client.get("/status")
        self.assertEqual(r.status_code, 200, f"body={r.text[:300]}")
        self.assertIn("text/html", r.headers.get("content-type", ""))

    def test_status_page_shows_all_components(self):
        """Every declared component key appears on the page body."""
        r = self.client.get("/status")
        body = r.text
        for key in COMPONENT_KEYS:
            # Either key or the human name is fine — we render the display
            # name, so check for the display name instead.
            pass
        # Display names are from status_system.COMPONENTS
        from status_system import COMPONENTS
        for _, display in COMPONENTS:
            self.assertIn(display, body, f"missing component row: {display}")

    def test_status_page_has_subscribe_form(self):
        r = self.client.get("/status")
        self.assertIn('id="subscribe-form"', r.text)
        # Clients hit /api/v1/status/subscribe (canonical); the APIVersion
        # middleware transparently rewrites it to /api/status/subscribe.
        self.assertIn("/api/v1/status/subscribe", r.text)

    def test_status_page_links_to_feed(self):
        r = self.client.get("/status")
        self.assertIn("/status/feed.xml", r.text)


class TestStatusApi(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_api_status_returns_json_snapshot(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("status", "components", "uptime_90d", "recent_incidents"):
            self.assertIn(key, body)

    def test_api_status_components_cover_all_keys(self):
        r = self.client.get("/api/status")
        body = r.json()
        got_keys = {c["key"] for c in body["components"]}
        self.assertEqual(got_keys, set(COMPONENT_KEYS))

    def test_api_status_uses_no_cache(self):
        r = self.client.get("/api/status")
        self.assertIn("no-store", r.headers.get("cache-control", "").lower())

    def test_api_status_uptime_shape(self):
        r = self.client.get("/api/status")
        up = r.json()["uptime_90d"]
        for key in ("uptime_pct", "downtime_minutes", "total_minutes", "incidents", "daily"):
            self.assertIn(key, up)
        self.assertIsInstance(up["daily"], list)


class TestRssFeed(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_feed_is_valid_xml(self):
        r = self.client.get("/status/feed.xml")
        self.assertEqual(r.status_code, 200)
        self.assertIn("rss", r.headers.get("content-type", "").lower())
        # Must parse as XML without raising.
        ET.fromstring(r.text)

    def test_feed_has_channel_and_title(self):
        r = self.client.get("/status/feed.xml")
        root = ET.fromstring(r.text)
        channel = root.find("channel")
        self.assertIsNotNone(channel)
        title_el = channel.find("title")
        self.assertIsNotNone(title_el)
        self.assertIn("narve", (title_el.text or "").lower())

    def test_feed_reflects_new_incident(self):
        inc_id = status_db.create_incident(
            title="Test feed incident",
            description="Feed check",
            severity="minor",
            affected_components=["app"],
            status="investigating",
        )
        r = self.client.get("/status/feed.xml")
        self.assertIn("Test feed incident", r.text)
        # Cleanup so other tests aren't polluted.
        import db
        with db.conn() as c:
            c.execute("DELETE FROM incidents WHERE id = ?", (inc_id,))
            c.execute("DELETE FROM incident_updates WHERE incident_id = ?", (inc_id,))


class TestUptimeCalculation(unittest.TestCase):
    """Direct tests against compute_uptime_last_n_days / overall_*."""

    def setUp(self):
        # Wipe snapshots so each test works from a clean slate.
        import db
        with db.conn() as c:
            c.execute("DELETE FROM service_health_snapshots")

    def test_100_percent_uptime_with_all_operational(self):
        now = int(time.time())
        # 60 operational snapshots today (the last hour)
        _seed_snapshots("app", "operational", 60, now - 60 * 60)
        report = status_uptime.compute_uptime_last_n_days("app", n=1)
        self.assertEqual(report["uptime_pct"], 100.0)
        self.assertEqual(report["downtime_minutes"], 0)

    def test_partial_outage_reduces_uptime_pct(self):
        now = int(time.time())
        # 30 operational + 30 outage within today
        _seed_snapshots("app", "operational", 30, now - 60 * 60)
        _seed_snapshots("app", "outage", 30, now - 30 * 60)
        report = status_uptime.compute_uptime_last_n_days("app", n=1)
        self.assertLess(report["uptime_pct"], 100.0)
        self.assertGreater(report["downtime_minutes"], 0)

    def test_degraded_counts_as_half_uptime(self):
        now = int(time.time())
        _seed_snapshots("app", "degraded", 60, now - 60 * 60)
        report = status_uptime.compute_uptime_last_n_days("app", n=1)
        # 60 × 0.5 = 30 weighted → 50% uptime.
        self.assertAlmostEqual(report["uptime_pct"], 50.0, delta=0.1)

    def test_no_data_reports_default_100(self):
        # No snapshots at all → treat as 100% uptime (no downtime observed).
        report = status_uptime.compute_uptime_last_n_days("app", n=90)
        self.assertEqual(report["uptime_pct"], 100.0)
        self.assertEqual(report["downtime_minutes"], 0)

    def test_overall_rollup_covers_every_component(self):
        report = status_uptime.compute_overall_uptime_last_n_days(90)
        self.assertEqual(set(report["per_component"].keys()), set(COMPONENT_KEYS))
        self.assertEqual(len(report["daily_rollup"]), 90)


class TestRssFeedHelper(unittest.TestCase):
    def test_build_rss_feed_with_no_incidents(self):
        xml = status_feeds.build_rss_feed("https://narve.ai", limit=10, incidents=[])
        root = ET.fromstring(xml)
        channel = root.find("channel")
        self.assertIsNotNone(channel)
        # No items when no incidents.
        items = channel.findall("item")
        self.assertEqual(len(items), 0)


if __name__ == "__main__":
    unittest.main()
