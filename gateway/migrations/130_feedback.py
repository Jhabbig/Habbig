"""Feedback + roadmap tables.

Three small tables to power the public /feedback page, the floating
"💬 Feedback" submission button, and the /admin/feedback triage:

  * ``feedback_items`` — one row per bug / feature / question. Public
    posts (is_public=1) show in /feedback; private posts only in the
    admin triage view. ``duplicate_of`` lets the admin mark dups without
    deleting so the user's notifications / vote still route to the
    canonical item.
  * ``feedback_votes`` — (user_id, feedback_id) composite PK. Idempotent
    upsert means the vote toggle is a single statement. Count on the
    parent item is denormalised for fast sort on /feedback.
  * ``feedback_comments`` — admin responses + user follow-ups. The
    admin flag lets the public detail page render the "Team response"
    banner differently from user replies.

Indexes:
  * feedback_items(status, upvotes DESC) — /feedback list: filter by
    status, sort by top-voted.
  * feedback_items(created_at DESC) — "newest first" sort.
  * feedback_items(user_id) — user's own submissions (for account page).
  * feedback_votes(feedback_id) — "who voted this?" lookup.
  * feedback_comments(feedback_id, created_at) — detail-page timeline.

Safe to re-run: every DDL is IF NOT EXISTS so sibling agents that race
to apply migrations don't poison each other.
"""

from __future__ import annotations

import logging
import sqlite3


revision = "130"
down_revision = "129"


log = logging.getLogger("migration.130")


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT NOT NULL,              -- 'bug' | 'feature' | 'question'
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
                                             -- 'open' | 'in_progress' | 'shipped'
                                             -- | 'declined' | 'dup'
            upvotes INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            admin_note TEXT,
            shipped_commit_sha TEXT,
            duplicate_of INTEGER,
            is_public INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY(duplicate_of) REFERENCES feedback_items(id)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_status_votes "
        "ON feedback_items(status, upvotes DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_created "
        "ON feedback_items(created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_user "
        "ON feedback_items(user_id)"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_votes (
            user_id INTEGER NOT NULL,
            feedback_id INTEGER NOT NULL,
            voted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, feedback_id)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_votes_item "
        "ON feedback_votes(feedback_id)"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id INTEGER NOT NULL,
            user_id INTEGER,
            body TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(feedback_id) REFERENCES feedback_items(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_comments_item "
        "ON feedback_comments(feedback_id, created_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS feedback_comments")
    c.execute("DROP TABLE IF EXISTS feedback_votes")
    c.execute("DROP TABLE IF EXISTS feedback_items")
