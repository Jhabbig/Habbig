"""QA Walk A — boot smoke.

Three cheap signals that the app is alive AND responsive:

  1. /health returns 200.
  2. Every response carries the X-Response-Time-ms middleware header so
     ops dashboards can graph it. We assert the header EXISTS (not just
     that one specific value is fast — that's flaky on CI).
  3. The recent log tail (when present) has no ERROR lines that aren't
     migration-related. Skipped silently when the file doesn't exist
     so dev machines without /tmp/gateway.log still pass.
"""

from __future__ import annotations

import os
import re
import unittest


from . import conftest as _conf  # noqa: F401 — pulls in TestClient + DB


class TestBootSmoke(unittest.TestCase):
    """Use the TestClient against the in-process app — no real port."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import server

        cls.client = TestClient(server.app)

    def test_health_returns_200(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        # /health body shape — most projects expose at least an ok flag.
        self.assertTrue(
            "ok" in r.text.lower() or r.json(),
            f"unexpected /health body: {r.text[:200]}",
        )

    def test_response_time_header_present(self):
        r = self.client.get("/health")
        # Header keys are case-insensitive but Starlette stores them lowered.
        keys = {k.lower() for k in r.headers.keys()}
        self.assertIn(
            "x-response-time-ms", keys,
            f"X-Response-Time-ms missing — perf middleware not wired? "
            f"Got headers: {sorted(keys)[:10]}",
        )
        try:
            ms = int(r.headers.get("x-response-time-ms") or
                     r.headers.get("X-Response-Time-ms") or "9999")
        except ValueError:
            self.fail("X-Response-Time-ms not numeric")
        # 1000ms is a generous bar — test client overhead alone is well under it.
        self.assertLess(ms, 1000, f"/health took {ms}ms")

    def test_recent_log_tail_clean(self):
        """If /tmp/gateway.log exists, last 200 lines should not contain
        non-migration ERROR entries. Silently skip on dev machines."""
        candidates = ["/tmp/gateway.log", "/var/log/narve/gateway.log"]
        log_path = next((p for p in candidates if os.path.isfile(p)), None)
        if not log_path:
            self.skipTest(f"no log file at any of {candidates}")
        # Read the tail — bounded to ~256KB so a runaway log doesn't OOM us.
        try:
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError as exc:
            self.skipTest(f"log read failed: {exc}")
        bad = [
            line for line in tail.splitlines()[-200:]
            if "ERROR" in line and not re.search(r"migration|migrate", line, re.I)
        ]
        self.assertFalse(bad, f"recent ERROR lines: {bad[:5]}")


if __name__ == "__main__":
    unittest.main()
