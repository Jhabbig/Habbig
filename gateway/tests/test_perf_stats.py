"""Tests for observability.perf_stats.

Covers query_stats.record() and endpoint_stats.record() in isolation:

* Query buckets: SQL statements are normalised (literals collapsed) so
  `WHERE id = 1` and `WHERE id = 2` bucket together.
* Endpoint buckets: path params are folded so `/api/credibility/sho`
  and `/api/credibility/julian` share a row.
* Slow-query / slow-request thresholds populate the ring buffers.
* reset_all() zeroes both stores.

The DB-instrumented path (db.conn() routing through the subclassed
Connection) is exercised implicitly by any test that opens a
connection; we don't unit-test it here because CPython's sqlite3
attribute-assignment rules vary by build, and we already test the
stats logic directly.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestQueryStats(unittest.TestCase):
    def setUp(self):
        from observability import perf_stats
        perf_stats.reset_all()
        self._perf = perf_stats

    def test_record_buckets_by_shape(self):
        # Different parameter values must bucket together.
        self._perf.query_stats.record("SELECT * FROM users WHERE id = 1", 0.001)
        self._perf.query_stats.record("SELECT * FROM users WHERE id = 42", 0.001)
        self._perf.query_stats.record("INSERT INTO t VALUES (1)", 0.001)
        snap = self._perf.query_stats.snapshot(limit=50)
        shapes = {row["shape"]: row["count"] for row in snap["top_by_total_time"]}
        select_bucket = next(
            (c for s, c in shapes.items() if s.startswith("SELECT")), None,
        )
        self.assertEqual(select_bucket, 2)
        insert_bucket = next(
            (c for s, c in shapes.items() if s.startswith("INSERT")), None,
        )
        self.assertEqual(insert_bucket, 1)

    def test_slow_query_populates_ring_buffer(self):
        # Lower threshold so every record is "slow".
        from observability import perf_stats
        original = perf_stats.SLOW_QUERY_THRESHOLD_SEC
        try:
            perf_stats.SLOW_QUERY_THRESHOLD_SEC = 0.0
            self._perf.query_stats.record("SELECT 1", 0.01)
            self._perf.query_stats.record("SELECT 2", 0.05)
            snap = self._perf.query_stats.snapshot(limit=10)
            self.assertEqual(len(snap["recent_slow_queries"]), 2)
            # Newest-first ordering.
            self.assertEqual(snap["recent_slow_queries"][0]["sql"], "SELECT 2")
        finally:
            perf_stats.SLOW_QUERY_THRESHOLD_SEC = original

    def test_reset_clears_counters(self):
        self._perf.query_stats.record("SELECT 1", 0.001)
        before = self._perf.query_stats.snapshot(limit=1)
        self.assertGreater(len(before["top_by_total_time"]), 0)
        self._perf.reset_all()
        after = self._perf.query_stats.snapshot(limit=1)
        self.assertEqual(after["top_by_total_time"], [])

    def test_max_seconds_tracked(self):
        self._perf.query_stats.record("SELECT 1", 0.010)
        self._perf.query_stats.record("SELECT 2", 0.500)
        self._perf.query_stats.record("SELECT 3", 0.100)
        snap = self._perf.query_stats.snapshot(limit=1)
        row = snap["top_by_total_time"][0]
        # All three fold into the same shape because literals are
        # collapsed — max_seconds reflects the slowest run.
        self.assertAlmostEqual(row["max_seconds"], 0.500, places=3)


class TestEndpointStats(unittest.TestCase):
    def setUp(self):
        from observability import perf_stats
        perf_stats.reset_all()
        self._perf = perf_stats

    def test_records_by_method_and_path(self):
        self._perf.endpoint_stats.record("GET", "/api/search", 0.05, 200)
        self._perf.endpoint_stats.record("GET", "/api/search", 0.15, 200)
        snap = self._perf.endpoint_stats.snapshot()
        self.assertEqual(snap["total_requests"], 2)
        rows = {r["endpoint"]: r for r in snap["top_by_avg_latency"]}
        self.assertIn("GET /api/search", rows)
        self.assertEqual(rows["GET /api/search"]["count"], 2)

    def test_buckets_source_handle_paths(self):
        # /api/credibility/sho and /api/credibility/julian bucket under
        # the templated form.
        self._perf.endpoint_stats.record("GET", "/api/credibility/sho", 0.1, 200)
        self._perf.endpoint_stats.record("GET", "/api/credibility/julian", 0.1, 200)
        snap = self._perf.endpoint_stats.snapshot()
        rows = {r["endpoint"]: r for r in snap["top_by_avg_latency"]}
        self.assertIn("GET /api/credibility/{h}", rows)
        self.assertEqual(rows["GET /api/credibility/{h}"]["count"], 2)

    def test_calibration_sub_route_preserved(self):
        self._perf.endpoint_stats.record(
            "GET", "/api/credibility/sho/calibration", 0.1, 200,
        )
        snap = self._perf.endpoint_stats.snapshot()
        rows = {r["endpoint"]: r for r in snap["top_by_avg_latency"]}
        self.assertIn("GET /api/credibility/{h}/calibration", rows)

    def test_public_source_handle_bucketed(self):
        self._perf.endpoint_stats.record("GET", "/sources/sho", 0.1, 200)
        self._perf.endpoint_stats.record("GET", "/sources/julian", 0.1, 404)
        snap = self._perf.endpoint_stats.snapshot()
        rows = {r["endpoint"]: r for r in snap["top_by_avg_latency"]}
        self.assertIn("GET /sources/{h}", rows)
        self.assertEqual(rows["GET /sources/{h}"]["count"], 2)

    def test_status_code_buckets(self):
        self._perf.endpoint_stats.record("GET", "/api/x", 0.01, 200)
        self._perf.endpoint_stats.record("GET", "/api/x", 0.01, 404)
        self._perf.endpoint_stats.record("GET", "/api/x", 0.01, 500)
        snap = self._perf.endpoint_stats.snapshot()
        row = snap["top_by_avg_latency"][0]
        self.assertEqual(row["status_2xx"], 1)
        self.assertEqual(row["status_4xx"], 1)
        self.assertEqual(row["status_5xx"], 1)


if __name__ == "__main__":
    unittest.main()
