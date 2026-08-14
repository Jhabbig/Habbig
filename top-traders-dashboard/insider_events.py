#!/usr/bin/env python3
"""
Unified insider-event store.

One normalized table for "someone with information traded something":
  - SEC Form 4 (corporate insiders)         venue='sec_form4'
  - Congress STOCK Act PTRs                 venue='congress_ptr'
  - 13F (hedge fund holdings)               venue='13f'
  - Polymarket smart-money positions        venue='polymarket'
  - Kalshi unusual prints                   venue='kalshi'

Every ingester writes here via `upsert_event`. Dedup is enforced by
UNIQUE(venue, source_id) — re-running an ingester is idempotent. The dashboard
reads via `recent_events`, `events_for_symbol`, `events_for_actor`.

Schema is intentionally wide+sparse rather than a join across per-venue tables
because the product question is always cross-venue ("everything on NVDA in
the last 30 days"), and SQLite handles wide tables with sparse columns just
fine at the volumes we expect (~10k rows/day across all sources).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).parent / "insider_events.db"

logger = logging.getLogger(__name__)


# ─── Schema ───────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS insider_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue           TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    ts_filed        INTEGER,
    ts_executed     INTEGER,
    actor_id        TEXT,
    actor_label     TEXT,
    actor_role      TEXT,
    symbol          TEXT,
    symbol_name     TEXT,
    side            TEXT,
    shares          REAL,
    price           REAL,
    size_usd_low    REAL,
    size_usd_high   REAL,
    raw_url         TEXT,
    extra_json      TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE(venue, source_id)
);
CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON insider_events(symbol, ts_filed DESC);
CREATE INDEX IF NOT EXISTS idx_events_actor_ts  ON insider_events(actor_id, ts_filed DESC);
CREATE INDEX IF NOT EXISTS idx_events_venue_ts  ON insider_events(venue, ts_filed DESC);
"""

VALID_VENUES = {"sec_form4", "congress_ptr", "13f", "polymarket", "kalshi"}
VALID_SIDES = {"buy", "sell", "option_buy", "option_sell", "exchange", "gift", "other"}


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
    with _conn() as c:
        c.executescript(_SCHEMA)


# ─── Writes ───────────────────────────────────────────────────────────

def upsert_event(
    *,
    venue: str,
    source_id: str,
    ts_filed: int | None = None,
    ts_executed: int | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    actor_role: str | None = None,
    symbol: str | None = None,
    symbol_name: str | None = None,
    side: str | None = None,
    shares: float | None = None,
    price: float | None = None,
    size_usd_low: float | None = None,
    size_usd_high: float | None = None,
    raw_url: str | None = None,
    extra: dict | None = None,
) -> bool:
    """Insert (or no-op if already present). Returns True if a new row was inserted."""
    if venue not in VALID_VENUES:
        raise ValueError(f"unknown venue: {venue!r}")
    if not source_id:
        raise ValueError("source_id required for dedup")
    if side is not None and side not in VALID_SIDES:
        # Don't reject — tag it as 'other' and stash original in extra
        extra = {**(extra or {}), "original_side": side}
        side = "other"

    extra_json = json.dumps(extra, default=str) if extra else None
    now = int(time.time())

    with _conn() as c:
        cur = c.execute(
            """
            INSERT OR IGNORE INTO insider_events (
                venue, source_id, ts_filed, ts_executed,
                actor_id, actor_label, actor_role,
                symbol, symbol_name, side,
                shares, price, size_usd_low, size_usd_high,
                raw_url, extra_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                venue, source_id, ts_filed, ts_executed,
                actor_id, actor_label, actor_role,
                (symbol or "").upper() or None, symbol_name, side,
                shares, price, size_usd_low, size_usd_high,
                raw_url, extra_json, now,
            ),
        )
        return cur.rowcount > 0


def upsert_many(rows: Iterable[dict]) -> dict:
    """Bulk upsert; returns {inserted, skipped, errors}."""
    inserted = skipped = errors = 0
    for row in rows:
        try:
            if upsert_event(**row):
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            logger.warning("upsert failed for %s/%s: %s",
                           row.get("venue"), row.get("source_id"), e)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


# ─── Reads ────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("extra_json"):
        try:
            d["extra"] = json.loads(d["extra_json"])
        except Exception:
            d["extra"] = None
    else:
        d["extra"] = None
    d.pop("extra_json", None)
    return d


def recent_events(
    *,
    venue: str | None = None,
    limit: int = 100,
    since_ts: int | None = None,
) -> list[dict]:
    """Most recent events, optionally filtered by venue / minimum filing time."""
    init_db()
    where: list[str] = []
    params: list[Any] = []
    if venue:
        where.append("venue = ?")
        params.append(venue)
    if since_ts is not None:
        where.append("COALESCE(ts_filed, ts_executed, created_at) >= ?")
        params.append(since_ts)
    sql = "SELECT * FROM insider_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(ts_filed, ts_executed, created_at) DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def events_for_symbol(symbol: str, limit: int = 50) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM insider_events WHERE symbol = ? "
            "ORDER BY COALESCE(ts_filed, ts_executed, created_at) DESC LIMIT ?",
            (symbol.upper(), limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def events_for_actor(actor_id: str, limit: int = 100) -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM insider_events WHERE actor_id = ? "
            "ORDER BY COALESCE(ts_filed, ts_executed, created_at) DESC LIMIT ?",
            (actor_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def stats_summary() -> dict:
    """Quick health snapshot for the dashboard."""
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM insider_events").fetchone()["n"]
        by_venue = {
            r["venue"]: r["n"]
            for r in c.execute(
                "SELECT venue, COUNT(*) AS n FROM insider_events GROUP BY venue"
            ).fetchall()
        }
        latest = c.execute(
            "SELECT venue, MAX(COALESCE(ts_filed, ts_executed, created_at)) AS ts "
            "FROM insider_events GROUP BY venue"
        ).fetchall()
        latest_by_venue = {r["venue"]: r["ts"] for r in latest}
    return {
        "total_events": total,
        "by_venue": by_venue,
        "latest_ts_by_venue": latest_by_venue,
    }


if __name__ == "__main__":
    init_db()
    print(json.dumps(stats_summary(), indent=2, default=str))
