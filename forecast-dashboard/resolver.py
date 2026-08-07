"""Market resolution against official venue settlement APIs.

Polymarket (gamma): GET /markets?condition_ids=<id>&closed=true. Verified
live 2026-08-07: plain condition_ids returns [] for closed markets, and the
/markets/<id> path form only accepts gamma's numeric id (422 on condition
ids), so the closed=true list query is the one working form — an empty list
simply means "not closed yet". outcomePrices and outcomes arrive as
JSON-encoded strings, e.g. '["1", "0"]' / '["Yes", "No"]'.

Kalshi: GET /trade-api/v2/markets/<ticker> -> {"market": {...}} with
status 'settled' or 'finalized' and result 'yes'/'no'.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import httpx

import db

log = logging.getLogger("forecast.resolver")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_MARKET_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"

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


async def _venue_outcome(client: httpx.AsyncClient, market: dict) -> int | None:
    venue = (market.get("venue") or "").lower()
    venue_id = market.get("venue_id")
    if not venue_id:
        return None
    if venue == "polymarket":
        return await _poly_outcome(client, venue_id)
    if venue == "kalshi":
        return await _kalshi_outcome(client, venue_id)
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
