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
"""

from __future__ import annotations

revision = "199"
down_revision = "198"


def upgrade(c):
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_send_email "
        "ON background_jobs(name, status, enqueued_at DESC)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_background_jobs_send_email")
