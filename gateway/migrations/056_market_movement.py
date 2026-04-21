"""Market mover alerts — detected movement events + user alert rules.

  market_movement_events
    Appended by markets/movement_detector.py on every 5-minute tick. The
    detector snapshots current market_snapshots vs the last one it saw
    for each market and writes a row for each movement class that
    breaches its threshold. Events are consumed by:
      - push + in-app notification dispatchers
      - email weekly digest "Notable movements" section

  user_market_alerts
    Per-user alert subscriptions. A user can have up to 10 rows, each
    scoped by category / market_slug / movement type / thresholds /
    predictor requirements. ``is_active=0`` rows are kept for history.
"""

revision = "056"
down_revision = "055"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_movement_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug          TEXT NOT NULL,
            event_type           TEXT NOT NULL,
            detected_at          INTEGER NOT NULL,
            previous_value       REAL,
            current_value        REAL,
            magnitude            REAL,
            window_seconds       INTEGER,
            narve_context_json   TEXT,
            notified_at          INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mm_events_slug_time ON market_movement_events(market_slug, detected_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mm_events_type_time ON market_movement_events(event_type, detected_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mm_events_notify ON market_movement_events(notified_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_market_alerts (
            id                            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                       INTEGER NOT NULL,
            alert_type                    TEXT NOT NULL,
            market_slug                   TEXT,
            category                      TEXT,
            min_movement_pct              REAL NOT NULL DEFAULT 0.08,
            min_volume_multiple           REAL NOT NULL DEFAULT 3.0,
            only_when_predictions_exist   INTEGER NOT NULL DEFAULT 0,
            min_predictor_credibility     REAL,
            is_active                     INTEGER NOT NULL DEFAULT 1,
            created_at                    INTEGER NOT NULL,
            updated_at                    INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_mm_alerts_user ON user_market_alerts(user_id, is_active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_mm_alerts_type ON user_market_alerts(alert_type, is_active)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_user_mm_alerts_type")
    c.execute("DROP INDEX IF EXISTS idx_user_mm_alerts_user")
    c.execute("DROP TABLE IF EXISTS user_market_alerts")
    c.execute("DROP INDEX IF EXISTS idx_mm_events_notify")
    c.execute("DROP INDEX IF EXISTS idx_mm_events_type_time")
    c.execute("DROP INDEX IF EXISTS idx_mm_events_slug_time")
    c.execute("DROP TABLE IF EXISTS market_movement_events")
