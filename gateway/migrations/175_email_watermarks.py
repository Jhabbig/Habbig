"""Per-recipient email watermarks — forensic attribution for Pro emails.

Each Pro-tier intelligence email (weekly digest, morning briefing, market
mover alert) carries a short HMAC-derived watermark unique to (user_id,
email_id). When a subscriber leaks the content of one of these emails,
the visible footer fragment (or the reconstructed zero-width run in the
body text) reverses back to a user_id via this table.

Schema:
  * ``watermark`` — 6-char hex, the first 24 bits of an HMAC-SHA256 over
    ``f"{user_id}:{email_id}"`` with key ``EMAIL_WATERMARK_KEY``.
  * ``user_id``   — the recipient. ON DELETE CASCADE: forensic value
    drops with the account, matches our GDPR posture.
  * ``email_id``  — opaque per-send identifier (template + batch + ts);
    lets us tell which email of the user's the leak came from.
  * ``created_at`` — unix seconds; lets us scope traces to a time window.

The (watermark) PRIMARY KEY makes the trace lookup O(1). A 24-bit space
gives ~16.7M slots — collisions for a single user across many sends are
already prevented by the (user_id, email_id) input to HMAC, but if two
distinct (user, email) pairs hash to the same 6 hex chars we let the
INSERT fail silently and re-generate with a longer hash window (see
``watermark.py``). This is rare enough at our scale that the simple
table shape is worth it.
"""

from __future__ import annotations

import sqlite3


revision = "175"
down_revision = "174"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS email_watermarks (
            watermark   TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
            email_id    TEXT NOT NULL,
            template    TEXT,
            created_at  INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_watermarks_user "
        "ON email_watermarks(user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_watermarks_created "
        "ON email_watermarks(created_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_email_watermarks_created")
    c.execute("DROP INDEX IF EXISTS idx_email_watermarks_user")
    c.execute("DROP TABLE IF EXISTS email_watermarks")
