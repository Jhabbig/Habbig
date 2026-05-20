"""Kalshi events ingestor — scoped to curated AI series tickers.

Stdlib-only mirror of the proven pattern from
``centralbank-dashboard/ingestion/kalshi_client.py``. The public ``/events``
endpoint accepts ``?series_ticker=...&with_nested_markets=true`` and returns
all sub-markets in one call — perfect for the multi-outcome event view.

The dashboard treats Kalshi and Polymarket entries uniformly (same
``{venue, title, slug, url, markets[]}`` shape) so the UI can render them
identically. Prices are normalized to 0–1 (Kalshi quotes 0–100 cents).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

HOST = "https://api.elections.kalshi.com"
API = "/trade-api/v2"
_UA = "Mozilla/5.0 (AIRaceDashboard/1.0; +markets)"
VENUE = "kalshi"

_TTL = 5 * 60
_lock = Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}


def _http_get_json(path: str, params: dict | None = None, timeout: float = 12.0):
    url = f"{HOST}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _normalize_market(m: dict, event_ticker: str) -> dict | None:
    # Kalshi exposes yes side prices in cents; prefer last_price (last trade),
    # fall back to mid of bid/ask, then yes_bid alone.
    raw = m.get("last_price")
    if raw is None and m.get("yes_ask") is not None and m.get("yes_bid") is not None:
        try:
            raw = (float(m["yes_ask"]) + float(m["yes_bid"])) / 2.0
        except (TypeError, ValueError):
            raw = None
    if raw is None:
        raw = m.get("yes_bid")
    if raw is None:
        return None
    try:
        yes_price = float(raw) / 100.0
    except (TypeError, ValueError):
        return None
    ticker = m.get("ticker") or ""
    return {
        "id": ticker,
        "question": m.get("title") or m.get("subtitle") or "",
        "yes_outcome": m.get("yes_sub_title") or "Yes",
        "yes_price": yes_price,
        "outcomes": [m.get("yes_sub_title") or "Yes", m.get("no_sub_title") or "No"],
        "prices": [yes_price, max(0.0, 1.0 - yes_price)],
        "one_day_change": 0.0,  # Kalshi /events doesn't surface a 1d delta here.
        "volume_24h": float(m.get("volume_24h") or 0.0),
        "volume_total": float(m.get("volume") or 0.0),
        "end_date": m.get("close_time") or "",
        "url": f"https://kalshi.com/markets/{event_ticker.lower()}/{ticker.lower()}",
    }


def fetch_series(series_ticker: str, status: str = "open") -> list[dict]:
    """Return normalized events for one series ticker; [] on failure."""
    now = time.time()
    cache_key = f"{series_ticker}:{status}"
    with _lock:
        cached = _cache.get(cache_key)
        if cached and (now - cached[0]) < _TTL:
            return cached[1]

    try:
        payload = _http_get_json(
            f"{API}/events",
            params={
                "series_ticker": series_ticker,
                "status": status,
                "with_nested_markets": "true",
                "limit": 50,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Kalshi series %s fetch failed: %s", series_ticker, e)
        with _lock:
            _cache[cache_key] = (now, [])
        return []

    events = (payload or {}).get("events") or []
    out: list[dict] = []
    for ev in events:
        event_ticker = ev.get("event_ticker") or ev.get("ticker") or ""
        markets = []
        for raw in (ev.get("markets") or []):
            nm = _normalize_market(raw, event_ticker)
            if nm:
                markets.append(nm)
        if not markets:
            continue
        markets.sort(key=lambda m: m["yes_price"], reverse=True)
        out.append({
            "venue": VENUE,
            "slug": event_ticker.lower(),
            "title": ev.get("title") or "",
            "description": (ev.get("sub_title") or "")[:280],
            "url": f"https://kalshi.com/events/{event_ticker.lower()}",
            "series_ticker": series_ticker,
            "markets": markets,
            "volume_24h_total": sum(m["volume_24h"] for m in markets),
            "end_date": markets[0].get("end_date", ""),
        })

    with _lock:
        _cache[cache_key] = (now, out)
    return out


def fetch_featured(series_tickers: list[str]) -> list[dict]:
    """Fetch every series; concatenate events; sort by 24h volume."""
    out: list[dict] = []
    for s in series_tickers:
        out.extend(fetch_series(s))
    out.sort(key=lambda e: e.get("volume_24h_total", 0), reverse=True)
    return out
