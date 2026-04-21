"""Performance indexes for the caching + N+1 cleanup pass.

The gateway already indexes most foreign keys and common filter columns
(see db.py schema and earlier migrations). This migration adds the few
columns that profiling flagged as full-scan offenders:

* `predictions.resolved` — every credibility-recompute, resolution-polling
  and list query filters on this; it was a full table scan.
* `predictions.(source_handle, resolved)` — composite index makes the
  per-source resolved scan in recompute_all_credibilities() an index
  lookup. Leaves the single-column idx_predictions_source in place for
  list-by-source queries that don't care about resolved.
* `predictions.(market_id, resolved)` — same logic for
  resolve_predictions_for_market().
* `predictions.(extracted_at, resolved)` — feed / list_recent_predictions
  orders by extracted_at and usually excludes resolved ones.
* `sessions.expires_at` — the hourly session sweep scans this column.
* `source_credibility.accuracy_unlocked` — the public /sources/{handle}
  page and sitemap filter on this to find rated sources.

Also pins `journal_mode = WAL` on the database file itself. db.py sets
this on every connection, which is idempotent — but once the file is in
WAL mode it stays there, so setting it here too means a fresh clone
picks up the faster mode immediately rather than on first connection.

Additive only; `DROP INDEX IF EXISTS` in downgrade.
"""

from __future__ import annotations

import sqlite3


revision = "035"
down_revision = "034"


def upgrade(c: sqlite3.Connection) -> None:
    # ── Predictions hot path ────────────────────────────────────────────
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_resolved "
        "ON predictions(resolved)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_source_resolved "
        "ON predictions(source_handle, resolved)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_market_resolved "
        "ON predictions(market_id, resolved)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_extracted_resolved "
        "ON predictions(extracted_at DESC, resolved)"
    )

    # ── Session expiry sweep ────────────────────────────────────────────
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires "
        "ON sessions(expires_at)"
    )

    # ── Public source gate ──────────────────────────────────────────────
    # /sources/{handle} redirects to 404 unless accuracy_unlocked = 1, and
    # the sitemap job enumerates rated sources. The WHERE-clause filter
    # benefits from a single-column index here; queries that also filter
    # on source_handle still use the existing UNIQUE(source_handle).
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_cred_unlocked "
        "ON source_credibility(accuracy_unlocked)"
    )

    # ── Persistent journal mode ─────────────────────────────────────────
    # `journal_mode = WAL` is per-DB-file (persists), not per-connection.
    # db.py still sets it on every connection for belt-and-braces, but
    # running it once here means operators who reset auth.db (e.g. fresh
    # dev clone, restore from backup) start in WAL without having to wait
    # for the first connection.
    c.execute("PRAGMA journal_mode = WAL")


def downgrade(c: sqlite3.Connection) -> None:
    c.execute("DROP INDEX IF EXISTS idx_predictions_resolved")
    c.execute("DROP INDEX IF EXISTS idx_predictions_source_resolved")
    c.execute("DROP INDEX IF EXISTS idx_predictions_market_resolved")
    c.execute("DROP INDEX IF EXISTS idx_predictions_extracted_resolved")
    c.execute("DROP INDEX IF EXISTS idx_sessions_expires")
    c.execute("DROP INDEX IF EXISTS idx_cred_unlocked")
    # journal_mode stays — switching back to DELETE on a production DB
    # would lose any in-flight WAL checkpoint and the fast path is strictly
    # better. Operator can flip manually via PRAGMA if they really want to.
