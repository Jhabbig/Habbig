"""On-chain whale position tracking (F14)."""

revision = "016"
down_revision = "015"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS whale_positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_hash     TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            side            TEXT NOT NULL,
            amount_usd      REAL NOT NULL,
            tier            TEXT NOT NULL,
            detected_at     INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_whale_market ON whale_positions(market_id, detected_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_whale_wallet ON whale_positions(wallet_hash)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_whale_wallet")
    c.execute("DROP INDEX IF EXISTS idx_whale_market")
    c.execute("DROP TABLE IF EXISTS whale_positions")
