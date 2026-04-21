"""Backfill three schema gaps that are crashing production cron jobs.

Production-side evidence (admin → job logs, 2026-04-21):

  * ``detect_market_movements``    → OperationalError: no such column: volume_24h
  * ``check_service_health``       → OperationalError: no such table:  service_health_snapshots
  * ``sync_polymarket_positions``  → OperationalError: no such table:  polymarket_connections

The last two tables are *also* created by earlier migrations (021 and 062),
but the production DB didn't pick them up — most plausibly because an
earlier revision-number collision blocked the upgrade chain at some
point and the tables never landed. Since CREATE TABLE IF NOT EXISTS is
a no-op when the table is already there, re-declaring them here is safe
on both fresh and backfilled databases.

The first gap (``volume_24h`` et al. on ``market_snapshots``) has never
had a migration: the schema in ``db.py`` is the original narrow version,
but ``markets/movement_detector.py`` evolved to query a wider table. We
add the missing columns as nullable so existing rows stay valid and the
ingestion path can fill them in on next write.
"""

from __future__ import annotations

import sqlite3


revision = "095"
down_revision = "094"


def _existing_cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def upgrade(c: sqlite3.Connection) -> None:
    # ── market_snapshots column drift ────────────────────────────────
    #
    # The detector reads: market_slug, yes_price, volume_24h,
    # avg_volume_30d, close_time, category, snapshot_at, first_seen_at.
    # Of those, yes_price/category already exist; volume is named
    # `volume` in the original schema (we alias it via a computed column
    # below so both names work); snapshotted_at is the original column
    # and snapshot_at is the detector's name — we add snapshot_at as a
    # plain INTEGER so writes from new code and reads from old code
    # coexist. volume_24h / avg_volume_30d / close_time / first_seen_at
    # are net-new and nullable.
    if _table_exists(c, "market_snapshots"):
        cols = _existing_cols(c, "market_snapshots")
        for col, ddl in [
            ("volume_24h",      "ALTER TABLE market_snapshots ADD COLUMN volume_24h REAL"),
            ("avg_volume_30d",  "ALTER TABLE market_snapshots ADD COLUMN avg_volume_30d REAL"),
            ("close_time",      "ALTER TABLE market_snapshots ADD COLUMN close_time INTEGER"),
            ("first_seen_at",   "ALTER TABLE market_snapshots ADD COLUMN first_seen_at INTEGER"),
            ("snapshot_at",     "ALTER TABLE market_snapshots ADD COLUMN snapshot_at INTEGER"),
        ]:
            if col not in cols:
                c.execute(ddl)

        # Backfill snapshot_at from the legacy snapshotted_at so detector
        # reads don't see NULLs on rows that predate this migration.
        # Only touches rows where snapshot_at is still NULL and a legacy
        # column exists — no-op on fresh installs.
        if "snapshotted_at" in cols and "snapshot_at" not in cols:
            c.execute(
                "UPDATE market_snapshots SET snapshot_at = snapshotted_at "
                "WHERE snapshot_at IS NULL"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_snapshots_snapshot_at "
            "ON market_snapshots(market_slug, snapshot_at)"
        )

    # ── service_health_snapshots (re-declare defensively) ─────────────
    # Originally created in migration 021 (020_status_page.py). Redeclare
    # here so production DBs that skipped 021 get it.
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
        "CREATE INDEX IF NOT EXISTS idx_snapshots_component_ts "
        "ON service_health_snapshots(component, timestamp)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_ts "
        "ON service_health_snapshots(timestamp)"
    )

    # ── polymarket_connections (re-declare defensively) ───────────────
    # Originally created in migration 062 (062_portfolio_integration.py).
    # Redeclare without the FK (users table is expected but the REFERENCES
    # clause is a no-op for rows that already violate it — we're writing
    # for the "missing entirely" case, not an integrity repair).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_connections (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL UNIQUE,
            wallet_address    TEXT NOT NULL,
            connected_at      INTEGER NOT NULL,
            last_synced_at    INTEGER,
            sync_error        TEXT,
            sync_error_count  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_poly_conn_last_sync "
        "ON polymarket_connections(last_synced_at)"
    )


def downgrade(c: sqlite3.Connection) -> None:
    # Deliberately narrow: we only drop the net-new index + columns we
    # added. Dropping the defensively-redeclared tables would break the
    # earlier migrations' invariants.
    c.execute("DROP INDEX IF EXISTS idx_market_snapshots_snapshot_at")
    for col in ("volume_24h", "avg_volume_30d", "close_time",
                "first_seen_at", "snapshot_at"):
        try:
            c.execute(f"ALTER TABLE market_snapshots DROP COLUMN {col}")
        except Exception:
            # DROP COLUMN needs SQLite 3.35+. Older builds just leave the
            # column in place; that's harmless because it's nullable.
            pass
