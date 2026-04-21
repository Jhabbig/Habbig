"""Weekly source-network computation.

Recomputes pairwise stats between every pair of sources that have at
least :data:`credibility.network.MIN_SHARED_MARKETS` shared markets.
Also snapshots the resulting clusters into ``source_networks`` so the
admin panel can plot network state over time.

Runs every Sunday 03:00 UTC — late enough that the Saturday
credibility recompute has finished, early enough that Monday's
digest / weekly report have it available.

Schema probes make the job safe to run on branches that haven't
applied migration 054 yet (it no-ops).
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from credibility.network import (
    classify_relationship,
    echo_chamber_clusters,
    pairwise_stats,
)
from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.source_relationships")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


@register_job("compute_source_relationships")
async def compute_source_relationships(min_shared: int = 5, max_sources: int = 200) -> dict[str, Any]:
    conn = _connect()
    try:
        if not _table_exists(conn, "source_relationships"):
            return {"skipped": "source_relationships table missing"}
        if not _table_exists(conn, "predictions"):
            return {"skipped": "predictions table missing"}

        # Pull the top-N sources by prediction count — with many sources
        # the pair count explodes, so cap defensively.
        if _table_exists(conn, "source_credibility"):
            sources = [r["source_handle"] for r in conn.execute(
                "SELECT source_handle FROM source_credibility "
                "WHERE accuracy_unlocked = 1 "
                "ORDER BY total_predictions DESC LIMIT ?",
                (max_sources,),
            ).fetchall()]
        else:
            sources = [r["source_handle"] for r in conn.execute(
                "SELECT source_handle, COUNT(*) AS n FROM predictions "
                "GROUP BY source_handle ORDER BY n DESC LIMIT ?",
                (max_sources,),
            ).fetchall()]

        # Preload records per source once.
        records: dict[str, list[dict]] = {}
        for handle in sources:
            rows = conn.execute(
                "SELECT market_id AS market_slug, direction, resolved_correct "
                "FROM predictions WHERE source_handle = ? AND market_id IS NOT NULL",
                (handle,),
            ).fetchall()
            records[handle] = [dict(r) for r in rows]

        now = int(time.time())
        pair_rows: list[dict] = []
        for a, b in itertools.combinations(sources, 2):
            stats = pairwise_stats(records.get(a, []), records.get(b, []))
            if stats is None:
                continue
            rel_type = classify_relationship(stats)
            pair_rows.append({
                "source_a": a,
                "source_b": b,
                "relationship_type": rel_type,
                **stats,
            })
            conn.execute(
                """
                INSERT INTO source_relationships (
                    source_a, source_b, markets_both_predicted,
                    agreement_rate, both_correct_rate,
                    independent_signal_score, relationship_type,
                    last_computed_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(source_a, source_b) DO UPDATE SET
                    markets_both_predicted = excluded.markets_both_predicted,
                    agreement_rate = excluded.agreement_rate,
                    both_correct_rate = excluded.both_correct_rate,
                    independent_signal_score = excluded.independent_signal_score,
                    relationship_type = excluded.relationship_type,
                    last_computed_at = excluded.last_computed_at
                """,
                (
                    a, b, stats["shared_markets"],
                    stats["agreement_rate"], stats["both_correct_rate"],
                    stats["independent_signal_score"], rel_type, now,
                ),
            )

        # Snapshot clusters + most-independent sources.
        clusters = echo_chamber_clusters(pair_rows)
        # Most-independent: top 10 sources by average independent_signal_score.
        indie: dict[str, list[float]] = {}
        for r in pair_rows:
            indie.setdefault(r["source_a"], []).append(r["independent_signal_score"])
            indie.setdefault(r["source_b"], []).append(r["independent_signal_score"])
        most_independent = sorted(
            (
                {"handle": h, "score": round(sum(scores) / len(scores), 4)}
                for h, scores in indie.items() if scores
            ),
            key=lambda x: x["score"], reverse=True,
        )[:10]

        if _table_exists(conn, "source_networks"):
            conn.execute(
                "INSERT INTO source_networks "
                "(computed_at, echo_chamber_clusters, most_independent_sources, stats_json) "
                "VALUES (?,?,?,?)",
                (
                    now,
                    json.dumps(clusters),
                    json.dumps(most_independent),
                    json.dumps({
                        "total_pairs": len(pair_rows),
                        "clusters": len(clusters),
                        "sources_analysed": len(sources),
                    }),
                ),
            )
        conn.commit()
        return {
            "sources_analysed": len(sources),
            "pairs_updated": len(pair_rows),
            "clusters": len(clusters),
        }
    except Exception as exc:
        log.exception("source relationships compute failed")
        return {"error": str(exc)}
    finally:
        conn.close()


# Every Sunday at 03:00 UTC.
register_cron("compute_source_relationships", weekday=6, hour=3, minute=0)
