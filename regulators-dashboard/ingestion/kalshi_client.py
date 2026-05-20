"""Kalshi public-API client — regulator-action markets.

Reads from `https://api.elections.kalshi.com/trade-api/v2/markets`
(Kalshi's public, no-auth listing endpoint). Returns normalized markets
in the same shape as `polymarket_client.py` so the matcher can treat
them uniformly.

Cache: 5 min, same rationale as Polymarket.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
UA = "regulators-dashboard/0.5"

_CACHE_TTL = 5 * 60
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_lock = Lock()


def _fetch_page(params: dict, timeout: float = 20.0) -> dict:
    url = f"{KALSHI_MARKETS_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read(10_000_000)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _normalize(market: dict) -> dict | None:
    title = (market.get("title") or "").strip()
    if not title:
        return None
    subtitle = (market.get("subtitle") or "").strip()
    question = f"{title} — {subtitle}" if subtitle else title

    # Kalshi prices are integer cents (0..100). yes_bid + yes_ask spread;
    # we use the midpoint when both are present, else fall back to last_price.
    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    if isinstance(yes_bid, (int, float)) and isinstance(yes_ask, (int, float)) and yes_ask > 0:
        yes_price = (yes_bid + yes_ask) / 200.0
    else:
        last = market.get("last_price")
        yes_price = (last / 100.0) if isinstance(last, (int, float)) else None
    no_price = (1.0 - yes_price) if yes_price is not None else None

    event_ticker = market.get("event_ticker") or ""
    market_ticker = market.get("ticker") or ""
    if event_ticker and market_ticker:
        url = f"https://kalshi.com/markets/{event_ticker.lower()}/{market_ticker.lower()}"
    elif event_ticker:
        url = f"https://kalshi.com/markets/{event_ticker.lower()}"
    else:
        url = "https://kalshi.com"

    return {
        "source": "kalshi",
        "id": market_ticker or market.get("id") or "",
        "question": question,
        "yes_price": yes_price,
        "no_price": no_price,
        "end_date": market.get("close_time") or market.get("expiration_time"),
        "url": url,
        "volume": market.get("volume_24h") or market.get("volume"),
    }


def fetch_all(limit: int = 1000) -> list[dict]:
    """Page through open markets and normalize. Kalshi paginates with
    `cursor`; we keep going until the cursor stops advancing or we hit
    `limit`. The public endpoint has its own per-page cap (~200)."""
    out: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        params = {"status": "open", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        body = _fetch_page(params)
        markets = body.get("markets") or []
        for m in markets:
            norm = _normalize(m)
            if norm:
                out.append(norm)
            if len(out) >= limit:
                return out
        next_cursor = body.get("cursor") or ""
        pages += 1
        if not next_cursor or next_cursor == cursor or pages >= 10:
            break
        cursor = next_cursor
    return out


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    try:
        markets = fetch_all()
        payload = {"ok": True, "markets": markets, "count": len(markets), "error": None}
    except Exception as exc:
        log.warning("Kalshi fetch failed: %s", exc)
        payload = {"ok": False, "markets": [], "count": 0, "error": str(exc)}
    payload["fetched_at"] = now
    with _lock:
        _CACHE["data"] = payload
        _CACHE["fetched_at"] = now
    return payload


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    data = get_cached(force=True)
    print(f"ok={data['ok']}  count={data['count']}  err={data['error']}")
    for m in data["markets"][:3]:
        print(f"  yes={m['yes_price']!s:<6}  {m['question'][:90]}")
    if not data["ok"]:
        sys.exit(0)
