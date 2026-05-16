"""Add visitor_id column to analytics_events for cookie-based grouping.

Why
---
Until now ``analytics_events`` could only group anonymous traffic by
``ip_hash``, which collapses everyone behind a single NAT / household
into one "visitor" and conversely splits a single user across networks
(home -> office -> mobile). The ``narve_visitor`` cookie is a stable
opaque ID minted on first hit and persists across sessions, so events
keyed by it cluster correctly per browser.

The column is additive and nullable — existing rows stay valid with a
NULL visitor_id, and old beacon clients that don't carry the cookie
yet still record successfully. Aggregations that want cookie-based
uniques should filter ``WHERE visitor_id IS NOT NULL``.

Index
-----
``idx_analytics_visitor`` on ``(visitor_id, created_at DESC)`` covers
the per-visitor event-stream lookups (admin "what did this visitor do
in the last 24h?" queries and per-visitor funnel rollups) without a
table scan.

Idempotent — re-running is safe; the column-add is skipped if the
column is already present, and the index uses ``IF NOT EXISTS``.
"""

from __future__ import annotations

revision = "200"
down_revision = "199"


def upgrade(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(analytics_events)").fetchall()}
    if "visitor_id" not in cols:
        c.execute("ALTER TABLE analytics_events ADD COLUMN visitor_id TEXT")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_analytics_visitor "
        "ON analytics_events(visitor_id, created_at DESC)"
    )


def downgrade(c):
    # No-op: SQLite can only drop columns by rebuilding the table, and
    # we want to keep visitor_id (and the historical data captured in
    # it) even if a future migration supersedes the schema. The index
    # is similarly cheap to retain.
    pass
