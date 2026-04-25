"""Add per-user public-profile opt-in columns.

Four nullable columns and a unique partial index on profile_handle.

  public_profile_enabled  0/1 — gate that hides /u/{handle} when off.
  profile_handle          The slug under /u/. Lowercase letters/digits/_,
                          3–20 chars, unique across all users that have
                          set one. Index uses ``WHERE profile_handle IS
                          NOT NULL`` so the (huge) tail of users who
                          never set one don't bloat the index or fight
                          the NULL-uniqueness rules.
  profile_bio             Free text, ≤200 chars. Validated route-side
                          rather than via CHECK to keep this migration
                          replayable on legacy data.
  profile_avatar_url      Path under /_gateway_static/avatars/. NULL =
                          gravatar fallback.
"""

from __future__ import annotations


revision = "172"
down_revision = "171"


def upgrade(cur) -> None:
    # SQLite ALTER TABLE ADD COLUMN doesn't support ``IF NOT EXISTS``.
    # PRAGMA-check first so re-running is safe (e.g. if 173 fails after
    # 172 partially applied).
    existing = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "public_profile_enabled" not in existing:
        cur.execute(
            "ALTER TABLE users ADD COLUMN public_profile_enabled INTEGER DEFAULT 0"
        )
    if "profile_handle" not in existing:
        cur.execute("ALTER TABLE users ADD COLUMN profile_handle TEXT")
    if "profile_bio" not in existing:
        cur.execute("ALTER TABLE users ADD COLUMN profile_bio TEXT")
    if "profile_avatar_url" not in existing:
        cur.execute("ALTER TABLE users ADD COLUMN profile_avatar_url TEXT")
    if "profile_handle_changed_at" not in existing:
        # Tracks the last time the user set/changed their handle so the
        # 30-day cooldown doesn't need a separate audit query.
        cur.execute("ALTER TABLE users ADD COLUMN profile_handle_changed_at INTEGER")

    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_profile_handle "
        "ON users(profile_handle) WHERE profile_handle IS NOT NULL"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_users_profile_handle")
    # SQLite doesn't support DROP COLUMN before 3.35; leaving the columns
    # in place is harmless because they're nullable.
