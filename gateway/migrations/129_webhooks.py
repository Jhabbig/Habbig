"""Outbound webhooks — subscription registry + delivery log.

Two tables:

  ``webhook_subscriptions`` — one row per (user, url, event-list, secret)
  tuple. ``events`` is a JSON array of event-type strings; we keep it as
  TEXT for the same json1-optional reason noted in migration 128.

  ``webhook_deliveries`` — append-only log of every attempted POST. The
  delivery worker in gateway/webhooks.py writes a row per attempt (so a
  subscription with 5 retries yields 5 rows). Kept as a single append-only
  table rather than a per-attempt chain so the admin panel can render a
  flat timeline without recursive joins.

consecutive_failures on the subscription is what triggers auto-disable
after 5 in a row; failure_count is a cumulative all-time counter we keep
for analytics. Both live on the subscription row so the worker can read
+ increment in a single UPDATE.

Additive only. Safe to re-run.
"""

revision = "129"
down_revision = "128"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            url                   TEXT NOT NULL,
            events                TEXT NOT NULL DEFAULT '[]',
            secret                TEXT NOT NULL,
            created_at            INTEGER NOT NULL,
            is_active             INTEGER NOT NULL DEFAULT 1,
            last_delivered_at     INTEGER,
            failure_count         INTEGER NOT NULL DEFAULT 0,
            consecutive_failures  INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_wh_user "
        "ON webhook_subscriptions(user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_wh_active "
        "ON webhook_subscriptions(is_active) WHERE is_active = 1"
    )

    c.execute("""
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id    INTEGER NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
            event_type    TEXT NOT NULL,
            payload       TEXT NOT NULL,
            status_code   INTEGER,
            delivered_at  INTEGER NOT NULL,
            attempts      INTEGER NOT NULL DEFAULT 1,
            error         TEXT
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_whd_webhook "
        "ON webhook_deliveries(webhook_id, delivered_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_whd_event "
        "ON webhook_deliveries(event_type, delivered_at DESC)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_whd_event")
    c.execute("DROP INDEX IF EXISTS idx_whd_webhook")
    c.execute("DROP TABLE IF EXISTS webhook_deliveries")
    c.execute("DROP INDEX IF EXISTS idx_wh_active")
    c.execute("DROP INDEX IF EXISTS idx_wh_user")
    c.execute("DROP TABLE IF EXISTS webhook_subscriptions")
