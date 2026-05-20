"""Unusual options activity + dark pool prints.

Paid feeds — without an API key set, both `fetch_flow_alerts()` and
`fetch_dark_pool_prints()` return [] and the dashboard surfaces an
"unconfigured" state in the UI. When `UNUSUAL_WHALES_API_KEY` is
provided, the adapter pulls from the unusual_whales API and normalises
to the dashboard's schema.

We use unusual_whales as the default vendor because:
  - Single API key covers both options flow and dark pool prints
  - Stock-level filtering and lookback windows are well-supported
  - The auth model (Authorization: Bearer <token>) is simple

The adapter is intentionally written as a thin pass-through — if a user
wants to swap in Polygon / CBOE / Tradier, replace the two `fetch_*`
methods. The data model below is stable.

Reference: https://unusualwhales.com/api
Endpoints (subject to vendor change):
  GET /api/option-trades/flow-alerts            — recent unusual options activity
  GET /api/darkpool/recent                      — recent dark pool prints
"""

from __future__ import annotations

import logging
import os
import datetime as dt
from typing import Any

import httpx

log = logging.getLogger("options_flow")

UNUSUAL_WHALES_BASE = os.environ.get(
    "UNUSUAL_WHALES_BASE_URL", "https://api.unusualwhales.com"
)
UNUSUAL_WHALES_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
USER_AGENT = os.environ.get(
    "OPTIONS_FLOW_USER_AGENT",
    "narve.ai whale tracker contact@narve.ai",
)
_TIMEOUT = 30.0


def is_configured() -> bool:
    return bool(UNUSUAL_WHALES_KEY)


def _client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if UNUSUAL_WHALES_KEY:
        headers["Authorization"] = f"Bearer {UNUSUAL_WHALES_KEY}"
    return httpx.AsyncClient(headers=headers, timeout=_TIMEOUT, follow_redirects=True)


async def fetch_flow_alerts(limit: int = 200) -> list[dict]:
    """Pull recent flow alerts, return normalised dicts."""
    if not is_configured():
        return []
    url = f"{UNUSUAL_WHALES_BASE}/api/option-trades/flow-alerts"
    try:
        async with _client() as cx:
            r = await cx.get(url, params={"limit": limit})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.info("options flow fetch failed: %s", e)
        return []
    items = data.get("data") or data if isinstance(data, dict) else data
    return [_normalise_flow(it) for it in (items or []) if isinstance(it, dict)]


async def fetch_dark_pool_prints(limit: int = 200) -> list[dict]:
    if not is_configured():
        return []
    url = f"{UNUSUAL_WHALES_BASE}/api/darkpool/recent"
    try:
        async with _client() as cx:
            r = await cx.get(url, params={"limit": limit})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.info("dark pool fetch failed: %s", e)
        return []
    items = data.get("data") or data if isinstance(data, dict) else data
    return [_normalise_dp(it) for it in (items or []) if isinstance(it, dict)]


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalise_flow(item: dict) -> dict:
    # unusual_whales field names differ slightly between endpoints; we
    # accept the common variants. Anything missing degrades to None.
    ticker = (item.get("ticker") or item.get("underlying") or "").upper()
    side_raw = (item.get("type") or item.get("side") or item.get("option_type") or "").upper()
    side = "CALL" if side_raw.startswith("C") else ("PUT" if side_raw.startswith("P") else None)

    sweep = 1 if (item.get("is_sweep") or item.get("sweep")) else 0
    sentiment = (item.get("sentiment") or "").lower() or None

    volume = _f(item.get("volume"))
    oi = _f(item.get("open_interest") or item.get("oi"))
    voi = _f(item.get("volume_oi_ratio")) or (
        (volume / oi) if (volume and oi and oi > 0) else None
    )
    alert_id = str(
        item.get("id") or item.get("alert_id") or item.get("tradeId") or item.get("trade_id") or ""
    )
    if not alert_id:
        # Fall back to a deterministic id so re-pulls dedupe.
        alert_id = (
            f"flow:{ticker}:{item.get('executed_at') or item.get('created_at') or ''}"
            f":{item.get('strike')}:{item.get('expiry') or item.get('expiration')}:"
            f"{item.get('premium')}"
        )

    return {
        "alert_id":        alert_id[:200],
        "alerted_at":      str(item.get("executed_at") or item.get("created_at") or item.get("alerted_at") or ""),
        "ticker":          ticker or None,
        "side":            side,
        "sentiment":       sentiment,
        "sweep":           sweep,
        "strike":          _f(item.get("strike")),
        "expiry":          str(item.get("expiry") or item.get("expiration") or "") or None,
        "premium":         _f(item.get("premium") or item.get("total_premium")),
        "volume":          volume,
        "open_interest":   oi,
        "volume_oi_ratio": voi,
        "spot_price":      _f(item.get("underlying_price") or item.get("spot")),
        "source":          "unusual_whales",
        "raw_url":         str(item.get("url") or "") or None,
    }


def _normalise_dp(item: dict) -> dict:
    ticker = (item.get("ticker") or item.get("symbol") or "").upper()
    size = _f(item.get("size") or item.get("volume"))
    price = _f(item.get("price"))
    premium = _f(item.get("premium")) or (
        (size * price) if (size and price) else None
    )
    print_id = str(
        item.get("id") or item.get("print_id") or item.get("tradeId") or ""
    )
    executed = str(item.get("executed_at") or item.get("created_at") or "")
    if not print_id:
        print_id = f"dp:{ticker}:{executed}:{price}:{size}"

    return {
        "print_id":      print_id[:200],
        "executed_at":   executed,
        "ticker":        ticker or None,
        "size":          size,
        "price":         price,
        "premium":       premium,
        "market_center": (item.get("market_center") or item.get("exchange") or "") or None,
        "source":        "unusual_whales",
    }
