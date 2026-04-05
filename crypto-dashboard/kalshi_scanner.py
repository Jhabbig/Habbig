#!/usr/bin/env python3
"""
Kalshi Markets Scanner
Fetches event markets from Kalshi's public API and returns data for the dashboard.
"""

import requests
import time
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_TTL = 600  # 10 minutes


def _get(endpoint, params=None):
    """Make a GET request to Kalshi API."""
    url = f"{KALSHI_API}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15, headers={
                "Accept": "application/json",
                "User-Agent": "CryptoEdge/1.0",
            })
            if resp.status_code == 429:
                time.sleep(2)
                continue
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            if attempt == 2:
                print(f"  [Kalshi] API error: {e}")
            time.sleep(1)
    return None


def fetch_markets(limit=200, status="open"):
    """Fetch active Kalshi markets."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"kalshi_markets_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}.json"

    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_file) as f:
                return json.load(f)

    print("  [Kalshi] Fetching markets...")
    all_markets = []
    cursor = None

    for _ in range(5):  # max 5 pages
        params = {"limit": min(limit, 200), "status": status}
        if cursor:
            params["cursor"] = cursor

        data = _get("/markets", params)
        if not data or "markets" not in data:
            break

        markets = data["markets"]
        all_markets.extend(markets)

        cursor = data.get("cursor")
        if not cursor or len(markets) < 200:
            break
        time.sleep(0.2)

    print(f"  [Kalshi] Fetched {len(all_markets)} markets")

    # Process into clean format
    processed = []
    for m in all_markets:
        try:
            yes_price = m.get("yes_ask", 0) or m.get("last_price", 0) or 0
            no_price = m.get("no_ask", 0) or (100 - yes_price) if yes_price else 0

            # Convert cents to probability
            yes_prob = yes_price / 100 if yes_price > 1 else yes_price
            no_prob = no_price / 100 if no_price > 1 else no_price

            processed.append({
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "subtitle": m.get("subtitle", ""),
                "category": m.get("category", ""),
                "status": m.get("status", ""),
                "yes_price": round(yes_prob, 3),
                "no_price": round(no_prob, 3),
                "yes_bid": (m.get("yes_bid", 0) or 0) / 100,
                "yes_ask": (m.get("yes_ask", 0) or 0) / 100,
                "volume": m.get("volume", 0) or 0,
                "volume_24h": m.get("volume_24h", 0) or 0,
                "open_interest": m.get("open_interest", 0) or 0,
                "close_time": m.get("close_time", ""),
                "event_ticker": m.get("event_ticker", ""),
            })
        except Exception:
            continue

    # Sort by volume
    processed.sort(key=lambda x: x.get("volume", 0), reverse=True)

    # Cache
    with open(cache_file, "w") as f:
        json.dump(processed, f)

    return processed


def fetch_events(limit=100, status="open"):
    """Fetch Kalshi events (groups of markets)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"kalshi_events_{datetime.now(timezone.utc).strftime('%Y%m%d_%H')}.json"

    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_file) as f:
                return json.load(f)

    print("  [Kalshi] Fetching events...")
    data = _get("/events", {"limit": limit, "status": status})
    if not data or "events" not in data:
        return []

    events = []
    for e in data["events"]:
        markets = e.get("markets", [])
        events.append({
            "ticker": e.get("event_ticker", ""),
            "title": e.get("title", ""),
            "category": e.get("category", ""),
            "num_markets": len(markets),
            "volume": sum(m.get("volume", 0) or 0 for m in markets),
            "markets": [{
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "yes_price": (m.get("yes_ask", 0) or m.get("last_price", 0) or 0) / 100,
                "volume": m.get("volume", 0) or 0,
            } for m in markets[:10]],
        })

    events.sort(key=lambda x: x["volume"], reverse=True)

    with open(cache_file, "w") as f:
        json.dump(events, f)

    print(f"  [Kalshi] Fetched {len(events)} events")
    return events


def get_market_categories(markets: list) -> dict:
    """Group markets by category with stats."""
    categories = {}
    for m in markets:
        cat = m.get("category", "Other") or "Other"
        if cat not in categories:
            categories[cat] = {"count": 0, "total_volume": 0, "markets": []}
        categories[cat]["count"] += 1
        categories[cat]["total_volume"] += m.get("volume", 0)
        if len(categories[cat]["markets"]) < 10:
            categories[cat]["markets"].append(m)
    return dict(sorted(categories.items(), key=lambda x: x[1]["total_volume"], reverse=True))


def run_scanner():
    """Full Kalshi scan — returns data for dashboard."""
    markets = fetch_markets(limit=500)
    events = fetch_events(limit=50)
    categories = get_market_categories(markets)

    # Find interesting markets (high volume, close to 50/50)
    trending = [m for m in markets if m["volume_24h"] > 100]
    trending.sort(key=lambda x: x["volume_24h"], reverse=True)

    close_calls = [m for m in markets if 0.35 <= m["yes_price"] <= 0.65 and m["volume"] > 50]
    close_calls.sort(key=lambda x: x["volume"], reverse=True)

    return {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(markets),
        "total_events": len(events),
        "categories": categories,
        "trending": trending[:30],
        "close_calls": close_calls[:30],
        "top_events": events[:20],
        "all_markets": markets[:200],
    }


if __name__ == "__main__":
    result = run_scanner()
    print(f"\nKalshi Markets: {result['total_markets']}")
    print(f"Events: {result['total_events']}")
    print(f"Categories: {list(result['categories'].keys())}")
    print(f"\nTop 10 by volume:")
    for m in result["all_markets"][:10]:
        print(f"  {m['title'][:60]:60s} | YES: {m['yes_price']:.0%} | Vol: {m['volume']:,}")
