"""CoinGecko universe + market data.

CoinGecko's free /coins/markets endpoint returns every coin sorted by
market cap with price / volume / 24h-change / market-cap / circulating
supply / ATH / ATL. No API key required for the public endpoint at
modest rate limits.

We grab the top 500 coins (5 pages of 100). Cache 60s on the universe
list - it doesn't move fast enough to need sub-minute refresh.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def _fetch_page(page: int, per_page: int = 100, vs: str = "usd") -> Optional[list[dict]]:
    params = {
        "vs_currency": vs,
        "order": "market_cap_desc",
        "per_page": str(per_page),
        "page": str(page),
        "sparkline": "false",
        "price_change_percentage": "1h,24h,7d,30d",
    }
    r = http_get(f"{COINGECKO_BASE}/coins/markets", params=params, timeout=20)
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _summarise(coin: dict) -> dict:
    return {
        "id": coin.get("id"),
        "symbol": (coin.get("symbol") or "").upper(),
        "name": coin.get("name"),
        "image": coin.get("image"),
        "market_cap_rank": coin.get("market_cap_rank"),
        "current_price": coin.get("current_price"),
        "market_cap": coin.get("market_cap"),
        "fully_diluted_valuation": coin.get("fully_diluted_valuation"),
        "total_volume": coin.get("total_volume"),
        "circulating_supply": coin.get("circulating_supply"),
        "total_supply": coin.get("total_supply"),
        "max_supply": coin.get("max_supply"),
        "ath": coin.get("ath"),
        "ath_change_pct": coin.get("ath_change_percentage"),
        "ath_date": coin.get("ath_date"),
        "atl": coin.get("atl"),
        "atl_change_pct": coin.get("atl_change_percentage"),
        "atl_date": coin.get("atl_date"),
        "change_1h": coin.get("price_change_percentage_1h_in_currency"),
        "change_24h": coin.get("price_change_percentage_24h_in_currency"),
        "change_7d": coin.get("price_change_percentage_7d_in_currency"),
        "change_30d": coin.get("price_change_percentage_30d_in_currency"),
        "last_updated": coin.get("last_updated"),
    }


def universe(top_n: int = 500) -> dict:
    """Top-N coins by market cap with price + change + supply metadata."""
    top_n = max(50, min(top_n, 1000))
    cache_key = f"universe_{top_n}"
    hit = _cache.get(cache_key, ttl_s=60)
    if hit is not None:
        return hit
    pages_needed = (top_n + 99) // 100
    rows: list[dict] = []
    for page in range(1, pages_needed + 1):
        chunk = _fetch_page(page)
        if not chunk:
            break
        rows.extend(_summarise(c) for c in chunk)
        if len(rows) >= top_n:
            break
    rows = rows[:top_n]
    out = {
        "source": "CoinGecko /coins/markets",
        "count": len(rows),
        "coins": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def coin_detail(coin_id: str) -> dict:
    """Full per-coin detail incl. description, homepage, repos, social,
    market-data dict with 24h/7d/14d/30d/200d/1y change."""
    cache_key = f"coin_{coin_id}"
    hit = _cache.get(cache_key, ttl_s=120)
    if hit is not None:
        return hit
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "true",
        "developer_data": "true",
        "sparkline": "true",
    }
    r = http_get(f"{COINGECKO_BASE}/coins/{coin_id}", params=params, timeout=20)
    if not r:
        return {"error": "CoinGecko detail fetch failed", "id": coin_id}
    try:
        d = r.json()
    except ValueError:
        return {"error": "CoinGecko detail parse failed", "id": coin_id}
    md = d.get("market_data") or {}
    out = {
        "id": d.get("id"),
        "symbol": (d.get("symbol") or "").upper(),
        "name": d.get("name"),
        "categories": d.get("categories"),
        "description": (d.get("description") or {}).get("en", "")[:1200],
        "homepage": (d.get("links") or {}).get("homepage", [None])[0],
        "twitter": (d.get("links") or {}).get("twitter_screen_name"),
        "github": ((d.get("links") or {}).get("repos_url") or {}).get("github", []),
        "current_price_usd": (md.get("current_price") or {}).get("usd"),
        "market_cap_usd": (md.get("market_cap") or {}).get("usd"),
        "total_volume_usd": (md.get("total_volume") or {}).get("usd"),
        "change_24h": md.get("price_change_percentage_24h"),
        "change_7d": md.get("price_change_percentage_7d"),
        "change_30d": md.get("price_change_percentage_30d"),
        "change_1y": md.get("price_change_percentage_1y"),
        "ath_usd": (md.get("ath") or {}).get("usd"),
        "ath_date": (md.get("ath_date") or {}).get("usd"),
        "atl_usd": (md.get("atl") or {}).get("usd"),
        "atl_date": (md.get("atl_date") or {}).get("usd"),
        "sparkline_7d": (md.get("sparkline_7d") or {}).get("price"),
        "circulating_supply": md.get("circulating_supply"),
        "total_supply": md.get("total_supply"),
        "max_supply": md.get("max_supply"),
        "community": d.get("community_data"),
        "developer": d.get("developer_data"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def global_metrics() -> dict:
    """Total market cap, BTC dominance, ETH dominance, active coins."""
    hit = _cache.get("global", ttl_s=120)
    if hit is not None:
        return hit
    r = http_get(f"{COINGECKO_BASE}/global", timeout=15)
    if not r:
        return {"error": "CoinGecko /global failed"}
    try:
        d = (r.json() or {}).get("data") or {}
    except ValueError:
        return {"error": "CoinGecko /global parse failed"}
    out = {
        "source": "CoinGecko /global",
        "active_cryptocurrencies": d.get("active_cryptocurrencies"),
        "markets": d.get("markets"),
        "total_market_cap_usd": (d.get("total_market_cap") or {}).get("usd"),
        "total_volume_24h_usd": (d.get("total_volume") or {}).get("usd"),
        "btc_dominance_pct": (d.get("market_cap_percentage") or {}).get("btc"),
        "eth_dominance_pct": (d.get("market_cap_percentage") or {}).get("eth"),
        "market_cap_change_24h_pct": d.get("market_cap_change_percentage_24h_usd"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("global", out)
    return out


def trending() -> dict:
    """Top 7 trending coins on CoinGecko in the last 24h (by search volume)."""
    hit = _cache.get("trending", ttl_s=300)
    if hit is not None:
        return hit
    r = http_get(f"{COINGECKO_BASE}/search/trending", timeout=15)
    if not r:
        return {"error": "CoinGecko trending fetch failed", "coins": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "CoinGecko trending parse failed", "coins": []}
    rows = []
    for item in (d.get("coins") or []):
        coin = item.get("item") or {}
        rows.append({
            "id": coin.get("id"),
            "symbol": (coin.get("symbol") or "").upper(),
            "name": coin.get("name"),
            "market_cap_rank": coin.get("market_cap_rank"),
            "thumb": coin.get("thumb"),
            "score": coin.get("score"),
            "price_btc": coin.get("price_btc"),
        })
    out = {
        "source": "CoinGecko /search/trending",
        "count": len(rows),
        "coins": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("trending", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(global_metrics(), indent=2))
    print(json.dumps(trending(), indent=2)[:1200])
