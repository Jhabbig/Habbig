"""Impersonation cookie tokens — hash at rest.

Audit finding (HIGH, queries/admin.py)
--------------------------------------
The original ``impersonation_sessions.cookie_token`` column stored the
cookie value in plain text, which means a DB dump (e.g. a backup leak
or read-only SQL injection finding) hands an attacker every active
impersonation cookie. Worse, the impersonation middleware accepted
that raw token alone — no cross-check that the request was coming
from the admin who started the session — so a stolen cookie granted
4h of target-user access.

Fix
---
Mirror the ``user_sessions`` pattern: the cookie value remains a fresh
``secrets.token_urlsafe(48)`` random string, but only its SHA-256 hash
is stored in the DB. On lookup we hash the incoming cookie and SELECT
by ``cookie_token_hash``.

Schema change
-------------
1. Add ``cookie_token_hash TEXT`` column (nullable; populated by the
   updated ``create_impersonation_session``).
2. Add a UNIQUE index on the new column so collision attempts are
   caught at insert time.

Existing sessions
-----------------
All currently-active sessions are ended at migration time
(``ended_at = now, end_reason = 'token_format_change'``). They cannot
be looked up under the new code path, so leaving them "active" in the
DB would orphan the rows. Admins will need to start any in-progress
impersonation flow afresh.

Idempotent: skips the column add and the invalidation pass if the new
column already exists.
"""

from __future__ import annotations

import time


revision = "192"
down_revision = "191"


def _has_column(c, table: str, name: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == name for r in rows)


def _table_exists(c, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone() is not None


def upgrade(c) -> None:
    # Guard for fresh DBs in case the bootstrap order doesn't include
    # the admin-features migration yet.
    if not _table_exists(c, "impersonation_sessions"):
        return
    if _has_column(c, "impersonation_sessions", "cookie_token_hash"):
        return
    c.execute(
        "ALTER TABLE impersonation_sessions "
        "ADD COLUMN cookie_token_hash TEXT"
    )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_imp_sess_token_hash "
        "ON impersonation_sessions(cookie_token_hash) "
        "WHERE cookie_token_hash IS NOT NULL"
    )
    # Invalidate every still-active session: their cookie_token was
    # stored raw, has no hash, and would not match any incoming cookie
    # under the new lookup. Mark them ended so the audit trail is
    # self-consistent.
    now = int(time.time())
    c.execute(
        "UPDATE impersonation_sessions "
        "SET ended_at = ?, end_reason = 'token_format_change' "
        "WHERE ended_at IS NULL",
        (now,),
    )


def downgrade(c) -> None:
    # SQLite doesn't support DROP COLUMN cleanly on older versions; leave
    # the column in place on rollback. The column is additive and unused
    # by anything outside the impersonation flow, so leaving it is harmless.
    pass
