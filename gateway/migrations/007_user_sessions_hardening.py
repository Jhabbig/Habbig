"""Hardened session store for the token-first auth flow.

Adds a `user_sessions` table that stores the session token as SHA-256
hash at rest and tracks device metadata (ip, user agent, last activity)
for the active-sessions management UI.

The legacy `sessions` table is kept intact and in use — the token-first
flow writes to BOTH tables on login/register so existing code paths
(CSRF lookup, admin audit, etc) keep working. Once every reader has
been migrated to user_sessions, the legacy table can be dropped in a
future migration.
"""

revision = "007"
down_revision = "006"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash      TEXT NOT NULL UNIQUE,
            legacy_token    TEXT,
            created_at      INTEGER NOT NULL,
            expires_at      INTEGER NOT NULL,
            last_active_at  INTEGER NOT NULL,
            ip_address      TEXT,
            user_agent      TEXT,
            revoked         INTEGER NOT NULL DEFAULT 0,
            revoked_at      INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_hash ON user_sessions(token_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_active ON user_sessions(user_id, revoked, expires_at)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS user_sessions")
