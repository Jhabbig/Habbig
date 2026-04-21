"""Claude API usage + cost tracking.

Every Claude call the gateway makes is logged here with input/output tokens,
computed USD cost, the feature that triggered it, and whether it was served
from cache. The admin panel reads this table to surface daily spend, per-
feature breakdown, and cache-hit-rate metrics, and the daily spend-alert
cron compares yesterday's total against a configurable threshold.

Schema:
  - claude_usage_log: one row per Claude request (including cache hits,
    which log 0 tokens and 0 cost so the denominator for hit-rate math is
    correct).
"""

revision = "025"
down_revision = "021"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS claude_usage_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       INTEGER NOT NULL,
            feature         TEXT NOT NULL,
            model           TEXT NOT NULL,
            input_tokens    INTEGER NOT NULL DEFAULT 0,
            output_tokens   INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            cached_hit      INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Admin panel queries filter by day and feature; index accordingly.
    c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_ts ON claude_usage_log(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_feature_ts ON claude_usage_log(feature, timestamp)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_claude_usage_feature_ts")
    c.execute("DROP INDEX IF EXISTS idx_claude_usage_ts")
    c.execute("DROP TABLE IF EXISTS claude_usage_log")
