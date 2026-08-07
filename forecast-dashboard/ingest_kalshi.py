"""Kalshi ingestion via the public elections API (no auth).

Market dict contract (shared with ingest_polymarket.fetch_horizon):
    {
        "venue": "kalshi",
        "venue_id": str,          # market ticker, stable per market
        "event_ticker": str | None,
        "question": str,
        "category": str | None,
        "url": str | None,
        "end_date": str | None,   # ISO 8601, UTC
        "yes_bid": float | None,  # 0..1 (convert Kalshi cents to probability)
        "yes_ask": float | None,  # 0..1
        "last_price": float | None,
        "liquidity": float | None,
        "volume_24h": float | None,
        "yes_token_id": str | None,
        "no_token_id": str | None,
    }

Price normalization vendored from centralbank-dashboard/ingestion/kalshi_client.py
(_normalize_price) — Kalshi v2 sends prices either as 0-1 dollar floats or 1-99
cent ints depending on endpoint/era, so both must be handled. As of 2026-08 the
public /markets payload carries dollar-string fields (yes_bid_dollars "0.2800",
volume_24h_fp "7.00", liquidity_dollars) and the legacy cent-int keys are gone;
we prefer the *_dollars/_fp keys and fall back to the legacy names. The sibling's
_yes_price (last-trade-first fallback) is deliberately NOT vendored: the spine's
db.compute_baseline owns the bid/ask/last fallback ordering here.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
PAGE_LIMIT = 1000
TIMEOUT = 20.0
_MAX_PAGES = 60
_PAGE_PAUSE = 0.25
_RETRY_BACKOFF = 1.0
_UA = "forecast-dashboard/0.1"

# The open 7-day universe exceeds the page budget (60k+ rows), and the cursor
# order does not surface these low-volume series before the cap — yet the
# weather/fed models and rulepacks depend on them. One targeted request per
# series guarantees coverage.
SUPPLEMENT_SERIES = (
    "KXHIGHNY",
    "KXHIGHCHI",
    "KXHIGHMIA",
    "KXHIGHLAX",
    "KXHIGHDEN",
    "KXFEDDECISION",
)

_UNINFORMATIVE_SUBTITLES = {"yes", "no", "-", "--"}


def _normalize_price(p) -> float | None:
    """Kalshi v2 prices arrive either as 0-1 dollar floats or as 1-99 cents.
    Normalize to 0-1 probability. Returns None on any failure."""
    if p is None:
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        v = v / 100.0
    if v < 0 or v > 1:
        return None
    return round(v, 4)


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cents_to_dollars(v) -> float | None:
    f = _to_float(v)
    return round(f / 100.0, 2) if f is not None else None


def _pick(raw: dict, *keys):
    for k in keys:
        v = raw.get(k)
        if v is not None:
            return v
    return None


def _normalize_market(raw: dict) -> dict | None:
    ticker = raw.get("ticker")
    if not ticker or not isinstance(ticker, str):
        return None
    event_ticker = raw.get("event_ticker") or None
    title = (raw.get("title") or "").strip()
    yes_sub = (raw.get("yes_sub_title") or "").strip()
    if title and yes_sub and yes_sub.lower() not in _UNINFORMATIVE_SUBTITLES and yes_sub.lower() not in title.lower():
        question = f"{title} — {yes_sub}"
    else:
        question = title or ticker
    slug = (event_ticker or ticker).lower()
    liq = _to_float(raw.get("liquidity_dollars"))
    if liq is None:
        liq = _cents_to_dollars(raw.get("liquidity"))
    return {
        "venue": "kalshi",
        "venue_id": ticker,
        "event_ticker": event_ticker,
        "question": question,
        "category": raw.get("category") or None,
        "url": f"https://kalshi.com/markets/{slug}",
        "end_date": raw.get("close_time") or raw.get("expiration_time") or None,
        "yes_bid": _normalize_price(_pick(raw, "yes_bid_dollars", "yes_bid")),
        "yes_ask": _normalize_price(_pick(raw, "yes_ask_dollars", "yes_ask")),
        "last_price": _normalize_price(_pick(raw, "last_price_dollars", "last_price")),
        "liquidity": liq,
        "volume_24h": _to_float(_pick(raw, "volume_24h_fp", "volume_24h")),
        "volume": _to_float(_pick(raw, "volume_fp", "volume")),
        "open_interest": _to_float(_pick(raw, "open_interest_fp", "open_interest")),
        "yes_token_id": None,
        "no_token_id": None,
    }


async def _get_page(client: httpx.AsyncClient, params: dict) -> dict | None:
    for attempt in (0, 1):
        try:
            resp = await client.get(API_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict):
                return payload
            return None
        except Exception as exc:
            if attempt:
                log.warning("Kalshi GET %s failed after retry: %s", params, exc)
            else:
                await asyncio.sleep(_RETRY_BACKOFF)
    return None


async def fetch_horizon(horizon_days: int) -> list[dict]:
    """Fetch active Kalshi markets closing within `horizon_days`.

    Returns a list of normalized market dicts (see module docstring).
    Never raises — on any failure, returns whatever was collected so far.
    """
    out: list[dict] = []
    try:
        try:
            days = max(1, int(horizon_days))
        except (TypeError, ValueError):
            days = 7
        now = int(time.time())
        base_params = {
            "status": "open",
            "min_close_ts": now,
            "max_close_ts": now + days * 86400,
            "limit": PAGE_LIMIT,
        }
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        ) as client:
            cursor: str | None = None
            for _ in range(_MAX_PAGES):
                params = dict(base_params)
                if cursor:
                    params["cursor"] = cursor
                payload = await _get_page(client, params)
                if payload is None:
                    break
                markets = payload.get("markets") or []
                for raw in markets:
                    if not isinstance(raw, dict):
                        continue
                    row = _normalize_market(raw)
                    if row is not None:
                        out.append(row)
                cursor = payload.get("cursor") or None
                if not cursor or not markets:
                    break
                await asyncio.sleep(_PAGE_PAUSE)

            seen = {r["venue_id"] for r in out}
            for series in SUPPLEMENT_SERIES:
                payload = await _get_page(client, dict(base_params, series_ticker=series))
                if payload is None:
                    continue
                for raw in payload.get("markets") or []:
                    if not isinstance(raw, dict):
                        continue
                    row = _normalize_market(raw)
                    if row is not None and row["venue_id"] not in seen:
                        seen.add(row["venue_id"])
                        out.append(row)
                await asyncio.sleep(_PAGE_PAUSE)
    except Exception as exc:
        log.error("Kalshi ingest failed: %s", exc)
    return out
