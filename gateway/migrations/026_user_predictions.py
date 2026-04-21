"""User-authored predictions + per-user scoring stats.

Subscribers can record their own predictions on any market and have them
scored on resolution. Adds:

  - `user_predictions`: one row per prediction a subscriber makes.
  - `user_prediction_stats`: aggregated stats per user (accuracy, Brier,
    timing, streaks, category breakdown). Recomputed on each resolution.

The existing `predictions` table (source-authored) is untouched. These
are a parallel pipeline keyed on user_id instead of source_handle.
Leaderboard integration reuses `user_accuracy` from migration 021 — we
mirror total/correct/accuracy into it so the leaderboard job can pick up
user-authored predictions without a second join path.
"""

revision = "031"
down_revision = "025"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_predictions (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                     INTEGER NOT NULL
                                        REFERENCES users(id) ON DELETE CASCADE,
            market_slug                 TEXT NOT NULL,
            market_question             TEXT NOT NULL DEFAULT '',
            category                    TEXT NOT NULL DEFAULT 'other',
            predicted_outcome           TEXT NOT NULL,
            predicted_probability       REAL NOT NULL,
            reasoning                   TEXT,
            created_at                  INTEGER NOT NULL,
            market_price_at_prediction  REAL,
            edge_at_prediction          REAL,
            is_public                   INTEGER NOT NULL DEFAULT 0,
            is_anonymous                INTEGER NOT NULL DEFAULT 0,
            resolved                    INTEGER NOT NULL DEFAULT 0,
            resolved_at                 INTEGER,
            resolved_correct            INTEGER,
            final_market_price          REAL,
            brier_score                 REAL,
            timing_score                REAL
        )
    """)
    # One active prediction per (user, market). Partial unique index lets
    # a user re-predict on the same market once their earlier one resolves.
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_upred_user_market_active "
        "ON user_predictions(user_id, market_slug) WHERE resolved = 0"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_upred_user "
        "ON user_predictions(user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_upred_market "
        "ON user_predictions(market_slug)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_upred_public "
        "ON user_predictions(is_public, created_at DESC) WHERE is_public = 1"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_upred_unresolved_market "
        "ON user_predictions(market_slug, resolved) WHERE resolved = 0"
    )

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_prediction_stats (
            user_id              INTEGER PRIMARY KEY
                                 REFERENCES users(id) ON DELETE CASCADE,
            total_predictions    INTEGER NOT NULL DEFAULT 0,
            resolved_predictions INTEGER NOT NULL DEFAULT 0,
            correct_predictions  INTEGER NOT NULL DEFAULT 0,
            accuracy             REAL,
            avg_brier_score      REAL,
            avg_timing_score     REAL,
            current_streak       INTEGER NOT NULL DEFAULT 0,
            best_streak          INTEGER NOT NULL DEFAULT 0,
            category_stats       TEXT NOT NULL DEFAULT '{}',
            last_computed_at     INTEGER
        )
    """)


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS user_prediction_stats")
    c.execute("DROP INDEX IF EXISTS idx_upred_unresolved_market")
    c.execute("DROP INDEX IF EXISTS idx_upred_public")
    c.execute("DROP INDEX IF EXISTS idx_upred_market")
    c.execute("DROP INDEX IF EXISTS idx_upred_user")
    c.execute("DROP INDEX IF EXISTS idx_upred_user_market_active")
    c.execute("DROP TABLE IF EXISTS user_predictions")
