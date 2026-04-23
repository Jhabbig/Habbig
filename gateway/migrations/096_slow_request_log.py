"""Request-timing log for the RequestTiming middleware.

Every HTTP request gets timed. Only requests that cross a slow-request
threshold (default 500 ms) persist here — fast requests would drown the
log in noise and bloat the WAL. The admin performance dashboard reads
from this table alongside ``slow_query_log`` (migration 081).

Retention is policy, not enforced: a nightly trim in
``jobs.db_maintenance`` keeps the last 30 days.

The middleware is fire-and-forget — a failed write here never blocks
the response. See ``middleware/perf.py``.
"""

from __future__ import annotations

import sqlite3


revision = "096"
down_revision = "095"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS slow_request_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER NOT NULL,
            path            TEXT    NOT NULL,
            method          TEXT    NOT NULL,
            status_code     INTEGER NOT NULL,
            duration_ms     INTEGER NOT NULL,
            user_id         INTEGER,
            ip_hash         TEXT,
            user_agent_kind TEXT
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_slow_req_log_ts "
        "ON slow_request_log(timestamp DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_slow_req_log_path_ts "
        "ON slow_request_log(path, timestamp DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_slow_req_log_duration "
        "ON slow_request_log(duration_ms DESC)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_slow_req_log_duration")
    c.execute("DROP INDEX IF EXISTS idx_slow_req_log_path_ts")
    c.execute("DROP INDEX IF EXISTS idx_slow_req_log_ts")
    c.execute("DROP TABLE IF EXISTS slow_request_log")
