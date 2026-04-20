"""Weekly intelligence reports — storage for generated Pro reports.

Each row tracks one generated PDF report for one user for one week.
The PDF itself is stored on disk at `reports/{user_id}/{week_start}.pdf`
relative to the gateway root. Delivered via email and viewable in-app.

Additive only — safe to re-run.
"""

revision = "019"
down_revision = "018"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            week_start      INTEGER NOT NULL,
            week_end        INTEGER NOT NULL,
            generated_at    INTEGER NOT NULL,
            delivered_at    INTEGER,
            pdf_path        TEXT,

            -- Summary stats for quick display in the reports list
            best_bets_correct   INTEGER DEFAULT 0,
            best_bets_total     INTEGER DEFAULT 0,
            simulated_roi_pct   REAL DEFAULT 0.0,
            top_signal_market   TEXT,
            top_source_handle   TEXT,
            total_predictions   INTEGER DEFAULT 0,
            total_markets       INTEGER DEFAULT 0,
            high_cred_accuracy  REAL,

            UNIQUE(user_id, week_start)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_reports_user
        ON weekly_reports(user_id, week_start DESC)
    """)


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS weekly_reports")
    c.execute("DROP INDEX IF EXISTS idx_weekly_reports_user")
