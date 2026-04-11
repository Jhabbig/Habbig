"""Email unsubscribe tracking + user email preferences."""

revision = "002"
down_revision = "001"


def upgrade(c):
    # Unsubscribe records — tracks both logged-in users and newsletter-only emails.
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_unsubscribes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER REFERENCES users(id) ON DELETE CASCADE,
            email              TEXT NOT NULL,
            unsubscribed_from  TEXT NOT NULL,
            token              TEXT UNIQUE NOT NULL,
            created_at         INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_unsub_email ON email_unsubscribes(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_unsub_token ON email_unsubscribes(token)")

    # Additive columns on users.
    existing = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    if "email_digest" not in existing:
        c.execute("ALTER TABLE users ADD COLUMN email_digest INTEGER NOT NULL DEFAULT 1")
    if "email_marketing" not in existing:
        c.execute("ALTER TABLE users ADD COLUMN email_marketing INTEGER NOT NULL DEFAULT 1")
    if "email_unsubscribed_at" not in existing:
        c.execute("ALTER TABLE users ADD COLUMN email_unsubscribed_at INTEGER")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS email_unsubscribes")
    # SQLite ALTER TABLE DROP COLUMN only in 3.35+. Leave user columns
    # in place on downgrade — they're nullable and harmless.
