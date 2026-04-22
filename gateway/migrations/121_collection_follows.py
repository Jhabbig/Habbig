"""Collection follows — users can subscribe to public collections and get
a notification when the owner adds new items.

Composite primary key on (user_id, collection_id) so the same user can
follow a collection exactly once. ``notifications_on`` is separate from
the follow state so a user can mute noisy boards without losing their
place.
"""

revision = "121"
down_revision = "120"


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def upgrade(c):
    if not _table_exists(c, "collection_follows"):
        c.execute("""
            CREATE TABLE collection_follows (
                user_id           INTEGER NOT NULL,
                collection_id     INTEGER NOT NULL,
                followed_at       INTEGER NOT NULL,
                notifications_on  INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, collection_id),
                FOREIGN KEY(user_id)       REFERENCES users(id)       ON DELETE CASCADE,
                FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_coll_follows_collection "
            "ON collection_follows(collection_id)"
        )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS collection_follows")
