"""Performance: slow-query log table.

Receives writes from ``gateway.queries.query_tracer.SlowQueryTracer`` via
a background flush — never from the request hot path, so a slow write
here can never stall a user request. See the tracer module for the
fire-and-forget details.

Retention: trimming happens in ``queries.performance.trim_slow_query_log``.
We expect ~100-500 rows/day on narve's current load; keep 30 days of
history for the /admin/performance dashboard so slow-query drift is
visible week-over-week.

Schema notes:
  * ``query_signature`` is a normalized form of the SQL — literal values
    stripped, whitespace collapsed — so the admin page can group
    "10 000 point-lookups of users by id" into a single row. The tracer
    computes the signature before writing.
  * ``endpoint`` is populated when the tracer can derive it from the
    active request context (``contextvars.ContextVar``). Background jobs
    and cron tasks write the job name instead.
  * ``user_id`` is nullable — anonymous requests write NULL rather than
    a sentinel so the FK stays clean.
"""

from __future__ import annotations

import sqlite3


revision = "081"
down_revision = "080"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS slow_query_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            query            TEXT NOT NULL,
            query_signature  TEXT NOT NULL,
            duration_ms      INTEGER NOT NULL,
            rowcount         INTEGER,
            endpoint         TEXT,
            user_id          INTEGER,
            timestamp        INTEGER NOT NULL
        )
        """
    )
    # Admin dashboard reads always sort by recency, so the (ts DESC)
    # index covers "last 24h" and "last 5 min" windows equally well.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_slow_query_log_ts "
        "ON slow_query_log(timestamp DESC)"
    )
    # Signature index powers the "top 20 slowest query shapes" grouping
    # on /admin/performance; without it, every page load triggers a full
    # scan plus sort.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_slow_query_log_signature "
        "ON slow_query_log(query_signature, timestamp DESC)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS slow_query_log")
