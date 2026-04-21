"""engagement events + prompt dismissals for churn-signal detection.

Two small append-only tables that power the engagement / re-engagement
pipeline built in jobs/compute_churn_signals.py and engagement_routes.py:

  * ``engagement_events`` — one row per user action. Written fire-and-forget
    by gateway/engagement.py and read by the nightly churn-score job. We
    need a compound index on (user_id, created_at DESC) so the 7d / 14d
    window aggregates don't full-scan, and one on (event_type, created_at DESC)
    so admin analytics (funnel-style) stay fast.

  * ``engagement_prompt_dismissals`` — records that a user dismissed a
    specific prompt tier. GET /api/engagement/prompt consults this to
    enforce the 7-day cooldown. A single row per (user_id, prompt_tier)
    is all we need; we overwrite ``dismissed_at`` on re-dismissal.

The ``metadata`` column is plain TEXT carrying JSON — kept schema-less so
instrumentation can evolve without schema churn. Readers that care about
a specific field decode on demand.

IF NOT EXISTS everywhere so re-running is safe if a sibling agent's work
lands in between.
"""

from __future__ import annotations

import logging
import sqlite3


revision = "092"
down_revision = "091"


log = logging.getLogger("migration.092")


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS engagement_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            -- 'login' | 'feed_view' | 'market_detail_view' | 'save' | 'follow'
            -- | 'prediction_made' | 'signal_search' | 'intelligence_query'
            -- | 'click_notification'
            metadata TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_engagement_user_time "
        "ON engagement_events(user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_engagement_type_time "
        "ON engagement_events(event_type, created_at DESC)"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS engagement_prompt_dismissals (
            user_id INTEGER NOT NULL,
            prompt_tier TEXT NOT NULL,  -- 'at_risk' | 'critical'
            dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, prompt_tier),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS engagement_prompt_dismissals")
    c.execute("DROP TABLE IF EXISTS engagement_events")
