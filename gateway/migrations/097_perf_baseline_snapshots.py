"""Nightly perf-baseline snapshot storage.

``jobs/perf_baseline.py`` samples the duration of N hot-read queries
each night and writes the distribution here. The admin performance
dashboard reads the last 30 days to render a sparkline per endpoint
and an alert when any endpoint's 7-day p95 regresses > 20% from the
prior week.

We deliberately store a single row per (endpoint, timestamp) with the
percentiles pre-computed, rather than the raw sample list. A sparkline
needs p50/p95/p99 — not 100 data points — and the smaller row size
keeps the table bounded even on a multi-year horizon.
"""

from __future__ import annotations

import sqlite3


revision = "097"
down_revision = "096"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS perf_baseline_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER NOT NULL,
            endpoint        TEXT NOT NULL,
            sample_count    INTEGER NOT NULL,
            p50_ms          REAL,
            p95_ms          REAL,
            p99_ms          REAL,
            max_ms          REAL,
            error_count     INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_baseline_endpoint_ts "
        "ON perf_baseline_snapshots(endpoint, timestamp DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_baseline_ts "
        "ON perf_baseline_snapshots(timestamp DESC)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_perf_baseline_ts")
    c.execute("DROP INDEX IF EXISTS idx_perf_baseline_endpoint_ts")
    c.execute("DROP TABLE IF EXISTS perf_baseline_snapshots")
