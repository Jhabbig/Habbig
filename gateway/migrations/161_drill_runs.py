"""Quarterly DB recovery-drill log.

One row per automated drill: take a live-DB snapshot, restore to a
throwaway copy, run integrity + foreign_key checks, compare row counts
for a handful of core tables, record the outcome.

The job itself lives in `jobs/db_maintenance.py::recovery_drill`,
scheduled every 90 days. This table is the audit trail so ops has
proof the restore path works BEFORE the day it's needed.

Columns:

* ``started_at`` / ``completed_at`` — wall-clock UTC seconds. A run
  that never writes ``completed_at`` is either still in flight or
  crashed mid-drill (alertable condition).

* ``integrity_ok`` / ``foreign_key_ok`` — booleans. Either False triggers
  an alert; historical False rows are how we trace a regression back to
  a specific schema change.

* ``users_live`` / ``users_restore`` + ``predictions_live`` /
  ``predictions_restore`` — row counts for two core tables on both
  sides. If they diverge by > 1% the drill is considered failed even
  when integrity_check says "ok" — the online .backup API is atomic at
  the row level, so a mismatch implies a broader problem (corrupt
  snapshot, stale backup, parallel INSERT storm during backup, etc.).

* ``notes`` — free-text; populated by the drill job with the actual
  mismatch numbers so the /admin/backups page has context without
  reopening the log file.
"""

from __future__ import annotations


revision = "161"
down_revision = "130"  # Last known landed migration as of 2026-04-23


def upgrade(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drill_runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at           INTEGER NOT NULL,
            completed_at         INTEGER,
            integrity_ok         INTEGER,
            foreign_key_ok       INTEGER,
            users_live           INTEGER,
            users_restore        INTEGER,
            predictions_live     INTEGER,
            predictions_restore  INTEGER,
            backup_source        TEXT,
            notes                TEXT
        )
    """)
    # Most /admin/backups queries want "latest 10 drills DESC", so order
    # the index to match.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_drill_runs_started "
        "ON drill_runs(started_at DESC)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_drill_runs_started")
    cur.execute("DROP TABLE IF EXISTS drill_runs")
