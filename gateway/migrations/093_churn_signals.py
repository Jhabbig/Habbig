"""Churn risk scores — one row per user, recomputed nightly.

The nightly ``compute_churn_signals`` job recomputes every row of this
table for every subscribed user (tier != 'none'). The ``engagement_routes``
endpoint then reads the row to decide what in-app prompt to show, and the
admin /admin/churn dashboard aggregates ``risk_tier`` for the funnel view.

Primary key is user_id so re-running the job idempotently upserts each
user without accumulating duplicate rows. ``computed_at`` tells the admin
page when the data last refreshed — a stale score across all rows is a
signal that the cron didn't fire.

Schema notes:
  * ``risk_score`` is a float clamped to [0.0, 1.0] by the job — we don't
    enforce the range in SQL because SQLite's CHECK handling across
    migrations isn't worth the blast radius.
  * ``risk_tier`` is derived from ``risk_score`` and stored redundantly so
    the admin dashboard can GROUP BY without having to replay the
    clamping math in SQL.
  * ``engagement_trend`` is a human-readable label ('rising' / 'stable' /
    'declining' / 'dormant') — cheaper to read on the admin page than
    reconstructing it from recent_7d / prior_7d.

No indexes — this table is tiny (one row per subscriber) and every
admin query is a full scan by design.
"""

from __future__ import annotations

import logging
import sqlite3


revision = "093"
down_revision = "092"


log = logging.getLogger("migration.093")


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS churn_signals (
            user_id INTEGER PRIMARY KEY,
            last_login_at DATETIME,
            last_active_at DATETIME,
            days_since_last_active INTEGER,
            recent_7d_events INTEGER NOT NULL DEFAULT 0,
            prior_7d_events INTEGER NOT NULL DEFAULT 0,
            engagement_trend TEXT,  -- 'rising' | 'stable' | 'declining' | 'dormant'
            risk_score REAL,        -- 0.0 (healthy) to 1.0 (high churn risk)
            risk_tier TEXT,         -- 'healthy' | 'at_risk' | 'critical'
            computed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    # Admin dashboard groups by risk_tier for the pie-chart view; cheap
    # secondary index keeps that aggregate fast even when the subscriber
    # count grows past a few thousand.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_churn_tier "
        "ON churn_signals(risk_tier)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS churn_signals")
