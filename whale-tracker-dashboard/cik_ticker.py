"""CIK → ticker lookup.

SEC publishes a free, authoritative CIK→ticker map at
https://www.sec.gov/files/company_tickers.json. We cache it on disk
and refresh once a day. Used to enrich 13D/G and 8-K rows with a ticker
so they join cleanly with insider transactions in the synthesis view.

The Form 4 XML carries `issuerTradingSymbol` directly, so we don't
need this for Form 4 — only for filings where the issuer is referenced
by CIK alone.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import edgar

log = logging.getLogger("cik_ticker")

CACHE_PATH = Path(__file__).parent / "cik_tickers.json"
TTL_S = 24 * 3600
URL = "https://www.sec.gov/files/company_tickers.json"

_state: dict = {"loaded_at": 0.0, "map": {}}


async def ensure_loaded() -> int:
    """Make sure the map is loaded. Returns entry count."""
    if _state["map"] and (time.time() - _state["loaded_at"]) < TTL_S:
        return len(_state["map"])

    if CACHE_PATH.exists() and (time.time() - CACHE_PATH.stat().st_mtime) < TTL_S:
        try:
            _state["map"] = json.loads(CACHE_PATH.read_text())
            _state["loaded_at"] = time.time()
            return len(_state["map"])
        except Exception as e:
            log.warning("cik_tickers cache parse failed (%s) — refetching", e)

    try:
        data = await edgar.fetch_json(URL)
    except Exception as e:
        log.warning("cik_tickers fetch failed: %s", e)
        return len(_state["map"])

    # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    m: dict[str, dict] = {}
    for v in data.values():
        cik_int = str(v.get("cik_str", ""))
        if not cik_int:
            continue
        entry = {"ticker": (v.get("ticker") or "").upper(), "name": v.get("title", "")}
        m[cik_int] = entry
        m[cik_int.zfill(10)] = entry

    _state["map"] = m
    _state["loaded_at"] = time.time()
    try:
        CACHE_PATH.write_text(json.dumps(m))
    except Exception as e:
        log.info("cik_tickers cache write skipped: %s", e)
    log.info("cik_tickers loaded: %d entries", len(m))
    return len(m)


def lookup(cik: str) -> dict | None:
    if not cik:
        return None
    m = _state["map"]
    if not m:
        return None
    if cik in m:
        return m[cik]
    if cik.isdigit():
        s = str(int(cik))
        return m.get(s) or m.get(s.zfill(10))
    return None


def lookup_ticker(cik: str) -> str | None:
    e = lookup(cik)
    return e["ticker"] if e and e.get("ticker") else None


def lookup_name(cik: str) -> str | None:
    e = lookup(cik)
    return e["name"] if e and e.get("name") else None
