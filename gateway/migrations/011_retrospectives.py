"""Post-resolution retrospective analysis cache (F6).

When a market resolves, Claude generates a retrospective: how did narve.ai's
intelligence perform? Which sources called it? Who was wrong? The analysis
is stored here for in-app viewing and email delivery.
"""

revision = "011"
down_revision = "010"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS resolution_retrospectives (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id               TEXT NOT NULL,
            market_question         TEXT NOT NULL,
            outcome                 TEXT NOT NULL,
            betyc_consensus_was     REAL,
            market_price_was        REAL,
            edge_was                REAL,
            analysis_text           TEXT NOT NULL,
            top_correct_sources     TEXT,
            top_wrong_sources       TEXT,
            prediction_count        INTEGER NOT NULL DEFAULT 0,
            generated_at            INTEGER NOT NULL,
            generated_by            TEXT NOT NULL
        )
    """)
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_retro_market "
        "ON resolution_retrospectives(market_id)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_retro_market")
    c.execute("DROP TABLE IF EXISTS resolution_retrospectives")
