"""Per-user invite tokens with tiered monthly replenishment.

Distinct from ``invite_tokens`` (the legacy table that stores
admin-minted bootstrap tokens) — these are tied to a specific user and
consumed from that user's monthly allotment.

Replenishment schedule (implemented in ``jobs/invite_replenish.py``):
  * Trader tier:     2 tokens / month
  * Pro tier:        5 tokens / month
  * Enterprise tier: 20 tokens / month

Rollover cap: unused tokens accumulate up to 2× the monthly allotment
so a light-use user doesn't stockpile indefinitely. Enforced at
replenish time — the job deletes the oldest unused tokens before
minting new ones when the cap would be exceeded.

Attribution: when a token is redeemed, we fill both ``used_at`` and
``used_by_user_id``. The referral system (migration 023) sees the
token's owner as the ``referrer_user_id`` via a JOIN on
``invite_token_id`` → ``referrals``.

The ``is_active`` column exists so we can revoke a compromised token
without deleting the row (preserves history for the admin dashboard).
"""

from __future__ import annotations

import sqlite3


revision = "113"
down_revision = "112"


def upgrade(c: sqlite3.Connection) -> None:
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_invite_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token           TEXT NOT NULL UNIQUE,
            user_id         INTEGER NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
            tier_at_grant   TEXT NOT NULL,
            created_at      INTEGER NOT NULL,
            used_at         INTEGER,
            used_by_user_id INTEGER
                            REFERENCES users(id) ON DELETE SET NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,
            source          TEXT NOT NULL DEFAULT 'monthly_replenish'
        )
        """
    )
    # Filter "my unused tokens" is the hot path for /settings/invites.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_invite_unused "
        "ON user_invite_tokens(user_id, used_at, is_active)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_invite_token "
        "ON user_invite_tokens(token)"
    )
    # Accounting for the replenish job: count this user's unused +
    # active rows to decide how many to mint. With the composite above
    # a simple filter also works, but a dedicated covering index on
    # (user_id, is_active, used_at IS NULL) keeps the job fast.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_invite_replenish "
        "ON user_invite_tokens(user_id, is_active, used_at)"
    )

    # Track last-replenished-at on users so the replenish job can be
    # reentrant within a month (e.g. a user tier-upgraded mid-month and
    # deserves the upgrade-delta, or a retry after a crash mustn't
    # double-grant). Store both the YYYYMM key and the exact ts.
    user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "invites_replenished_yyyymm" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN invites_replenished_yyyymm INTEGER"
        )
    if "invites_replenished_at" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN invites_replenished_at INTEGER"
        )


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP TABLE IF EXISTS user_invite_tokens")
    # Leave the users columns — dropping them on downgrade would force
    # a table rewrite on a live DB; harmless to keep.
