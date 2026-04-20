"""Market movement detection — event log + user alert rules.

Adds:
  - market_movement_events: detected price swings, volume spikes, new markets,
    approaching resolutions, and reversals
  - user_market_alerts: per-user configurable alert rules (event types,
    thresholds, delivery preferences)
"""

revision = "022"
down_revision = "021"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_movement_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type        TEXT NOT NULL,
            market_slug       TEXT NOT NULL,
            market_question   TEXT,
            category          TEXT,
            source_platform   TEXT NOT NULL DEFAULT 'polymarket',
            old_price         REAL,
            new_price         REAL,
            price_change      REAL,
            old_volume        REAL,
            new_volume        REAL,
            volume_change     REAL,
            close_time        INTEGER,
            hours_to_close    REAL,
            severity          TEXT NOT NULL DEFAULT 'medium',
            metadata_json     TEXT NOT NULL DEFAULT '{}',
            detected_at       INTEGER NOT NULL,
            notified          INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_mme_type "
        "ON market_movement_events(event_type)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_mme_slug "
        "ON market_movement_events(market_slug)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_mme_detected "
        "ON market_movement_events(detected_at)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_mme_severity "
        "ON market_movement_events(severity)"
    )

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_market_alerts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type        TEXT NOT NULL,
            min_severity      TEXT NOT NULL DEFAULT 'medium',
            min_price_change  REAL,
            categories_json   TEXT NOT NULL DEFAULT '[]',
            only_saved        INTEGER NOT NULL DEFAULT 0,
            only_followed     INTEGER NOT NULL DEFAULT 0,
            delivery          TEXT NOT NULL DEFAULT 'in_app',
            enabled           INTEGER NOT NULL DEFAULT 1,
            created_at        INTEGER NOT NULL,
            updated_at        INTEGER NOT NULL
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_uma_user "
        "ON user_market_alerts(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_uma_type "
        "ON user_market_alerts(event_type)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_uma_enabled "
        "ON user_market_alerts(enabled)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS user_market_alerts")
    c.execute("DROP TABLE IF EXISTS market_movement_events")
