"""Forensic watermark seeds — per (user, session) steganographic seed.

Every authenticated page renders an invisible canvas watermark keyed by a
32-bit seed derived from user_id + session_id. When a screenshot leaks, the
forensics recovery tool (``gateway/forensics/extract_watermark.py``) walks
this table, applies each user's seed to the image, and returns the match.

One row per (user_id, session_id). Rotated whenever the session rotates.
"""

from __future__ import annotations

import sqlite3


revision = "070"
down_revision = "064"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS watermark_seeds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
            session_id      TEXT NOT NULL,
            seed            INTEGER NOT NULL,
            generated_at    INTEGER NOT NULL,
            last_seen_at    INTEGER NOT NULL,
            UNIQUE(user_id, session_id)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_watermark_seeds_user "
        "ON watermark_seeds(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_watermark_seeds_session "
        "ON watermark_seeds(session_id)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_watermark_seeds_session")
    c.execute("DROP INDEX IF EXISTS idx_watermark_seeds_user")
    c.execute("DROP TABLE IF EXISTS watermark_seeds")
