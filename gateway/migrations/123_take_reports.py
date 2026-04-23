"""Take reports — user-flagged content queue for /admin/moderation.

A take can be reported by many users (one row per (reporter, take)). Admins
work the queue from oldest unresolved report; `resolved=1` keeps the row for
audit + analytics but drops it from the default admin view.
"""

revision = "123"
down_revision = "122"


def upgrade(c):
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS take_reports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            take_id          INTEGER NOT NULL REFERENCES market_takes(id) ON DELETE CASCADE,
            reporter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason           TEXT NOT NULL,
            details          TEXT,
            reported_at      INTEGER NOT NULL,
            resolved         INTEGER NOT NULL DEFAULT 0,
            admin_action     TEXT,
            resolved_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            resolved_at      INTEGER
        )
        """
    )
    # One report per (reporter, take) — clicking "Report" twice is a no-op,
    # but different users can each report the same take.
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_take_reports_reporter_take "
        "ON take_reports(reporter_user_id, take_id)"
    )
    # Admin queue query: `WHERE resolved = 0 ORDER BY reported_at ASC`.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_take_reports_queue "
        "ON take_reports(resolved, reported_at)"
    )
    # Reverse index for "show all reports on this take" in the admin drawer.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_take_reports_take "
        "ON take_reports(take_id)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS take_reports")
