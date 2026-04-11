"""User bankroll and Kelly fraction preferences (F16)."""

revision = "017"
down_revision = "016"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    cols = _existing_cols(c, "users")
    if "bankroll" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN bankroll REAL")
    if "kelly_fraction" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN kelly_fraction REAL NOT NULL DEFAULT 0.5")


def downgrade(c):
    pass  # additive-only
