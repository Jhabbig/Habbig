#!/usr/bin/env python3
"""
Wallet metadata via Polygonscan — wallet age + first funding tx.

A copy-trade wallet that's only 2 weeks old, or that was funded by a
Tornado mixer, is uncopyable regardless of how good its PnL looks. This
module gives the dashboard cheap filters to weed those out.

Falls back gracefully when POLYGONSCAN_API_KEY isn't set: every function
returns `None` so callers can branch on availability without crashing.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

DB_PATH = Path(__file__).parent / "wallet_metadata.sqlite3"
POLYGONSCAN_API = "https://api.polygonscan.com/api"
RATE_PAUSE = 0.21          # Polygonscan free tier ≈ 5 req/s
TTL_SECONDS = 30 * 86400   # 30 days — wallet age basically doesn't change

logger = logging.getLogger(__name__)


def _api_key() -> str:
    return os.environ.get("POLYGONSCAN_API_KEY", "").strip()


def is_available() -> bool:
    return bool(_api_key())


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS wallet_metadata (
                address          TEXT PRIMARY KEY,
                first_tx_ts      INTEGER,
                first_tx_hash    TEXT,
                funding_source   TEXT,
                tx_count_seen    INTEGER NOT NULL DEFAULT 0,
                fetched_at       INTEGER NOT NULL DEFAULT 0,
                fetch_status     TEXT
            )
        """)


def _fetch_polygonscan(address: str) -> dict | None:
    """Hit polygonscan for the wallet's first transaction."""
    key = _api_key()
    if not key:
        return None
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "sort": "asc",
        "page": 1,
        "offset": 1,
        "apikey": key,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(POLYGONSCAN_API, params=params)
            r.raise_for_status()
            data = r.json()
        if data.get("status") != "1":
            return {"status": "no_data", "result": []}
        result = data.get("result", [])
        if not result:
            return {"status": "no_data", "result": []}
        first = result[0]
        return {
            "status": "ok",
            "first_tx_ts": int(first.get("timeStamp", 0)) or None,
            "first_tx_hash": first.get("hash"),
            "funding_source": first.get("from"),  # who sent the very first tx
        }
    except Exception as e:
        logger.warning("polygonscan fetch failed for %s: %s", address[:10], e)
        return None


def get_wallet_metadata(address: str, force_refresh: bool = False) -> dict | None:
    """
    Cached lookup. Returns:
      {address, first_tx_ts, age_days, first_tx_hash, funding_source}
    or None if unavailable / not found.
    """
    if not address:
        return None
    address = address.lower()
    init_db()
    now = int(time.time())

    with _conn() as c:
        row = c.execute(
            "SELECT * FROM wallet_metadata WHERE address = ?", (address,),
        ).fetchone()
        if row and not force_refresh:
            if (now - (row["fetched_at"] or 0)) < TTL_SECONDS and row["fetch_status"] in ("ok", "no_data"):
                return _row_to_meta(row)

    if not is_available():
        return None

    fetched = _fetch_polygonscan(address)
    time.sleep(RATE_PAUSE)
    if fetched is None:
        return None

    with _conn() as c:
        c.execute("""
            INSERT INTO wallet_metadata (address, first_tx_ts, first_tx_hash,
                                         funding_source, tx_count_seen, fetched_at, fetch_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                first_tx_ts    = excluded.first_tx_ts,
                first_tx_hash  = excluded.first_tx_hash,
                funding_source = excluded.funding_source,
                tx_count_seen  = excluded.tx_count_seen,
                fetched_at     = excluded.fetched_at,
                fetch_status   = excluded.fetch_status
        """, (
            address,
            fetched.get("first_tx_ts"),
            fetched.get("first_tx_hash"),
            fetched.get("funding_source"),
            1 if fetched.get("first_tx_hash") else 0,
            now,
            fetched.get("status", "unknown"),
        ))
        row = c.execute(
            "SELECT * FROM wallet_metadata WHERE address = ?", (address,),
        ).fetchone()
    return _row_to_meta(row) if row else None


def _row_to_meta(row: sqlite3.Row) -> dict:
    first_tx_ts = row["first_tx_ts"]
    age_days = None
    if first_tx_ts and first_tx_ts > 0:
        age_days = round((time.time() - first_tx_ts) / 86400, 1)
    return {
        "address": row["address"],
        "first_tx_ts": first_tx_ts,
        "age_days": age_days,
        "first_tx_hash": row["first_tx_hash"],
        "funding_source": row["funding_source"],
        "fetch_status": row["fetch_status"],
        "fetched_at": row["fetched_at"],
    }


def enrich_addresses(addresses: list[str], limit: int = 20) -> dict[str, dict]:
    """Batch enrichment with rate limiting. Caps at `limit` to keep scans fast."""
    out: dict[str, dict] = {}
    for a in addresses[:limit]:
        meta = get_wallet_metadata(a)
        if meta:
            out[a.lower()] = meta
    return out


if __name__ == "__main__":
    print("Polygonscan available:", is_available())
    if is_available():
        meta = get_wallet_metadata("0x000000000000000000000000000000000000dead")
        print(meta)
