"""Post-AUDIT #5 integrity cleanup.

Two fixes bundled because they share a downtime window:

1. Backfill NULL rows in `users.kelly_fraction`.
   Migration 017 added the column with `NOT NULL DEFAULT 0.5`. SQLite's
   `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT X` is supposed to fill
   existing rows, but on some historical sqlite versions (3.31 and
   earlier) the default propagation was lossy under WAL + incremental
   vacuum. Prod `PRAGMA integrity_check` failed with
   "NULL value in users.kelly_fraction" — this migration fixes that
   class of row so a re-run of integrity_check returns "ok".

2. Add `ON DELETE SET NULL` to the two FKs flagged in AUDIT #5 MED #1:
     users.invite_token_id          -> invite_tokens(id)
     invite_tokens.claimed_by_user_id -> users(id)
   Both currently declared bare REFERENCES, so deleting either row
   orphans the other silently. SET NULL is the right behaviour — the
   back-reference is cosmetic metadata, not a hard dependency.

   Done via the classic sqlite dance: rename → create with FKs → copy →
   drop → restore indexes. Each half runs only if the column shape
   actually needs changing, so the migration is idempotent.
"""

revision = "162"
down_revision = "161"


# ── 1: backfill kelly_fraction ──────────────────────────────────────────


def _fix_kelly_fraction(c):
    null_rows = c.execute(
        "SELECT COUNT(*) AS n FROM users WHERE kelly_fraction IS NULL"
    ).fetchone()
    n = int(null_rows["n"] if null_rows else 0)
    if n:
        c.execute("UPDATE users SET kelly_fraction = 0.5 WHERE kelly_fraction IS NULL")


# ── 2: rebuild users + invite_tokens with ON DELETE SET NULL FKs ───────


def _has_on_delete_set_null(c, table: str, col: str) -> bool:
    """Inspect the stored CREATE TABLE SQL rather than PRAGMA foreign_key_list
    because the latter doesn't expose the ON DELETE clause in sqlite < 3.32."""
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    if not row or not row["sql"]:
        return False
    sql = row["sql"].upper()
    # We're looking for "<col> ... REFERENCES ... ON DELETE SET NULL" anywhere.
    # Simple substring test is plenty — the ALTER loop below only fires once,
    # and a false-positive here just means we skip a safe no-op.
    needle = f"{col.upper()} "
    idx = sql.find(needle)
    if idx < 0:
        return False
    tail = sql[idx:]
    return "ON DELETE SET NULL" in tail


def _rebuild_users(c):
    """users.invite_token_id -> invite_tokens(id) ON DELETE SET NULL."""
    if _has_on_delete_set_null(c, "users", "invite_token_id"):
        return
    # Make the rebuild a single transaction — sqlite still applies it
    # atomically because the migration runner already wraps us.
    c.execute("ALTER TABLE users RENAME TO users_old")
    # Schema matches the canonical one in db.py SCHEMA, PLUS every ALTER
    # added by migrations 006-160. Rather than hand-maintain that list,
    # we snapshot PRAGMA table_info and rebuild from the result.
    cols = c.execute("PRAGMA table_info(users_old)").fetchall()
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
    fk_decl = (
        'FOREIGN KEY ("invite_token_id") REFERENCES invite_tokens(id) '
        "ON DELETE SET NULL"
    )
    c.execute(f"CREATE TABLE users ({', '.join(col_decls)}, {fk_decl})")
    quoted_cols = ", ".join(f'"{n}"' for n in col_names)
    c.execute(f"INSERT INTO users ({quoted_cols}) SELECT {quoted_cols} FROM users_old")
    c.execute("DROP TABLE users_old")
    # Rebuild the canonical indexes we know about — anything extra will be
    # restored by its own CREATE INDEX IF NOT EXISTS when server.py boots.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")


def _rebuild_invite_tokens(c):
    """invite_tokens.claimed_by_user_id -> users(id) ON DELETE SET NULL."""
    if _has_on_delete_set_null(c, "invite_tokens", "claimed_by_user_id"):
        return
    c.execute("ALTER TABLE invite_tokens RENAME TO invite_tokens_old")
    cols = c.execute("PRAGMA table_info(invite_tokens_old)").fetchall()
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
    fk_decl = (
        'FOREIGN KEY ("claimed_by_user_id") REFERENCES users(id) '
        "ON DELETE SET NULL"
    )
    c.execute(f"CREATE TABLE invite_tokens ({', '.join(col_decls)}, {fk_decl})")
    quoted_cols = ", ".join(f'"{n}"' for n in col_names)
    c.execute(f"INSERT INTO invite_tokens ({quoted_cols}) SELECT {quoted_cols} FROM invite_tokens_old")
    c.execute("DROP TABLE invite_tokens_old")
    c.execute("CREATE INDEX IF NOT EXISTS idx_invite_token ON invite_tokens(token)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status)")


def upgrade(c):
    # Turn FKs off for the rebuild so we can rename/copy without sqlite
    # rejecting the intermediate state. The migration runner wraps us in
    # a single transaction, so this won't leak outside the migration.
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        _fix_kelly_fraction(c)
        _rebuild_users(c)
        _rebuild_invite_tokens(c)
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def downgrade(c):
    # Additive-only — both changes are safe to leave in place. Downgrading
    # the kelly_fraction backfill would be destructive (what would we
    # replace non-null rows with?), and downgrading the FK clauses means
    # silently restoring the orphan-risk state AUDIT #5 flagged.
    pass
