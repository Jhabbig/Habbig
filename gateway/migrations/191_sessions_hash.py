"""Hash legacy session tokens at rest.

Audit finding (HIGH)
--------------------
The legacy `sessions` table stored raw cookie tokens as the primary key
(plaintext, queryable via `WHERE token = ?`). A database dump therefore
exposed live, usable 90-day cookies — any reader of the DB file could
copy a `token` value straight into the session cookie and assume that
user's session. The newer `user_sessions` table already hashes at rest
(`token_hash = sha256(raw)`), but both rows were written on every
login, so the weaker row remained the canonical compromise vector.

Fix
---
Rebuild `sessions` so the PK is no longer the raw token. The cookie
still ships the raw value to the browser, but only its SHA-256 hash
is persisted server-side. Read paths in `queries/auth.py` re-hash the
incoming cookie before SELECT, matching the convention the hardened
`user_sessions` table already uses.

Idempotency
-----------
The migration inspects the live schema and skips the rebuild if the
`token` column is already absent (i.e. the migration has already run,
or the DB was freshly created from `db.py` SCHEMA after the SCHEMA
edit shipped). Re-running the migration is therefore a no-op.

Caveat
------
Existing logged-in users on the legacy cookie will need to log in
again — we cannot recover the raw token from a SHA-256 digest, so
their pre-migration cookies will fail the `_hash_session_token`
re-lookup and be treated as unknown. The hardened `user_sessions`
cookie (separate cookie name) is unaffected.
"""

from __future__ import annotations

import hashlib

revision = "191"
down_revision = "188"


def _sessions_has_raw_token(c) -> bool:
    """True iff the live `sessions` table still carries the raw `token` column.

    Used as the idempotency guard. We don't trust just the presence of
    `token_hash` because a partial prior run could have added the hash
    column without dropping `token`.
    """
    cols = {row["name"] for row in c.execute("PRAGMA table_info(sessions)")}
    return "token" in cols


def upgrade(c):
    if not _sessions_has_raw_token(c):
        # Already migrated, or fresh DB built from the post-fix SCHEMA.
        return

    # SQLite can't ALTER a PK in place, so we do the classic rebuild dance:
    # rename -> create new shape -> copy with hashed token -> drop old.
    # FK enforcement is disabled for the rebuild so the transient rename
    # doesn't break the sessions.user_id -> users(id) FK; the migration
    # runner wraps us in a single transaction, so this scope is local.
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        # Snapshot existing column shape so we preserve every column added
        # by prior migrations (csrf_token, csrf_created_at, two_fa_verified,
        # two_fa_verified_at, pending_totp_secret, pending_totp_secret_at)
        # and any later additions.
        cols = c.execute("PRAGMA table_info(sessions)").fetchall()
        # Drop the raw `token` column from the rebuilt shape. Everything
        # else carries forward verbatim. We synthesize a fresh integer PK
        # (`id`) because SQLite needs a single PK clause on rebuild.
        kept_cols = [col for col in cols if col["name"] != "token"]
        col_decls = ['"id" INTEGER PRIMARY KEY AUTOINCREMENT', '"token_hash" TEXT NOT NULL UNIQUE']
        copy_names = []
        for col in kept_cols:
            name = col["name"]
            if name == "id":
                continue
            copy_names.append(name)
            pieces = [f'"{name}"', col["type"] or ""]
            if col["notnull"]:
                pieces.append("NOT NULL")
            if col["dflt_value"] is not None:
                pieces.append(f'DEFAULT {col["dflt_value"]}')
            col_decls.append(" ".join(p for p in pieces if p))

        fk_decl = (
            'FOREIGN KEY ("user_id") REFERENCES users(id) ON DELETE CASCADE'
        )

        c.execute("ALTER TABLE sessions RENAME TO sessions_old_hash")
        c.execute(
            f"CREATE TABLE sessions ({', '.join(col_decls)}, {fk_decl})"
        )

        # Hash every existing raw token and copy the row across.
        quoted_copy = ", ".join(f'"{n}"' for n in copy_names)
        rows = c.execute(
            f"SELECT token, {quoted_copy} FROM sessions_old_hash"
        ).fetchall()
        for row in rows:
            raw = row["token"]
            if not raw:
                continue
            token_hash = hashlib.sha256(raw.encode()).hexdigest()
            placeholders = ", ".join(["?"] * (1 + len(copy_names)))
            values = [token_hash] + [row[n] for n in copy_names]
            c.execute(
                f"INSERT INTO sessions (token_hash, {quoted_copy}) "
                f"VALUES ({placeholders})",
                values,
            )

        c.execute("DROP TABLE sessions_old_hash")

        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_token_hash "
            "ON sessions(token_hash)"
        )
    finally:
        c.execute("PRAGMA foreign_keys = ON")


def downgrade(c):
    # No-op. Reintroducing the raw-token PK would be a security regression;
    # the audit finding the migration exists to fix would simply reappear.
    pass
