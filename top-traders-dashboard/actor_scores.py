#!/usr/bin/env python3
"""
Actor leakage scoring.

The dashboard's headline question — "who actually has an edge?" — is
answered by aggregating insider_market_correlations per actor:

  Per actor (Pelosi, a CIK, a wallet):
    avg_abs_delta_pre  = mean |Δ_pre| across their correlation rows
    cross_venue_matches = how many of their trades had a matching PM market
    leakage_score      = avg_abs_delta_pre × ln(1 + cross_venue_matches)
                         (rewards consistent moves; penalises 1-shot luck)
    leakage_percentile = 0-100 rank within actors with ≥ MIN_MATCHES

The percentile is the most useful number for the UI ("Pelosi sits at the
P78 leakage rank — top quintile") because raw |Δ_pre| values mean nothing
to a human without context.

Materialised into a separate table (`actor_scores`) refreshed every 30 min
so the dashboard never blocks on the underlying GROUP BY.

Note: this is a *signal*, not a verdict. Big |Δ_pre| could be leakage,
news-ahead-of-disclosure, reporting lag, or coincidence. The scoring just
ranks who's worth paying attention to.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from math import log
from pathlib import Path
from typing import Any

import insider_events  # ensures parent DB exists

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "insider_events.db"  # share the DB
MIN_MATCHES_FOR_PERCENTILE = 3
TOP_TICKERS_PER_ACTOR = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actor_scores (
    actor_id            TEXT PRIMARY KEY,
    actor_label         TEXT,
    actor_role          TEXT,
    venue_primary       TEXT,                -- venue with most rows for this actor
    total_events        INTEGER NOT NULL DEFAULT 0,
    tradeable_events    INTEGER NOT NULL DEFAULT 0,  -- events with a symbol
    cross_venue_matches INTEGER NOT NULL DEFAULT 0,  -- correlation rows
    avg_abs_delta_pre   REAL,
    max_abs_delta_pre   REAL,
    sum_abs_delta_pre   REAL,
    top_tickers_json    TEXT,                -- [{symbol, count, sum_usd_low}]
    leakage_score       REAL NOT NULL DEFAULT 0,
    leakage_percentile  INTEGER,             -- 0-100 within peer group
    first_event_ts      INTEGER,
    last_event_ts       INTEGER,
    computed_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actor_scores_leakage
    ON actor_scores(leakage_score DESC);
CREATE INDEX IF NOT EXISTS idx_actor_scores_percentile
    ON actor_scores(leakage_percentile DESC);
CREATE INDEX IF NOT EXISTS idx_actor_scores_matches
    ON actor_scores(cross_venue_matches DESC);
"""


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    insider_events.init_db()
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── Compute ─────────────────────────────────────────────────────────

def _gather_actor_aggregates() -> list[dict]:
    """
    One pass over insider_events + insider_market_correlations to compute
    everything we need per actor. Cheap even at 100k events thanks to the
    indexes on actor_id.
    """
    init_db()
    with _conn() as c:
        # Per-actor event counts + venue + first/last seen
        events_by_actor = {
            r["actor_id"]: dict(r) for r in c.execute(
                """
                SELECT
                    actor_id,
                    MAX(actor_label)        AS actor_label,
                    MAX(actor_role)         AS actor_role,
                    COUNT(*)                AS total_events,
                    SUM(CASE WHEN symbol IS NOT NULL THEN 1 ELSE 0 END)
                                           AS tradeable_events,
                    MIN(COALESCE(ts_filed, ts_executed, created_at)) AS first_event_ts,
                    MAX(COALESCE(ts_filed, ts_executed, created_at)) AS last_event_ts
                FROM insider_events
                WHERE actor_id IS NOT NULL
                GROUP BY actor_id
                """
            )
        }

        # Primary venue per actor (the one they appear in most often)
        venue_primary = {
            r["actor_id"]: r["venue"] for r in c.execute(
                """
                SELECT actor_id, venue, COUNT(*) AS n
                FROM (
                    SELECT actor_id, venue,
                           ROW_NUMBER() OVER (PARTITION BY actor_id ORDER BY COUNT(*) DESC) AS rn
                    FROM insider_events
                    WHERE actor_id IS NOT NULL
                    GROUP BY actor_id, venue
                ) WHERE rn = 1
                """
            )
        }

        # Cross-venue match aggregates per actor
        match_aggs = {
            r["actor_id"]: dict(r) for r in c.execute(
                """
                SELECT
                    e.actor_id,
                    COUNT(c.id)             AS matches,
                    AVG(ABS(c.delta_pre))   AS avg_abs_delta_pre,
                    MAX(ABS(c.delta_pre))   AS max_abs_delta_pre,
                    SUM(ABS(c.delta_pre))   AS sum_abs_delta_pre
                FROM insider_market_correlations c
                JOIN insider_events e ON e.id = c.event_id
                WHERE c.delta_pre IS NOT NULL
                GROUP BY e.actor_id
                """
            )
        }

        # Top tickers per actor (limit to TOP_TICKERS_PER_ACTOR each)
        top_tickers_raw = list(c.execute(
            """
            SELECT actor_id, symbol, COUNT(*) AS n,
                   SUM(COALESCE(size_usd_low, 0)) AS sum_usd
            FROM insider_events
            WHERE actor_id IS NOT NULL AND symbol IS NOT NULL
            GROUP BY actor_id, symbol
            """
        ))

    # Bucket and trim top tickers per actor
    by_actor_tickers: dict[str, list[dict]] = {}
    for r in top_tickers_raw:
        by_actor_tickers.setdefault(r["actor_id"], []).append({
            "symbol": r["symbol"],
            "count": r["n"],
            "sum_usd_low": r["sum_usd"] or 0,
        })
    for aid, lst in by_actor_tickers.items():
        lst.sort(key=lambda t: (t["count"], t["sum_usd_low"]), reverse=True)
        by_actor_tickers[aid] = lst[:TOP_TICKERS_PER_ACTOR]

    # Build the final per-actor record
    out: list[dict] = []
    for actor_id, base in events_by_actor.items():
        m = match_aggs.get(actor_id) or {}
        matches = int(m.get("matches") or 0)
        avg_d = float(m.get("avg_abs_delta_pre") or 0.0)
        # leakage_score: rewards both magnitude AND consistency
        # ln(1+0)=0, ln(1+1)≈0.69, ln(1+5)≈1.79, ln(1+50)≈3.93
        leakage = avg_d * log(1 + matches) if matches > 0 else 0.0
        out.append({
            "actor_id":           actor_id,
            "actor_label":        base.get("actor_label"),
            "actor_role":         base.get("actor_role"),
            "venue_primary":      venue_primary.get(actor_id),
            "total_events":       int(base.get("total_events") or 0),
            "tradeable_events":   int(base.get("tradeable_events") or 0),
            "cross_venue_matches": matches,
            "avg_abs_delta_pre":  avg_d if matches else None,
            "max_abs_delta_pre":  float(m.get("max_abs_delta_pre") or 0.0) if matches else None,
            "sum_abs_delta_pre":  float(m.get("sum_abs_delta_pre") or 0.0) if matches else None,
            "top_tickers":        by_actor_tickers.get(actor_id, []),
            "leakage_score":      leakage,
            "first_event_ts":     base.get("first_event_ts"),
            "last_event_ts":      base.get("last_event_ts"),
        })
    return out


def _compute_percentiles(records: list[dict]) -> None:
    """In-place: assign 0-100 percentile rank to actors with ≥ MIN_MATCHES."""
    qualifying = [r for r in records if r["cross_venue_matches"] >= MIN_MATCHES_FOR_PERCENTILE]
    if not qualifying:
        for r in records:
            r["leakage_percentile"] = None
        return
    # Sort ascending so rank/N gives percentile
    qualifying.sort(key=lambda r: r["leakage_score"])
    n = len(qualifying)
    rank_by_id = {r["actor_id"]: i for i, r in enumerate(qualifying)}
    for r in records:
        rank = rank_by_id.get(r["actor_id"])
        if rank is None:
            r["leakage_percentile"] = None
        else:
            # 0-100 inclusive — top actor gets 100
            r["leakage_percentile"] = int(round(100 * rank / max(1, n - 1))) if n > 1 else 100


def refresh_actor_scores() -> dict:
    """Run a full refresh of the actor_scores table. Returns summary."""
    init_db()
    records = _gather_actor_aggregates()
    if not records:
        return {"actors_scored": 0, "with_matches": 0, "qualifying_for_percentile": 0}

    _compute_percentiles(records)
    now = int(time.time())

    inserted = updated = 0
    with _conn() as c:
        for r in records:
            top_json = json.dumps(r["top_tickers"], default=str) if r["top_tickers"] else None
            cur = c.execute(
                """
                INSERT INTO actor_scores (
                    actor_id, actor_label, actor_role, venue_primary,
                    total_events, tradeable_events, cross_venue_matches,
                    avg_abs_delta_pre, max_abs_delta_pre, sum_abs_delta_pre,
                    top_tickers_json, leakage_score, leakage_percentile,
                    first_event_ts, last_event_ts, computed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(actor_id) DO UPDATE SET
                    actor_label=excluded.actor_label,
                    actor_role=excluded.actor_role,
                    venue_primary=excluded.venue_primary,
                    total_events=excluded.total_events,
                    tradeable_events=excluded.tradeable_events,
                    cross_venue_matches=excluded.cross_venue_matches,
                    avg_abs_delta_pre=excluded.avg_abs_delta_pre,
                    max_abs_delta_pre=excluded.max_abs_delta_pre,
                    sum_abs_delta_pre=excluded.sum_abs_delta_pre,
                    top_tickers_json=excluded.top_tickers_json,
                    leakage_score=excluded.leakage_score,
                    leakage_percentile=excluded.leakage_percentile,
                    first_event_ts=excluded.first_event_ts,
                    last_event_ts=excluded.last_event_ts,
                    computed_at=excluded.computed_at
                """,
                (
                    r["actor_id"], r.get("actor_label"), r.get("actor_role"),
                    r.get("venue_primary"),
                    r.get("total_events"), r.get("tradeable_events"),
                    r.get("cross_venue_matches"),
                    r.get("avg_abs_delta_pre"), r.get("max_abs_delta_pre"),
                    r.get("sum_abs_delta_pre"),
                    top_json,
                    r.get("leakage_score"), r.get("leakage_percentile"),
                    r.get("first_event_ts"), r.get("last_event_ts"),
                    now,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                updated += 1

    return {
        "actors_scored": len(records),
        "with_matches": sum(1 for r in records if r["cross_venue_matches"] > 0),
        "qualifying_for_percentile": sum(
            1 for r in records if r["cross_venue_matches"] >= MIN_MATCHES_FOR_PERCENTILE
        ),
        "rows_written": inserted + updated,
    }


# ─── Reads ────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("top_tickers_json"):
        try:
            d["top_tickers"] = json.loads(d["top_tickers_json"])
        except Exception:
            d["top_tickers"] = []
    else:
        d["top_tickers"] = []
    d.pop("top_tickers_json", None)
    return d


def top_actors_by_leakage(
    *,
    venue: str | None = None,
    min_matches: int = MIN_MATCHES_FOR_PERCENTILE,
    limit: int = 50,
) -> list[dict]:
    """Ranked list of actors by leakage_score. Default: only those with ≥3 matches."""
    init_db()
    sql = "SELECT * FROM actor_scores WHERE cross_venue_matches >= ?"
    params: list[Any] = [min_matches]
    if venue:
        sql += " AND venue_primary = ?"
        params.append(venue)
    sql += " ORDER BY leakage_score DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_actor_score(actor_id: str) -> dict | None:
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM actor_scores WHERE actor_id = ?", (actor_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def scores_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM actor_scores").fetchone()["n"]
        with_matches = c.execute(
            "SELECT COUNT(*) AS n FROM actor_scores WHERE cross_venue_matches > 0"
        ).fetchone()["n"]
        qualifying = c.execute(
            "SELECT COUNT(*) AS n FROM actor_scores WHERE cross_venue_matches >= ?",
            (MIN_MATCHES_FOR_PERCENTILE,),
        ).fetchone()["n"]
        last = c.execute(
            "SELECT MAX(computed_at) AS t FROM actor_scores"
        ).fetchone()["t"]
        avg = c.execute(
            "SELECT AVG(leakage_score) AS a FROM actor_scores WHERE cross_venue_matches >= ?",
            (MIN_MATCHES_FOR_PERCENTILE,),
        ).fetchone()["a"] or 0
    return {
        "total_actors": total,
        "with_matches": with_matches,
        "qualifying_for_percentile": qualifying,
        "min_matches_threshold": MIN_MATCHES_FOR_PERCENTILE,
        "avg_leakage_score": round(float(avg), 4),
        "last_computed_at": last,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(refresh_actor_scores(), indent=2))
    print()
    print("Top 10 actors by leakage:")
    for a in top_actors_by_leakage(limit=10):
        print(f"  P{a['leakage_percentile'] or 0:3} | {a['actor_label'] or a['actor_id']:30} | "
              f"score={a['leakage_score']:.4f} | matches={a['cross_venue_matches']:>3} | "
              f"avg|Δ|={a['avg_abs_delta_pre'] or 0:.4f}")
