"""Environmental Impact analysis cache + per-user display preferences.

Adds a Claude-generated environmental analysis layer on top of existing
prediction markets. Pro users see estimated CO2 impact of YES/NO outcomes
in plain English. Generation is lazy (on first view) and cached for 24h.

Schema:
  - environmental_impacts: one row per market_id (poly:slug or kalshi:ticker)
  - users.env_show: per-user opt-in toggle (default ON)
  - users.env_unit: display unit preference (default 'co2_mt')

Additive only. Safe to re-run: every column add is guarded.
"""

revision = "008"
down_revision = "007"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # ── environmental_impacts cache table ────────────────────────────────
    #
    # market_id is the unified id format used by backend.markets.unified_markets
    # ("poly:slug" or "kalshi:ticker"). Stored as plain TEXT with a UNIQUE index
    # because the markets themselves are fetched live from the upstream APIs
    # (cached 5 min in-memory) and never persisted as DB rows — so there is no
    # FK target to reference.
    #
    # is_relevant=False rows are still cached: they prevent the analyser from
    # repeatedly calling Claude on markets it has already determined are not
    # environmental in nature (e.g. "Will Taylor Swift tour in 2026?").
    #
    # yes_market_price_at_gen lets the analyser detect significant market
    # movement (≥ 10% drift) and automatically regenerate the analysis without
    # requiring a manual force-refresh.
    c.execute("""
        CREATE TABLE IF NOT EXISTS environmental_impacts (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id                TEXT NOT NULL UNIQUE,
            market_question          TEXT NOT NULL,
            market_category          TEXT,
            generated_at             INTEGER NOT NULL,
            generated_by             TEXT NOT NULL,
            cache_valid_until        INTEGER NOT NULL,
            is_relevant              INTEGER NOT NULL DEFAULT 0,
            irrelevance_reason       TEXT,
            yes_outcome_label        TEXT NOT NULL DEFAULT 'YES',
            no_outcome_label         TEXT NOT NULL DEFAULT 'NO',
            yes_co2_impact_mt        REAL,
            no_co2_impact_mt         REAL,
            yes_impact_description   TEXT,
            no_impact_description    TEXT,
            yes_impact_timeframe     TEXT,
            no_impact_timeframe      TEXT,
            confidence               TEXT,
            confidence_reason        TEXT,
            data_sources             TEXT,
            category                 TEXT,
            yes_market_price_at_gen  REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_env_impacts_market ON environmental_impacts(market_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_env_impacts_top ON environmental_impacts(is_relevant, cache_valid_until)")

    # ── users: per-user display preferences (matches notify_*, onboarding_* pattern) ──
    user_cols = _existing_cols(c, "users")
    if "env_show" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN env_show INTEGER NOT NULL DEFAULT 1")
    if "env_unit" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN env_unit TEXT NOT NULL DEFAULT 'co2_mt'")


def downgrade(c):
    # Drop the cache table; the user columns are additive and harmless to leave.
    # SQLite has no native ALTER TABLE DROP COLUMN before 3.35, and the legacy
    # rebuild dance is risky for a feature rollback. Matches convention from
    # migration 006 which also leaves additive columns in place on downgrade.
    c.execute("DROP INDEX IF EXISTS idx_env_impacts_top")
    c.execute("DROP INDEX IF EXISTS idx_env_impacts_market")
    c.execute("DROP TABLE IF EXISTS environmental_impacts")
