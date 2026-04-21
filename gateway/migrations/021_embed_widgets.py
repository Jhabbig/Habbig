"""Embed widgets — token-gated, domain-locked widgets for partner sites.

Subscribers generate embed codes that render narve.ai data on external
sites (source credibility, market probability, top-EV "best bets"). Each
widget is:

  - Owned by one user via user_id (cascades on account deletion).
  - Locked to a single domain; the /embed/{widget_id} handler checks the
    request's Referer against `domain` and rejects mismatches.
  - Gated by a signed token derived from `token_salt` using
    EMBED_SIGNING_SECRET. Rotating the token replaces the salt, which
    invalidates every in-the-wild copy of the old token without touching
    the widget_id URL.
  - Deactivatable without deletion (is_active=0) so the owner keeps the
    historical impression count, and a sub lapse can bulk-disable all
    widgets without losing audit data.

`widget_id` is a URL-safe random string (not the integer PK). Public
URLs shouldn't leak the auto-increment ID and they shouldn't be
enumerable.
"""

revision = "022"
down_revision = "020"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS embed_widgets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            widget_id      TEXT    NOT NULL UNIQUE,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            widget_type    TEXT    NOT NULL,
            target         TEXT    NOT NULL,
            domain         TEXT    NOT NULL,
            token_salt     TEXT    NOT NULL,
            theme          TEXT    NOT NULL DEFAULT 'auto',
            created_at     INTEGER NOT NULL,
            last_used_at   INTEGER,
            impressions    INTEGER NOT NULL DEFAULT 0,
            is_active      INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_embed_widgets_user "
        "ON embed_widgets(user_id, created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_embed_widgets_active "
        "ON embed_widgets(user_id, is_active)"
    )


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS embed_widgets")
