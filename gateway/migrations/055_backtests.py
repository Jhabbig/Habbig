"""Backtesting engine — async historical replay.

Tolerates an earlier branch shipping a simpler ``backtests`` table
(migration 015_backtests.py). This migration adds the newer
``backtest_runs`` + ``backtest_comparisons`` tables used by the Pro
backtest UI introduced in the intelligence layer pass.

  backtest_runs
    One row per backtest job. status flows queued → running → done|failed.
    params_json holds the full strategy spec; result_json holds the equity
    curve + summary stats (win rate, ROI, Sharpe, max drawdown).

  backtest_comparisons
    Saved comparisons let the Pro UI overlay equity curves from N runs on
    one chart. Pure presentation layer — no compute here.
"""

revision = "055"
down_revision = "054"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            name            TEXT NOT NULL,
            params_json     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'queued',
            result_json     TEXT,
            error_message   TEXT,
            bet_count       INTEGER NOT NULL DEFAULT 0,
            final_bankroll  REAL,
            roi_pct         REAL,
            win_rate        REAL,
            sharpe          REAL,
            max_drawdown    REAL,
            created_at      INTEGER NOT NULL,
            started_at      INTEGER,
            completed_at    INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_user ON backtest_runs(user_id, created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_status ON backtest_runs(status, created_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS backtest_comparisons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            name            TEXT NOT NULL,
            run_ids_json    TEXT NOT NULL,
            created_at      INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_backtest_comparisons_user ON backtest_comparisons(user_id)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_backtest_comparisons_user")
    c.execute("DROP TABLE IF EXISTS backtest_comparisons")
    c.execute("DROP INDEX IF EXISTS idx_backtest_runs_status")
    c.execute("DROP INDEX IF EXISTS idx_backtest_runs_user")
    c.execute("DROP TABLE IF EXISTS backtest_runs")
