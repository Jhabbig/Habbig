"""Weekly intelligence report (PDF) metadata.

WeasyPrint generates the PDF; this table tracks generation state, the
path on disk, attachment status, and a rendered html_excerpt used by
the in-app viewer (so the list view doesn't have to re-render PDFs).

Cron ``generate_weekly_reports`` runs every Monday 07:00 UTC, one hour
before the weekly digest email batch so the attachment is ready.
"""

revision = "057"
down_revision = "056"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER NOT NULL,
            period_start       INTEGER NOT NULL,
            period_end         INTEGER NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending',
            pdf_path           TEXT,
            html_excerpt       TEXT,
            error_message      TEXT,
            created_at         INTEGER NOT NULL,
            completed_at       INTEGER,
            emailed_at         INTEGER,
            UNIQUE(user_id, period_start)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_reports_user ON weekly_reports(user_id, period_start DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_weekly_reports_status ON weekly_reports(status, created_at)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_weekly_reports_status")
    c.execute("DROP INDEX IF EXISTS idx_weekly_reports_user")
    c.execute("DROP TABLE IF EXISTS weekly_reports")
