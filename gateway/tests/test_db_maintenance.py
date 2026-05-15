"""Tests for the daily DB maintenance job.

Covers ``vacuum_db_daily``:
  - VACUUM runs against the test DB without error
  - ANALYZE follows successfully (refreshes ``sqlite_stat1``)
  - ``PRAGMA wal_checkpoint(TRUNCATE)`` is exercised and result tuple
    surfaces in the job's return payload
  - sqlite3 ``OperationalError`` (the canonical lock-contention class)
  - any other exception
  are both swallowed and reported via the result dict, so the
  scheduler stays healthy

The conftest plumbs every test in this package onto a shared in-memory
sqlite connection (`tests._testdb._fake_conn`), so the job runs against
the same DB the rest of the suite uses. Side effects are confined to
that one connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
import unittest
from contextlib import contextmanager
from unittest import mock

from tests import _testdb  # noqa: F401 — sets up in-memory DB + migrations
import db  # noqa: E402

from jobs import db_maintenance  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestVacuumDbDaily(unittest.TestCase):
    """End-to-end happy-path: VACUUM + ANALYZE + WAL-truncate."""

    def test_runs_without_error(self):
        result = _run(db_maintenance.vacuum_db_daily())
        self.assertTrue(
            result["ok"],
            msg=f"vacuum_db_daily reported failure: {result!r}",
        )
        self.assertTrue(result["vacuum_ok"])
        self.assertTrue(result["analyze_ok"])
        self.assertTrue(result["wal_ok"])
        self.assertNotIn("error", result)
        # Duration is wall-clock — should be a non-negative int.
        self.assertIsInstance(result["duration_ms"], int)
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_analyze_populates_sqlite_stat1(self):
        """After ANALYZE, ``sqlite_stat1`` exists and the planner can use it.

        The job runs VACUUM (which rebuilds the file) then ANALYZE
        (which writes the stats). We don't assert on specific rows
        because the table is only populated for non-trivial tables, but
        ``sqlite_stat1`` must at least exist after a successful
        ANALYZE.
        """
        _run(db_maintenance.vacuum_db_daily())
        with db.conn() as c:
            row = c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sqlite_stat1'"
            ).fetchone()
        self.assertIsNotNone(
            row,
            "ANALYZE should have created sqlite_stat1",
        )

    def test_wal_checkpoint_truncate_surfaces_result(self):
        """``PRAGMA wal_checkpoint(TRUNCATE)`` returns (busy, log, ckpt).

        For an in-memory DB without WAL the values may all be zero or
        the pragma may return ``(0, 0, 0)``. We assert the three keys
        are present in the job result so downstream observability
        (admin panel) gets the diagnostic tuple.
        """
        result = _run(db_maintenance.vacuum_db_daily())
        self.assertIn("wal_busy", result)
        self.assertIn("wal_log_pages", result)
        self.assertIn("wal_checkpointed", result)


class TestVacuumDbDailySizeLogging(unittest.TestCase):
    """Size before/after telemetry."""

    def test_size_keys_always_present(self):
        result = _run(db_maintenance.vacuum_db_daily())
        # In-memory DB → getsize() fails on a non-file path; helper
        # returns None. Either an int or None is acceptable — the
        # critical contract is the keys exist for the admin UI to read.
        self.assertIn("size_before_bytes", result)
        self.assertIn("size_after_bytes", result)
        for key in ("size_before_bytes", "size_after_bytes"):
            val = result[key]
            self.assertTrue(
                val is None or isinstance(val, int),
                f"{key} must be int|None, got {type(val).__name__}",
            )

    def test_size_helper_can_be_patched(self):
        """Tests can stub ``_db_file_size_bytes`` to assert delta logging."""
        with mock.patch.object(
            db_maintenance,
            "_db_file_size_bytes",
            side_effect=[1024, 512],
        ):
            result = _run(db_maintenance.vacuum_db_daily())
        self.assertEqual(result["size_before_bytes"], 1024)
        self.assertEqual(result["size_after_bytes"], 512)


class TestVacuumDbDailyLockContention(unittest.TestCase):
    """Failure modes: lock contention + arbitrary exceptions stay contained."""

    def test_operational_error_does_not_propagate(self):
        """A locked DB raises ``sqlite3.OperationalError`` — the job
        must catch it, log a warning, and return ``ok=False`` rather
        than letting the scheduler crash."""

        @contextmanager
        def _locked_conn():
            raise sqlite3.OperationalError("database is locked")
            yield  # pragma: no cover — unreachable

        with mock.patch.object(db, "conn", _locked_conn):
            result = _run(db_maintenance.vacuum_db_daily())

        self.assertFalse(result["ok"])
        self.assertFalse(result["vacuum_ok"])
        self.assertIn("error", result)
        self.assertIn("OperationalError", result["error"])

    def test_generic_exception_does_not_propagate(self):
        """Any other exception (corruption, OS error, ...) must also
        be swallowed and surfaced via the result dict."""

        @contextmanager
        def _broken_conn():
            raise RuntimeError("disk on fire")
            yield  # pragma: no cover — unreachable

        with mock.patch.object(db, "conn", _broken_conn):
            result = _run(db_maintenance.vacuum_db_daily())

        self.assertFalse(result["ok"])
        self.assertIn("error", result)
        self.assertIn("disk on fire", result["error"])

    def test_partial_failure_reports_per_step_flags(self):
        """If VACUUM succeeds but ANALYZE blows up, ``vacuum_ok`` must
        remain True while ``analyze_ok`` and ``ok`` go False.

        This proves the job's three boolean flags are independent
        enough that the admin panel can pinpoint which step regressed.
        """

        class _FlakyConn:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, stmt: str, *args, **kwargs):
                self.calls.append(stmt)
                if stmt.upper().startswith("ANALYZE"):
                    raise sqlite3.OperationalError("synthetic analyze failure")

                class _Cur:
                    def fetchone(self_inner):
                        return (0, 0, 0)

                return _Cur()

            def commit(self):
                pass

        flaky = _FlakyConn()

        @contextmanager
        def _flaky_cm():
            yield flaky

        with mock.patch.object(db, "conn", _flaky_cm):
            result = _run(db_maintenance.vacuum_db_daily())

        self.assertTrue(result["vacuum_ok"])
        self.assertFalse(result["analyze_ok"])
        self.assertFalse(result["wal_ok"])
        self.assertFalse(result["ok"])
        self.assertIn("synthetic analyze failure", result["error"])


class TestVacuumDbDailyRegistration(unittest.TestCase):
    """The job + its back-compat alias must both be in the registry."""

    def test_daily_job_registered(self):
        from jobs.registry import job_registry
        self.assertIn("vacuum_db_daily", job_registry)

    def test_alias_still_registered(self):
        """``vacuum_db_maybe`` is the pre-refactor name. Old admin-panel
        retry buttons and any in-flight ``background_jobs`` rows still
        reference it, so the alias must dispatch to the same handler."""
        from jobs.registry import job_registry
        self.assertIn("vacuum_db_maybe", job_registry)

    def test_alias_delegates_to_daily(self):
        result = _run(db_maintenance.vacuum_db_maybe())
        # Same shape as vacuum_db_daily; "ok" depends on the test DB
        # state but the structural keys must match.
        for key in (
            "ok", "vacuum_ok", "analyze_ok", "wal_ok",
            "size_before_bytes", "size_after_bytes", "duration_ms",
        ):
            self.assertIn(key, result)


class TestTrimJobRuns(unittest.TestCase):
    """``trim_job_runs`` removes old rows and keeps recent ones.

    Per the perf audit, ``job_runs`` would otherwise grow unbounded
    while ``/admin/jobs`` polls every 5s with three full-table scans —
    so the retention contract (drop > 30d, keep ≤ 30d) is the load-
    bearing behaviour to lock down.
    """

    def setUp(self) -> None:
        # Clean slate per test — _testdb shares one in-memory connection
        # across the whole suite so unrelated tests may have inserted
        # rows we don't want to assert against.
        with db.conn() as c:
            c.execute("DELETE FROM job_runs")

    def _insert(self, name: str, started_at: int, completed_at: int | None) -> None:
        with db.conn() as c:
            c.execute(
                "INSERT INTO job_runs (job_name, started_at, completed_at, ok) "
                "VALUES (?, ?, ?, ?)",
                (name, started_at, completed_at, 1 if completed_at else None),
            )

    def _count(self) -> int:
        with db.conn() as c:
            row = c.execute("SELECT COUNT(*) FROM job_runs").fetchone()
        return int(row[0])

    def test_removes_rows_older_than_cutoff(self):
        now = int(__import__("time").time())
        # 40 days old — must be swept (default retention 30 days).
        self._insert("old_job", started_at=now - 40 * 86400,
                     completed_at=now - 40 * 86400 + 5)
        result = _run(db_maintenance.trim_job_runs())
        self.assertTrue(result["ok"], msg=f"trim returned: {result!r}")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self._count(), 0)

    def test_keeps_rows_inside_retention_window(self):
        now = int(__import__("time").time())
        # 5 days old — well inside the 30-day window.
        self._insert("recent_job", started_at=now - 5 * 86400,
                     completed_at=now - 5 * 86400 + 5)
        result = _run(db_maintenance.trim_job_runs())
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_mixed_dataset_only_old_rows_swept(self):
        now = int(__import__("time").time())
        # Two old, one recent — only the two old rows go.
        self._insert("a", now - 60 * 86400, now - 60 * 86400 + 1)
        self._insert("b", now - 31 * 86400, now - 31 * 86400 + 1)
        self._insert("c", now - 1 * 86400, now - 1 * 86400 + 1)
        result = _run(db_maintenance.trim_job_runs())
        self.assertEqual(result["removed"], 2)
        with db.conn() as c:
            remaining = [r[0] for r in c.execute(
                "SELECT job_name FROM job_runs ORDER BY job_name"
            ).fetchall()]
        self.assertEqual(remaining, ["c"])

    def test_in_flight_rows_never_swept(self):
        """A row with ``completed_at IS NULL`` is still running — even
        if ``started_at`` is ancient (a hung worker), we must not delete
        it out from under the scheduler."""
        now = int(__import__("time").time())
        self._insert("hung_worker", started_at=now - 90 * 86400,
                     completed_at=None)
        result = _run(db_maintenance.trim_job_runs())
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_custom_days_parameter(self):
        """A non-default ``days`` argument shifts the cutoff."""
        now = int(__import__("time").time())
        self._insert("seven_days_old", started_at=now - 7 * 86400,
                     completed_at=now - 7 * 86400 + 1)
        # 14-day window keeps it.
        result = _run(db_maintenance.trim_job_runs(days=14))
        self.assertEqual(result["removed"], 0)
        # 3-day window sweeps it.
        result = _run(db_maintenance.trim_job_runs(days=3))
        self.assertEqual(result["removed"], 1)

    def test_registered_in_job_registry(self):
        from jobs.registry import job_registry
        self.assertIn("trim_job_runs", job_registry)

    def test_registered_in_cron_schedule(self):
        from jobs.registry import cron_jobs
        match = [j for j in cron_jobs if j["name"] == "trim_job_runs"]
        self.assertEqual(len(match), 1, msg=f"cron_jobs={cron_jobs!r}")
        self.assertEqual(match[0]["hour"], 4)
        self.assertEqual(match[0]["minute"], 15)


class TestTrimAnalyticsEvents(unittest.TestCase):
    """``trim_analytics_events`` drops > 180 d, keeps ≤ 180 d.

    Per the log-retention audit, ``analytics_events`` had no retention
    path despite indefinite growth from page-view writes. The 180-day
    contract (drop older, keep newer) is the load-bearing behaviour.
    """

    def setUp(self) -> None:
        # Clean slate per test — _testdb shares one in-memory
        # connection across the suite so unrelated tests may have
        # inserted rows we don't want to assert against.
        with db.conn() as c:
            c.execute("DELETE FROM analytics_events")

    def _insert(self, event_type: str, created_at: int,
                ip_hash: str = "h") -> None:
        with db.conn() as c:
            c.execute(
                "INSERT INTO analytics_events "
                "(event_type, ip_hash, created_at) VALUES (?, ?, ?)",
                (event_type, ip_hash, created_at),
            )

    def _count(self) -> int:
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM analytics_events"
            ).fetchone()
        return int(row[0])

    def test_removes_rows_older_than_cutoff(self):
        now = int(__import__("time").time())
        # 200 days old — must be swept (default retention 180 days).
        self._insert("page_view", created_at=now - 200 * 86400)
        result = _run(db_maintenance.trim_analytics_events())
        self.assertTrue(result["ok"], msg=f"trim returned: {result!r}")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self._count(), 0)

    def test_keeps_rows_inside_retention_window(self):
        now = int(__import__("time").time())
        # 30 days old — well inside the 180-day window.
        self._insert("page_view", created_at=now - 30 * 86400)
        result = _run(db_maintenance.trim_analytics_events())
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_mixed_dataset_only_old_rows_swept(self):
        now = int(__import__("time").time())
        # Two old, one recent — only the two old rows go.
        self._insert("a", now - 365 * 86400)
        self._insert("b", now - 181 * 86400)
        self._insert("c", now - 30 * 86400)
        result = _run(db_maintenance.trim_analytics_events())
        self.assertEqual(result["removed"], 2)
        with db.conn() as c:
            remaining = [r[0] for r in c.execute(
                "SELECT event_type FROM analytics_events ORDER BY event_type"
            ).fetchall()]
        self.assertEqual(remaining, ["c"])

    def test_custom_days_parameter(self):
        now = int(__import__("time").time())
        self._insert("forty_day_old", created_at=now - 40 * 86400)
        # 60-day window keeps it.
        result = _run(db_maintenance.trim_analytics_events(days=60))
        self.assertEqual(result["removed"], 0)
        # 14-day window sweeps it.
        result = _run(db_maintenance.trim_analytics_events(days=14))
        self.assertEqual(result["removed"], 1)

    def test_registered_in_job_registry(self):
        from jobs.registry import job_registry
        self.assertIn("trim_analytics_events", job_registry)

    def test_registered_in_cron_schedule(self):
        from jobs.registry import cron_jobs
        match = [j for j in cron_jobs if j["name"] == "trim_analytics_events"]
        self.assertEqual(len(match), 1, msg=f"cron_jobs={cron_jobs!r}")
        self.assertEqual(match[0]["hour"], 3)
        self.assertEqual(match[0]["minute"], 50)


class TestTrimSecurityEvents(unittest.TestCase):
    """``trim_security_events`` drops > 90 d, keeps ≤ 90 d, never sweeps
    rows tagged ``status='active'`` or ``severity='critical'``.

    The audit-tier preservation is the load-bearing distinction from
    the generic perf-log trims: forensic rows must outlive the
    retention window so incident response can re-read them weeks or
    months after the event.
    """

    def setUp(self) -> None:
        with db.conn() as c:
            c.execute("DELETE FROM security_events")

    def _insert(self, event_type: str, created_at: int,
                metadata: str = "{}") -> None:
        with db.conn() as c:
            c.execute(
                "INSERT INTO security_events "
                "(event_type, metadata, created_at) VALUES (?, ?, ?)",
                (event_type, metadata, created_at),
            )

    def _count(self) -> int:
        with db.conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM security_events"
            ).fetchone()
        return int(row[0])

    def test_removes_rows_older_than_cutoff(self):
        now = int(__import__("time").time())
        # 100 days old — must be swept (default retention 90 days).
        self._insert("capture_attempt", created_at=now - 100 * 86400)
        result = _run(db_maintenance.trim_security_events())
        self.assertTrue(result["ok"], msg=f"trim returned: {result!r}")
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self._count(), 0)

    def test_keeps_rows_inside_retention_window(self):
        now = int(__import__("time").time())
        # 10 days old — well inside the 90-day window.
        self._insert("capture_attempt", created_at=now - 10 * 86400)
        result = _run(db_maintenance.trim_security_events())
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_critical_severity_preserved_forever(self):
        """A row with ``metadata.severity = 'critical'`` must survive
        even when its age is far past the retention cutoff. Forensics
        and incident response re-read these long after the event."""
        now = int(__import__("time").time())
        self._insert(
            "capture_attempt",
            created_at=now - 365 * 86400,
            metadata='{"severity":"critical"}',
        )
        result = _run(db_maintenance.trim_security_events())
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_active_status_preserved_forever(self):
        """Rows under active investigation (``status='active'``) are
        also preserved regardless of age."""
        now = int(__import__("time").time())
        self._insert(
            "capture_attempt",
            created_at=now - 500 * 86400,
            metadata='{"status":"active"}',
        )
        result = _run(db_maintenance.trim_security_events())
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self._count(), 1)

    def test_non_critical_old_row_swept_alongside_critical_kept(self):
        """The audit-tier exception applies row-by-row: an ordinary
        old row in the same sweep as a critical old row goes; the
        critical one stays."""
        now = int(__import__("time").time())
        self._insert("a", now - 200 * 86400, metadata='{}')
        self._insert("b", now - 200 * 86400,
                     metadata='{"severity":"critical"}')
        self._insert("c", now - 200 * 86400,
                     metadata='{"status":"active"}')
        self._insert("d", now - 5 * 86400, metadata='{}')
        result = _run(db_maintenance.trim_security_events())
        self.assertEqual(result["removed"], 1)
        with db.conn() as c:
            remaining = sorted(r[0] for r in c.execute(
                "SELECT event_type FROM security_events"
            ).fetchall())
        self.assertEqual(remaining, ["b", "c", "d"])

    def test_malformed_metadata_treated_as_non_critical(self):
        """A row whose ``metadata`` isn't valid JSON must still be
        eligible for sweeping — we can't read fields out of it, so
        treating it as "no status/severity tag" is the safe default."""
        now = int(__import__("time").time())
        self._insert("a", now - 200 * 86400, metadata="not-json")
        result = _run(db_maintenance.trim_security_events())
        # SQLite's json_extract returns NULL on invalid JSON; the
        # COALESCE then yields '' and the row is swept.
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self._count(), 0)

    def test_custom_days_parameter(self):
        now = int(__import__("time").time())
        self._insert("a", created_at=now - 60 * 86400)
        # 90-day window (default) keeps it.
        result = _run(db_maintenance.trim_security_events())
        self.assertEqual(result["removed"], 0)
        # 30-day window sweeps it.
        result = _run(db_maintenance.trim_security_events(days=30))
        self.assertEqual(result["removed"], 1)

    def test_registered_in_job_registry(self):
        from jobs.registry import job_registry
        self.assertIn("trim_security_events", job_registry)

    def test_registered_in_cron_schedule(self):
        from jobs.registry import cron_jobs
        match = [j for j in cron_jobs if j["name"] == "trim_security_events"]
        self.assertEqual(len(match), 1, msg=f"cron_jobs={cron_jobs!r}")
        self.assertEqual(match[0]["hour"], 3)
        self.assertEqual(match[0]["minute"], 50)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
