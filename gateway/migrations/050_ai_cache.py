"""General-purpose AI response cache.

A plain key/value TTL cache used by every Claude-backed feature in ai/.
Kept deliberately narrow — one row per cache entry, opaque JSON value.
Features compose their own key schemes on top:

    extract:<sha256(post_text)>          prediction extractor
    categorise:<market_slug>             market categoriser
    summary:<source_handle>              source summariser
    env:<market_slug>                    environmental analyser

Features are free to also write to their own typed caches (ai/categoriser.py
uses market_categorisations). This table exists for anything that's purely
"call Claude with this prompt, cache the response" without its own schema.

Additive only. Safe to re-run — table creation is guarded.
"""

revision = "050"
down_revision = "035"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_cache (
            cache_key  TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            feature    TEXT NOT NULL DEFAULT 'unknown',
            model      TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache(expires_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_feature ON ai_cache(feature, expires_at)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_ai_cache_feature")
    c.execute("DROP INDEX IF EXISTS idx_ai_cache_expires")
    c.execute("DROP TABLE IF EXISTS ai_cache")
