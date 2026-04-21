"""Public status page — incidents, component health snapshots, subscriptions.

Adds the schema for /status (status.github.com-style). Four tables:

  * incidents — manually-created or auto-created outage records
  * incident_updates — append-only timeline of status/message changes
  * service_health_snapshots — per-component per-minute rolling snapshots
  * status_subscriptions — one-click email subscriptions to incident updates

All timestamps are unix epoch seconds (INTEGER) to match the rest of the
codebase — no TEXT ISO strings, no REAL seconds.

The snapshots table is the bulk of the write volume: one row per
component per minute (~8700 rows/component/90 days). Indexed on
(component, timestamp) so the 90-day uptime aggregation stays cheap.
"""

revision = "021"
down_revision = "019"


def upgrade(c):
    # ── incidents ──
    # Resolved incidents have `resolved_at` filled and `status = 'resolved'`.
    # `affected_components` is a JSON array stored as TEXT.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at           INTEGER NOT NULL,
            resolved_at          INTEGER,
            severity             TEXT NOT NULL DEFAULT 'minor',
            affected_components  TEXT NOT NULL DEFAULT '[]',
            title                TEXT NOT NULL,
            description          TEXT NOT NULL DEFAULT '',
            status               TEXT NOT NULL DEFAULT 'investigating',
            auto_created         INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_created_at ON incidents(created_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status)"
    )

    # ── incident_updates ──
    # Append-only; never deleted. The first row per incident is the initial
    # description; later rows are admin-posted updates.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_updates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
            timestamp   INTEGER NOT NULL,
            status      TEXT NOT NULL,
            message     TEXT NOT NULL DEFAULT ''
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_incident_updates_incident ON incident_updates(incident_id, timestamp)"
    )

    # ── service_health_snapshots ──
    # One row per component per cron tick. Retention is policy (not enforced
    # by schema) — the uptime helper only reads the last 90 days.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS service_health_snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         INTEGER NOT NULL,
            component         TEXT NOT NULL,
            status            TEXT NOT NULL,
            response_time_ms  REAL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_component_ts ON service_health_snapshots(component, timestamp)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON service_health_snapshots(timestamp)"
    )

    # ── status_subscriptions ──
    # Public email list. `components` is a JSON array (e.g. ["app","scraper"])
    # or the string "all" to receive every incident.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS status_subscriptions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            email              TEXT UNIQUE NOT NULL,
            subscribed_at      INTEGER NOT NULL,
            unsubscribe_token  TEXT UNIQUE NOT NULL,
            components         TEXT NOT NULL DEFAULT '"all"',
            confirmed          INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_status_subs_token ON status_subscriptions(unsubscribe_token)"
    )


def downgrade(c):
    # Drop in FK-safe order (child tables first).
    c.execute("DROP TABLE IF EXISTS incident_updates")
    c.execute("DROP TABLE IF EXISTS service_health_snapshots")
    c.execute("DROP TABLE IF EXISTS status_subscriptions")
    c.execute("DROP TABLE IF EXISTS incidents")
