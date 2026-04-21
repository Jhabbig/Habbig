"""Status monitoring cron — probes, snapshot recording, auto-incident logic."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401,E402

from jobs import status_jobs  # noqa: E402
from status_system import COMPONENT_KEYS  # noqa: E402
from status_system import db as status_db  # noqa: E402
from status_system import probes as status_probes  # noqa: E402


def _run(coro):
    """Run a coroutine in a fresh event loop.

    We can't rely on the ambient `asyncio.get_event_loop()` because another
    test module (e.g. test_rate_limiting) closes the global loop when it
    tears down its own fixtures. Allocating a fresh loop per call is
    wasteful but trivially safe — probe runs are ~10ms.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestProbes(unittest.TestCase):
    """Sanity check each probe returns (status, ms) and never raises."""

    def test_run_all_probes_covers_every_component(self):
        results = _run(status_probes.run_all_probes())
        self.assertEqual(set(results.keys()), set(COMPONENT_KEYS))
        for key, (status, ms) in results.items():
            self.assertIn(status, ("operational", "degraded", "outage"),
                          f"{key}: unexpected status {status!r}")

    def test_database_probe_returns_operational_on_working_db(self):
        status, ms = _run(status_probes.probe_database())
        self.assertEqual(status, "operational")
        self.assertIsNotNone(ms)
        self.assertGreaterEqual(ms, 0.0)

    def test_app_probe_returns_operational_on_working_app(self):
        status, ms = _run(status_probes.probe_app())
        self.assertEqual(status, "operational")


class TestCheckServiceHealth(unittest.TestCase):
    """End-to-end: the cron job writes snapshots and summarises results."""

    def setUp(self):
        import db
        with db.conn() as c:
            c.execute("DELETE FROM service_health_snapshots")
            c.execute("DELETE FROM incident_updates")
            c.execute("DELETE FROM incidents")

    def test_cron_writes_snapshot_per_component(self):
        _run(status_jobs.check_service_health())
        for key in COMPONENT_KEYS:
            snap = status_db.get_latest_snapshot(key)
            self.assertIsNotNone(snap, f"{key}: no snapshot recorded")
            self.assertEqual(snap["component"], key)

    def test_cron_returns_summary_with_statuses(self):
        summary = _run(status_jobs.check_service_health())
        self.assertIn("checked_components", summary)
        self.assertIn("statuses", summary)
        self.assertEqual(set(summary["statuses"].keys()), set(COMPONENT_KEYS))

    def test_degradation_auto_opens_incident(self):
        """Force one probe to report outage; cron should open an auto-incident.

        `run_all_probes()` iterates the `_PROBES` dict values (resolved at
        import time), so `patch.object` on the module-level name wouldn't
        intercept anything — we override the dict entry instead.
        """
        # Seed "operational" baseline snapshots for every component so the
        # next tick observes a clean transition for the one we corrupt.
        now = int(time.time())
        for k in COMPONENT_KEYS:
            status_db.record_snapshot(k, "operational", 10.0, timestamp=now - 120)

        async def bad_scraper():
            return ("outage", 9999.0)

        async def ok_probe():
            return ("operational", 10.0)

        # Force every probe to a predictable value so the only observed
        # transition is the one we're testing (scraper → outage).
        patched = {k: ok_probe for k in COMPONENT_KEYS}
        patched["scraper"] = bad_scraper
        with patch.dict(status_probes._PROBES, patched, clear=True):
            _run(status_jobs.check_service_health())

        opens = [i for i in status_db.list_open_incidents_for_component("scraper")
                 if i["auto_created"]]
        self.assertEqual(len(opens), 1, "scraper auto-incident should be opened")
        self.assertIn("scraper", opens[0]["affected_components"])

    def test_recovery_auto_resolves_incident(self):
        """After an auto-incident is open, a fully-recovered tick resolves it."""
        # Open an auto-incident manually.
        inc_id = status_db.create_incident(
            title="Scraper outage",
            affected_components=["scraper"],
            severity="critical",
            status="investigating",
            auto_created=True,
        )
        # Seed a fresh operational snapshot so the transition logic fires.
        now = int(time.time())
        for k in COMPONENT_KEYS:
            status_db.record_snapshot(k, "operational", 10.0, timestamp=now - 120)
            status_db.record_snapshot(k, "degraded" if k == "scraper" else "operational",
                                      10.0, timestamp=now - 60)
        # Next cron tick should observe scraper back to operational.
        summary = _run(status_jobs.check_service_health())
        self.assertGreaterEqual(summary["incidents_resolved"], 1)

        fresh = status_db.get_incident(inc_id)
        self.assertEqual(fresh["status"], "resolved")
        self.assertIsNotNone(fresh["resolved_at"])

    def test_no_duplicate_auto_incident_while_open(self):
        """Two outage ticks on the same component must produce only one auto-incident."""
        now = int(time.time())
        for k in COMPONENT_KEYS:
            status_db.record_snapshot(k, "operational", 10.0, timestamp=now - 120)

        async def bad_api():
            return ("outage", 9999.0)

        async def ok_probe():
            return ("operational", 10.0)

        patched = {k: ok_probe for k in COMPONENT_KEYS}
        patched["api"] = bad_api
        with patch.dict(status_probes._PROBES, patched, clear=True):
            _run(status_jobs.check_service_health())  # opens one
            _run(status_jobs.check_service_health())  # second tick — no dup
            _run(status_jobs.check_service_health())  # third — still no dup

        opens = [i for i in status_db.list_open_incidents_for_component("api")
                 if i["auto_created"]]
        self.assertEqual(len(opens), 1, "should only have one auto-incident for api")


if __name__ == "__main__":
    unittest.main()
