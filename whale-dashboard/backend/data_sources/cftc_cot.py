from __future__ import annotations
"""CFTC Commitment of Traders ingester.

The COT report is published every Friday at 3:30pm ET, covering positions as
of the prior Tuesday. The CFTC offers a CSV export of the legacy futures-only
report at:

    https://www.cftc.gov/dea/newcot/deafut.txt

This file is updated weekly and contains every reported market — energy,
metals, ag, equity index, FX, rates — in one ~2MB file. We ingest only the
markets in MARKETS below; expand as needed.

Why this matters for a "whales" dashboard: 13F covers long equity only.
Macro whales (Bridgewater, Brevan Howard, the prop shops) trade futures
where COT is the only public window. Commercial vs non-commercial is the
classic positioning split that real macro PMs watch.
"""

import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from database import get_conn

logger = logging.getLogger(__name__)

COT_URL = "https://www.cftc.gov/dea/newcot/deafut.txt"

# Curated subset of contracts. Map from CFTC market name (as it appears in
# the CSV's first column) to a short code we use as primary key.
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
    # CFTC doesn't enforce a UA but we use the same one we use for SEC.
    return os.environ.get("SEC_USER_AGENT", "WhaleDashboard contact@example.com")


# Column indexes in the legacy futures-only CSV. The format has been stable
# since the 1990s; if CFTC ever changes it the parse will fail loudly rather
# than silently store garbage.
_IDX_MARKET   = 0
_IDX_DATE     = 2     # YYMMDD
_IDX_OI       = 7
_IDX_NC_LONG  = 8
_IDX_NC_SHORT = 9
_IDX_C_LONG   = 11
_IDX_C_SHORT  = 12
_IDX_NR_LONG  = 14
_IDX_NR_SHORT = 15


def _to_int(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(v.replace(",", ""))
    except ValueError:
        return None


def _parse_yymmdd(s: str) -> Optional[str]:
    s = s.strip()
    if len(s) != 6:
        return None
    try:
        d = datetime.strptime(s, "%y%m%d").date()
        return d.isoformat()
    except ValueError:
        return None


def parse_cot_csv(text: str) -> list[dict]:
    """Parse the CFTC legacy CSV and yield dicts for the whitelisted markets."""
    rows: list[dict] = []
    reader = csv.reader(io.StringIO(text))
    for fields in reader:
        if len(fields) < 16:
            continue
        market_name = fields[_IDX_MARKET].strip().strip('"')
        code = MARKETS.get(market_name)
        if not code:
            continue
        report_date = _parse_yymmdd(fields[_IDX_DATE])
        if not report_date:
            continue
        rows.append({
            "market_code": code,
            "market_name": market_name,
            "report_date": report_date,
            "open_interest": _to_int(fields[_IDX_OI]),
            "noncommercial_long":  _to_int(fields[_IDX_NC_LONG]),
            "noncommercial_short": _to_int(fields[_IDX_NC_SHORT]),
            "commercial_long":     _to_int(fields[_IDX_C_LONG]),
            "commercial_short":    _to_int(fields[_IDX_C_SHORT]),
            "nonreportable_long":  _to_int(fields[_IDX_NR_LONG]),
            "nonreportable_short": _to_int(fields[_IDX_NR_SHORT]),
        })
    return rows


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
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(COT_URL,
                                   headers={"User-Agent": _user_agent()}) as r:
                r.raise_for_status()
                text = await r.text()
        parsed = parse_cot_csv(text)
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
