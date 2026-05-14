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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
