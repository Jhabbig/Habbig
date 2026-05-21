"""Macro cross-asset prices for crypto-vs-tradfi correlation.

Yahoo Finance has a public, key-free chart endpoint at
  https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo

Returns OHLC + timestamps. Rate-limited (no key) but works for daily polls.

Symbols we track:
  ^GSPC   - S&P 500
  ^IXIC   - Nasdaq Composite
  ^DJI    - Dow Jones
  DX-Y.NYB - US Dollar Index (DXY)
  ^TNX    - US 10-year Treasury yield (×10, so 4.5% prints as 45)
  ^VIX    - CBOE volatility index
  GC=F    - Gold futures
  CL=F    - WTI crude futures
  BTC-USD - Bitcoin (for sanity check / correlation anchor)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

TRACKED = [
    ("S&P 500",       "^GSPC",    "equity"),
    ("Nasdaq",        "^IXIC",    "equity"),
    ("Dow Jones",     "^DJI",     "equity"),
    ("DXY",           "DX-Y.NYB", "fx"),
    ("US 10y yield",  "^TNX",     "rate"),
    ("VIX",           "^VIX",     "vol"),
    ("Gold",          "GC=F",     "commodity"),
    ("WTI crude",     "CL=F",     "commodity"),
    ("Bitcoin",       "BTC-USD",  "crypto"),
]


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_symbol(symbol: str) -> Optional[dict]:
    r = http_get(f"{BASE}/{symbol}",
                 params={"interval": "1d", "range": "1mo"}, timeout=12,
                 headers={"User-Agent": "Mozilla/5.0 narve-crypto-trackers"})
    if not r:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    result = ((d.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return None
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    indicators = (result.get("indicators") or {}).get("quote") or [{}]
    quote = indicators[0] if indicators else {}
    closes = quote.get("close") or []
    if not closes:
        return None
    closes_clean = [c for c in closes if c is not None]
    if not closes_clean:
        return None
    current = _f(meta.get("regularMarketPrice")) or _f(closes_clean[-1])
    prev_close = _f(meta.get("chartPreviousClose")) or _f(meta.get("previousClose"))
    change_pct = None
    if current is not None and prev_close not in (None, 0):
        change_pct = (current - prev_close) / prev_close * 100
    return {
        "symbol": symbol,
        "current": current,
        "previous_close": prev_close,
        "change_pct": change_pct,
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName"),
        "series_close": closes_clean[-30:],
        "series_ts": timestamps[-30:],
    }


def snapshot() -> dict:
    hit = _cache.get("macro_snapshot", ttl_s=300)  # 5 min
    if hit is not None:
        return hit
    rows: list[dict] = []
    for label, sym, asset_class in TRACKED:
        s = _fetch_symbol(sym)
        if not s:
            rows.append({"label": label, "symbol": sym, "asset_class": asset_class,
                         "error": "fetch failed"})
            continue
        rows.append({"label": label, "asset_class": asset_class, **s})
    out = {
        "source": "Yahoo Finance v8/chart",
        "count": len(rows),
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("macro_snapshot", out)
    return out


def btc_correlation_30d() -> dict:
    """30-day daily-return correlation of BTC vs each tracked symbol."""
    snap = snapshot()
    rows = snap.get("rows") or []
    btc_row = next((r for r in rows if r.get("symbol") == "BTC-USD"), None)
    if not btc_row or not btc_row.get("series_close"):
        return {"error": "no BTC series", "correlations": []}
    btc_closes = btc_row["series_close"]
    btc_rets = [(btc_closes[i] - btc_closes[i-1]) / btc_closes[i-1]
                for i in range(1, len(btc_closes)) if btc_closes[i-1]]
    if len(btc_rets) < 5:
        return {"error": "BTC series too short", "correlations": []}

    def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
        n = min(len(xs), len(ys))
        if n < 5:
            return None
        xs, ys = xs[-n:], ys[-n:]
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        dx = sum((xs[i] - mx) ** 2 for i in range(n))
        dy = sum((ys[i] - my) ** 2 for i in range(n))
        if dx <= 0 or dy <= 0:
            return None
        return num / (dx * dy) ** 0.5

    out_rows = []
    for r in rows:
        if r.get("symbol") == "BTC-USD" or r.get("error"):
            continue
        s = r.get("series_close") or []
        rets = [(s[i] - s[i-1]) / s[i-1] for i in range(1, len(s)) if s[i-1]]
        corr = pearson(btc_rets, rets)
        out_rows.append({
            "label": r["label"],
            "symbol": r.get("symbol"),
            "asset_class": r.get("asset_class"),
            "btc_corr_30d": round(corr, 3) if corr is not None else None,
        })
    out_rows.sort(key=lambda r: abs(r.get("btc_corr_30d") or 0), reverse=True)
    return {
        "source": "30-day daily-return Pearson correlation vs BTC-USD",
        "correlations": out_rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), indent=2)[:2000])
    print(json.dumps(btc_correlation_30d(), indent=2))
