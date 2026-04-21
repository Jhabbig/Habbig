"""Portfolio integration — persistent positions + connection active flag."""

revision = "020"
down_revision = "019"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    cred_cols = _existing_cols(c, "user_market_credentials")
    if "is_active" not in cred_cols:
        c.execute(
            "ALTER TABLE user_market_credentials "
            "ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_positions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            platform            TEXT NOT NULL,
            market_id           TEXT NOT NULL,
            market_title        TEXT NOT NULL,
            side                TEXT NOT NULL,
            shares              REAL NOT NULL DEFAULT 0,
            avg_entry_price     REAL NOT NULL DEFAULT 0,
            current_price       REAL NOT NULL DEFAULT 0,
            unrealised_pnl      REAL NOT NULL DEFAULT 0,
            position_value_usd  REAL NOT NULL DEFAULT 0,
            last_synced_at      INTEGER NOT NULL,
            UNIQUE(user_id, platform, market_id, side),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_positions_user ON user_positions(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_positions_user_platform ON user_positions(user_id, platform)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS user_positions")
    cred_cols = _existing_cols(c, "user_market_credentials")
    if "is_active" in cred_cols:
        try:
            c.execute("ALTER TABLE user_market_credentials DROP COLUMN is_active")
        except Exception:
            pass
