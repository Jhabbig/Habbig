"""Forensic sentinels + per-user data-signing seed.

Two related tables:

  user_forensic_seeds
    One row per user. ``seed`` drives the decimal-precision watermark
    (which digit is perturbed, by how much) and the row-order shuffle for
    list endpoints with no canonical sort. ``rotation_version`` lets the
    admin flip the signing scheme if the old one is burned.

  sentinel_predictions
    Plausible-looking synthetic rows we inject into large list responses.
    One-to-many per user. When a sentinel shows up in a leak, that user
    is the source.

Both tables are append-only in steady state; cleaned up on account delete
via the ON DELETE CASCADE on users.
"""

from __future__ import annotations

import sqlite3


revision = "071"
down_revision = "070"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_forensic_seeds (
            user_id           INTEGER PRIMARY KEY
                              REFERENCES users(id) ON DELETE CASCADE,
            seed              INTEGER NOT NULL,
            rotation_version  INTEGER NOT NULL DEFAULT 1,
            created_at        INTEGER NOT NULL,
            updated_at        INTEGER NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sentinel_predictions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL
                           REFERENCES users(id) ON DELETE CASCADE,
            sentinel_id    TEXT NOT NULL,
            endpoint       TEXT NOT NULL,
            payload_json   TEXT NOT NULL,
            injected_at    INTEGER NOT NULL,
            expires_at     INTEGER,
            UNIQUE(user_id, sentinel_id)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_sentinel_predictions_user "
        "ON sentinel_predictions(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_sentinel_predictions_endpoint "
        "ON sentinel_predictions(endpoint)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_sentinel_predictions_endpoint")
    c.execute("DROP INDEX IF EXISTS idx_sentinel_predictions_user")
    c.execute("DROP TABLE IF EXISTS sentinel_predictions")
    c.execute("DROP TABLE IF EXISTS user_forensic_seeds")
