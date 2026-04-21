"""Claude-powered market categorisation cache.

Replaces the keyword matcher in backend.markets.unified_markets._guess_category
with LLM classification. Markets don't change category during their life,
so rows are cached for 1 year; the keyword matcher remains as the fallback
for uncached markets (the Claude call happens lazily via a cron or the
normaliser, not on the hot read path).

Schema:
  - market_categorisations: one row per unified market id
"""

revision = "028"
down_revision = "023"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_categorisations (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id               TEXT NOT NULL UNIQUE,
            market_title            TEXT NOT NULL,
            generated_at            INTEGER NOT NULL,
            generated_by            TEXT NOT NULL,
            cache_valid_until       INTEGER NOT NULL,
            primary_category        TEXT NOT NULL,
            sub_category            TEXT,
            tags                    TEXT NOT NULL DEFAULT '[]',
            political_leaning       TEXT,
            sensitivity             TEXT NOT NULL DEFAULT 'normal',
            insider_trading_relevant INTEGER NOT NULL DEFAULT 0,
            environmental_relevant  INTEGER NOT NULL DEFAULT 0,
            requires_expert_knowledge INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_cat_id ON market_categorisations(market_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_cat_primary ON market_categorisations(primary_category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_cat_valid ON market_categorisations(cache_valid_until)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_market_cat_valid")
    c.execute("DROP INDEX IF EXISTS idx_market_cat_primary")
    c.execute("DROP INDEX IF EXISTS idx_market_cat_id")
    c.execute("DROP TABLE IF EXISTS market_categorisations")
