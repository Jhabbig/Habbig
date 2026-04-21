"""First-week goals — one row per (user, goal).

Goals are identified by stable string keys. The current set:

  follow_3_sources
  save_1_prediction
  enable_notifications
  visit_5_distinct_tabs
  view_1_market_detail
  complete_first_prediction

Completion is stamped via ``completed_at`` — a NULL row is an explicit
placeholder (the widget shows it as unchecked). Callers insert with
``INSERT OR IGNORE`` so re-marking a done goal is a no-op.

Paired with user_onboarding.widget_dismissed_at so the getting-started
widget can auto-hide 14 days after signup OR once all six goals ship
OR after a user explicitly dismisses it.
"""

revision = "091"
down_revision = "090"


ALL_GOALS = (
    "follow_3_sources",
    "save_1_prediction",
    "enable_notifications",
    "visit_5_distinct_tabs",
    "view_1_market_detail",
    "complete_first_prediction",
)


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_first_week_goals (
            user_id       INTEGER NOT NULL,
            goal_key      TEXT NOT NULL,
            completed_at  INTEGER,
            PRIMARY KEY (user_id, goal_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_first_week_goals_user ON user_first_week_goals(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_first_week_goals_done ON user_first_week_goals(completed_at)")


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_first_week_goals_done")
    c.execute("DROP INDEX IF EXISTS idx_first_week_goals_user")
    c.execute("DROP TABLE IF EXISTS user_first_week_goals")
