"""Remove 2FA completely — drop columns + tables added in migration 006.

The TOTP/email-OTP implementation was broken and created user friction.
This migration reverses everything 006 added EXCEPT `audit_log`, which
is kept because it logs general admin actions, not just 2FA ones.

Drops:
  - users columns: totp_enabled, totp_secret, totp_setup_at,
                   email_otp_enabled, two_fa_method, two_fa_verified_at,
                   backup_codes, backup_codes_generated_at
  - sessions columns: two_fa_verified, two_fa_verified_at,
                      pending_totp_secret, pending_totp_secret_at
  - tables: two_fa_attempts, email_otps

Also cleans `audit_log` of the two 2FA-specific action types.

Requires SQLite 3.35+ for ALTER TABLE ... DROP COLUMN. Falls back to a
table-rebuild for older versions (code path kept for parity; not exercised
on current server since it runs SQLite 3.37+).
"""

revision = "019"
down_revision = "018"


_USER_COLS_TO_DROP = (
    "totp_enabled",
    "totp_secret",
    "totp_setup_at",
    "email_otp_enabled",
    "two_fa_method",
    "two_fa_verified_at",
    "backup_codes",
    "backup_codes_generated_at",
)

_SESSION_COLS_TO_DROP = (
    "two_fa_verified",
    "two_fa_verified_at",
    "pending_totp_secret",
    "pending_totp_secret_at",
)


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _drop_column_safely(c, table: str, col: str) -> None:
    """ALTER TABLE ... DROP COLUMN with a graceful no-op if unsupported."""
    cols = _existing_cols(c, table)
    if col not in cols:
        return
    try:
        c.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    except Exception:
        # SQLite < 3.35 — leave the column in place. The column is unused at
        # the code level (helpers removed), so it's harmless orphan data.
        pass


def upgrade(c):
    # 1. Drop the 2FA-specific tables outright.
    c.execute("DROP TABLE IF EXISTS two_fa_attempts")
    c.execute("DROP TABLE IF EXISTS email_otps")

    # 2. Drop each 2FA column from users.
    for col in _USER_COLS_TO_DROP:
        _drop_column_safely(c, "users", col)

    # 3. Drop each 2FA column from sessions.
    for col in _SESSION_COLS_TO_DROP:
        _drop_column_safely(c, "sessions", col)

    # 4. Scrub the two 2FA-specific audit log action types. The audit_log
    #    table itself is kept (it logs non-2FA admin actions too).
    try:
        c.execute(
            "DELETE FROM audit_log WHERE action IN ('admin.2fa_setup', 'admin.2fa_disable')"
        )
    except Exception:
        # If the table doesn't exist yet (fresh DB), nothing to delete.
        pass


def downgrade(c):
    # Re-add the columns + tables exactly as migration 006 did. We don't
    # restore the deleted audit rows — they're gone for good.
    user_cols = _existing_cols(c, "users")
    if "totp_enabled" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
    if "totp_secret" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
    if "totp_setup_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN totp_setup_at INTEGER")
    if "email_otp_enabled" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN email_otp_enabled INTEGER NOT NULL DEFAULT 0")
    if "two_fa_method" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN two_fa_method TEXT")
    if "two_fa_verified_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN two_fa_verified_at INTEGER")
    if "backup_codes" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN backup_codes TEXT")
    if "backup_codes_generated_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN backup_codes_generated_at INTEGER")

    sess_cols = _existing_cols(c, "sessions")
    if "two_fa_verified" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN two_fa_verified INTEGER NOT NULL DEFAULT 0")
    if "two_fa_verified_at" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN two_fa_verified_at INTEGER")
    if "pending_totp_secret" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN pending_totp_secret TEXT")
    if "pending_totp_secret_at" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN pending_totp_secret_at INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS two_fa_attempts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            method      TEXT NOT NULL,
            success     INTEGER NOT NULL DEFAULT 0,
            ip_address  TEXT,
            created_at  INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_2fa_attempts_user_ts ON two_fa_attempts(user_id, created_at)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS email_otps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash   TEXT NOT NULL,
            code_salt   TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL,
            used_at     INTEGER,
            ip_address  TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_otps_user ON email_otps(user_id, expires_at)")
