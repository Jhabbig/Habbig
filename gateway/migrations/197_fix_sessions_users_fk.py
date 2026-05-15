"""Fix dangling sessions.user_id FK reference (mirror of migration 188).

Bug history
-----------
Migration 195 (`drop bankroll_usd column`) rebuilt the `users` table via
the standard SQLite column-drop dance:

  1. rename users      -> users_drop_bankroll_usd
  2. CREATE TABLE users (... without bankroll_usd)
  3. INSERT INTO users SELECT ... FROM users_drop_bankroll_usd
  4. DROP TABLE users_drop_bankroll_usd

Step 1 triggers SQLite's automatic-FK-rewrite: every other table whose
CREATE SQL referenced `users(id)` got its stored DDL silently mutated to
reference `users_drop_bankroll_usd(id)`. Step 4 then drops the temp
table, leaving the dangling FK behind. Sessions stored as

    FOREIGN KEY ("user_id") REFERENCES "users_drop_bankroll_usd"(id) ON DELETE CASCADE

even though the application never declared it that way. Every
INSERT into sessions then 500s with::

    sqlite3.OperationalError: no such table: main.users_drop_bankroll_usd

The bug went undetected for migrations 196-… because most servers had
no fresh login flow exercise the path; the moment the auth refactor
landed (the /login → POST /auth/login → create_session path), every
login attempt 500'd.

Affected tables on inspection: ``sessions`` only.

Fix
---
Rebuild the ``sessions`` table with the FK clause written correctly.
Identical pattern to migration 188 (which fixed the same class of bug
on ``users.invite_token_id``). Idempotent — skips the rebuild if the
stored CREATE SQL no longer mentions ``users_drop_bankroll_usd``.
"""

from __future__ import annotations

revision = "197"
down_revision = "196"


def _sessions_sql_has_dangling_fk(c) -> bool:
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    if not row or not row["sql"]:
        return False
    return "users_drop_bankroll_usd" in row["sql"]


def upgrade(c):
    if not _sessions_sql_has_dangling_fk(c):
        # Already fixed — either a fresh DB built from db.py SCHEMA, or a
        # prior run of this migration. No-op.
        return

    c.execute("PRAGMA foreign_keys = OFF")
    try:
        c.execute("ALTER TABLE sessions RENAME TO sessions_old_fk_fix")
        cols = c.execute("PRAGMA table_info(sessions_old_fk_fix)").fetchall()
        col_decls = []
        col_names = []
        for col in cols:
            name = col["name"]
            col_names.append(name)
            pieces = [f'"{name}"', col["type"] or ""]
            if col["notnull"]:
                pieces.append("NOT NULL")
            if col["dflt_value"] is not None:
                pieces.append(f'DEFAULT {col["dflt_value"]}')
            if col["pk"]:
                pieces.append("PRIMARY KEY")
                if (col["type"] or "").upper() == "INTEGER":
                    pieces.append("AUTOINCREMENT")
            col_decls.append(" ".join(p for p in pieces if p))

        # token_hash UNIQUE constraint lives on an autoindex
        # (sqlite_autoindex_sessions_1) created from the column-level
        # UNIQUE in db.py's SCHEMA. We re-declare it here so the rebuild
        # preserves the same uniqueness guarantee.
        extras = []
        for col in cols:
            if col["name"] == "token_hash":
                # column-level UNIQUE already encoded; PRAGMA didn't surface
                # it but it's part of the original CREATE — restore it.
                extras.append('UNIQUE ("token_hash")')

        fk = (
            'FOREIGN KEY ("user_id") REFERENCES users(id) '
            'ON DELETE CASCADE'
        )
        parts = col_decls + extras + [fk]
        c.execute(f"CREATE TABLE sessions ({', '.join(parts)})")
        quoted_cols = ", ".join(f'"{n}"' for n in col_names)
        c.execute(
            f"INSERT INTO sessions ({quoted_cols}) "
            f"SELECT {quoted_cols} FROM sessions_old_fk_fix"
        )
        c.execute("DROP TABLE sessions_old_fk_fix")

        # Recreate the canonical indexes — match db.py SCHEMA. Anything
        # else recreates itself at server boot via CREATE INDEX IF NOT
        # EXISTS, but token_hash + user_id are hot enough to recreate here.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_token_hash "
            "ON sessions(token_hash)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
        )
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def downgrade(c):
    # No-op: re-introducing the dangling FK would deliberately re-break
    # logins. The migration is corrective.
    pass
