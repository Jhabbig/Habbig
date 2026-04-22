"""Collections — Spotify-style playlists for markets/sources/predictions.

Adds:

  ``collections``
      One row per user-curated playlist. (owner_user_id, slug) is unique
      so the public URL ``/c/{handle}/{slug}`` resolves to exactly one
      row. ``visibility`` drives access control: ``private`` (only the
      owner), ``shared`` (any signed-in narve user), ``public`` (indexed,
      share-by-link). ``is_system=1`` marks the auto-created "saved" +
      "watchlist" rows so the UI hides delete/rename controls.
      ``is_featured=1`` surfaces the row on the /explore page.

  ``collection_items``
      Ordered items within a collection. ``item_type`` is 'market',
      'source', or 'prediction'; ``item_ref`` is the slug, handle, or
      prediction id. ``position`` is a dense-ish integer the reorder
      endpoint rewrites on drag-and-drop.

Both tables are additive — nothing existing is touched. Safe to re-run.
"""

revision = "120"
down_revision = "119"


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def upgrade(c):
    if not _table_exists(c, "collections"):
        c.execute("""
            CREATE TABLE collections (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id      INTEGER NOT NULL,
                slug               TEXT NOT NULL,
                title              TEXT NOT NULL,
                description        TEXT,
                visibility         TEXT NOT NULL DEFAULT 'private',
                is_system          INTEGER NOT NULL DEFAULT 0,
                is_featured        INTEGER NOT NULL DEFAULT 0,
                cover_image_url    TEXT,
                item_count         INTEGER NOT NULL DEFAULT 0,
                view_count         INTEGER NOT NULL DEFAULT 0,
                follower_count     INTEGER NOT NULL DEFAULT 0,
                created_at         INTEGER NOT NULL,
                updated_at         INTEGER NOT NULL,
                UNIQUE(owner_user_id, slug),
                FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_collections_owner ON collections(owner_user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_collections_visibility ON collections(visibility)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_collections_featured ON collections(is_featured, updated_at)")

    if not _table_exists(c, "collection_items"):
        c.execute("""
            CREATE TABLE collection_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id  INTEGER NOT NULL,
                item_type      TEXT NOT NULL,
                item_ref       TEXT NOT NULL,
                position       INTEGER NOT NULL,
                note           TEXT,
                added_at       INTEGER NOT NULL,
                FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE,
                UNIQUE(collection_id, item_type, item_ref)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_coll_items_collection ON collection_items(collection_id, position)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS collection_items")
    c.execute("DROP TABLE IF EXISTS collections")
