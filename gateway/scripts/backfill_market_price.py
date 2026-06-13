#!/usr/bin/env python3
"""Backfill predictions.market_price_at_prediction from market_snapshots.

Why
---
Migration 202 adds the nullable column ``predictions.market_price_at_prediction``
so the backtester (``gateway/backtest.py``: ``simulate()`` reads
``market_price_at_prediction`` per prediction) can compute edge vs. the market
at the moment a prediction was made. The ~150 predictions that already exist
were extracted before that column existed, so they carry NULL.

This one-shot fills the gap. For each prediction that has a ``market_id`` but a
NULL ``market_price_at_prediction``, it finds the ``market_snapshots`` row whose
``snapshotted_at`` is *closest* to the prediction's ``extracted_at`` and copies
that snapshot's ``yes_price`` into the prediction.

Key-name mismatch (read this)
-----------------------------
``predictions.market_id`` and ``market_snapshots.market_slug`` are different
column names that, in practice, hold the same Polymarket identifier (slug). We
match ``predictions.market_id == market_snapshots.market_slug`` directly. This
is a best-effort join: any prediction whose market_id never appears as a slug in
market_snapshots simply won't match, and is reported as ``unmatched`` rather
than guessed at.

"Nearest" vs "<="
-----------------
``queries/markets.py:get_market_snapshot_at()`` picks the latest snapshot at or
before a time. Here we deliberately pick the *absolute* nearest snapshot in
either direction, because the snapshot stream for a slug may start slightly
after a prediction was made (e.g. the market was added to the snapshot cron a
few minutes later) and a same-day snapshot just after extraction is a far
better estimate of "the market price at prediction time" than nothing at all.

Idempotent
----------
Only rows with ``market_price_at_prediction IS NULL`` are touched, so re-running
fills any new NULLs without disturbing values written on a previous run.

Run it (from the gateway dir)
-----------------------------
    python3 scripts/backfill_market_price.py            # against ../auth.db
    python3 scripts/backfill_market_price.py --dry-run  # report only, no writes
    python3 scripts/backfill_market_price.py --db /path/to/auth.db

Guard
-----
If the column does not exist yet, the script tells you to run migrations first
(migration 202) and exits without touching anything.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _resolve_db_path(cli_db: str | None) -> str:
    """Match find_orphans.py / db.py resolution order:
    explicit --db > $GATEWAY_DB_PATH > ../auth.db (relative to this file)."""
    db_path = cli_db or os.environ.get("GATEWAY_DB_PATH")
    if not db_path:
        db_path = str(Path(__file__).resolve().parent.parent / "auth.db")
    return db_path


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _nearest_snapshot_price(
    conn: sqlite3.Connection, market_slug: str, extracted_at: int
) -> float | None:
    """Return yes_price of the snapshot whose snapshotted_at is closest (in
    either direction) to extracted_at for this slug, or None if the slug has
    no snapshots at all.

    ORDER BY ABS(snapshotted_at - target) keeps SQLite doing the work and
    handles ties deterministically by preferring the smaller snapshotted_at
    (earlier snapshot) via the secondary sort key — a stable, reproducible
    pick across re-runs.
    """
    row = conn.execute(
        """
        SELECT yes_price
        FROM market_snapshots
        WHERE market_slug = ?
        ORDER BY ABS(snapshotted_at - ?) ASC, snapshotted_at ASC
        LIMIT 1
        """,
        (market_slug, int(extracted_at)),
    ).fetchone()
    if row is None:
        return None
    return float(row["yes_price"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        default=None,
        help="path to auth.db (default: $GATEWAY_DB_PATH or ../auth.db)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    args = ap.parse_args()

    db_path = _resolve_db_path(args.db)
    if not os.path.isfile(db_path):
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        # Guard: the target column must exist (added by migration 202).
        if not _column_exists(conn, "predictions", "market_price_at_prediction"):
            print(
                "Column predictions.market_price_at_prediction does not exist.\n"
                "Run migrations first (migration 202 adds it), then re-run this "
                "script.",
                file=sys.stderr,
            )
            return 3

        # Candidates: have a market_id, still missing the price.
        # (market_id NULL/empty can't be matched to a slug, so exclude it up
        # front — those are reported via the total-vs-candidates gap below.)
        total_null = conn.execute(
            "SELECT COUNT(*) FROM predictions "
            "WHERE market_price_at_prediction IS NULL"
        ).fetchone()[0]

        candidates = conn.execute(
            """
            SELECT id, market_id, extracted_at
            FROM predictions
            WHERE market_price_at_prediction IS NULL
              AND market_id IS NOT NULL
              AND TRIM(market_id) != ''
            ORDER BY id ASC
            """
        ).fetchall()

        total_candidates = len(candidates)
        no_market_id = total_null - total_candidates

        matched = 0
        updated = 0
        unmatched = 0

        for pred in candidates:
            price = _nearest_snapshot_price(
                conn, pred["market_id"].strip(), pred["extracted_at"]
            )
            if price is None:
                unmatched += 1
                continue
            matched += 1
            if not args.dry_run:
                cur = conn.execute(
                    "UPDATE predictions SET market_price_at_prediction = ? "
                    "WHERE id = ? AND market_price_at_prediction IS NULL",
                    (price, pred["id"]),
                )
                updated += cur.rowcount
            else:
                updated += 1  # would-update count under --dry-run

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    mode = "DRY RUN — no writes" if args.dry_run else "applied"
    print("Backfill market_price_at_prediction —", mode)
    print(f"  db                         : {db_path}")
    print(f"  predictions with NULL price: {total_null}")
    print(f"    of which no market_id    : {no_market_id} (cannot match)")
    print(f"    candidates (have slug)   : {total_candidates}")
    print(f"  matched to a snapshot      : {matched}")
    print(f"  {'would update' if args.dry_run else 'updated':<26}: {updated}")
    print(f"  unmatched (no snapshots)   : {unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
