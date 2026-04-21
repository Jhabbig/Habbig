"""Per-user onboarding state.

Tracks where each user is in the guided first-run flow: which step
they're on, whether they've completed or dismissed it, and which
post-signup goals they've checked off. The goals JSON is a free-form
object like ``{"follow_source": true, "enable_notifications": false}``.
It's not authoritative — the per-goal checkmarks in the getting-
started widget read ``user_first_week_goals`` (migration 091) for
canonical state. This JSON stays here for lightweight "has the user
picked topics yet" reads that don't need a second table lookup.

Idempotent: CREATE TABLE IF NOT EXISTS.

users.onboarding_completed + onboarding_completed_at already exist
(added in db.init_db lightweight migration block) — we do NOT touch
them here; this table lives in parallel and carries the richer state
that doesn't belong on the user row.
"""

revision = "090"
down_revision = "081"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_onboarding (
            user_id          INTEGER PRIMARY KEY,
            started_at       INTEGER NOT NULL,
            completed_at     INTEGER,
            step_completed   INTEGER NOT NULL DEFAULT 0,
            dismissed        INTEGER NOT NULL DEFAULT 0,
            goals_completed  TEXT NOT NULL DEFAULT '{}',
            widget_dismissed_at INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_onboarding_completed ON user_onboarding(completed_at)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_user_onboarding_completed")
    c.execute("DROP TABLE IF EXISTS user_onboarding")
