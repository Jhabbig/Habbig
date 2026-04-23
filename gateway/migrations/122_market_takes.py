"""Community Takes — paid subscribers annotate markets with analysis.

Two tables:
  - `market_takes`: one row per (user, market). Stores position, confidence,
    reasoning, vote totals, and a computed quality_score. `resolved_correct`
    is written by the daily resolution job (migration 124 / take_resolution).
  - `take_votes`: one row per (user, take). `vote ∈ {-1, +1}`; clearing a
    vote deletes the row rather than storing 0, so the aggregate query
    stays trivial.

Idempotent: every CREATE is `IF NOT EXISTS`.
"""

revision = "122"
down_revision = "121"


def upgrade(c):
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS market_takes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
            market_slug      TEXT NOT NULL,
            position         TEXT NOT NULL CHECK (position IN ('yes','no','neutral')),
            confidence       INTEGER CHECK (confidence IS NULL OR (confidence BETWEEN 1 AND 10)),
            reasoning        TEXT NOT NULL,
            created_at       INTEGER NOT NULL,
            edited_at        INTEGER,
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            upvotes          INTEGER NOT NULL DEFAULT 0,
            downvotes        INTEGER NOT NULL DEFAULT 0,
            quality_score    REAL,
            resolved_correct INTEGER,
            shadow_hidden    INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Hot path: list all takes for a market, newest first — index covers both
    # the default "newest" sort and the quality-score sort (falls back to
    # seqscan with tiny n once quality_score is NULL, which is fine).
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_takes_market "
        "ON market_takes(market_slug, created_at DESC)"
    )
    # List takes by a single user — /settings/takes + public profile.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_takes_user "
        "ON market_takes(user_id, created_at DESC)"
    )
    # Enforce the one-take-per-(user, market) invariant at the DB level so a
    # concurrent double-submit races to the UNIQUE conflict, not to two rows.
    # Partial index excludes soft-deleted rows so a user can re-post after
    # deleting their previous take.
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_takes_user_market "
        "ON market_takes(user_id, market_slug) WHERE is_deleted = 0"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS take_votes (
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            take_id  INTEGER NOT NULL REFERENCES market_takes(id) ON DELETE CASCADE,
            vote     INTEGER NOT NULL CHECK (vote IN (-1, 1)),
            voted_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, take_id)
        )
        """
    )
    # Reverse index for aggregation when recomputing a take's totals.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_take_votes_take "
        "ON take_votes(take_id)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS take_votes")
    c.execute("DROP TABLE IF EXISTS market_takes")
