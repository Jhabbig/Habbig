"""Insider-disclosure fetcher jobs.

Six fetchers, one cron each:

  congressional_trades  every 6h  (minute=17 on hours divisible by 6)
  sec_form4             every 4h  (minute=23 on hours 0/4/8/12/16/20)
  sec_form13f           daily     (02:07 UTC)
  unusual_options       every 2h  (minute=31 on even hours)
  fec_campaign          daily     (02:34 UTC)
  lobbying              daily     (02:53 UTC)

After each fetch that inserted rows, we also run the correlator against
the currently-active market set (best-effort — if we can't load markets,
we skip correlation this tick).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.insider")


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


def _active_markets_snapshot() -> list[dict]:
    """Cheap: pull the most recent snapshot per market_slug where
    close_time is in the future. Tolerates missing schema.
    """
    try:
        conn = _connect()
    except Exception:
        return []
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT market_slug, market_title AS question, category "
            "FROM market_snapshots "
            "WHERE close_time > ? "
            "GROUP BY market_slug "
            "ORDER BY snapshot_at DESC LIMIT 250",
            (int(time.time()),),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


async def _run_fetcher_and_correlate(source_key: str) -> dict[str, Any]:
    """Shared shell: run the named fetcher, then correlate any new rows."""
    # Lazy import so a tree without insider/ doesn't crash at module load.
    try:
        import insider  # noqa: F401 ensures ALL_FETCHERS populated
        from insider.base import ALL_FETCHERS
        from insider.correlator import correlate_signal
    except ImportError as exc:
        log.warning("insider package unavailable: %s", exc)
        return {"error": f"insider import failed: {exc}"}

    # Ensure the fetcher module is imported so its @register_fetcher runs.
    try:
        __import__(f"insider.{source_key}")
    except ImportError as exc:
        return {"error": f"insider.{source_key} import failed: {exc}"}

    fetcher_cls = ALL_FETCHERS.get(source_key)
    if fetcher_cls is None:
        return {"error": f"unknown fetcher: {source_key}"}

    result = await fetcher_cls().fetch_once()
    payload = {
        "source": source_key,
        "fetched": result.fetched,
        "inserted": result.inserted,
        "duplicates": result.duplicates,
        "errors": result.errors,
        "error_message": result.error_message,
        "duration_s": result.duration_s,
    }
    if result.inserted == 0:
        return payload

    # Correlate the fresh rows.
    markets = _active_markets_snapshot()
    if not markets:
        payload["correlated"] = 0
        return payload

    conn = _connect()
    correlated = 0
    try:
        new_rows = conn.execute(
            "SELECT * FROM insider_signals "
            "WHERE source = ? AND external_id IN ({}) "
            "LIMIT ?".format(",".join("?" * len(result.sample_external_ids))),
            (source_key, *result.sample_external_ids, len(result.sample_external_ids)),
        ).fetchall() if result.sample_external_ids else []
        # ── N+1 fix ──────────────────────────────────────────────────────
        # Collect every correlation tuple in memory first, then write
        # them with one executemany. The original loop did one INSERT
        # round-trip per correlation; for a fetcher that emits hundreds
        # of correlations per tick this is a substantial saving even on
        # a local SQLite (each .execute() goes through the cursor +
        # statement-compile path).
        rows_to_insert: list[tuple] = []
        now_ts = int(time.time())
        for row in new_rows:
            signal = dict(row)
            correlations = await correlate_signal(signal, markets)
            for c in correlations:
                rows_to_insert.append((
                    signal["id"], c["market_slug"], c["correlation_type"],
                    c["correlation_explanation"], c["implied_direction"],
                    c["implied_confidence"], c["insider_score"],
                    now_ts,
                ))
        if rows_to_insert:
            conn.executemany(
                "INSERT OR REPLACE INTO insider_market_correlations ("
                "  signal_id, market_slug, correlation_type, correlation_explanation,"
                "  implied_direction, implied_confidence, insider_score, computed_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                rows_to_insert,
            )
            correlated = len(rows_to_insert)
        # ──────────────────────────────────────────────────────────────────
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("insider correlate persist failed: %s", exc)
    finally:
        conn.close()
    payload["correlated"] = correlated
    return payload


@register_job("fetch_congressional_trades")
async def fetch_congressional_trades() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("congressional_trades")


@register_job("fetch_sec_form4")
async def fetch_sec_form4() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("sec_form4")


@register_job("fetch_sec_form13f")
async def fetch_sec_form13f() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("sec_form13f")


@register_job("fetch_unusual_options")
async def fetch_unusual_options() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("unusual_options")


@register_job("fetch_fec_campaign")
async def fetch_fec_campaign() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("fec_campaign")


@register_job("fetch_lobbying")
async def fetch_lobbying() -> dict[str, Any]:
    return await _run_fetcher_and_correlate("lobbying")


# Cron layout
register_cron("fetch_congressional_trades", hour=0, minute=17)
register_cron("fetch_congressional_trades", hour=6, minute=17)
register_cron("fetch_congressional_trades", hour=12, minute=17)
register_cron("fetch_congressional_trades", hour=18, minute=17)

register_cron("fetch_sec_form4", hour=0,  minute=23)
register_cron("fetch_sec_form4", hour=4,  minute=23)
register_cron("fetch_sec_form4", hour=8,  minute=23)
register_cron("fetch_sec_form4", hour=12, minute=23)
register_cron("fetch_sec_form4", hour=16, minute=23)
register_cron("fetch_sec_form4", hour=20, minute=23)

register_cron("fetch_sec_form13f", hour=2, minute=7)
register_cron("fetch_fec_campaign",  hour=2, minute=34)
register_cron("fetch_lobbying",      hour=2, minute=53)

for _hour in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22):
    register_cron("fetch_unusual_options", hour=_hour, minute=31)
