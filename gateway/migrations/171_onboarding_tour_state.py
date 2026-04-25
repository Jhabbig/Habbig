"""Onboarding tour state — three columns on user_onboarding.

Adds:
  - tour_completed_at  (INTEGER, NULL = tour not yet completed)
  - tour_skipped       (INTEGER 0/1, default 0)
  - tour_skipped_at    (INTEGER, NULL until skipped)

The post-onboarding overlay tour (`gateway/static/js/onboarding_tour.js`)
gates on these. `should_show` is true iff the user has completed the
onboarding flow (`completed_at IS NOT NULL`) AND has neither completed
nor skipped the tour.

Idempotent — safe to re-run; each ALTER is gated on `PRAGMA table_info`.
"""

revision = "171"
down_revision = "170"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # The base table comes from migration 090. If it isn't present yet,
    # create it with all the bits we need so a fresh DB doesn't blow up.
    cols = _existing_cols(c, "user_onboarding") if c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_onboarding'"
    ).fetchone() else set()
    if not cols:
        # Mirror migration 090's schema verbatim, plus our three new columns.
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_onboarding (
                user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                started_at       INTEGER NOT NULL,
                completed_at     INTEGER,
                step_completed   INTEGER NOT NULL DEFAULT 0,
                dismissed        INTEGER NOT NULL DEFAULT 0,
                goals_completed  INTEGER NOT NULL DEFAULT 0,
                widget_dismissed_at INTEGER,
                tour_completed_at INTEGER,
                tour_skipped      INTEGER NOT NULL DEFAULT 0,
                tour_skipped_at   INTEGER
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_onboarding_completed "
            "ON user_onboarding(completed_at)"
        )
        return

    # Otherwise, add only the columns that don't exist yet.
    if "tour_completed_at" not in cols:
        c.execute("ALTER TABLE user_onboarding ADD COLUMN tour_completed_at INTEGER")
    if "tour_skipped" not in cols:
        c.execute(
            "ALTER TABLE user_onboarding ADD COLUMN tour_skipped INTEGER NOT NULL DEFAULT 0"
        )
    if "tour_skipped_at" not in cols:
        c.execute("ALTER TABLE user_onboarding ADD COLUMN tour_skipped_at INTEGER")


def downgrade(c):
    # SQLite 3.35+ supports DROP COLUMN; older falls through silently.
    for col in ("tour_completed_at", "tour_skipped", "tour_skipped_at"):
        try:
            c.execute(f"ALTER TABLE user_onboarding DROP COLUMN {col}")
        except Exception:
            pass
