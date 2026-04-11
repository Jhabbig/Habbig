"""User preferences for morning intelligence briefing (F7).

Pro users can opt into a daily personalized email with top edge markets,
new predictions from followed sources, and approaching resolutions.
"""

revision = "013"
down_revision = "012"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    cols = _existing_cols(c, "users")
    if "morning_briefing_enabled" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN morning_briefing_enabled INTEGER NOT NULL DEFAULT 0")
    if "morning_briefing_hour" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN morning_briefing_hour INTEGER NOT NULL DEFAULT 8")


def downgrade(c):
    pass  # additive-only; safe to leave columns in place
