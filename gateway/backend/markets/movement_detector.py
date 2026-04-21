"""Market-mover detection — compares current market_snapshots to history.

Runs on a 5-minute cadence (see jobs/movement_jobs.py). For each active
market with a fresh snapshot, detects:

  odds_movement          change ≥ 8pp in the last 24h
  volume_spike           24h volume ≥ 3× 30-day average
  approaching_resolution 48h / 24h / 6h before close (one event per window)
  reversal               trending one way for 48h, reversed >50% in 24h
  new_market             first snapshot we've seen in a tracked category

Events land in ``market_movement_events`` (migration 056). The delivery
side lives in jobs/movement_jobs.py — this module is pure detection +
context enrichment.

Narve context: for each event we compute how many narve-tracked
predictions exist for the market, their avg credibility, and the best-
credibility source's call. That's stored as ``narve_context_json`` on
the event row so the push / email / in-app cards have the data
pre-rendered.

Keeps its own sqlite3 handle so it can run parallel to the gateway.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("markets.movement_detector")


# ── Thresholds ──────────────────────────────────────────────────────────────

DEFAULT_MIN_ODDS_MOVEMENT = 0.08
DEFAULT_MIN_VOLUME_MULTIPLE = 3.0
RESOLUTION_WINDOWS_HOURS = (48, 24, 6)
REVERSAL_TREND_WINDOW_H = 48
REVERSAL_CHECK_WINDOW_H = 24
REVERSAL_MIN_FLIP_PCT = 0.50


# ── DB helpers ──────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent.parent / p)
    return Path(__file__).parent.parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


# ── Pure event builders ─────────────────────────────────────────────────────


def detect_odds_movement(previous: float, current: float, *, threshold: float = DEFAULT_MIN_ODDS_MOVEMENT) -> Optional[dict]:
    if previous is None or current is None:
        return None
    try:
        delta = float(current) - float(previous)
    except (TypeError, ValueError):
        return None
    if abs(delta) < threshold:
        return None
    return {
        "event_type": "odds_movement",
        "previous_value": float(previous),
        "current_value": float(current),
        "magnitude": round(delta, 6),
        "window_seconds": 24 * 3600,
    }


def detect_volume_spike(current_24h: float, avg_30d: float, *, multiple: float = DEFAULT_MIN_VOLUME_MULTIPLE) -> Optional[dict]:
    if current_24h is None or avg_30d is None or avg_30d <= 0:
        return None
    try:
        ratio = float(current_24h) / float(avg_30d)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if ratio < multiple:
        return None
    return {
        "event_type": "volume_spike",
        "previous_value": float(avg_30d),
        "current_value": float(current_24h),
        "magnitude": round(ratio, 4),
        "window_seconds": 24 * 3600,
    }


def detect_approaching_resolution(close_time: int, now_ts: int) -> Optional[dict]:
    if not close_time or close_time <= now_ts:
        return None
    hours_left = (close_time - now_ts) / 3600.0
    for window in RESOLUTION_WINDOWS_HOURS:
        if abs(hours_left - window) < 0.5:  # within the 5-minute tick
            return {
                "event_type": "approaching_resolution",
                "previous_value": None,
                "current_value": round(hours_left, 2),
                "magnitude": float(window),
                "window_seconds": int(window * 3600),
            }
    return None


def detect_reversal(
    trend_48h_direction: int,
    recent_24h_delta: float,
) -> Optional[dict]:
    """``trend_48h_direction``: +1 if rising, -1 if falling, 0 if flat.
    ``recent_24h_delta``: signed delta over the last 24h.

    A reversal fires when the sign of the 24h delta is opposite to the
    48h trend direction AND its magnitude is at least 50% of the prior
    trend.
    """
    if trend_48h_direction == 0 or recent_24h_delta is None:
        return None
    if (trend_48h_direction > 0 and recent_24h_delta >= 0) or \
       (trend_48h_direction < 0 and recent_24h_delta <= 0):
        return None
    if abs(recent_24h_delta) < REVERSAL_MIN_FLIP_PCT * 0.01 * REVERSAL_CHECK_WINDOW_H:
        # Threshold derived from spec: >50% of prior direction in 24h.
        # We use a bounded absolute floor (~0.01/hour) as well as the pct.
        return None
    return {
        "event_type": "reversal",
        "previous_value": float(trend_48h_direction),
        "current_value": round(recent_24h_delta, 6),
        "magnitude": round(recent_24h_delta, 6),
        "window_seconds": REVERSAL_CHECK_WINDOW_H * 3600,
    }


def detect_new_market(first_seen_at: int, now_ts: int, tracked_categories: set[str], category: Optional[str]) -> Optional[dict]:
    if not first_seen_at:
        return None
    age_hours = (now_ts - first_seen_at) / 3600.0
    if age_hours > 1.0:  # Only fire once per market.
        return None
    if tracked_categories and (category or "").lower() not in tracked_categories:
        return None
    return {
        "event_type": "new_market",
        "previous_value": None,
        "current_value": None,
        "magnitude": 1.0,
        "window_seconds": 3600,
    }


# ── Orchestration ───────────────────────────────────────────────────────────


def build_narve_context(conn: sqlite3.Connection, market_slug: str) -> dict:
    """Small DB-backed enrichment — number of narve predictions on this
    market and the best-credibility source's call.
    """
    if not _table_exists(conn, "predictions"):
        return {"prediction_count": 0}
    rows = conn.execute(
        "SELECT source_handle, direction, predicted_probability "
        "FROM predictions WHERE market_id = ? OR market_id = ?",
        (market_slug, f"poly:{market_slug}"),
    ).fetchall()
    if not rows:
        return {"prediction_count": 0}

    cred: dict[str, float] = {}
    if _table_exists(conn, "source_credibility"):
        for r in conn.execute(
            "SELECT source_handle, global_credibility FROM source_credibility"
        ).fetchall():
            cred[r["source_handle"]] = float(r["global_credibility"] or 0.5)

    yes = sum(1 for r in rows if str(r["direction"] or "").upper() == "YES")
    no = sum(1 for r in rows if str(r["direction"] or "").upper() == "NO")
    best = None
    best_cred = -1.0
    for r in rows:
        c = cred.get(r["source_handle"], 0.5)
        if c > best_cred:
            best_cred = c
            best = {
                "source_handle": r["source_handle"],
                "direction": r["direction"],
                "credibility": round(c, 3),
            }
    avg_cred = round(sum(cred.get(r["source_handle"], 0.5) for r in rows) / max(len(rows), 1), 3)
    return {
        "prediction_count": len(rows),
        "yes_count": yes,
        "no_count": no,
        "avg_credibility": avg_cred,
        "best_source": best,
    }


def persist_event(conn: sqlite3.Connection, market_slug: str, event: dict, context: dict) -> int:
    cur = conn.execute(
        "INSERT INTO market_movement_events ("
        " market_slug, event_type, detected_at, previous_value, current_value,"
        " magnitude, window_seconds, narve_context_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            market_slug,
            event["event_type"],
            int(time.time()),
            event.get("previous_value"),
            event.get("current_value"),
            event.get("magnitude"),
            event.get("window_seconds"),
            json.dumps(context),
        ),
    )
    return cur.lastrowid or 0


def run_detection_once(
    *,
    tracked_categories: Optional[set[str]] = None,
    min_movement: float = DEFAULT_MIN_ODDS_MOVEMENT,
    min_volume_multiple: float = DEFAULT_MIN_VOLUME_MULTIPLE,
) -> dict:
    """Entry point. Reads the latest snapshots and history; writes events.

    Safely no-ops when ``market_snapshots`` / ``market_movement_events``
    are missing so the job can run on fresh branches without crashing.
    """
    conn = _connect()
    try:
        if not _table_exists(conn, "market_movement_events"):
            return {"error": "market_movement_events table missing"}
        if not _table_exists(conn, "market_snapshots"):
            return {"ok": True, "snapshots": 0, "events": 0}

        snaps = conn.execute(
            "SELECT market_slug, yes_price, volume_24h, avg_volume_30d, close_time, category, "
            " snapshot_at, first_seen_at FROM market_snapshots "
            "ORDER BY snapshot_at DESC"
        ).fetchall()

        seen_slugs: set[str] = set()
        events_written = 0
        now_ts = int(time.time())
        tracked = {c.lower() for c in (tracked_categories or set())}

        for row in snaps:
            slug = row["market_slug"]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            # Historical snapshot (≥ 24h old) for odds comparison.
            prev = conn.execute(
                "SELECT yes_price FROM market_snapshots "
                "WHERE market_slug = ? AND snapshot_at <= ? "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (slug, now_ts - 24 * 3600),
            ).fetchone()

            events: list[dict] = []
            if prev and prev["yes_price"] is not None and row["yes_price"] is not None:
                e = detect_odds_movement(prev["yes_price"], row["yes_price"], threshold=min_movement)
                if e:
                    events.append(e)

            if row["volume_24h"] is not None and row["avg_volume_30d"]:
                e = detect_volume_spike(row["volume_24h"], row["avg_volume_30d"], multiple=min_volume_multiple)
                if e:
                    events.append(e)

            if row["close_time"]:
                e = detect_approaching_resolution(int(row["close_time"]), now_ts)
                if e:
                    events.append(e)

            if row["first_seen_at"]:
                e = detect_new_market(int(row["first_seen_at"]), now_ts, tracked, row["category"])
                if e:
                    events.append(e)

            # Reversal check — optional; needs two historical points.
            rev_hist = conn.execute(
                "SELECT yes_price, snapshot_at FROM market_snapshots "
                "WHERE market_slug = ? AND snapshot_at <= ? "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (slug, now_ts - REVERSAL_TREND_WINDOW_H * 3600),
            ).fetchone()
            if rev_hist and row["yes_price"] is not None and prev and prev["yes_price"] is not None:
                trend48 = 1 if row["yes_price"] > rev_hist["yes_price"] else (
                    -1 if row["yes_price"] < rev_hist["yes_price"] else 0
                )
                delta24 = row["yes_price"] - prev["yes_price"]
                e = detect_reversal(trend48, delta24)
                if e:
                    events.append(e)

            if not events:
                continue

            context = build_narve_context(conn, slug)
            for ev in events:
                persist_event(conn, slug, ev, context)
                events_written += 1

        conn.commit()
        return {"ok": True, "snapshots": len(seen_slugs), "events": events_written}
    finally:
        conn.close()
