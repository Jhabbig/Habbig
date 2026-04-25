"""Forecaster-to-forecaster follow graph.

Each row is one directed follow: ``follower_user_id`` follows
``followed_user_id``. The composite PK enforces "no double-follows"
without needing a separate UNIQUE index. ON DELETE CASCADE keeps the
graph clean when accounts are deleted (the user-deletion path already
walks every table with a ``user_id`` column; this is a belt + braces).

Index on ``followed_user_id`` so "who follows me" queries don't scan
the full table — that's the hot read path for the public profile page
(``follower_count`` shown next to the handle).
"""

from __future__ import annotations


revision = "173"
down_revision = "172"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_follows (
            follower_user_id INTEGER NOT NULL,
            followed_user_id INTEGER NOT NULL,
            followed_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (follower_user_id, followed_user_id),
            FOREIGN KEY (follower_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (followed_user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_follows_followed "
        "ON user_follows(followed_user_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_follows_follower "
        "ON user_follows(follower_user_id)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_follows_follower")
    cur.execute("DROP INDEX IF EXISTS idx_follows_followed")
    cur.execute("DROP TABLE IF EXISTS user_follows")
