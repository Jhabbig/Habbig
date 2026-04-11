"""API keys for the developer/quant API (F12).

Bearer-token auth for programmatic access to credibility scores, predictions,
and edge data. Keys are stored as SHA-256 hashes — the raw key is shown once
at creation and never stored.
"""

revision = "014"
down_revision = "013"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash        TEXT NOT NULL UNIQUE,
            key_prefix      TEXT NOT NULL,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name            TEXT NOT NULL DEFAULT '',
            tier            TEXT NOT NULL DEFAULT 'standard',
            rate_limit_hour INTEGER NOT NULL DEFAULT 1000,
            created_at      INTEGER NOT NULL,
            last_used_at    INTEGER,
            revoked_at      INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_api_keys_user")
    c.execute("DROP INDEX IF EXISTS idx_api_keys_hash")
    c.execute("DROP TABLE IF EXISTS api_keys")
