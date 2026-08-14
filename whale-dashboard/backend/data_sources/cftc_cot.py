from __future__ import annotations
"""CFTC Commitment of Traders ingester.

The COT report is published every Friday at 3:30pm ET, covering positions as
of the prior Tuesday. CFTC retired the legacy plaintext endpoint
(`cftc.gov/dea/newcot/deafut.txt` now returns 403) and migrated to a
Socrata-powered open data portal:

    https://publicreporting.cftc.gov/resource/6dca-aqww.json

Socrata gives us per-field JSON instead of column-position parsing, plus a
SQL-ish $where filter so we can fetch only the latest week and only the
contracts in MARKETS — ~20 KB instead of 1 MB.

Why this matters for a "whales" dashboard: 13F covers long equity only.
Macro whales (Bridgewater, Brevan Howard, the prop shops) trade futures
where COT is the only public window. Commercial vs non-commercial is the
classic positioning split that real macro PMs watch.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)

# Socrata endpoint for "Commitments of Traders - Futures Only Reports" (legacy).
# Date-filtered fetches only the most recent week.
COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Curated subset of contracts. Map from CFTC market_and_exchange_names (Socrata
# field) → short code we use as primary key.
MARKETS: dict[str, str] = {
    "CRUDE OIL, LIGHT SWEET-NYMEX": "CL",
    "GOLD - COMMODITY EXCHANGE INC.": "GC",
    "SILVER - COMMODITY EXCHANGE INC.": "SI",
    "COPPER- #1 - COMMODITY EXCHANGE INC.": "HG",
    "S&P 500 STOCK INDEX - CHICAGO MERCANTILE EXCHANGE": "SP",
    "E-MINI S&P 500 STOCK INDEX - CHICAGO MERCANTILE EXCHANGE": "ES",
    "NASDAQ-100 Consolidated - CHICAGO MERCANTILE EXCHANGE": "NQ",
    "DJIA Consolidated - CHICAGO BOARD OF TRADE": "YM",
    "RUSSELL 2000 MINI INDEX FUTURE - CHICAGO MERCANTILE EXCHANGE": "RTY",
    "U.S. DOLLAR INDEX - ICE FUTURES U.S.": "DX",
    "EURO FX - CHICAGO MERCANTILE EXCHANGE": "6E",
    "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE": "6J",
    "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE": "6B",
    "10-YEAR U.S. TREASURY NOTES - CHICAGO BOARD OF TRADE": "ZN",
    "30-YEAR U.S. TREASURY BONDS - CHICAGO BOARD OF TRADE": "ZB",
    "WHEAT-SRW - CHICAGO BOARD OF TRADE": "ZW",
    "CORN - CHICAGO BOARD OF TRADE": "ZC",
    "SOYBEANS - CHICAGO BOARD OF TRADE": "ZS",
    "BITCOIN - CHICAGO MERCANTILE EXCHANGE": "BTC",
}


def _user_agent() -> str:
    # CFTC's Socrata endpoint doesn't enforce UA but we send the same one
    # we use for SEC for politeness.
    return os.environ.get("SEC_USER_AGENT", "WhaleDashboard contact@example.com")


def _to_int(v) -> Optional[int]:
    """Socrata returns numeric fields as JSON strings — parse defensively."""
    if v is None or v == "":
        return None
    try:
        # Numbers may be "123" or "123.0" — int() chokes on the latter
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_cot_payload(payload: list[dict]) -> list[dict]:
    """Filter Socrata JSON rows to whitelisted markets and project the columns
    we actually care about."""
    out: list[dict] = []
    for r in payload:
        market_name = (r.get("market_and_exchange_names") or "").strip()
        code = MARKETS.get(market_name)
        if not code:
            continue
        # Socrata date is ISO 8601 with time; we only want yyyy-mm-dd
        rd = (r.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not rd:
            continue
        out.append({
            "market_code": code,
            "market_name": market_name,
            "report_date": rd,
            "open_interest":      _to_int(r.get("open_interest_all")),
            "noncommercial_long":  _to_int(r.get("noncomm_positions_long_all")),
            "noncommercial_short": _to_int(r.get("noncomm_positions_short_all")),
            "commercial_long":     _to_int(r.get("comm_positions_long_all")),
            "commercial_short":    _to_int(r.get("comm_positions_short_all")),
            "nonreportable_long":  _to_int(r.get("nonrept_positions_long_all")),
            "nonreportable_short": _to_int(r.get("nonrept_positions_short_all")),
        })
    return out


async def ingest() -> dict:
    """Fetch the latest CFTC CSV and upsert all whitelisted markets."""
    started = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (source, started_at, status) VALUES (?, ?, 'running')",
            ("cftc_cot", started),
        )
        run_id = cur.lastrowid

    n_new = 0
    error: Optional[str] = None
    try:
        # Build a Socrata $where clause to fetch only the markets we want.
        # IN-list of contract names; quoted SQL strings.
        names = ", ".join("'" + n.replace("'", "''") + "'" for n in MARKETS)
        params = {
            "$where": f"market_and_exchange_names in ({names})",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": "200",  # 19 markets × ~10 weeks of history is ample
        }
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(COT_URL,
                                   params=params,
                                   headers={"User-Agent": _user_agent(),
                                            "Accept": "application/json"}) as r:
                r.raise_for_status()
                payload = await r.json()
        parsed = parse_cot_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            for row in parsed:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO cftc_cot
                         (market_code, market_name, report_date,
                          commercial_long, commercial_short,
                          noncommercial_long, noncommercial_short,
                          nonreportable_long, nonreportable_short,
                          open_interest, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["market_code"], row["market_name"], row["report_date"],
                     row["commercial_long"], row["commercial_short"],
                     row["noncommercial_long"], row["noncommercial_short"],
                     row["nonreportable_long"], row["nonreportable_short"],
                     row["open_interest"], now),
                )
                if cur.rowcount:
                    n_new += 1
    except Exception as e:
        logger.exception("cot: ingest failed")
        error = f"{type(e).__name__}: {e}"
    finally:
        finished = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                """UPDATE ingest_runs SET finished_at=?, status=?, n_new=?, error=?
                    WHERE id=?""",
                (finished, "error" if error else "ok", n_new, error, run_id),
            )
    return {"new_rows": n_new, "error": error}
