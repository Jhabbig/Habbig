"""Environmental impact — tolerates pre-existing table from migration 008.

Earlier work (migration 008_environmental_impact.py) created the core
``environmental_impacts`` table + per-user display preferences. This
migration fills gaps for the new ai/environmental.py analyser:

  - Creates the table if absent (tree not on that migration path yet)
  - Adds ``cache_valid_until`` if the older schema called it
    ``valid_until`` or never had it
  - Adds ``market_slug`` UNIQUE if earlier shipped as ``market_id``

The module normalises the column names when it reads so both layouts
stay callable.

Additive. Safe to re-run.
"""

revision = "058"
down_revision = "057"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c):
    if not _table_exists(c, "environmental_impacts"):
        c.execute("""
            CREATE TABLE environmental_impacts (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id                TEXT NOT NULL UNIQUE,
                market_slug              TEXT,
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
    else:
        cols = _existing_cols(c, "environmental_impacts")
        if "market_slug" not in cols:
            c.execute("ALTER TABLE environmental_impacts ADD COLUMN market_slug TEXT")
        if "cache_valid_until" not in cols:
            # Older revisions used valid_until; backfill.
            if "valid_until" in cols:
                c.execute("ALTER TABLE environmental_impacts ADD COLUMN cache_valid_until INTEGER")
                c.execute("UPDATE environmental_impacts SET cache_valid_until = valid_until")
            else:
                c.execute("ALTER TABLE environmental_impacts ADD COLUMN cache_valid_until INTEGER NOT NULL DEFAULT 0")

    # User prefs — idempotent adds. These are duplicated from migration 008
    # but harmless to re-assert on a tree that skipped 008.
    if _table_exists(c, "users"):
        ucols = _existing_cols(c, "users")
        if "show_environmental_impact" not in ucols and "env_show" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN show_environmental_impact INTEGER NOT NULL DEFAULT 1")
        if "preferred_unit" not in ucols and "env_unit" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN preferred_unit TEXT NOT NULL DEFAULT 'co2_mt'")


def downgrade(c):
    pass  # Additive only.
