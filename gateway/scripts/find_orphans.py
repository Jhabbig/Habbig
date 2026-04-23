#!/usr/bin/env python3
"""Orphan sweep — cross-table references that soft-reference (no FK)
or that we want to double-check beyond SQLite's own enforcement.

Prints one line per orphan class; exits 0 if clean (zero non-test
orphans) and 1 if anything real leaked through.

Run nightly from the scheduler OR manually:
    python3 scripts/find_orphans.py
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path


# Every query here counts rows in the LEFT side that have no match on
# the RIGHT side. Label should be human-readable — it's the ops alert
# text if anything exceeds expected.
QUERIES: list[tuple[str, str]] = [
    # Soft reference: predictions.source_handle has no FK to
    # source_credibility because sources appear before the nightly
    # scorer runs. We flag anyway so we notice if the scorer stalls.
    (
        "predictions missing source_credibility row",
        """SELECT COUNT(*) FROM predictions p
           LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
           WHERE sc.source_handle IS NULL""",
    ),
    # Explicit CASCADE paths we re-verify in case a migration weakened
    # them. Any non-zero here = real bug.
    (
        "saved_predictions without prediction",
        """SELECT COUNT(*) FROM saved_predictions sp
           LEFT JOIN predictions p ON p.id = sp.prediction_id
           WHERE p.id IS NULL""",
    ),
    (
        "saved_predictions without user",
        """SELECT COUNT(*) FROM saved_predictions sp
           LEFT JOIN users u ON u.id = sp.user_id
           WHERE u.id IS NULL""",
    ),
    (
        "user_predictions without user",
        """SELECT COUNT(*) FROM user_predictions up
           LEFT JOIN users u ON u.id = up.user_id
           WHERE u.id IS NULL""",
    ),
    (
        "followed_sources without user",
        """SELECT COUNT(*) FROM followed_sources fs
           LEFT JOIN users u ON u.id = fs.user_id
           WHERE u.id IS NULL""",
    ),
    (
        "subscriptions without user",
        """SELECT COUNT(*) FROM subscriptions s
           LEFT JOIN users u ON u.id = s.user_id
           WHERE u.id IS NULL""",
    ),
    (
        "sessions without user",
        """SELECT COUNT(*) FROM sessions s
           LEFT JOIN users u ON u.id = s.user_id
           WHERE u.id IS NULL""",
    ),
    (
        "intelligence_messages without conversation",
        """SELECT COUNT(*) FROM intelligence_messages m
           LEFT JOIN intelligence_conversations c ON c.id = m.conversation_id
           WHERE c.id IS NULL""",
    ),
    (
        "insider_market_correlations without signal",
        """SELECT COUNT(*) FROM insider_market_correlations imc
           LEFT JOIN insider_signals s ON s.id = imc.signal_id
           WHERE s.id IS NULL""",
    ),
    (
        "predictions_reextracted pointing at deleted original",
        """SELECT COUNT(*) FROM predictions_reextracted pr
           LEFT JOIN predictions p ON p.id = pr.original_prediction_id
           WHERE pr.original_prediction_id IS NOT NULL AND p.id IS NULL""",
    ),
]


# predictions whose handle is a test fixture (test_<something>_src)
# aren't real orphans — they're seeded by older tests that INSERT
# directly. Excluded from the non-zero failure count.
_TEST_FIXTURE_RE = re.compile(r"^test_[a-z0-9_]+_src$")


def _count_test_fixtures(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """SELECT COUNT(*) FROM predictions p
           LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
           WHERE sc.source_handle IS NULL
             AND p.source_handle LIKE 'test\\_%\\_src' ESCAPE '\\'"""
    ).fetchone()
    return int(row[0] or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="path to auth.db (default: $GATEWAY_DB_PATH or ../auth.db)")
    args = ap.parse_args()

    db_path = args.db or os.environ.get("GATEWAY_DB_PATH")
    if not db_path:
        db_path = str(Path(__file__).resolve().parent.parent / "auth.db")
    if not os.path.isfile(db_path):
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    real_issues = 0
    try:
        test_fixture_bypass = _count_test_fixtures(conn)
        for label, sql in QUERIES:
            try:
                n = int(conn.execute(sql).fetchone()[0] or 0)
            except sqlite3.OperationalError as exc:
                # A missing table is not an orphan — it's an unused
                # feature. Skip silently.
                print(f"SKIP  {label}: {exc}")
                continue
            adjusted = n
            if label.startswith("predictions missing source_credibility"):
                adjusted = max(0, n - test_fixture_bypass)
                if test_fixture_bypass:
                    print(
                        f"NOTE  {label}: {n} total "
                        f"({test_fixture_bypass} test fixtures ignored)"
                    )
            if adjusted == 0:
                print(f"OK    {label}: 0")
            else:
                real_issues += adjusted
                print(f"FAIL  {label}: {adjusted}")
    finally:
        conn.close()

    if real_issues:
        print(f"\n{real_issues} real orphan(s) found", file=sys.stderr)
        return 1
    print("\nall clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
