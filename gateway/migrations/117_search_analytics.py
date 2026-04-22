"""Search analytics — capture what users search for + what they click.

Two useful signals fall out of this:
  1. Zero-result queries — surface content gaps (admin sees the list in
     /admin/search-analytics so they know what's missing from the
     index or from the product).
  2. Common queries + click-through — which results are actually useful,
     which ones never get clicked.

One row per query (logged at /api/search). A second UPDATE fires from
/api/search/click with the clicked result's type + id. Queries without
clicks stay NULL in the click columns so we can compute no-click rates.

Columns kept narrow on purpose: we only log what's needed to fix search
quality, not telemetry for its own sake. user_id is nullable so
anon/public searches can still populate the analytics table.
"""

from __future__ import annotations


revision = "117"
down_revision = "116"


def upgrade(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER,
            query                TEXT NOT NULL,
            result_count         INTEGER NOT NULL,
            clicked_result_type  TEXT,
            clicked_result_id    TEXT,
            clicked_at           INTEGER,
            ts                   INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        )
    """)
    # Partial index: only non-null queries, helps the "top last 7d" page.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_search_queries_ts ON search_queries(ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_search_queries_query_ts ON search_queries(query, ts DESC)")
    # Zero-result lookup index — a common admin dashboard query.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_search_queries_zero ON search_queries(ts DESC) WHERE result_count = 0")


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_search_queries_zero")
    cur.execute("DROP INDEX IF EXISTS idx_search_queries_query_ts")
    cur.execute("DROP INDEX IF EXISTS idx_search_queries_ts")
    cur.execute("DROP TABLE IF EXISTS search_queries")
