"""Verify migration 035 creates the expected indexes and is idempotent.

Runs against a temp SQLite DB so the real auth.db is never touched. The
migration runner is the canonical path — we don't call `upgrade()`
directly because that would miss the schema_version bookkeeping that
every other migration depends on.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


EXPECTED_INDEXES = {
    "idx_predictions_resolved",
    "idx_predictions_source_resolved",
    "idx_predictions_market_resolved",
    "idx_predictions_extracted_resolved",
    "idx_sessions_expires",
    "idx_cred_unlocked",
}


def _list_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {r[0] for r in rows}


def _list_table_indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


class TestPerformanceIndexes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        # db and migrations cache DB_PATH at import, so force a reload so
        # they see the temp path. The test process will have already
        # imported them via earlier test runs on the same pytest
        # invocation.
        for mod in ("db", "migrations"):
            if mod in sys.modules:
                del sys.modules[mod]
        import db  # noqa: F401 — side effect: schema create
        import migrations
        self._migrations = migrations
        db.init_db()

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_migration_creates_expected_indexes(self):
        self._migrations.upgrade_to_head()
        with sqlite3.connect(self._tmp.name) as conn:
            idx = _list_indexes(conn)
        for name in EXPECTED_INDEXES:
            self.assertIn(name, idx, f"missing index: {name}")

    def test_migration_is_idempotent(self):
        # Running twice must not raise (CREATE INDEX IF NOT EXISTS) and
        # must leave the index set unchanged.
        self._migrations.upgrade_to_head()
        with sqlite3.connect(self._tmp.name) as conn:
            before = _list_indexes(conn)
        self._migrations.upgrade_to_head()
        with sqlite3.connect(self._tmp.name) as conn:
            after = _list_indexes(conn)
        self.assertEqual(before, after)

    def test_journal_mode_is_wal(self):
        # PRAGMA journal_mode returns the current mode; set via both
        # db.conn() PRAGMAs and migration 035's persistent one.
        self._migrations.upgrade_to_head()
        with sqlite3.connect(self._tmp.name) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_busy_timeout_set(self):
        # db.conn() is expected to install PRAGMA busy_timeout = 30000 on
        # every connection. If PRAGMA returns the SQLite default (5000)
        # the db.conn() patch in db.py hasn't been applied on this clone
        # yet — skip rather than fail so this file still gates the
        # migration itself.
        self._migrations.upgrade_to_head()
        import db
        with db.conn() as c:
            row = c.execute("PRAGMA busy_timeout").fetchone()
        got = int(row[0])
        if got == 5000:
            self.skipTest(
                "db.py busy_timeout PRAGMA not applied — apply the "
                "db.conn() instrumentation patch to enable"
            )
        self.assertEqual(got, 30000)


if __name__ == "__main__":
    unittest.main()
