"""In-app notification bell + history (Feature: notifications).

Adds two tables:

  * ``notifications`` — one row per delivered notification, owned by a user.
    Carries the bell-dropdown content (title, body, link, icon) plus a JSON
    ``metadata`` blob for click-handler payloads. ``read_at`` is NULL until
    the user opens the dropdown or clicks through. ``archived=1`` hides the
    row from the main view without deleting.

  * ``notification_preferences`` — per-user opt-in/out matrix. One row per
    user. ``types_json`` stores a ``{type: bool}`` map — absent keys default
    to True. The three delivery channel booleans (inapp/push/email) gate the
    whole feed. Missing rows are treated as all-on defaults, so we never
    need a backfill.

Additive, idempotent, no data touched. See ``notifications.py`` (module) for
the runtime side.
"""

from __future__ import annotations


revision = "026"
down_revision = "021"


def upgrade(c):
    # ── notifications (bell history) ──────────────────────────────────
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
            type        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT NOT NULL DEFAULT '',
            link_url    TEXT,
            icon        TEXT,
            metadata    TEXT,                 -- JSON blob, nullable
            created_at  INTEGER NOT NULL,     -- unix seconds
            read_at     INTEGER,              -- NULL until marked read
            archived    INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Primary feed query: user's rows newest-first. Partial index on unread
    # rows keeps the badge-count lookup on O(unread) rather than O(total).
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifs_user_created "
        "ON notifications(user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifs_user_unread "
        "ON notifications(user_id) WHERE read_at IS NULL AND archived = 0"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifs_user_type "
        "ON notifications(user_id, type)"
    )

    # ── notification_preferences ──────────────────────────────────────
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_preferences (
            user_id         INTEGER PRIMARY KEY
                            REFERENCES users(id) ON DELETE CASCADE,
            inapp_enabled   INTEGER NOT NULL DEFAULT 1,
            push_enabled    INTEGER NOT NULL DEFAULT 0,
            email_enabled   INTEGER NOT NULL DEFAULT 1,
            types_json      TEXT NOT NULL DEFAULT '{}',
            updated_at      INTEGER NOT NULL
        )
        """
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_notifs_user_type")
    c.execute("DROP INDEX IF EXISTS idx_notifs_user_unread")
    c.execute("DROP INDEX IF EXISTS idx_notifs_user_created")
    c.execute("DROP TABLE IF EXISTS notification_preferences")
    c.execute("DROP TABLE IF EXISTS notifications")
