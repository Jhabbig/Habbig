"""Per-subproduct feature flag dimension.

Adds a ``subproduct_key`` column to ``feature_flags`` so the same flag
key can have a per-subdomain override of the global value. Lookup
precedence (implemented in ``features.is_feature_enabled``):

    1. (key, subproduct_key=<host slug>)   -- subproduct-specific row
    2. (key, subproduct_key=NULL)          -- global fallback

Existing rows are unchanged: every pre-existing flag stays at
``subproduct_key = NULL``, i.e. it remains the global default.

The original schema had ``key TEXT NOT NULL UNIQUE``, which would block
having both a global row and a per-subproduct row with the same key. We
rebuild the table with the uniqueness constraint moved to the composite
``(key, subproduct_key)`` index so that:

  * key='foo', subproduct_key=NULL        -- global default
  * key='foo', subproduct_key='voters'    -- override for voters.narve.ai
  * key='foo', subproduct_key='crypto'    -- override for crypto.narve.ai

all coexist without colliding.

Additive on data -- no rows are dropped, IDs are preserved. The migration
is idempotent on re-runs.
"""

revision = "186"
down_revision = "185"


def upgrade(c):
    # Skip if already migrated (idempotent on re-runs).
    cols = {r["name"] for r in c.execute("PRAGMA table_info(feature_flags)").fetchall()}
    if "subproduct_key" in cols:
        # Make sure the indexes exist even if a previous partial run only
        # added the column.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_feature_flags_key_subproduct "
            "ON feature_flags(key, subproduct_key)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_feature_flags_subproduct "
            "ON feature_flags(subproduct_key, key)"
        )
        return

    # SQLite cannot drop a UNIQUE constraint in place -- rebuild the table.
    # No other table foreign-keys to ``feature_flags(id)``, so a straight
    # rename + recreate + copy is safe and preserves AUTOINCREMENT ids.
    c.execute("ALTER TABLE feature_flags RENAME TO feature_flags_old")

    c.execute("""
        CREATE TABLE feature_flags (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            key                    TEXT NOT NULL,
            name                   TEXT NOT NULL,
            description            TEXT,
            enabled_globally       INTEGER NOT NULL DEFAULT 0,
            enabled_for_tiers      TEXT NOT NULL DEFAULT '[]',
            enabled_for_user_ids   TEXT NOT NULL DEFAULT '[]',
            disabled_for_user_ids  TEXT NOT NULL DEFAULT '[]',
            rollout_percentage     INTEGER NOT NULL DEFAULT 0,
            created_at             INTEGER NOT NULL,
            updated_at             INTEGER NOT NULL,
            updated_by_admin_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            subproduct_key         TEXT
        )
    """)

    c.execute("""
        INSERT INTO feature_flags (
            id, key, name, description, enabled_globally,
            enabled_for_tiers, enabled_for_user_ids, disabled_for_user_ids,
            rollout_percentage, created_at, updated_at, updated_by_admin_id,
            subproduct_key
        )
        SELECT
            id, key, name, description, enabled_globally,
            enabled_for_tiers, enabled_for_user_ids, disabled_for_user_ids,
            rollout_percentage, created_at, updated_at, updated_by_admin_id,
            NULL
        FROM feature_flags_old
    """)

    c.execute("DROP TABLE feature_flags_old")

    # Composite uniqueness: (key, subproduct_key). NULL subproduct_key acts
    # as the global default. SQLite treats NULLs as distinct in UNIQUE
    # indexes, so multiple global rows would technically be allowed -- but
    # the application-level CRUD enforces "one global row per key" via
    # ``get_feature_flag(key, subproduct_key=None)``.
    c.execute(
        "CREATE UNIQUE INDEX idx_feature_flags_key_subproduct "
        "ON feature_flags(key, subproduct_key)"
    )
    # Back-compat: keep the non-unique key index so any external tooling
    # that referenced it still sees an index by that name.
    c.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_key ON feature_flags(key)")
    # Fast filter for "all flags scoped to <subproduct>" in the admin UI.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feature_flags_subproduct "
        "ON feature_flags(subproduct_key, key)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_feature_flags_subproduct")
    c.execute("DROP INDEX IF EXISTS idx_feature_flags_key_subproduct")
    # Reverse the table rebuild. Any row with a non-NULL subproduct_key
    # would violate the old ``key TEXT UNIQUE``; we discard those rows
    # (downgrade is rare and the override rows are conceptually new data
    # that didn't exist before 186 anyway).
    c.execute("ALTER TABLE feature_flags RENAME TO feature_flags_new")
    c.execute("""
        CREATE TABLE feature_flags (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            key                    TEXT NOT NULL UNIQUE,
            name                   TEXT NOT NULL,
            description            TEXT,
            enabled_globally       INTEGER NOT NULL DEFAULT 0,
            enabled_for_tiers      TEXT NOT NULL DEFAULT '[]',
            enabled_for_user_ids   TEXT NOT NULL DEFAULT '[]',
            disabled_for_user_ids  TEXT NOT NULL DEFAULT '[]',
            rollout_percentage     INTEGER NOT NULL DEFAULT 0,
            created_at             INTEGER NOT NULL,
            updated_at             INTEGER NOT NULL,
            updated_by_admin_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    c.execute("""
        INSERT INTO feature_flags (
            id, key, name, description, enabled_globally,
            enabled_for_tiers, enabled_for_user_ids, disabled_for_user_ids,
            rollout_percentage, created_at, updated_at, updated_by_admin_id
        )
        SELECT
            id, key, name, description, enabled_globally,
            enabled_for_tiers, enabled_for_user_ids, disabled_for_user_ids,
            rollout_percentage, created_at, updated_at, updated_by_admin_id
        FROM feature_flags_new
        WHERE subproduct_key IS NULL
    """)
    c.execute("DROP TABLE feature_flags_new")
    c.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_key ON feature_flags(key)")
