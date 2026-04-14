#!/usr/bin/env python3
"""
Persistent cluster history.

A single co-trading observation is noise — the same wallets repeatedly
appearing as a cluster across many scans is a much stronger signal of
coordinated activity. This module fingerprints each detected cluster
by the canonical (sorted) tuple of its wallets, hashes that into a
stable cluster_id, and tracks how many distinct scans have observed
the same cluster over time.

Tables
------
cluster_observations
  cluster_id      TEXT PRIMARY KEY  -- sha1 of sorted wallet list
  wallets_json    TEXT              -- JSON-encoded sorted wallet list
  wallet_count    INTEGER
  first_seen_ts   INTEGER           -- unix seconds, first time we saw it
  last_seen_ts    INTEGER           -- unix seconds, most recent observation
  seen_count      INTEGER           -- number of distinct scans that saw it
  max_score       INTEGER           -- best (highest) score we've ever given it
  total_volume    REAL              -- max total_usd we've seen
  last_co_markets INTEGER           -- co_markets at most recent observation

Schema is stable & idempotent — `init_db` is safe to call repeatedly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "cluster_history.sqlite3"

# A cluster is "recurring" once we've observed the same wallet set in this many scans.
RECURRING_THRESHOLD = 3


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cluster_observations (
                cluster_id      TEXT PRIMARY KEY,
                wallets_json    TEXT NOT NULL,
                wallet_count    INTEGER NOT NULL,
                first_seen_ts   INTEGER NOT NULL,
                last_seen_ts    INTEGER NOT NULL,
                seen_count      INTEGER NOT NULL DEFAULT 1,
                max_score       INTEGER NOT NULL DEFAULT 0,
                total_volume    REAL NOT NULL DEFAULT 0,
                last_co_markets INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_count ON cluster_observations(seen_count DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON cluster_observations(last_seen_ts DESC)")


def cluster_fingerprint(wallets: list[str]) -> str:
    """Stable cluster ID = sha256 of canonicalized wallet list."""
    canonical = json.dumps(sorted(w.lower() for w in wallets), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def record_clusters(clusters: list[dict[str, Any]], scan_ts: int | None = None) -> dict[str, Any]:
    """
    Persist a freshly-detected list of clusters.

    Each cluster gets `cluster_id`, `seen_count`, `first_seen_ts`, and
    `is_recurring` injected back into the dict (mutated in place) so
    callers can use the enriched values immediately.
    """
    init_db()
    if scan_ts is None:
        scan_ts = int(time.time())

    enriched_count = 0
    recurring_count = 0
    new_count = 0

    with _conn() as c:
        for cluster in clusters:
            wallets = cluster.get("wallets") or []
            if not wallets:
                continue
            cid = cluster_fingerprint(wallets)
            score = int(cluster.get("score") or 0)
            volume = float(cluster.get("total_usd") or 0)
            co_markets = int(cluster.get("co_markets") or 0)

            row = c.execute(
                "SELECT * FROM cluster_observations WHERE cluster_id = ?",
                (cid,),
            ).fetchone()

            if row is None:
                c.execute(
                    """
                    INSERT INTO cluster_observations
                        (cluster_id, wallets_json, wallet_count,
                         first_seen_ts, last_seen_ts, seen_count,
                         max_score, total_volume, last_co_markets)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        cid,
                        json.dumps(sorted(w.lower() for w in wallets)),
                        len(wallets),
                        scan_ts,
                        scan_ts,
                        score,
                        volume,
                        co_markets,
                    ),
                )
                cluster["cluster_id"] = cid
                cluster["seen_count"] = 1
                cluster["first_seen_ts"] = scan_ts
                cluster["last_seen_ts"] = scan_ts
                cluster["is_recurring"] = False
                new_count += 1
            else:
                new_seen = row["seen_count"] + 1
                new_max_score = max(row["max_score"], score)
                new_max_volume = max(row["total_volume"], volume)
                c.execute(
                    """
                    UPDATE cluster_observations
                       SET last_seen_ts   = ?,
                           seen_count     = ?,
                           max_score      = ?,
                           total_volume   = ?,
                           last_co_markets = ?
                     WHERE cluster_id = ?
                    """,
                    (scan_ts, new_seen, new_max_score, new_max_volume, co_markets, cid),
                )
                cluster["cluster_id"] = cid
                cluster["seen_count"] = new_seen
                cluster["first_seen_ts"] = row["first_seen_ts"]
                cluster["last_seen_ts"] = scan_ts
                cluster["is_recurring"] = new_seen >= RECURRING_THRESHOLD
                if cluster["is_recurring"]:
                    recurring_count += 1
            enriched_count += 1

    return {
        "scan_ts": scan_ts,
        "clusters_recorded": enriched_count,
        "new_clusters": new_count,
        "recurring_clusters": recurring_count,
    }


def top_recurring_clusters(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most-frequently-observed clusters from history."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT cluster_id, wallets_json, wallet_count,
                   first_seen_ts, last_seen_ts, seen_count,
                   max_score, total_volume, last_co_markets
              FROM cluster_observations
             WHERE seen_count >= ?
             ORDER BY seen_count DESC, max_score DESC
             LIMIT ?
            """,
            (RECURRING_THRESHOLD, limit),
        ).fetchall()

    return [
        {
            "cluster_id": r["cluster_id"],
            "wallets": json.loads(r["wallets_json"]),
            "wallet_count": r["wallet_count"],
            "first_seen_ts": r["first_seen_ts"],
            "last_seen_ts": r["last_seen_ts"],
            "seen_count": r["seen_count"],
            "max_score": r["max_score"],
            "total_volume": r["total_volume"],
            "last_co_markets": r["last_co_markets"],
        }
        for r in rows
    ]


def history_stats() -> dict[str, Any]:
    init_db()
    with _conn() as c:
        total = (c.execute("SELECT COUNT(*) FROM cluster_observations").fetchone() or (0,))[0]
        recurring = (c.execute(
            "SELECT COUNT(*) FROM cluster_observations WHERE seen_count >= ?",
            (RECURRING_THRESHOLD,),
        ).fetchone() or (0,))[0]
        max_seen = (c.execute(
            "SELECT MAX(seen_count) FROM cluster_observations"
        ).fetchone() or (0,))[0] or 0
    return {
        "total_clusters_tracked": total,
        "recurring_clusters": recurring,
        "max_seen_count": max_seen,
        "recurring_threshold": RECURRING_THRESHOLD,
    }


if __name__ == "__main__":
    # Smoke test — feed the same fake clusters twice and confirm seen_count rises.
    fake_clusters = [
        {
            "wallets": ["0xaa", "0xbb", "0xcc"],
            "score": 75,
            "total_usd": 12000,
            "co_markets": 3,
        },
        {
            "wallets": ["0xdd", "0xee", "0xff", "0x11"],
            "score": 50,
            "total_usd": 4000,
            "co_markets": 2,
        },
    ]

    print("First pass:", record_clusters(fake_clusters))
    print("Second pass:", record_clusters(fake_clusters))
    print("Third pass:", record_clusters(fake_clusters))
    print()
    print("Stats:", history_stats())
    print()
    print("Top recurring:")
    for c in top_recurring_clusters():
        print(f"  {c['cluster_id']}  seen={c['seen_count']}  max_score={c['max_score']}  wallets={c['wallets'][:3]}")
