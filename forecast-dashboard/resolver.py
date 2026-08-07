"""Market resolution against official venue settlement APIs.

Polymarket (gamma): GET /markets?condition_ids=<id>&closed=true. Verified
live 2026-08-07: plain condition_ids returns [] for closed markets, and the
/markets/<id> path form only accepts gamma's numeric id (422 on condition
ids), so the closed=true list query is the one working form — an empty list
simply means "not closed yet". outcomePrices and outcomes arrive as
JSON-encoded strings, e.g. '["1", "0"]' / '["Yes", "No"]'.

Kalshi: GET /trade-api/v2/markets/<ticker> -> {"market": {...}} with
status 'settled' or 'finalized' and result 'yes'/'no'.

Options (self-issued markets from ingest_options): venue_id encodes symbol,
expiry, and strike; the official close for the expiry date comes from the
Yahoo daily chart API and the market resolves close >= strike. Left pending
until that day's bar exists.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

import db
import ingest_options

log = logging.getLogger("forecast.resolver")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo blocks non-browser UAs on some edges (see models_fed.py).
_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15
BATCH_LIMIT = 100
GRACE_HOURS = 6
SETTLE_EPS = 0.01


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": "narve-forecast/1.0"})


def _parse_json_list(raw) -> list | None:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _poly_outcome_from_market(m: dict) -> int | None:
    if not m.get("closed"):
        return None
    prices = _parse_json_list(m.get("outcomePrices"))
    if not prices or len(prices) != 2:
        return None
    try:
        p = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None

    yes_idx = 0
    outcomes = _parse_json_list(m.get("outcomes")) or []
    for i, name in enumerate(outcomes):
        if isinstance(name, str) and name.strip().lower() == "yes":
            yes_idx = i
            break
    no_idx = 1 - yes_idx

    if p[yes_idx] >= 1 - SETTLE_EPS and p[no_idx] <= SETTLE_EPS:
        return 1
    if p[yes_idx] <= SETTLE_EPS and p[no_idx] >= 1 - SETTLE_EPS:
        return 0
    return None


async def _poly_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    resp = await client.get(GAMMA_MARKETS_URL, params={"condition_ids": venue_id, "closed": "true"})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return None
    return _poly_outcome_from_market(data[0])


async def _kalshi_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    resp = await client.get(KALSHI_MARKET_URL.format(ticker=venue_id))
    resp.raise_for_status()
    market = (resp.json() or {}).get("market") or {}
    if market.get("status") not in ("settled", "finalized"):
        return None
    result = (market.get("result") or "").strip().lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    return None


async def _options_outcome(client: httpx.AsyncClient, venue_id: str) -> int | None:
    parsed = ingest_options.parse_venue_id(venue_id)
    if not parsed:
        return None
    symbol, expiry, strike = parsed
    close_dt = datetime(expiry.year, expiry.month, expiry.day, ingest_options.CLOSE_HOUR_UTC, tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < close_dt + timedelta(hours=GRACE_HOURS):
        return None
    day_start = datetime(expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc)
    params = {
        "period1": int((day_start - timedelta(days=1)).timestamp()),
        "period2": int((day_start + timedelta(days=2)).timestamp()),
        "interval": "1d",
    }
    resp = await client.get(YAHOO_CHART_URL.format(symbol=symbol), params=params, headers=_YAHOO_HEADERS)
    resp.raise_for_status()
    results = ((resp.json() or {}).get("chart") or {}).get("result") or []
    if not results or not isinstance(results[0], dict):
        return None
    result = results[0]
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    closes = quotes.get("close") or []
    # US daily bars are stamped at session open (13:30/14:30 UTC), so the
    # stamp's UTC date equals the trading date.
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        if datetime.fromtimestamp(ts, tz=timezone.utc).date() == expiry:
            return 1 if float(close) >= strike else 0
    return None


async def _venue_outcome(client: httpx.AsyncClient, market: dict) -> int | None:
    venue = (market.get("venue") or "").lower()
    venue_id = market.get("venue_id")
    if not venue_id:
        return None
    if venue == "polymarket":
        return await _poly_outcome(client, venue_id)
    if venue == "kalshi":
        return await _kalshi_outcome(client, venue_id)
    if venue == "options":
        return await _options_outcome(client, venue_id)
    return None


async def resolve_pending(conn: sqlite3.Connection) -> int:
    candidates = db.unresolved_past_close(conn, grace_hours=GRACE_HOURS)[:BATCH_LIMIT]
    if not candidates:
        return 0

    resolved = 0
    async with _make_client() as client:
        for market in candidates:
            uid = market.get("uid")
            try:
                outcome = await _venue_outcome(client, market)
            except Exception as e:
                log.warning("resolver: outcome check failed for %s: %s", uid, e)
                continue
            if outcome is None:
                continue
            try:
                db.mark_resolved(conn, uid, outcome)
                backfilled = db.backfill_calibration_outcomes(conn, uid, outcome)
            except Exception as e:
                log.error("resolver: failed to persist %s: %s", uid, e)
                continue
            resolved += 1
            log.info("resolved %s outcome=%s (calibration rows backfilled: %s)", uid, outcome, backfilled)
    return resolved
