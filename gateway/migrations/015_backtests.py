"""Backtest results table (F13).

Stores parameters and results of historical trading simulations. Backtests
run as async jobs and results are polled via the API.
"""

revision = "015"
down_revision = "014"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS backtests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            params          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            result          TEXT,
            created_at      INTEGER NOT NULL,
            completed_at    INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_backtests_user ON backtests(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_backtests_status ON backtests(status)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_backtests_status")
    c.execute("DROP INDEX IF EXISTS idx_backtests_user")
    c.execute("DROP TABLE IF EXISTS backtests")
