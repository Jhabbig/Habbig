"""Two-factor authentication, audit log, and cookie/legal schema changes.

Additive only. Safe to re-run: every ALTER/CREATE is guarded.

Adds:
  - users: totp_enabled, totp_secret, totp_setup_at, email_otp_enabled,
           two_fa_method, two_fa_verified_at, backup_codes, backup_codes_generated_at
  - sessions: two_fa_verified, two_fa_verified_at,
              pending_totp_secret, pending_totp_secret_at
  - new tables: two_fa_attempts, email_otps, audit_log
"""

revision = "006"
down_revision = "005"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # ── users: 2FA columns ──────────────────────────────────────────────
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

    # ── sessions: 2FA verification flag + pending TOTP setup slot ───────
    sess_cols = _existing_cols(c, "sessions")
    if "two_fa_verified" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN two_fa_verified INTEGER NOT NULL DEFAULT 0")
    if "two_fa_verified_at" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN two_fa_verified_at INTEGER")
    if "pending_totp_secret" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN pending_totp_secret TEXT")
    if "pending_totp_secret_at" not in sess_cols:
        c.execute("ALTER TABLE sessions ADD COLUMN pending_totp_secret_at INTEGER")

    # ── two_fa_attempts (audit of verify calls, not rate-limit bucket) ──
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

    # ── email_otps (pending email one-time codes) ───────────────────────
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

    # ── audit_log (admin actions, append-only) ──────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp          INTEGER NOT NULL,
            admin_user_id      INTEGER,
            admin_email        TEXT,
            action             TEXT NOT NULL,
            target_type        TEXT,
            target_id          TEXT,
            target_description TEXT,
            before_state       TEXT,
            after_state        TEXT,
            ip_address         TEXT,
            user_agent         TEXT,
            request_id         TEXT,
            notes              TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_user_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id)")


def downgrade(c):
    # Additive-only — no-op. Matches convention from migration 003.
    pass
