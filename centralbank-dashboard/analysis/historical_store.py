"""Historical snapshot store — turns every panel value into "vs 1d / 7d / 30d".

Every numeric thing the dashboard surfaces (Polymarket prices, Kalshi prices,
implied probabilities, OIS curve points, CPI/PCE/NFP latest, stance scores)
is pinned to a moment in time. Without history, a "Core CPI: 3.2% YoY"
reading is unreadable — the user can't tell if that's notable or normal.
With history, every cell shows ``current vs 1d ago / 7d range / percentile
over 30d`` — same data, an order of magnitude more useful.

Storage:
  Local SQLite at ``data/history.db``. Single table indexed on
  ``(series_key, ts)``. Every existing analysis module gets a one-line
  ``snapshot()`` call appended to its compute path; this module then handles
  the dedup, retention, and delta math.

Dedup:
  ``snapshot()`` skips writes when the most recent value for a given key is
  newer than ``min_interval_seconds`` (default 600s = 10 min). Without this,
  a chatty frontend that polls every minute would inflate the DB by 10× for
  no extra information.

Retention:
  ``prune()`` drops snapshots older than 400 days. Run it idempotently from
  any compute path; cheap because the index already exists.

Series-key conventions (extend freely — all callers just need to agree):
  ``poly_price.{bucket}``        Polymarket YES on a bucket (e.g. ``poly_price.cut25``)
  ``kalshi_price.{bucket}``      Kalshi YES on a bucket
  ``implied_prob.{bucket}``      Our model-implied probability per bucket
  ``ois.month.{ym}``             OIS curve implied avg rate for a YYYY-MM month
  ``rate.{cb}``                  Spot policy rate (e.g. ``rate.US``, ``rate.EA``)
  ``econ.{fred_id}.yoy``         YoY % for a FRED inflation series
  ``econ.{fred_id}.value``       Raw level for any FRED series
  ``econ.PAYEMS.mom_change_k``   NFP monthly change (k jobs)
  ``stance.{cb}.norm``           Hawkish/dovish norm score per CB
  ``implied.delta_bps``          Next-FOMC implied move in bps
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"
DEFAULT_RETENTION_DAYS = 400
DEFAULT_MIN_INTERVAL_SECONDS = 600   # 10 minutes

_lock = Lock()


def _connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # WAL mode keeps reads non-blocking while we snapshot from a request handler.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            series_key TEXT NOT NULL,
            ts INTEGER NOT NULL,         -- unix milliseconds
            value REAL NOT NULL,
            metadata TEXT,                -- optional JSON for context (bucket, contract, etc.)
            PRIMARY KEY (series_key, ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_series_ts ON snapshots(series_key, ts)")
    conn.commit()
    return conn


# --- Write path ------------------------------------------------------------

def snapshot(
    series_key: str,
    value: float | int | None,
    *,
    metadata: dict | None = None,
    ts_ms: int | None = None,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """Record a single (series_key, value) sample. Returns True if written,
    False if dedup-skipped or value-skipped."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    now_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    meta_json = json.dumps(metadata, separators=(",", ":")) if metadata else None

    with _lock, _connect(db_path) as conn:
        # Dedup: skip if the most recent snapshot for this key is < min_interval_seconds ago
        if min_interval_seconds > 0:
            row = conn.execute(
                "SELECT ts FROM snapshots WHERE series_key = ? ORDER BY ts DESC LIMIT 1",
                (series_key,),
            ).fetchone()
            if row and (now_ms - row["ts"]) < min_interval_seconds * 1000:
                return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (series_key, ts, value, metadata) VALUES (?, ?, ?, ?)",
                (series_key, now_ms, v, meta_json),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            log.warning("history.snapshot write failed for %s: %s", series_key, exc)
            return False


def snapshot_many(
    items: list[tuple[str, float | int | None]],
    *,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """Bulk variant — single DB roundtrip, same dedup semantics. Returns
    number of rows written."""
    written = 0
    for key, val in items:
        if snapshot(key, val, min_interval_seconds=min_interval_seconds, db_path=db_path):
            written += 1
    return written


# --- Read path -------------------------------------------------------------

@dataclass
class Sample:
    ts_ms: int
    value: float


def latest(series_key: str, *, db_path: Path = DEFAULT_DB_PATH) -> Sample | None:
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT ts, value FROM snapshots WHERE series_key = ? ORDER BY ts DESC LIMIT 1",
            (series_key,),
        ).fetchone()
    if not row:
        return None
    return Sample(ts_ms=row["ts"], value=row["value"])


def value_at(series_key: str, ago_seconds: int, *, db_path: Path = DEFAULT_DB_PATH) -> Sample | None:
    """Snapshot whose timestamp is closest to ``ago_seconds`` ago. Returns
    ``None`` if no snapshot is within ``2 × ago_seconds`` of that target —
    we don't want to compare today's value against a 6-month-old reading
    just because the series is new."""
    target_ms = int(time.time() * 1000) - ago_seconds * 1000
    tolerance_ms = max(ago_seconds * 1000 * 2, 24 * 3600 * 1000)  # at least 1 day
    with _lock, _connect(db_path) as conn:
        # Pick the row whose ts is closest to target_ms within tolerance.
        row = conn.execute("""
            SELECT ts, value
            FROM snapshots
            WHERE series_key = ? AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) ASC
            LIMIT 1
        """, (
            series_key,
            target_ms - tolerance_ms,
            target_ms + tolerance_ms,
            target_ms,
        )).fetchone()
    if not row:
        return None
    return Sample(ts_ms=row["ts"], value=row["value"])


def range_over(series_key: str, days: int, *, db_path: Path = DEFAULT_DB_PATH) -> tuple[float, float] | None:
    cutoff_ms = int(time.time() * 1000) - days * 24 * 3600 * 1000
    with _lock, _connect(db_path) as conn:
        row = conn.execute("""
            SELECT MIN(value) AS lo, MAX(value) AS hi
            FROM snapshots
            WHERE series_key = ? AND ts >= ?
        """, (series_key, cutoff_ms)).fetchone()
    if not row or row["lo"] is None:
        return None
    return float(row["lo"]), float(row["hi"])


def percentile(series_key: str, days: int, *, db_path: Path = DEFAULT_DB_PATH) -> float | None:
    """Where does the current value sit in the [0, 1] percentile of the
    last N days? 0.5 = at the median, 0.9 = unusually high, etc."""
    cutoff_ms = int(time.time() * 1000) - days * 24 * 3600 * 1000
    with _lock, _connect(db_path) as conn:
        last_row = conn.execute(
            "SELECT value FROM snapshots WHERE series_key = ? ORDER BY ts DESC LIMIT 1",
            (series_key,),
        ).fetchone()
        if not last_row:
            return None
        last_val = float(last_row["value"])
        cnt_row = conn.execute("""
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN value < ? THEN 1 ELSE 0 END) AS below
            FROM snapshots WHERE series_key = ? AND ts >= ?
        """, (last_val, series_key, cutoff_ms)).fetchone()
    if not cnt_row or cnt_row["n"] == 0:
        return None
    return cnt_row["below"] / cnt_row["n"]


def delta_summary(series_key: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """All the standard window comparisons in one shot. Result shape is
    optimized for direct rendering — dotted fields are absent when the
    underlying snapshot doesn't exist (rather than zero)."""
    cur = latest(series_key, db_path=db_path)
    if cur is None:
        return {"series_key": series_key, "current": None}

    out: dict = {
        "series_key": series_key,
        "current": cur.value,
        "current_ts_ms": cur.ts_ms,
    }
    for label, ago in [("1d", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("365d", 365 * 86400)]:
        s = value_at(series_key, ago, db_path=db_path)
        if s is not None:
            out[f"value_{label}"] = s.value
            out[f"delta_{label}"] = round(cur.value - s.value, 6)

    for label, days in [("7d", 7), ("30d", 30), ("365d", 365)]:
        rng = range_over(series_key, days, db_path=db_path)
        if rng is not None:
            out[f"range_{label}"] = list(rng)

    pct30 = percentile(series_key, 30, db_path=db_path)
    if pct30 is not None:
        out["percentile_30d"] = round(pct30, 3)

    return out


# --- Maintenance -----------------------------------------------------------

def prune(max_age_days: int = DEFAULT_RETENTION_DAYS, *, db_path: Path = DEFAULT_DB_PATH) -> int:
    cutoff_ms = int(time.time() * 1000) - max_age_days * 24 * 3600 * 1000
    with _lock, _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff_ms,))
        conn.commit()
    return cur.rowcount


def stats(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """How many snapshots, how many distinct series, oldest/newest. Cheap
    diagnostic for the operator."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) AS n,
              COUNT(DISTINCT series_key) AS keys,
              MIN(ts) AS oldest_ms,
              MAX(ts) AS newest_ms
            FROM snapshots
        """).fetchone()
    return {
        "snapshots": row["n"] or 0,
        "series_keys": row["keys"] or 0,
        "oldest_ms": row["oldest_ms"],
        "newest_ms": row["newest_ms"],
    }


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    logging.basicConfig(level=logging.INFO)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        # Fake history: 30 daily snapshots
        now_ms = int(time.time() * 1000)
        for i in range(30):
            t = now_ms - (29 - i) * 86400 * 1000
            v = 0.05 + 0.001 * i  # rising series 0.05 → 0.079
            snapshot("poly_price.cut25", v, ts_ms=t, min_interval_seconds=0, db_path=db)
        # Bonus current value
        snapshot("poly_price.cut25", 0.12, min_interval_seconds=0, db_path=db)

        ds = delta_summary("poly_price.cut25", db_path=db)
        print(f"current:      {ds['current']:.4f}")
        print(f"1d ago:       {ds.get('value_1d', '—')}    Δ {ds.get('delta_1d', '—')}")
        print(f"7d ago:       {ds.get('value_7d', '—')}    Δ {ds.get('delta_7d', '—')}")
        print(f"30d range:    {ds.get('range_30d')}")
        print(f"30d pctile:   {ds.get('percentile_30d')}")
        print(f"stats:        {stats(db)}")

        assert ds["current"] == 0.12
        assert ds["delta_30d"] is not None
        # Series rose 0.05 → 0.12. Percentile counts strictly-below: the
        # current value sits at the top of its window (only itself is >= it),
        # so percentile ≈ 30/31 = 0.968.
        assert ds["percentile_30d"] >= 0.95, f"got {ds['percentile_30d']}"
        print("\n✓ historical_store self-test passed")
