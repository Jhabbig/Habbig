"""Index to speed up credibility recomputation queries.

The recompute job queries `predictions WHERE resolved = 1` grouped by
source_handle. Without this composite index, every 6-hour recompute does a
full table scan. With it, SQLite walks the index directly.

Also adds a compound index on (resolved, market_id) for the resolution
auto-detection job (F2) which queries unresolved predictions grouped by
market.

Additive only — safe to re-run.
"""

revision = "010"
down_revision = "009"


def upgrade(c):
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_resolved_source "
        "ON predictions(resolved, source_handle)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_resolved_market "
        "ON predictions(resolved, market_id)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_predictions_resolved_source")
    c.execute("DROP INDEX IF EXISTS idx_predictions_resolved_market")
