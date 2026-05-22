#!/usr/bin/env python3
"""
Cross-venue suspicious-trade scoring.

The existing `suspicious_trades.py` flags Polymarket-only patterns
(long-shot wins, new wallets, etc). This module brings the same
"this looks fishy" intuition to the *unified* event store, scoring every
insider_events row on signals that span SEC Form 4, Congress PTR, 13F,
Polymarket, and Kalshi.

Per-event score (0–100) is the sum of weighted reason hits:

  out_of_pattern_size      +25  trade ≥ 3× actor's median size
  inactive_then_active     +15  actor's first event in ≥ 180 days
  big_pre_disclosure_move  +30  matching PM market moved |Δ_pre| ≥ 0.05
  cluster_trading          +15  ≥3 distinct actors traded same ticker
                                 inside a ±7-day window around this event
  defendant_actor          +25  actor was named in a prior insider-trading
                                 enforcement case
  large_absolute_size      +10  trade size ≥ $1,000,000

Cap at 100. Reasons stored as JSON so the UI can show "why" beyond the
single number.

Materialised into `cross_venue_suspicious(event_id, score, reasons_json)`
refreshed every 30 min. The dashboard then sorts insider_events by
suspicion score for a "what should I be looking at" view.

Why a separate module instead of in-line scoring? Two reasons:
  1. Recompute is independent of ingest. New enforcement matches arriving
     change the defendant_actor flag for events ingested days ago.
  2. Composable signals: easy to add a 7th heuristic later by appending
     to SCORERS without touching the SQL writer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "insider_events.db"
LOOKBACK_DAYS = 540              # how far back to score (avoid scanning ancient PTRs)
CLUSTER_WINDOW_DAYS = 7
INACTIVE_GAP_DAYS = 180
SIZE_OUTLIER_MULTIPLIER = 3.0
LARGE_ABSOLUTE_USD = 1_000_000

# Per-reason weights — tweak here, no other code changes needed
WEIGHTS = {
    "out_of_pattern_size":    25,
    "inactive_then_active":   15,
    "big_pre_disclosure_move": 30,
    "cluster_trading":        15,
    "defendant_actor":        25,
    "large_absolute_size":    10,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_venue_suspicious (
    event_id      INTEGER PRIMARY KEY,
    score         INTEGER NOT NULL,
    reasons_json  TEXT,
    computed_at   INTEGER NOT NULL,
    FOREIGN KEY(event_id) REFERENCES insider_events(id)
);
CREATE INDEX IF NOT EXISTS idx_csv_score
    ON cross_venue_suspicious(score DESC);
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
    import insider_events
    insider_events.init_db()
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── Pre-compute lookups (one snapshot per pass) ─────────────────────

def _gather_actor_size_baselines(c: sqlite3.Connection) -> dict[str, float]:
    """Per-actor median trade size (low-bound of the bracket)."""
    rows = c.execute(
        """
        SELECT actor_id, size_usd_low
        FROM insider_events
        WHERE actor_id IS NOT NULL AND size_usd_low IS NOT NULL AND size_usd_low > 0
        """
    ).fetchall()
    by_actor: dict[str, list[float]] = {}
    for r in rows:
        by_actor.setdefault(r["actor_id"], []).append(float(r["size_usd_low"]))
    return {a: statistics.median(v) for a, v in by_actor.items() if len(v) >= 3}


def _gather_actor_last_event_before(c: sqlite3.Connection) -> dict[tuple[str, int], int]:
    """
    For each (actor, event) pair: timestamp of the actor's previous event.
    Used by inactive_then_active. We compute lazily inside the scorer to
    avoid materialising N² rows; here we just preload event lists per actor.
    """
    rows = c.execute(
        """
        SELECT actor_id, id, COALESCE(ts_filed, ts_executed, created_at) AS ts
        FROM insider_events
        WHERE actor_id IS NOT NULL
        ORDER BY actor_id, ts ASC
        """
    ).fetchall()
    prev_ts_by_eventid: dict[int, int | None] = {}
    cur_actor = None
    last_ts = None
    for r in rows:
        a = r["actor_id"]
        if a != cur_actor:
            cur_actor = a
            last_ts = None
        prev_ts_by_eventid[r["id"]] = last_ts
        last_ts = r["ts"]
    return prev_ts_by_eventid


def _gather_correlation_deltas(c: sqlite3.Connection) -> dict[int, float]:
    """{event_id: max(|delta_pre|) across that event's correlation rows}."""
    rows = c.execute(
        """
        SELECT event_id, MAX(ABS(delta_pre)) AS max_abs_delta
        FROM insider_market_correlations
        WHERE delta_pre IS NOT NULL
        GROUP BY event_id
        """
    ).fetchall()
    return {r["event_id"]: float(r["max_abs_delta"] or 0) for r in rows}


def _gather_defendant_actor_ids(c: sqlite3.Connection) -> set[str]:
    """Actors with ≥1 enforcement link to an insider-trading-flagged case."""
    try:
        rows = c.execute(
            """
            SELECT DISTINCT l.actor_id
            FROM enforcement_actor_links l
            JOIN enforcement_cases e ON e.id = l.enforcement_id
            WHERE e.is_insider_related = 1
            """
        ).fetchall()
        return {r["actor_id"] for r in rows}
    except sqlite3.OperationalError:
        # Tables don't exist yet (sec_litigation hasn't run)
        return set()


def _gather_ticker_clusters(c: sqlite3.Connection, lookback_ts: int) -> dict[str, list[tuple[int, str]]]:
    """{symbol: [(ts, actor_id), ...]} sorted asc — for cluster detection."""
    rows = c.execute(
        """
        SELECT symbol, COALESCE(ts_filed, ts_executed, created_at) AS ts, actor_id
        FROM insider_events
        WHERE symbol IS NOT NULL AND actor_id IS NOT NULL
          AND COALESCE(ts_filed, ts_executed, created_at) >= ?
        ORDER BY symbol, ts
        """,
        (lookback_ts,),
    ).fetchall()
    out: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append((int(r["ts"]), r["actor_id"]))
    return out


def _events_to_score(c: sqlite3.Connection, lookback_ts: int) -> list[dict]:
    rows = c.execute(
        """
        SELECT id, venue, actor_id, symbol, side,
               size_usd_low, size_usd_high,
               COALESCE(ts_filed, ts_executed, created_at) AS ts
        FROM insider_events
        WHERE COALESCE(ts_filed, ts_executed, created_at) >= ?
        """,
        (lookback_ts,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Scorers ────────────────────────────────────────────────────────

def _score_one(
    ev: dict,
    *,
    size_baselines: dict[str, float],
    prev_ts_by_eventid: dict[int, int | None],
    delta_by_event: dict[int, float],
    defendant_actors: set[str],
    ticker_clusters: dict[str, list[tuple[int, str]]],
) -> tuple[int, list[dict]]:
    reasons: list[dict] = []

    # 1. Out-of-pattern size
    actor = ev.get("actor_id")
    size_low = float(ev.get("size_usd_low") or 0)
    if actor and size_low > 0:
        baseline = size_baselines.get(actor)
        if baseline and baseline > 0 and size_low >= SIZE_OUTLIER_MULTIPLIER * baseline:
            reasons.append({
                "code": "out_of_pattern_size",
                "label": f"{size_low/baseline:.1f}× this actor's median size (${baseline:,.0f})",
                "weight": WEIGHTS["out_of_pattern_size"],
            })

    # 2. Inactive then active
    prev_ts = prev_ts_by_eventid.get(ev["id"])
    if prev_ts is not None and ev.get("ts"):
        gap_days = (ev["ts"] - prev_ts) / 86400.0
        if gap_days >= INACTIVE_GAP_DAYS:
            reasons.append({
                "code": "inactive_then_active",
                "label": f"first event in {int(gap_days)} days",
                "weight": WEIGHTS["inactive_then_active"],
            })

    # 3. Big pre-disclosure PM market move
    delta = delta_by_event.get(ev["id"], 0.0)
    if delta >= 0.05:
        reasons.append({
            "code": "big_pre_disclosure_move",
            "label": f"matching market moved |Δ|={delta:.3f} in 24h before disclosure",
            "weight": WEIGHTS["big_pre_disclosure_move"],
        })

    # 4. Cluster trading
    sym = ev.get("symbol")
    if sym and ev.get("ts"):
        window_lo = ev["ts"] - CLUSTER_WINDOW_DAYS * 86400
        window_hi = ev["ts"] + CLUSTER_WINDOW_DAYS * 86400
        cluster_actors = {
            a for (t, a) in ticker_clusters.get(sym, [])
            if window_lo <= t <= window_hi and a != actor
        }
        if len(cluster_actors) >= 2:  # ev's actor + 2 others = 3 distinct
            reasons.append({
                "code": "cluster_trading",
                "label": f"{len(cluster_actors)+1} distinct actors traded {sym} within ±{CLUSTER_WINDOW_DAYS}d",
                "weight": WEIGHTS["cluster_trading"],
            })

    # 5. Defendant actor (charged with insider trading previously)
    if actor and actor in defendant_actors:
        reasons.append({
            "code": "defendant_actor",
            "label": "actor was named in a prior insider-trading enforcement case",
            "weight": WEIGHTS["defendant_actor"],
        })

    # 6. Large absolute size
    size_high = float(ev.get("size_usd_high") or size_low or 0)
    if size_high >= LARGE_ABSOLUTE_USD:
        reasons.append({
            "code": "large_absolute_size",
            "label": f"trade size ≥ ${LARGE_ABSOLUTE_USD:,.0f}",
            "weight": WEIGHTS["large_absolute_size"],
        })

    score = min(100, sum(r["weight"] for r in reasons))
    return score, reasons


# ─── Pass orchestrator ──────────────────────────────────────────────

def refresh_scores(*, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Re-score all events in the lookback window. Returns summary."""
    init_db()
    lookback_ts = int(time.time()) - lookback_days * 86400

    with _conn() as c:
        size_baselines = _gather_actor_size_baselines(c)
        prev_ts_by_eventid = _gather_actor_last_event_before(c)
        delta_by_event = _gather_correlation_deltas(c)
        defendant_actors = _gather_defendant_actor_ids(c)
        ticker_clusters = _gather_ticker_clusters(c, lookback_ts)
        events = _events_to_score(c, lookback_ts)

    scored = nonzero = 0
    now = int(time.time())
    with _conn() as c:
        for ev in events:
            score, reasons = _score_one(
                ev,
                size_baselines=size_baselines,
                prev_ts_by_eventid=prev_ts_by_eventid,
                delta_by_event=delta_by_event,
                defendant_actors=defendant_actors,
                ticker_clusters=ticker_clusters,
            )
            scored += 1
            if score > 0:
                nonzero += 1
            c.execute(
                """
                INSERT INTO cross_venue_suspicious (event_id, score, reasons_json, computed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    score = excluded.score,
                    reasons_json = excluded.reasons_json,
                    computed_at = excluded.computed_at
                """,
                (ev["id"], score, json.dumps(reasons) if reasons else None, now),
            )

    return {
        "scored": scored,
        "nonzero_scores": nonzero,
        "lookback_days": lookback_days,
        "defendant_actors_in_index": len(defendant_actors),
    }


# ─── Reads ────────────────────────────────────────────────────────────

def top_suspicious_events(
    *,
    min_score: int = 30,
    venue: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Joins back to insider_events for the dashboard."""
    init_db()
    sql = """
        SELECT
            s.event_id, s.score, s.reasons_json, s.computed_at,
            e.venue, e.actor_id, e.actor_label, e.actor_role,
            e.symbol, e.symbol_name, e.side,
            e.size_usd_low, e.size_usd_high,
            e.ts_filed, e.ts_executed, e.raw_url
        FROM cross_venue_suspicious s
        JOIN insider_events e ON e.id = s.event_id
        WHERE s.score >= ?
    """
    params: list = [min_score]
    if venue:
        sql += " AND e.venue = ?"
        params.append(venue)
    sql += " ORDER BY s.score DESC, e.ts_filed DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("reasons_json"):
            try:
                d["reasons"] = json.loads(d["reasons_json"])
            except Exception:
                d["reasons"] = []
        else:
            d["reasons"] = []
        d.pop("reasons_json", None)
        out.append(d)
    return out


def score_for_event(event_id: int) -> dict | None:
    init_db()
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM cross_venue_suspicious WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("reasons_json"):
        try:
            d["reasons"] = json.loads(d["reasons_json"])
        except Exception:
            d["reasons"] = []
    d.pop("reasons_json", None)
    return d


def stats_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM cross_venue_suspicious").fetchone()["n"]
        nonzero = c.execute(
            "SELECT COUNT(*) AS n FROM cross_venue_suspicious WHERE score > 0"
        ).fetchone()["n"]
        high = c.execute(
            "SELECT COUNT(*) AS n FROM cross_venue_suspicious WHERE score >= 50"
        ).fetchone()["n"]
        last = c.execute("SELECT MAX(computed_at) AS t FROM cross_venue_suspicious").fetchone()["t"]
    return {
        "total_scored": total,
        "nonzero_scores": nonzero,
        "high_scores_50plus": high,
        "weights": WEIGHTS,
        "last_computed_at": last,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(refresh_scores(), indent=2))
    print(json.dumps(stats_summary(), indent=2))
    print()
    print("Top 10 suspicious events:")
    for e in top_suspicious_events(min_score=30, limit=10):
        reasons = ", ".join(r["code"] for r in e.get("reasons", []))
        print(f"  score={e['score']:>3} | {e.get('actor_label')} | "
              f"{e.get('side')} {e.get('symbol')} | {reasons}")
