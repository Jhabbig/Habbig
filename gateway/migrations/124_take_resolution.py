"""Take resolution — index + run log for the daily resolver job.

The resolver (`jobs.take_resolution_jobs.resolve_takes_for_finished_markets`)
walks every market that resolved in the last 24h and stamps
`market_takes.resolved_correct` ∈ {1, 0} on each take based on whether the
take's position matched the market's outcome. It also recomputes
`quality_score` with the correctness multiplier.

Schema additions:
  - Partial index on `market_takes(market_slug)` WHERE resolved_correct IS
    NULL — lets the resolver fetch "un-scored takes for this market" in O(1).
  - `take_resolution_runs` table — audit log of every resolver run. Useful
    for debugging "why did my take not get scored?" and for the admin
    health dashboard.

No destructive operations: pure additive.
"""

revision = "124"
down_revision = "123"


def upgrade(c):
    # Resolver hot path.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_takes_unresolved "
        "ON market_takes(market_slug) WHERE resolved_correct IS NULL "
        "AND is_deleted = 0"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS take_resolution_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        INTEGER NOT NULL,
            finished_at       INTEGER,
            markets_considered INTEGER NOT NULL DEFAULT 0,
            takes_resolved    INTEGER NOT NULL DEFAULT 0,
            takes_correct     INTEGER NOT NULL DEFAULT 0,
            takes_incorrect   INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'running',
            error             TEXT
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_take_resolution_started "
        "ON take_resolution_runs(started_at DESC)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS take_resolution_runs")
    c.execute("DROP INDEX IF EXISTS idx_takes_unresolved")
