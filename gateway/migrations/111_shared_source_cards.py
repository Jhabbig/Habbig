"""Shareable source-profile cards.

Parallel to ``shared_market_cards`` (migration 110). The shared
artifact is a signed, expiring URL /s/s/{token} that renders a public
snapshot of a source's credibility + accuracy. Full profile access
still requires an invite — the shared page is a teaser, not a
replacement.

The ``source_handle`` column is text because a source might not yet
have a ``source_credibility`` row (for example, a newly-tracked
handle with zero resolved predictions). We intentionally don't FK
against ``sources`` so that pruning a stale source doesn't invalidate
a link that was valid when it was shared.
"""

from __future__ import annotations

import sqlite3


revision = "111"
down_revision = "110"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_source_cards (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token           TEXT NOT NULL UNIQUE,
            source_handle   TEXT NOT NULL,
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
        "CREATE INDEX IF NOT EXISTS idx_shared_source_token "
        "ON shared_source_cards(token)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_source_user "
        "ON shared_source_cards(sharer_user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_source_expires "
        "ON shared_source_cards(expires_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS shared_source_cards")
