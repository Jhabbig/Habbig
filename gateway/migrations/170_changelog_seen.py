"""Per-user "I've already seen this changelog entry" state.

Drives the unseen badge + dot on the "What's new" widget on the
dashboard hub. Fully self-contained — no relations to user
profile / billing tables, just (user_id, entry_key) → seen_at.
Stable entry_key = sha1(version + date) computed at parse time so a
re-edit of the entry's body doesn't re-flash it as unseen.
"""

from __future__ import annotations


revision = "170"
down_revision = "162"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS changelog_seen (
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            entry_key   TEXT NOT NULL,
            seen_at     INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
            PRIMARY KEY (user_id, entry_key)
        )
        """
    )
    # Look-up shape for the badge query: "how many of these N entry_keys
    # has the user NOT yet seen?". Index by user_id alone is enough — N
    # is small (<= 5) so the IN-list scan is cheap.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_changelog_seen_user "
        "ON changelog_seen(user_id)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_changelog_seen_user")
    cur.execute("DROP TABLE IF EXISTS changelog_seen")
