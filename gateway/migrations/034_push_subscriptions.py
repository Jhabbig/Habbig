"""Web Push subscriptions.

One row per installed PWA / browser a user has opted into notifications
from. ``endpoint`` is the push service URL (unique per subscription);
``p256dh`` and ``auth`` are the encryption material the browser gave us
when the user accepted the permission prompt. Deleting a user cascades.

Subscriptions can expire silently (user clears browser data, reinstalls
the PWA, etc.); the sender treats 404/410 from the push service as a
signal to delete the row. ``last_error`` + ``failure_count`` exist so a
flaky endpoint doesn't get hammered forever.

Additive, idempotent, no data touched.
"""

revision = "034"
down_revision = "033"


def upgrade(c):
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL
                           REFERENCES users(id) ON DELETE CASCADE,
            endpoint       TEXT NOT NULL UNIQUE,
            p256dh         TEXT NOT NULL,
            auth           TEXT NOT NULL,
            user_agent     TEXT,
            created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            last_used_at   INTEGER,
            failure_count  INTEGER NOT NULL DEFAULT 0,
            last_error     TEXT
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_push_subs_user")
    c.execute("DROP TABLE IF EXISTS push_subscriptions")
