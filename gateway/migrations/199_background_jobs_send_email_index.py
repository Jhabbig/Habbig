"""Composite index on background_jobs(name, status, enqueued_at DESC).

Performance audit (2026-05-15) flagged the `/admin/email-addresses`
aggregator as a Priority-1 hot path. The aggregator scans
``background_jobs WHERE name = 'send_email'`` ordered by
``enqueued_at DESC`` to build per-recipient send stats. With ~2000
rows and no covering index, the planner falls back to a full table
scan plus a transient sort — O(n) walk + O(n log n) sort on every
page load.

The composite index ``(name, status, enqueued_at DESC)`` lets SQLite:

  1. Seek directly to the ``name = 'send_email'`` prefix
     (the cardinality killer — ~95%% of rows match today, but the
     planner still benefits from the seek).
  2. Filter by status without leaving the index.
  3. Walk the ``enqueued_at DESC`` suffix in order, skipping the
     sort step entirely.

Estimated 5-10x speedup on the aggregator endpoint. Index is small
(~80KB at current row count) and the write-amplification cost on
background_jobs inserts is negligible — that table sees <10 writes/s
peak from the email enqueuer.

Idempotent via ``IF NOT EXISTS`` — safe to re-run, and matches the
hot-applied production index created out-of-band on 2026-05-15.

Lazy-table guard (bug-hunt iteration 1, 2026-05-16):
``background_jobs`` is created lazily by ``jobs/backend.py``
(``_ensure_jobs_table``) at the first job enqueue, NOT by a prior
migration. Fresh DBs (e.g. test DBs that never enqueue a job, or
brand-new prod installs) therefore don't have the table when the
migration runner imports this file. We mirror the canonical CREATE
TABLE from ``jobs/backend.py`` here so the index can always be
created. Both statements are ``IF NOT EXISTS`` so re-running is
safe and existing DBs (incl. prod, which has the table already)
are untouched.
"""

from __future__ import annotations

revision = "199"
down_revision = "198"


def upgrade(c):
    # Mirror the canonical schema from jobs/backend.py::_ensure_jobs_table.
    # Keep this in sync if that table shape changes — same columns, same
    # types, same defaults, including the payload_hmac column from
    # migration 192.
    c.execute("""
        CREATE TABLE IF NOT EXISTS background_jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            payload        TEXT,
            payload_hmac   TEXT,
            status         TEXT NOT NULL DEFAULT 'queued',
            attempts       INTEGER NOT NULL DEFAULT 0,
            max_attempts   INTEGER NOT NULL DEFAULT 3,
            error          TEXT,
            result         TEXT,
            enqueued_at    INTEGER NOT NULL,
            started_at     INTEGER,
            finished_at    INTEGER,
            duration_ms    INTEGER
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_send_email "
        "ON background_jobs(name, status, enqueued_at DESC)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_background_jobs_send_email")
