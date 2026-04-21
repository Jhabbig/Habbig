"""Cancellation retention funnel + subscription pauses.

Two tables + one column add:

  * ``cancellation_attempts`` — append-only funnel log. The 3-step cancel
    UI writes a row every time the user starts the flow and updates
    ``reached_step`` / ``outcome`` as they progress. Lets the /admin/churn
    page show "40% bail at step 1, 25% take the pause, 35% cancel".

  * ``subscription_pauses`` — audit trail for pauses. The pause-state the
    access gate *actually* consults is on the users row (see below); this
    table records the history so the admin can see pause frequency per
    user and a user can see their pause history.

  * ``users.subscription_paused_until`` — nullable DATETIME on the users
    table. Columns-on-users instead of a join keeps the per-request
    access gate a single lookup. Existing row migration is safe because
    SQLite allows adding a nullable column without a default rewrite.

Safety:
  * Every statement is wrapped in a "column-already-exists" catch so a
    sibling agent's earlier partial application doesn't poison us.
  * Downgrade drops tables but leaves the column — dropping columns in
    SQLite requires a table rewrite and the risk isn't worth the tiny
    schema-cleanliness win. A future migration can drop it explicitly.
"""

from __future__ import annotations

import logging
import sqlite3


revision = "094"
down_revision = "093"


log = logging.getLogger("migration.094")


def _has_column(c: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS cancellation_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reason TEXT,
            reached_step INTEGER NOT NULL,
            outcome TEXT,  -- 'retained' | 'paused' | 'cancelled' | NULL (in-progress)
            pause_days INTEGER,
            completed_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    # Admin view filters by outcome IS NULL (in-flight) and by started_at
    # DESC (recent attempts list). Single compound index covers both.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_cancel_started "
        "ON cancellation_attempts(started_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_cancel_user_time "
        "ON cancellation_attempts(user_id, started_at DESC)"
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_pauses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resume_at DATETIME NOT NULL,
            resumed_early_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_pause_user_time "
        "ON subscription_pauses(user_id, started_at DESC)"
    )

    if not _has_column(c, "users", "subscription_paused_until"):
        try:
            c.execute(
                "ALTER TABLE users ADD COLUMN subscription_paused_until DATETIME"
            )
        except sqlite3.OperationalError as exc:
            # Parallel agent may have added the column between the check and
            # the ALTER; swallow the duplicate-column error, let real errors
            # propagate.
            if "duplicate column" in str(exc).lower():
                log.warning("094: subscription_paused_until already present")
            else:
                raise


def downgrade(c: sqlite3.Connection) -> None:
    # Column drop is a rewrite; leave ``subscription_paused_until`` behind
    # and only rip the dedicated tables. Re-upgrading is safe thanks to
    # the column guard in upgrade().
    c.execute("DROP TABLE IF EXISTS subscription_pauses")
    c.execute("DROP TABLE IF EXISTS cancellation_attempts")
