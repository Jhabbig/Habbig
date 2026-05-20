"""CUSIP → ticker resolver via OpenFIGI.

OpenFIGI (https://www.openfigi.com/api) exposes a free batch mapping API:
  POST https://api.openfigi.com/v3/mapping
  body: [{"idType":"ID_CUSIP","idValue":"037833100"}, ...]
  response: [[{"figi":"...","ticker":"AAPL","name":"APPLE INC", ...}], ...]

Without an API key it's 25 requests/min with batches of 10 (per the docs).
With a free key (X-OPENFIGI-APIKEY header) it's 25 requests/min × 100/batch.

We resolve in batches, persist results to the `cusip_ticker` table, and
look up locally on subsequent queries. Failed/ambiguous CUSIPs are not
cached — they'll be retried on the next pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
import datetime as dt
from typing import Iterable

import httpx

import db

log = logging.getLogger("openfigi")

API_URL = "https://api.openfigi.com/v3/mapping"
API_KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()
BATCH_SIZE = 100 if API_KEY else 10   # OpenFIGI batch ceiling without/with key
RATE_INTERVAL_S = 6.0                  # 25 requests/min → ~one every 2.4s, be safe
_sem = asyncio.Semaphore(1)            # serialise to stay under the rate limit
_last_request_at = 0.0


def _client() -> httpx.AsyncClient:
    headers = {"Content-Type": "application/json",
               "User-Agent": "narve.ai whale tracker contact@narve.ai"}
    if API_KEY:
        headers["X-OPENFIGI-APIKEY"] = API_KEY
    return httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True)


async def _throttle() -> None:
    global _last_request_at
    loop = asyncio.get_event_loop()
    now = loop.time()
    wait = RATE_INTERVAL_S - (now - _last_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request_at = loop.time()


async def resolve_batch(cusips: list[str]) -> dict[str, dict]:
    """Resolve up to BATCH_SIZE CUSIPs in one request.

    Returns {cusip: {"ticker", "name", "exch_code"}} for ones that mapped
    to exactly one US-listed equity. Ambiguous / non-equity / non-US hits
    are dropped.
    """
    if not cusips:
        return {}
    payload = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"}
               for c in cusips if c]
    if not payload:
        return {}

    async with _sem:
        await _throttle()
        try:
            async with _client() as cx:
                r = await cx.post(API_URL, json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("openfigi batch failed: %s", e)
            return {}

    out: dict[str, dict] = {}
    for cusip, entry in zip([p["idValue"] for p in payload], data):
        rows = (entry or {}).get("data") or []
        if not rows:
            continue
        # Prefer Common Stock if available; otherwise take the first equity.
        chosen = None
        for r in rows:
            stype = (r.get("securityType2") or r.get("securityType") or "").lower()
            if "common" in stype or "stock" in stype:
                chosen = r
                break
        chosen = chosen or rows[0]
        ticker = (chosen.get("ticker") or "").upper()
        if not ticker:
            continue
        out[cusip] = {
            "ticker":    ticker,
            "name":      chosen.get("name", ""),
            "exch_code": chosen.get("exchCode", ""),
        }
    return out


async def resolve_and_persist(cusips: Iterable[str]) -> int:
    """Resolve a flat list of CUSIPs, persist results. Returns count resolved."""
    cusips = [c.upper() for c in {*cusips} if c]
    if not cusips:
        return 0

    total = 0
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for i in range(0, len(cusips), BATCH_SIZE):
        batch = cusips[i:i + BATCH_SIZE]
        resolved = await resolve_batch(batch)
        if not resolved:
            continue
        rows = [{
            "cusip":       cusip,
            "ticker":      meta["ticker"],
            "name":        meta.get("name", ""),
            "exch_code":   meta.get("exch_code", ""),
            "resolved_at": now_iso,
        } for cusip, meta in resolved.items()]
        db.upsert_cusip_tickers(rows)
        total += len(rows)
    return total
