"""Tests for observability.perf_stats and the instrumented db connection.

Covers:
  - query_stats records each executed statement with a duration
  - endpoint_stats normalises path params so {user_id} routes bucket
  - slow_query log threshold does not break execution
  - reset_all() zeros both stores
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestQueryStats(unittest.TestCase):
    def setUp(self):
        # Fresh temp DB so we're the only writer and the slow-query
        # threshold can be tuned without interfering with the real auth.db.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        for mod in ("db",):
            if mod in sys.modules:
                del sys.modules[mod]
        import db  # noqa: F401

        from observability import perf_stats
        perf_stats.reset_all()
        self._perf = perf_stats

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_execute_records_timing(self):
        import db
        with db.conn() as c:
            c.execute("CREATE TABLE t (x INTEGER)")
            c.execute("INSERT INTO t VALUES (1)")
            c.execute("SELECT x FROM t").fetchone()
        snap = self._perf.query_stats.snapshot(limit=50)
        # CREATE TABLE / INSERT / SELECT each normalise to their own
        # shape. `top_by_total_time` must include at least the SELECT
        # bucket.
        shapes = {row["shape"] for row in snap["top_by_total_time"]}
        self.assertTrue(any("SELECT" in s for s in shapes))
        self.assertTrue(any("CREATE TABLE" in s for s in shapes))
        self.assertTrue(any("INSERT" in s for s in shapes))

    def test_slow_query_log_respects_threshold(self):
        # Force a "slow" query by lowering the threshold for the duration
        # of this test. Since we control the module-level constant we
        # swap it back in the finally block.
        import db
        from observability import perf_stats

        original = perf_stats.SLOW_QUERY_THRESHOLD_SEC
        try:
            perf_stats.SLOW_QUERY_THRESHOLD_SEC = 0.0  # every query is "slow"
            with db.conn() as c:
                c.execute("CREATE TABLE u (x INTEGER)")
                c.execute("INSERT INTO u VALUES (1)")
            snap = perf_stats.query_stats.snapshot(limit=10)
            self.assertGreater(len(snap["recent_slow_queries"]), 0)
        finally:
            perf_stats.SLOW_QUERY_THRESHOLD_SEC = original

    def test_reset_clears_counters(self):
        import db
        with db.conn() as c:
            c.execute("CREATE TABLE r (x INTEGER)")
            c.execute("INSERT INTO r VALUES (1)")
        before = self._perf.query_stats.snapshot(limit=1)
        self.assertGreater(len(before["top_by_total_time"]), 0)
        self._perf.reset_all()
        after = self._perf.query_stats.snapshot(limit=1)
        self.assertEqual(after["top_by_total_time"], [])


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
