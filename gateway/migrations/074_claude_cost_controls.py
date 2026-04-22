"""Claude cost controls — daily alert log + global kill switch.

Adds two tables the ops team needs to keep Claude spend bounded:

  ``claude_cost_alerts``
      One row per (alert_date, threshold) pair — written when the daily
      cost-check job observes yesterday's spend crossing a threshold.
      UNIQUE(alert_date, threshold_usd) so we don't re-notify on re-runs.

  ``claude_kill_switch``
      Single-row table. ``active=1`` → ``ai/client.py.call_claude`` short-
      circuits every uncached call and returns None so the dashboards
      degrade gracefully instead of running up more bill. Flipped by the
      daily job when cost > $200 or manually by a super-admin from
      ``/admin/ai-usage``.

Both tables are additive; nothing existing is touched.
"""

revision = "074"
down_revision = "073"


def _table_exists(c, name: str) -> bool:
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c):
    if not _table_exists(c, "claude_cost_alerts"):
        c.execute("""
            CREATE TABLE claude_cost_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_date      TEXT NOT NULL,
                threshold_usd   REAL NOT NULL,
                total_cost_usd  REAL NOT NULL,
                sent_at         INTEGER,
                UNIQUE(alert_date, threshold_usd)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_claude_cost_alerts_date "
            "ON claude_cost_alerts(alert_date)"
        )

    if not _table_exists(c, "claude_kill_switch"):
        c.execute("""
            CREATE TABLE claude_kill_switch (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                active          INTEGER NOT NULL DEFAULT 0,
                reason          TEXT,
                triggered_at    INTEGER,
                triggered_by    TEXT
            )
        """)
        # Seed the singleton row so UPDATEs work without a first-write race.
        c.execute("INSERT INTO claude_kill_switch (id, active) VALUES (1, 0)")


def downgrade(c):
    c.execute("DROP TABLE IF EXISTS claude_cost_alerts")
    c.execute("DROP TABLE IF EXISTS claude_kill_switch")
