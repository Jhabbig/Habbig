"""Binance spot + futures public endpoints.

Spot:
  - /api/v3/ticker/24hr        - 24h ticker for every symbol
  - /api/v3/depth?symbol=...   - L2 orderbook snapshot

Futures (USD-M perps):
  - /fapi/v1/premiumIndex      - funding rate + mark/index price per symbol
  - /fapi/v1/openInterest      - open interest per symbol
  - /fapi/v1/ticker/24hr       - 24h ticker for futures

All public, no auth, IP-rate-limited (no key). We cache aggressively
(15-30s) so a busy dashboard doesn't burn through rate limits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"


# ─── Spot ─────────────────────────────────────────────────────────────────────

def spot_ticker_24h() -> dict:
    """All-symbols 24h spot ticker. Each row has volume, price-change %,
    bid/ask, count. Used for cross-exchange spread + universe joins."""
    hit = _cache.get("binance_spot_24h", ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(f"{SPOT_BASE}/api/v3/ticker/24hr", timeout=20)
    if not r:
        return {"error": "Binance spot 24h fetch failed", "tickers": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Binance spot 24h parse failed", "tickers": []}
    tickers = [{
        "symbol": x.get("symbol"),
        "price": _f(x.get("lastPrice")),
        "bid": _f(x.get("bidPrice")),
        "ask": _f(x.get("askPrice")),
        "high_24h": _f(x.get("highPrice")),
        "low_24h": _f(x.get("lowPrice")),
        "volume": _f(x.get("volume")),
        "quote_volume": _f(x.get("quoteVolume")),
        "change_pct_24h": _f(x.get("priceChangePercent")),
        "count": x.get("count"),
    } for x in rows if isinstance(x, dict)]
    out = {
        "source": "Binance /api/v3/ticker/24hr",
        "count": len(tickers),
        "tickers": tickers,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("binance_spot_24h", out)
    return out


def spot_depth(symbol: str = "BTCUSDT", limit: int = 50) -> dict:
    """L2 orderbook snapshot for a symbol. ``limit`` ∈ {5,10,20,50,100,500,1000,5000}."""
    sym = symbol.upper()
    hit = _cache.get(f"binance_depth_{sym}_{limit}", ttl_s=5)
    if hit is not None:
        return hit
    r = http_get(f"{SPOT_BASE}/api/v3/depth",
                 params={"symbol": sym, "limit": str(limit)}, timeout=10)
    if not r:
        return {"error": "Binance depth fetch failed", "symbol": sym}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Binance depth parse failed", "symbol": sym}
    out = {
        "source": "Binance /api/v3/depth",
        "symbol": sym,
        "bids": [[_f(p), _f(q)] for p, q in (d.get("bids") or [])],
        "asks": [[_f(p), _f(q)] for p, q in (d.get("asks") or [])],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"binance_depth_{sym}_{limit}", out)
    return out


# ─── Futures (USD-M) ──────────────────────────────────────────────────────────

def futures_premium_index() -> dict:
    """Funding rate + mark/index prices for every USDM-perp symbol."""
    hit = _cache.get("binance_premium", ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(f"{FUTURES_BASE}/fapi/v1/premiumIndex", timeout=20)
    if not r:
        return {"error": "Binance premiumIndex fetch failed", "rows": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Binance premiumIndex parse failed", "rows": []}
    norm = [{
        "symbol": x.get("symbol"),
        "mark_price": _f(x.get("markPrice")),
        "index_price": _f(x.get("indexPrice")),
        "funding_rate": _f(x.get("lastFundingRate")),
        "next_funding_time_ms": x.get("nextFundingTime"),
        "interest_rate": _f(x.get("interestRate")),
    } for x in rows if isinstance(x, dict)]
    out = {
        "source": "Binance /fapi/v1/premiumIndex",
        "count": len(norm),
        "rows": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("binance_premium", out)
    return out


def futures_ticker_24h() -> dict:
    """24h ticker for every futures symbol."""
    hit = _cache.get("binance_fut_24h", ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=20)
    if not r:
        return {"error": "Binance futures 24h fetch failed", "tickers": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Binance futures 24h parse failed", "tickers": []}
    tickers = [{
        "symbol": x.get("symbol"),
        "price": _f(x.get("lastPrice")),
        "change_pct_24h": _f(x.get("priceChangePercent")),
        "volume": _f(x.get("volume")),
        "quote_volume": _f(x.get("quoteVolume")),
        "high_24h": _f(x.get("highPrice")),
        "low_24h": _f(x.get("lowPrice")),
        "count": x.get("count"),
    } for x in rows if isinstance(x, dict)]
    out = {
        "source": "Binance /fapi/v1/ticker/24hr",
        "count": len(tickers),
        "tickers": tickers,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("binance_fut_24h", out)
    return out


def futures_open_interest(symbol: str = "BTCUSDT") -> dict:
    sym = symbol.upper()
    hit = _cache.get(f"binance_oi_{sym}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{FUTURES_BASE}/fapi/v1/openInterest",
                 params={"symbol": sym}, timeout=10)
    if not r:
        return {"error": "Binance OI fetch failed", "symbol": sym}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Binance OI parse failed", "symbol": sym}
    out = {
        "source": "Binance /fapi/v1/openInterest",
        "symbol": sym,
        "open_interest": _f(d.get("openInterest")),
        "time_ms": d.get("time"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"binance_oi_{sym}", out)
    return out


def klines(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 168) -> dict:
    """OHLCV klines for charting. ``interval`` ∈ {1m,5m,15m,1h,4h,1d}.
    Default 168 1h candles = 7 days."""
    sym = symbol.upper()
    hit = _cache.get(f"binance_klines_{sym}_{interval}_{limit}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{SPOT_BASE}/api/v3/klines",
                 params={"symbol": sym, "interval": interval, "limit": str(limit)},
                 timeout=15)
    if not r:
        return {"error": "Binance klines fetch failed", "symbol": sym}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Binance klines parse failed", "symbol": sym}
    bars = []
    for row in rows:
        if len(row) < 6:
            continue
        bars.append({
            "open_ms": row[0],
            "open": _f(row[1]),
            "high": _f(row[2]),
            "low": _f(row[3]),
            "close": _f(row[4]),
            "volume": _f(row[5]),
        })
    out = {
        "source": "Binance /api/v3/klines",
        "symbol": sym,
        "interval": interval,
        "count": len(bars),
        "bars": bars,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"binance_klines_{sym}_{interval}_{limit}", out)
    return out


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    import json
    print(json.dumps(spot_ticker_24h(), indent=2)[:800])
    print(json.dumps(futures_premium_index(), indent=2)[:800])
