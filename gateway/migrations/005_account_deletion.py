"""Soft-delete fields on users + market view tracking for resolution notifications."""

revision = "005"
down_revision = "004"


def upgrade(c):
    cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    if "deletion_requested_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN deletion_requested_at INTEGER")
    if "deletion_scheduled_for" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN deletion_scheduled_for INTEGER")
    if "deletion_cancelled_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN deletion_cancelled_at INTEGER")
    if "deleted_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN deleted_at INTEGER")
    if "is_deleted" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_market_views (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market_slug               TEXT NOT NULL,
            first_viewed_at           INTEGER NOT NULL,
            last_viewed_at            INTEGER NOT NULL,
            view_count                INTEGER NOT NULL DEFAULT 1,
            notified_on_resolution    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, market_slug)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_umv_slug ON user_market_views(market_slug)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_umv_unnotified ON user_market_views(notified_on_resolution)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS user_market_views")
