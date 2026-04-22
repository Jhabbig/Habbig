"""Shareable market-card artifacts.

Every row is a signed, expiring URL of the shape /s/m/{token}. The
``token`` column stores the signature so a lookup by URL is a single
index hit — see ``gateway.share_tokens`` for the HMAC wrapping.

Attribution model:
  * ``sharer_user_id`` links back to the user who created the card.
    Deleting that user cascades the row (a share only makes sense
    while its creator exists).
  * ``referrals`` (migration 023) gets a new row when an invitee
    signs up after landing on a shared card — the wiring lives in
    ``routes_sharing.track_share_signup`` rather than here.

Expiry: enforced at read time by comparing ``expires_at`` to
``time.time()``. A background cron can prune expired rows, but no
cleanup is required for correctness — expired rows simply 404.
"""

from __future__ import annotations

import sqlite3


revision = "110"
down_revision = "105"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_market_cards (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token           TEXT NOT NULL UNIQUE,
            market_slug     TEXT NOT NULL,
            sharer_user_id  INTEGER NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
            sharer_handle   TEXT,
            created_at      INTEGER NOT NULL,
            expires_at      INTEGER NOT NULL,
            view_count      INTEGER NOT NULL DEFAULT 0,
            last_viewed_at  INTEGER
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_market_token "
        "ON shared_market_cards(token)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_market_user "
        "ON shared_market_cards(sharer_user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_market_expires "
        "ON shared_market_cards(expires_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS shared_market_cards")
