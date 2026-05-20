"""SQLite-backed history store for the Love Index.

One row per (date, iso3). Writes are idempotent (INSERT OR REPLACE) so a
country recomputed multiple times on the same UTC day overwrites cleanly,
not accumulating. Reads return rows ascending by date for direct charting.

Production setup: have a daily cron call `record_snapshot()`. In dev, the
server itself calls `snapshot_if_due()` from `/api/summary` so a fresh
checkout starts accumulating immediately.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date         TEXT NOT NULL,
    iso3         TEXT NOT NULL,
    composite    REAL,
    connection   REAL,
    partnership  REAL,
    stability    REAL,
    activity     REAL,
    used         TEXT,
    PRIMARY KEY (date, iso3)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_iso3 ON snapshots(iso3, date);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def today_utc() -> str:
    return datetime.utcnow().date().isoformat()


def record_snapshot(countries: Iterable[dict], db_path: Path, snap_date: str | None = None) -> int:
    """Upsert a snapshot for `snap_date` (defaults to today UTC). Returns rows written."""
    when = snap_date or today_utc()
    rows = []
    for c in countries:
        subs = c.get("subscores") or {}
        rows.append((
            when,
            c.get("iso3"),
            c.get("composite"),
            subs.get("connection"),
            subs.get("partnership"),
            subs.get("stability"),
            subs.get("activity"),
            json.dumps(c.get("used") or []),
        ))
    if not rows:
        return 0
    con = _connect(db_path)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO snapshots "
            "(date, iso3, composite, connection, partnership, stability, activity, used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)
    finally:
        con.close()


def has_snapshot_for(db_path: Path, snap_date: str | None = None) -> bool:
    when = snap_date or today_utc()
    if not db_path.exists():
        return False
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT 1 FROM snapshots WHERE date = ? LIMIT 1", (when,))
        return cur.fetchone() is not None
    finally:
        con.close()


def snapshot_if_due(countries: Iterable[dict], db_path: Path) -> int:
    """Record a snapshot only if there isn't one for today UTC yet.

    Cheap to call from request handlers — one indexed lookup; only writes
    when the day rolls over and the dashboard has fresh data to record.
    """
    if has_snapshot_for(db_path):
        return 0
    return record_snapshot(list(countries), db_path)


def _row_to_point(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "date":        row["date"],
        "composite":   row["composite"],
        "connection":  row["connection"],
        "partnership": row["partnership"],
        "stability":   row["stability"],
        "activity":    row["activity"],
        "used":        json.loads(row["used"] or "[]"),
    }


def get_country_history(iso3: str, db_path: Path, days: int = 365) -> list[dict]:
    """Return a country's time series, oldest first, last `days` days."""
    if not db_path.exists():
        return []
    cutoff = (datetime.utcnow().date() - timedelta(days=days)).isoformat()
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM snapshots WHERE iso3 = ? AND date >= ? ORDER BY date ASC",
            (iso3.upper(), cutoff),
        )
        return [_row_to_point(r) for r in cur.fetchall()]
    finally:
        con.close()


def get_global_history(db_path: Path, days: int = 365) -> list[dict]:
    """Per-day average composite across all ranked countries."""
    if not db_path.exists():
        return []
    cutoff = (datetime.utcnow().date() - timedelta(days=days)).isoformat()
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT date, AVG(composite) AS composite, COUNT(*) AS n "
            "FROM snapshots WHERE composite IS NOT NULL AND date >= ? "
            "GROUP BY date ORDER BY date ASC",
            (cutoff,),
        )
        return [{"date": r["date"], "composite": r["composite"], "n": r["n"]} for r in cur.fetchall()]
    finally:
        con.close()


def n_snapshots(db_path: Path) -> dict[str, int]:
    """Diagnostic: count distinct dates + total rows. Cheap."""
    if not db_path.exists():
        return {"dates": 0, "rows": 0}
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT COUNT(DISTINCT date), COUNT(*) FROM snapshots")
        dates, rows = cur.fetchone()
        return {"dates": int(dates or 0), "rows": int(rows or 0)}
    finally:
        con.close()
