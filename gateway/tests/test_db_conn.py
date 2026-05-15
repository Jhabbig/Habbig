"""Tests for the ``db.conn()`` context manager hardening.

The audit flagged that ``conn()`` previously lacked:
  - PRAGMA ``busy_timeout`` — under contention SQLite would raise
    ``SQLITE_BUSY`` immediately instead of waiting for the writer.
  - ``rollback()`` on exception — exceptions in user code could leave
    partially-committed state on disk.

This module exercises both behaviours against a real on-disk SQLite
file so the WAL journal and busy-handler are actually engaged (the
in-memory fake_conn used by the rest of the suite cannot reproduce
the lock-contention path).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


def _fresh_db_module(db_path: Path):
    """Reimport ``db`` with ``GATEWAY_DB_PATH`` pointed at ``db_path``.

    Returns the freshly imported module so each test gets its own
    isolated on-disk database and the production ``conn()`` exactly as
    it is shipped (no monkey-patched in-memory fake).
    """
    import importlib
    import sys

    os.environ["GATEWAY_DB_PATH"] = str(db_path)
    # Drop any cached import so DB_PATH is recomputed from the env var.
    sys.modules.pop("db", None)
    db_mod = importlib.import_module("db")
    return db_mod


class TestConnRollbackOnException(unittest.TestCase):
    """When user code raises inside ``with conn() as c``, the context
    manager must call ``rollback()`` so partial writes do not persist.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = _fresh_db_module(self.db_path)
        # Minimal schema — one table we can write to.
        with self.db.conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS t ("
                "id INTEGER PRIMARY KEY, v TEXT NOT NULL)"
            )

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("GATEWAY_DB_PATH", None)
        import sys
        sys.modules.pop("db", None)

    def test_exception_triggers_rollback(self):
        """Insert a row, then raise — the row must NOT be on disk."""

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with self.db.conn() as c:
                c.execute("INSERT INTO t (v) VALUES (?)", ("never-committed",))
                raise Boom("simulated failure mid-transaction")

        # Re-open and confirm nothing was persisted.
        with self.db.conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM t").fetchone()
        self.assertEqual(
            row["n"],
            0,
            msg="Expected rollback to discard the insert; row persisted instead.",
        )

    def test_normal_exit_still_commits(self):
        """Regression guard — happy path still commits."""
        with self.db.conn() as c:
            c.execute("INSERT INTO t (v) VALUES (?)", ("committed",))

        with self.db.conn() as c:
            row = c.execute("SELECT v FROM t").fetchone()
        self.assertEqual(row["v"], "committed")


class TestConnBusyTimeout(unittest.TestCase):
    """When a second connection contends for a write lock, it must wait
    (up to ~5s) instead of raising ``SQLITE_BUSY`` immediately.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.db = _fresh_db_module(self.db_path)
        with self.db.conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS t ("
                "id INTEGER PRIMARY KEY, v TEXT NOT NULL)"
            )

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("GATEWAY_DB_PATH", None)
        import sys
        sys.modules.pop("db", None)

    def test_busy_timeout_is_set(self):
        """Direct assertion: ``PRAGMA busy_timeout`` returns >= 5000."""
        with self.db.conn() as c:
            val = c.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertGreaterEqual(
            val,
            5000,
            msg=f"busy_timeout should be >=5000ms, got {val}",
        )

    def test_concurrent_writer_waits_instead_of_failing(self):
        """Hold a write lock in thread A; thread B's commit must succeed
        once A releases (rather than raising SQLITE_BUSY immediately).
        """
        # Use raw sqlite3 in thread A so we can hold the lock manually
        # without going through the conn() context manager (which would
        # auto-commit and release).
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        thread_a_error: list = []

        def hold_writer():
            try:
                a = sqlite3.connect(str(self.db_path), timeout=10.0)
                a.execute("PRAGMA journal_mode = WAL")
                a.execute("BEGIN IMMEDIATE")
                a.execute("INSERT INTO t (v) VALUES (?)", ("from-A",))
                lock_acquired.set()
                # Hold the lock briefly — long enough that without
                # busy_timeout, thread B's write would raise instantly,
                # but short enough that with the 5s timeout, B waits
                # then succeeds.
                release_lock.wait(timeout=2.0)
                a.commit()
                a.close()
            except Exception as exc:  # pragma: no cover - diagnostic
                thread_a_error.append(exc)

        t = threading.Thread(target=hold_writer)
        t.start()
        try:
            self.assertTrue(
                lock_acquired.wait(timeout=5.0),
                msg="Thread A never acquired its write lock",
            )

            # Thread B (this thread) opens a new conn() and writes.
            # Without busy_timeout this would raise OperationalError:
            # "database is locked" immediately. With the 5s timeout it
            # blocks briefly, then succeeds once A commits.
            start = time.monotonic()
            # Schedule A's release ~200ms from now so B definitely
            # has to wait, proving the timeout is engaged.
            threading.Timer(0.2, release_lock.set).start()
            with self.db.conn() as c:
                c.execute("INSERT INTO t (v) VALUES (?)", ("from-B",))
            elapsed = time.monotonic() - start
        finally:
            release_lock.set()
            t.join(timeout=5.0)

        self.assertFalse(
            thread_a_error,
            msg=f"Thread A failed: {thread_a_error!r}",
        )
        # B should have waited (>=100ms) and then succeeded.
        self.assertGreater(
            elapsed,
            0.05,
            msg="Thread B finished suspiciously fast — busy_timeout may not "
                "have been exercised",
        )

        # Both rows now committed.
        with self.db.conn() as c:
            rows = {r["v"] for r in c.execute("SELECT v FROM t")}
        self.assertEqual(rows, {"from-A", "from-B"})


if __name__ == "__main__":
    unittest.main()
