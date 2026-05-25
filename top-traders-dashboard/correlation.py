#!/usr/bin/env python3
"""
Cross-venue correlation engine.

For each insider event with a ticker, find matching Polymarket markets and
measure the price move in a window around the disclosure timestamp:

  Δ_pre  = price_at_disclosure − price_24h_before    (front-running signal)
  Δ_post = price_24h_after     − price_at_disclosure (post-disclosure drift)

Results are persisted to insider_market_correlations and ranked by
|Δ_pre| — that's where the *interesting* stories live: "Pelosi filed an
NVDA buy on day T; the matching Polymarket market moved 8¢ in the 24h
before her filing was public." Whether that's leakage, reporting lag, or
coincidence is for the human to decide; we just surface the candidates.

Data sources:
  - Insider events: insider_events.db (this repo)
  - Active markets: ticker_to_market.get_index()
  - Price history:  https://clob.polymarket.com/prices-history
                    (returns minute-fidelity history per token id)

Idempotent via UNIQUE(event_id, market_id). Re-running is cheap; only
events without a row get computed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

import insider_events
import ticker_to_market

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "insider_events.db"  # share the DB
CLOB_PRICES_API = "https://clob.polymarket.com/prices-history"
HTTP_TIMEOUT = 15.0
RATE_PAUSE = 0.10
WINDOW_SECONDS = 24 * 3600
MAX_MARKETS_PER_EVENT = 3   # cap to avoid blowing up on tickers w/ many markets


# ─── Schema (lives in the same SQLite file as insider_events) ────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_market_correlations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            INTEGER NOT NULL,
    market_id           TEXT NOT NULL,
    market_slug         TEXT,
    market_question     TEXT,
    market_volume       REAL,
    ticker              TEXT,
    ts_disclosure       INTEGER,
    price_at_disclosure REAL,
    price_24h_before    REAL,
    price_24h_after     REAL,
    delta_pre           REAL,
    delta_post          REAL,
    sample_count        INTEGER,
    computed_at         INTEGER NOT NULL,
    UNIQUE(event_id, market_id),
    FOREIGN KEY(event_id) REFERENCES insider_events(id)
);
CREATE INDEX IF NOT EXISTS idx_corr_delta_pre ON insider_market_correlations(ABS(delta_pre) DESC);
CREATE INDEX IF NOT EXISTS idx_corr_ticker_ts ON insider_market_correlations(ticker, ts_disclosure DESC);
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
    insider_events.init_db()  # ensure parent table exists too
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── Price fetch ─────────────────────────────────────────────────────

def _fetch_price_history(token_id: str, start_ts: int, end_ts: int) -> list[dict]:
    """
    Polymarket CLOB minute-fidelity price history for one outcome token.
    Returns [{"t": unix_seconds, "p": price}, ...] sorted ascending.
    """
    if not token_id:
        return []
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            r = client.get(CLOB_PRICES_API, params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 60,  # 60-min buckets — coarse enough to be cheap
            })
            if r.status_code != 200:
                return []
            data = r.json()
        history = data.get("history") or []
        # Normalise — some responses use 't'/'p', others 'timestamp'/'price'
        out = []
        for pt in history:
            t = pt.get("t") or pt.get("timestamp")
            p = pt.get("p") or pt.get("price")
            if t is None or p is None:
                continue
            out.append({"t": int(t), "p": float(p)})
        out.sort(key=lambda x: x["t"])
        return out
    except Exception as e:
        logger.debug("price history fetch failed for %s: %s", token_id[:10], e)
        return []


def _price_at(history: list[dict], target_ts: int) -> float | None:
    """Closest-prior sample's price at or before target_ts."""
    if not history:
        return None
    best: float | None = None
    for pt in history:
        if pt["t"] <= target_ts:
            best = pt["p"]
        else:
            break
    if best is None and history:
        # If everything is *after* target, use the earliest as best-effort
        return history[0]["p"]
    return best


# ─── Correlation computation ─────────────────────────────────────────

def _correlate_event(event: dict) -> list[dict]:
    """For one insider event, return correlation rows across matching markets."""
    ticker = (event.get("symbol") or "").upper()
    ts_disc = event.get("ts_filed") or event.get("ts_executed")
    if not ticker or not ts_disc:
        return []

    # Skip event types that aren't market-signal-bearing
    if event.get("side") in ("exchange", "gift", "other"):
        return []

    matches = ticker_to_market.markets_for_ticker(ticker, limit=MAX_MARKETS_PER_EVENT)
    if not matches:
        return []

    start_ts = int(ts_disc) - WINDOW_SECONDS
    end_ts = int(ts_disc) + WINDOW_SECONDS
    rows: list[dict] = []

    for m in matches:
        token_ids = m.get("clob_token_ids") or []
        if not token_ids:
            continue
        # Use the first outcome token (typically YES) for the price series.
        # For copy-trade purposes we just need a stable directional reference.
        token_id = str(token_ids[0])
        history = _fetch_price_history(token_id, start_ts, end_ts)
        time.sleep(RATE_PAUSE)
        if len(history) < 3:
            continue

        p_disc = _price_at(history, int(ts_disc))
        p_pre = _price_at(history, int(ts_disc) - WINDOW_SECONDS)
        p_post = _price_at(history, int(ts_disc) + WINDOW_SECONDS) or history[-1]["p"]
        if p_disc is None:
            continue
        delta_pre = (p_disc - p_pre) if p_pre is not None else None
        delta_post = (p_post - p_disc) if p_post is not None else None

        rows.append({
            "event_id": event["id"],
            "market_id": m.get("condition_id") or "",
            "market_slug": m.get("slug"),
            "market_question": m.get("question"),
            "market_volume": m.get("volume"),
            "ticker": ticker,
            "ts_disclosure": int(ts_disc),
            "price_at_disclosure": p_disc,
            "price_24h_before": p_pre,
            "price_24h_after": p_post,
            "delta_pre": delta_pre,
            "delta_post": delta_post,
            "sample_count": len(history),
        })
    return rows


def _upsert_corr(rows: list[dict]) -> int:
    inserted = 0
    now = int(time.time())
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                """
                INSERT OR REPLACE INTO insider_market_correlations (
                    event_id, market_id, market_slug, market_question, market_volume,
                    ticker, ts_disclosure, price_at_disclosure, price_24h_before,
                    price_24h_after, delta_pre, delta_post, sample_count, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["event_id"], r["market_id"], r.get("market_slug"),
                    r.get("market_question"), r.get("market_volume"),
                    r["ticker"], r["ts_disclosure"],
                    r.get("price_at_disclosure"), r.get("price_24h_before"),
                    r.get("price_24h_after"),
                    r.get("delta_pre"), r.get("delta_post"),
                    r.get("sample_count"), now,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
    return inserted


def _events_needing_correlation(limit: int = 200) -> list[dict]:
    """Recent events that have a symbol but no correlation row yet."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.* FROM insider_events e
            WHERE e.symbol IS NOT NULL
              AND e.side NOT IN ('exchange', 'gift', 'other')
              AND NOT EXISTS (
                  SELECT 1 FROM insider_market_correlations c WHERE c.event_id = e.id
              )
            ORDER BY COALESCE(e.ts_filed, e.ts_executed, e.created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def run_correlation_pass(max_events: int = 100) -> dict:
    """Find unprocessed events, correlate, persist. Returns summary."""
    init_db()
    events = _events_needing_correlation(limit=max_events)
    if not events:
        return {"events_processed": 0, "rows_inserted": 0, "events_with_matches": 0}

    # Warm the market index once for the whole pass
    ticker_to_market.get_index()

    rows_inserted = events_with_matches = 0
    for ev in events:
        try:
            rows = _correlate_event(ev)
        except Exception as e:
            logger.warning("correlate failed for event %s: %s", ev.get("id"), e)
            continue
        if not rows:
            # Still mark this event as "tried" by inserting a sentinel? No —
            # leave it; if a matching market appears later we'll backfill.
            continue
        rows_inserted += _upsert_corr(rows)
        events_with_matches += 1

    return {
        "events_processed": len(events),
        "rows_inserted": rows_inserted,
        "events_with_matches": events_with_matches,
    }


# ─── Reads ───────────────────────────────────────────────────────────

def top_correlations(
    *,
    min_abs_delta: float = 0.05,
    venue: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """
    The interesting cross-venue feed. Joins back to insider_events so the
    dashboard gets actor name + side + size in one shot.
    """
    init_db()
    sql = """
        SELECT
            c.*,
            e.venue, e.actor_id, e.actor_label, e.actor_role,
            e.side, e.shares, e.price, e.size_usd_low, e.size_usd_high,
            e.symbol_name, e.raw_url
        FROM insider_market_correlations c
        JOIN insider_events e ON e.id = c.event_id
        WHERE c.delta_pre IS NOT NULL
          AND ABS(c.delta_pre) >= ?
    """
    params: list[Any] = [min_abs_delta]
    if venue:
        sql += " AND e.venue = ?"
        params.append(venue)
    sql += " ORDER BY ABS(c.delta_pre) DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def correlations_summary() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM insider_market_correlations").fetchone()["n"]
        avg_pre = c.execute(
            "SELECT AVG(ABS(delta_pre)) AS a FROM insider_market_correlations WHERE delta_pre IS NOT NULL"
        ).fetchone()["a"] or 0.0
        last = c.execute(
            "SELECT MAX(computed_at) AS t FROM insider_market_correlations"
        ).fetchone()["t"]
    return {
        "total_correlations": total,
        "avg_abs_delta_pre": round(float(avg_pre), 4),
        "last_computed_at": last,
    }


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print("Pass:", json.dumps(run_correlation_pass(max_events=20), indent=2))
    print("Top:", json.dumps(top_correlations(min_abs_delta=0.03, limit=10), indent=2, default=str))
