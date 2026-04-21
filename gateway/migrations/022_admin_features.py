"""Admin tooling: user impersonation, feature flags, editable email templates.

Adds five tables:
  - impersonation_sessions  (admin → target user view-as sessions, with reason)
  - impersonation_actions   (per-request audit for each impersonation session)
  - feature_flags           (global/tier/user/rollout gating)
  - feature_flag_events     (optional evaluation audit; disabled by default)
  - email_templates         (admin-overridable subject/body per template key;
                             file-based templates remain as fallback)

Additive only. Every CREATE is guarded.
"""

revision = "024"
down_revision = "021"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS impersonation_sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            cookie_token     TEXT NOT NULL UNIQUE,
            reason           TEXT NOT NULL,
            ip_address       TEXT,
            user_agent       TEXT,
            started_at       INTEGER NOT NULL,
            ended_at         INTEGER,
            action_count     INTEGER NOT NULL DEFAULT 0,
            end_reason       TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_imp_sess_admin ON impersonation_sessions(admin_user_id, started_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_imp_sess_target ON impersonation_sessions(target_user_id, started_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_imp_sess_active ON impersonation_sessions(ended_at) WHERE ended_at IS NULL")

    c.execute("""
        CREATE TABLE IF NOT EXISTS impersonation_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES impersonation_sessions(id) ON DELETE CASCADE,
            timestamp    INTEGER NOT NULL,
            method       TEXT NOT NULL,
            path         TEXT NOT NULL,
            status_code  INTEGER,
            was_blocked  INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_imp_actions_session ON impersonation_actions(session_id, timestamp)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            key                    TEXT NOT NULL UNIQUE,
            name                   TEXT NOT NULL,
            description            TEXT,
            enabled_globally       INTEGER NOT NULL DEFAULT 0,
            enabled_for_tiers      TEXT NOT NULL DEFAULT '[]',
            enabled_for_user_ids   TEXT NOT NULL DEFAULT '[]',
            disabled_for_user_ids  TEXT NOT NULL DEFAULT '[]',
            rollout_percentage     INTEGER NOT NULL DEFAULT 0,
            created_at             INTEGER NOT NULL,
            updated_at             INTEGER NOT NULL,
            updated_by_admin_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_key ON feature_flags(key)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS feature_flag_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            flag_key   TEXT NOT NULL,
            user_id    INTEGER,
            result     INTEGER NOT NULL,
            timestamp  INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_ff_events_key_ts ON feature_flag_events(flag_key, timestamp)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS email_templates (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            key                   TEXT NOT NULL UNIQUE,
            subject               TEXT NOT NULL,
            body_html             TEXT NOT NULL,
            body_text             TEXT,
            variables             TEXT NOT NULL DEFAULT '[]',
            is_active             INTEGER NOT NULL DEFAULT 1,
            updated_at            INTEGER NOT NULL,
            updated_by_admin_id   INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_templates_key ON email_templates(key)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS impersonation_actions")
    c.execute("DROP TABLE IF EXISTS impersonation_sessions")
    c.execute("DROP TABLE IF EXISTS feature_flag_events")
    c.execute("DROP TABLE IF EXISTS feature_flags")
    c.execute("DROP TABLE IF EXISTS email_templates")
