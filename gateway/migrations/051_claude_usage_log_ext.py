"""Claude-API usage log — tolerant of earlier partial migrations.

An earlier migration (025_claude_usage_log.py) already created
``claude_usage_log``. This migration is intentionally a no-op on a tree
where 025 ran, and an idempotent create on a tree where it didn't —
so parallel branches that start from different schema baselines all
converge to the same shape.

Also backfills two columns the later features need but that 025 didn't
ship:

  - ``request_id``  — UUID for cross-referencing a row to its log trail
  - ``user_id``     — nullable, so cost attribution per user works for
                       the intelligence chat and summariser features

Additive only. Safe to re-run.
"""

revision = "051"
down_revision = "050"


def _existing_cols(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c):
    if not _table_exists(c, "claude_usage_log"):
        c.execute("""
            CREATE TABLE claude_usage_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       INTEGER NOT NULL,
                feature         TEXT NOT NULL,
                model           TEXT NOT NULL,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                cost_usd        REAL NOT NULL DEFAULT 0.0,
                cached_hit      INTEGER NOT NULL DEFAULT 0,
                request_id      TEXT,
                user_id         INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_ts ON claude_usage_log(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_feature_ts ON claude_usage_log(feature, timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_user ON claude_usage_log(user_id, timestamp)")
        return

    cols = _existing_cols(c, "claude_usage_log")
    if "request_id" not in cols:
        c.execute("ALTER TABLE claude_usage_log ADD COLUMN request_id TEXT")
    if "user_id" not in cols:
        c.execute("ALTER TABLE claude_usage_log ADD COLUMN user_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_ts ON claude_usage_log(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_feature_ts ON claude_usage_log(feature, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_claude_usage_user ON claude_usage_log(user_id, timestamp)")


def downgrade(c):
    # Additive columns left alone (SQLite pre-3.35 can't drop columns).
    c.execute("DROP INDEX IF EXISTS idx_claude_usage_user")
