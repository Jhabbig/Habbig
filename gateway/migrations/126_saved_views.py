"""Saved views — named, persisted filter sets scoped to markets/feed/sources/predictions.

One row per user-named view. ``scope`` is the data surface being filtered
(``markets`` | ``feed`` | ``sources`` | ``predictions``); ``filter_json`` is
the exact shape the filter validator consumes. ``is_default`` flips one view
per (user, scope) as the default landing state for that tab, enforced by the
partial unique index below. ``is_pinned`` surfaces the view in the sidebar.

Additive migration — nothing existing is touched. Safe to re-run.
"""

revision = "126"
down_revision = "120"


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def upgrade(c):
    if not _table_exists(c, "saved_views"):
        c.execute("""
            CREATE TABLE saved_views (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                scope        TEXT NOT NULL,
                name         TEXT NOT NULL,
                filter_json  TEXT NOT NULL,
                is_default   INTEGER NOT NULL DEFAULT 0,
                is_pinned    INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_views_user_scope "
            "ON saved_views(user_id, scope)"
        )
        # At most one default per (user, scope) — set-default endpoint
        # clears others in the same transaction before flipping the target.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_views_default_per_scope "
            "ON saved_views(user_id, scope) WHERE is_default = 1"
        )
        # Pinned views are listed in the sidebar — this index keeps the
        # ORDER BY created_at scan O(pinned) instead of O(all views).
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_views_pinned "
            "ON saved_views(user_id, is_pinned, created_at) "
            "WHERE is_pinned = 1"
        )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_saved_views_pinned")
    c.execute("DROP INDEX IF EXISTS uq_saved_views_default_per_scope")
    c.execute("DROP INDEX IF EXISTS idx_saved_views_user_scope")
    c.execute("DROP TABLE IF EXISTS saved_views")
