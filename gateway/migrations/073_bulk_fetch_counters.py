"""Bulk-fetch rate-limit counters — per-user, per-hour row budget.

Every list endpoint that returns >20 items writes to this table via the
``BulkDataRateLimitMiddleware``. Rows are keyed by ``(user_id, window_start)``
where ``window_start`` is the unix timestamp of the current hour bucket.

Budget rules (enforced in middleware):
  - >5000 rows in 1h      → 429 until the hour rolls
  - >20000 rows in 24h    → flagged for manual review (``flagged=1``)

The ``flagged`` column is set inline when a threshold is crossed so an
admin can review via /admin/security/bulk-fetches without a join.
"""

from __future__ import annotations

import sqlite3


revision = "073"
down_revision = "072"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bulk_fetch_counters (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL
                          REFERENCES users(id) ON DELETE CASCADE,
            window_start  INTEGER NOT NULL,
            rows_fetched  INTEGER NOT NULL DEFAULT 0,
            endpoint_hits INTEGER NOT NULL DEFAULT 0,
            flagged       INTEGER NOT NULL DEFAULT 0,
            last_updated  INTEGER NOT NULL,
            UNIQUE(user_id, window_start)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_bulk_fetch_counters_user "
        "ON bulk_fetch_counters(user_id, window_start)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_bulk_fetch_counters_flagged "
        "ON bulk_fetch_counters(flagged, window_start)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_bulk_fetch_counters_flagged")
    c.execute("DROP INDEX IF EXISTS idx_bulk_fetch_counters_user")
    c.execute("DROP TABLE IF EXISTS bulk_fetch_counters")
