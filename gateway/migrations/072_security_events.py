"""Security events — audit trail of capture-attempt and anti-forensic signals.

Fed by POST /api/security/capture-attempt and any future client-side or
server-side detector. One row per event. High-volume usage (>5 events per
user per 10 min) triggers an admin alert at read time (no scheduled worker).

Schema is intentionally loose — ``metadata`` is opaque JSON so new event
types don't need a migration.
"""

from __future__ import annotations

import sqlite3


revision = "072"
down_revision = "071"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS security_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER
                         REFERENCES users(id) ON DELETE SET NULL,
            event_type   TEXT NOT NULL,
            metadata     TEXT NOT NULL DEFAULT '{}',
            ip           TEXT,
            user_agent   TEXT,
            created_at   INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_events_user "
        "ON security_events(user_id, created_at)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_events_type "
        "ON security_events(event_type, created_at)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_events_recent "
        "ON security_events(created_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_security_events_recent")
    c.execute("DROP INDEX IF EXISTS idx_security_events_type")
    c.execute("DROP INDEX IF EXISTS idx_security_events_user")
    c.execute("DROP TABLE IF EXISTS security_events")
