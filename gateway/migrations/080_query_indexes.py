"""Performance: add indexes for hot query paths.

The prompt for this migration enumerated ~15 indexes. Most of them were
already in place (added incrementally across 035, 062, 070, 072) — this
migration only adds the ones actually missing as of 073:

  * ``user_market_views(user_id, market_slug)`` — composite lookup used
    by "have I notified this user about this market?" on resolution fire.
    Today only ``idx_umv_slug`` exists, so the per-user scan iterates
    every row for the market across all viewers.
  * ``predictions(market_id, extracted_at DESC)`` — compound used by
    /api/markets/{slug} to fetch the N most recent signals for a market.
    Today a single-column ``idx_predictions_market`` covers the lookup
    but the ORDER BY degrades to a full scan of that bucket.
  * ``predictions(category, resolved, extracted_at DESC)`` — used by the
    Best Bets feed (filter by category + open + sort by recency).
  * ``saved_predictions(user_id, saved_at DESC)`` — list-my-saves page,
    user-scoped ORDER BY saved_at degraded without the composite.
  * ``followed_sources(user_id, followed_at DESC)`` — list-my-follows,
    same shape.
  * ``source_credibility(accuracy_unlocked, global_credibility DESC)``
    partial index — ranked public leaderboard of sources.

Missing tables in this DB (documented so a future reviewer knows what
the prompt asked for but we intentionally skipped):
  * ``source_prediction_records`` — never created in this codebase.
  * ``notifications`` — there is no generic notifications table; the
    notify flags live on saved_predictions / user_market_views /
    followed_sources directly.

Every CREATE INDEX uses IF NOT EXISTS so re-running is safe and so a
parallel migration that added any of these first doesn't blow up.
"""

from __future__ import annotations

import logging
import sqlite3


revision = "080"
down_revision = "073"


log = logging.getLogger("migration.080")


# (index_name, required_table, required_columns, create_stmt). We skip
# any row whose table is missing or whose columns aren't all present,
# logging a warning so schema drift is visible but doesn't break the
# upgrade chain.
_INDEXES: list[tuple[str, str, tuple[str, ...], str]] = [
    (
        "idx_umv_user_market",
        "user_market_views",
        ("user_id", "market_slug"),
        "CREATE INDEX IF NOT EXISTS idx_umv_user_market "
        "ON user_market_views(user_id, market_slug)",
    ),
    (
        "idx_predictions_market_extracted",
        "predictions",
        ("market_id", "extracted_at"),
        "CREATE INDEX IF NOT EXISTS idx_predictions_market_extracted "
        "ON predictions(market_id, extracted_at DESC)",
    ),
    (
        "idx_predictions_cat_resolved_extracted",
        "predictions",
        ("category", "resolved", "extracted_at"),
        "CREATE INDEX IF NOT EXISTS idx_predictions_cat_resolved_extracted "
        "ON predictions(category, resolved, extracted_at DESC)",
    ),
    (
        "idx_saved_user_saved_at",
        "saved_predictions",
        ("user_id", "saved_at"),
        "CREATE INDEX IF NOT EXISTS idx_saved_user_saved_at "
        "ON saved_predictions(user_id, saved_at DESC)",
    ),
    (
        "idx_follow_user_followed_at",
        "followed_sources",
        ("user_id", "followed_at"),
        "CREATE INDEX IF NOT EXISTS idx_follow_user_followed_at "
        "ON followed_sources(user_id, followed_at DESC)",
    ),
    (
        # Partial index: the unlocked-only predicate scopes the b-tree to
        # the ~10% of rows that actually need sorting, not the whole
        # table. Matches the query shape in queries/sources.py.
        "idx_source_cred_unlocked_ranked",
        "source_credibility",
        ("accuracy_unlocked", "global_credibility"),
        "CREATE INDEX IF NOT EXISTS idx_source_cred_unlocked_ranked "
        "ON source_credibility(global_credibility DESC) "
        "WHERE accuracy_unlocked = 1",
    ),
]


def _table_columns(c: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def upgrade(c: sqlite3.Connection) -> None:
    for name, table, cols, stmt in _INDEXES:
        existing_cols = _table_columns(c, table)
        if not existing_cols:
            log.warning(
                "080_query_indexes: skip %s — table %s missing",
                name, table,
            )
            continue
        missing = [col for col in cols if col not in existing_cols]
        if missing:
            log.warning(
                "080_query_indexes: skip %s — %s missing column(s) %s",
                name, table, missing,
            )
            continue
        c.execute(stmt)


def downgrade(c: sqlite3.Connection) -> None:
    # Indexes are always safe to leave in place; dropping them would
    # re-introduce the exact performance regression this migration
    # exists to fix. If a future upgrade genuinely needs them gone, drop
    # them explicitly in a later migration.
    for name, _table, _cols, _stmt in _INDEXES:
        try:
            c.execute(f"DROP INDEX IF EXISTS {name}")
        except sqlite3.Error as exc:
            log.warning("080 downgrade: failed to drop %s — %s", name, exc)
