"""Realtime connection events — observability table for /ws.

Every connect, disconnect, subscribe, unsubscribe, and denied event is
written here. Purely for debugging + the admin live-stats panel — no
business logic reads from it. Indexed on ``ts DESC`` so the admin panel
can ``ORDER BY ts DESC LIMIT 500`` without a table scan.

Kept wide (user_id nullable, channel nullable, code nullable) so the
same table captures:
  - connect (user_id set, no channel/code)
  - denied (user_id may be NULL if auth failed, code set, reason set)
  - subscribe / unsubscribe (user_id + channel set)
  - disconnect (user_id + reason set)

Sized deliberately small. A 30-day cron should prune old rows; for now
the row is tiny (<100 bytes) so even at 10k events/day the table stays
under 10 MB/year.
"""

from __future__ import annotations


revision = "100"
down_revision = "095"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_connection_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            event       TEXT    NOT NULL,
            channel     TEXT,
            code        INTEGER,
            reason      TEXT,
            ip          TEXT,
            ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rt_events_ts ON realtime_connection_events(ts DESC)"
    )
    # Per-user lookup for the admin user-detail panel.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rt_events_user ON realtime_connection_events(user_id, ts DESC)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_rt_events_user")
    cur.execute("DROP INDEX IF EXISTS idx_rt_events_ts")
    cur.execute("DROP TABLE IF EXISTS realtime_connection_events")
