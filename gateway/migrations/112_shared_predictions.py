"""Shareable resolved-prediction cards.

Only predictions whose ``resolved_correct = 1`` are shareable. The
creator route enforces that invariant at insert time — we still store
``user_prediction_id`` here without a CHECK constraint so a future
relaxation (e.g. "share any resolved prediction for calibration
transparency") doesn't need a schema migration.

Why separate from shared_market_cards / shared_source_cards:
  * ``user_prediction_id`` is a first-class FK into
    ``user_predictions`` (migration 026); the market-card / source-card
    types have no comparable constraint.
  * A user can share the SAME market multiple times (different
    probability, different moment), so each prediction share gets its
    own row even for the same underlying market.
"""

from __future__ import annotations

import sqlite3


revision = "112"
down_revision = "111"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_predictions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            token               TEXT NOT NULL UNIQUE,
            user_prediction_id  INTEGER NOT NULL
                                REFERENCES user_predictions(id) ON DELETE CASCADE,
            sharer_user_id      INTEGER NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
            sharer_handle       TEXT,
            created_at          INTEGER NOT NULL,
            expires_at          INTEGER NOT NULL,
            view_count          INTEGER NOT NULL DEFAULT 0,
            last_viewed_at      INTEGER
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_prediction_token "
        "ON shared_predictions(token)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_prediction_user "
        "ON shared_predictions(sharer_user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_prediction_expires "
        "ON shared_predictions(expires_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS shared_predictions")
