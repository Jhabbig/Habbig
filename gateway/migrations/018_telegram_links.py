"""Telegram user links for the bot integration (F15)."""

revision = "018"
down_revision = "017"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS telegram_user_links (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            telegram_chat_id    TEXT NOT NULL UNIQUE,
            telegram_username   TEXT,
            linked_at           INTEGER NOT NULL,
            alerts_enabled      INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tg_user ON telegram_user_links(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tg_chat ON telegram_user_links(telegram_chat_id)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_tg_chat")
    c.execute("DROP INDEX IF EXISTS idx_tg_user")
    c.execute("DROP TABLE IF EXISTS telegram_user_links")
