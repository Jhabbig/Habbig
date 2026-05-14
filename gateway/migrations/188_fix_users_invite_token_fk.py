"""Fix dangling users.invite_token_id FK reference.

Bug history
-----------
Migration 162 (`integrity_cleanup`) rebuilt both `users` and `invite_tokens`
to add `ON DELETE SET NULL` to two FKs. The rebuild order was:

  1. rename users        -> users_old, recreate users with FK -> invite_tokens(id)
  2. rename invite_tokens -> invite_tokens_old, recreate invite_tokens

Step 2 triggers SQLite's automatic-FK-rewrite: when a table is renamed,
SQLite rewrites every other table's stored CREATE SQL so references to
the old name follow it to the new name. After step 2's first ALTER, the
`users` table's stored CREATE SQL changed from

    invite_token_id INTEGER REFERENCES invite_tokens(id)

to

    invite_token_id INTEGER REFERENCES "invite_tokens_old"(id)

even though the application never declared it that way. When step 2 then
drops `invite_tokens_old`, the FK references a table that no longer
exists. SQLite tolerates this at SELECT time but fails on every
INSERT/UPDATE under `PRAGMA foreign_keys = ON` with

    OperationalError: no such table: main.invite_tokens_old

The bug went undetected for migrations 163-187 because most servers
ran `ensure_dev_user()` once before 162 was added, so the only INSERT
that exercises this path stopped firing. New deployments (or any path
that lazily upserts an admin row) hits the bug immediately.

Fix
---
Rebuild the `users` table one more time with the FK clause written
correctly. Same shape as `_rebuild_users` in migration 162 but with the
order of operations fixed: we explicitly drop the auto-rewritten FK
and replace it with a fresh `FOREIGN KEY ... REFERENCES invite_tokens`
declaration. Idempotent — skips the rebuild if the stored CREATE SQL no
longer mentions `invite_tokens_old`.
"""

from __future__ import annotations

revision = "188"
down_revision = "187"


def _users_sql_has_dangling_fk(c) -> bool:
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or not row["sql"]:
        return False
    return "invite_tokens_old" in row["sql"]


def upgrade(c):
    if not _users_sql_has_dangling_fk(c):
        # Already fixed — either a fresh DB built from db.py SCHEMA, or a
        # prior run of this migration. No-op.
        return

    # Disable FK enforcement for the rebuild. The migration runner wraps us
    # in a single transaction so this scope doesn't leak.
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        # Snapshot column declarations from the existing table. PRAGMA
        # table_info gives name/type/notnull/dflt/pk — exactly the shape
        # migration 162 used to do the same operation.
        c.execute("ALTER TABLE users RENAME TO users_old_fk_fix")
        cols = c.execute("PRAGMA table_info(users_old_fk_fix)").fetchall()
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

        # Re-declare both FKs explicitly. We keep the second one (referred_by
        # -> users) because PRAGMA also stripped it during 162's snapshot.
        fk_invite = (
            'FOREIGN KEY ("invite_token_id") REFERENCES invite_tokens(id) '
            "ON DELETE SET NULL"
        )
        fk_referrer = (
            'FOREIGN KEY ("referred_by_user_id") REFERENCES users(id) '
            "ON DELETE SET NULL"
        )
        # Only include the referrer FK if the column exists (added in a
        # later migration; defensively skip on older snapshots).
        fks = [fk_invite]
        if "referred_by_user_id" in col_names:
            fks.append(fk_referrer)

        c.execute(f"CREATE TABLE users ({', '.join(col_decls)}, {', '.join(fks)})")
        quoted_cols = ", ".join(f'"{n}"' for n in col_names)
        c.execute(
            f"INSERT INTO users ({quoted_cols}) SELECT {quoted_cols} FROM users_old_fk_fix"
        )
        c.execute("DROP TABLE users_old_fk_fix")

        # Rebuild the canonical indexes — anything else recreates itself
        # at server boot via CREATE INDEX IF NOT EXISTS.
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        )
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def downgrade(c):
    # No-op: re-introducing the dangling FK would deliberately break the
    # database. The migration is additive in the sense that it only fixes
    # corruption.
    pass
