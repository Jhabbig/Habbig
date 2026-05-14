"""
Migration 001 — add ``polarity`` column to ``spikes``.

Unlocks DECISIONS.md #7 (one product, two views) — the Happiness view filters
``spikes.polarity = 'positive'``; the Annoyance view stays on the default
``'negative'``. Pre-existing rows inherit ``'negative'`` via the NOT NULL
DEFAULT, so the annoyance view is byte-for-byte unchanged.

Idempotent: introspects ``PRAGMA table_info(spikes)`` before issuing the
ALTER TABLE.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {r[1] for r in rows}


def apply(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "spikes")
    if "polarity" not in cols:
        conn.execute(
            "ALTER TABLE spikes "
            "ADD COLUMN polarity TEXT NOT NULL DEFAULT 'negative'"
        )

    # Composite index on (polarity, detected_at) so /api/happiness/spikes
    # is a single sorted scan.
    if "idx_spikes_polarity" not in _indexes(conn, "spikes"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_spikes_polarity "
            "ON spikes(polarity, detected_at)"
        )
