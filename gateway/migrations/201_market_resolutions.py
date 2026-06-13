"""Add ``market_resolutions`` audit table for backtest provenance.

Why
---
Market resolutions are the ground truth the backtester scores predictions
against. Today resolution lives only as side effects on the
``predictions`` table: ``jobs/resolution_jobs.py::poll_market_resolutions``
(cron minute=17) calls
``queries/predictions.py::resolve_predictions_for_market`` which flips
``predictions.resolved`` / ``resolved_correct`` in place and persists NO
audit row. That means once a market settles we have no durable record of
*when* it resolved, *which outcome* won, *how* we learned it, or the raw
payload we resolved from — all of which the backtest needs for an
auditable, replayable verdict (the #1 YC deliverable).

``market_resolutions`` is that durable, append-only ledger. One row per
resolved market captures the canonical outcome plus full provenance so a
backtest run can be reconstructed and defended after the fact.

Columns
-------
- ``id``               surrogate PK.
- ``market_id``        Polymarket condition/market id (NOT NULL) — the
                       join key back to ``predictions.market_id``.
- ``market_slug``      human/slug key, mirrors ``market_snapshots.market_slug``
                       (snapshots are keyed on slug, NOT market_id), so a
                       backtest can reconcile predictions <-> snapshots
                       across both identifiers.
- ``resolved_outcome`` INTEGER: 1 = YES won, 0 = NO won (nullable until known).
- ``resolved_at``      epoch seconds the resolution was observed.
- ``resolved_via``     provenance tag, e.g. 'gamma', 'manual', 'cron'.
- ``source_data``      raw JSON blob we resolved from (audit / replay).
- ``created_at``       epoch seconds the audit row was written.

Index
-----
``idx_market_resolutions_market`` on ``market_id`` covers the backtest's
per-market resolution lookup when joining to ``predictions.market_id``.

Idempotent — re-running is safe; the table uses ``IF NOT EXISTS`` and the
index uses ``IF NOT EXISTS``.
"""

from __future__ import annotations

revision = "201"
down_revision = "200"


def upgrade(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_resolutions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id        TEXT NOT NULL,
            market_slug      TEXT,
            resolved_outcome INTEGER,
            resolved_at      INTEGER,
            resolved_via     TEXT,
            source_data      TEXT,
            created_at       INTEGER
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_resolutions_market "
        "ON market_resolutions(market_id)"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_market_resolutions_market")
    c.execute("DROP TABLE IF EXISTS market_resolutions")
