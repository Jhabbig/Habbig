"""Deribit options public market data (no auth).

Deribit's `/public/get_book_summary_by_currency` returns every option
contract for a currency with mark price, IV, OI, bid/ask. Free, no key.

We pull all BTC + ETH options, derive aggregate metrics:
  - Total open interest in USD by currency
  - Put/call ratio (by OI and by 24h volume)
  - 25-delta skew proxy: nearest OTM put IV - nearest OTM call IV
  - ATM IV for the front-month expiry
  - Top 10 OI contracts per currency
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://www.deribit.com/api/v2"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch(currency: str) -> list[dict]:
    r = http_get(f"{BASE}/public/get_book_summary_by_currency",
                 params={"currency": currency.upper(), "kind": "option"},
                 timeout=20)
    if not r:
        return []
    try:
        d = r.json()
    except ValueError:
        return []
    return d.get("result") or []


def _parse_instrument(name: str) -> tuple[Optional[str], Optional[str], Optional[float], Optional[str]]:
    """Deribit instrument name format: BTC-31JAN26-100000-C
    Returns (currency, expiry_str, strike, put_or_call)."""
    parts = (name or "").split("-")
    if len(parts) != 4:
        return None, None, None, None
    cur, exp, strike, pc = parts
    try:
        return cur, exp, float(strike), pc.upper()
    except ValueError:
        return cur, exp, None, pc.upper()


def market_overview(currency: str = "BTC") -> dict:
    """Aggregate Deribit options metrics for one currency."""
    cur = currency.upper()
    cache_key = f"deribit_{cur}"
    hit = _cache.get(cache_key, ttl_s=60)
    if hit is not None:
        return hit
    rows = _fetch(cur)
    if not rows:
        return {"error": "Deribit fetch failed", "currency": cur}

    total_oi_usd = 0.0
    call_oi_contracts = 0.0
    put_oi_contracts = 0.0
    call_vol_usd = 0.0
    put_vol_usd = 0.0
    by_expiry: dict[str, dict] = defaultdict(lambda: {
        "calls_oi": 0.0, "puts_oi": 0.0, "calls_vol": 0.0, "puts_vol": 0.0,
        "atm_call_iv": None, "atm_put_iv": None, "atm_strike_diff": None,
    })
    underlying_price: Optional[float] = None
    parsed: list[dict] = []
    for row in rows:
        name = row.get("instrument_name") or ""
        _, exp, strike, pc = _parse_instrument(name)
        if not exp or strike is None or pc not in ("C", "P"):
            continue
        oi = _f(row.get("open_interest")) or 0
        vol = _f(row.get("volume_usd")) or 0
        mark = _f(row.get("mark_price")) or 0
        iv = _f(row.get("mark_iv"))
        und = _f(row.get("underlying_price")) or _f(row.get("estimated_delivery_price"))
        if und:
            underlying_price = und
        notional_usd = oi * (mark if mark else 0) * (und or 0)
        total_oi_usd += notional_usd
        if pc == "C":
            call_oi_contracts += oi
            call_vol_usd += vol
            by_expiry[exp]["calls_oi"] += oi
            by_expiry[exp]["calls_vol"] += vol
        else:
            put_oi_contracts += oi
            put_vol_usd += vol
            by_expiry[exp]["puts_oi"] += oi
            by_expiry[exp]["puts_vol"] += vol
        parsed.append({
            "name": name,
            "expiry": exp,
            "strike": strike,
            "type": pc,
            "mark_iv_pct": iv,
            "open_interest": oi,
            "volume_usd": vol,
            "mark_price": mark,
            "underlying_price": und,
        })

    # ATM front-month IV: pick the nearest-dated expiry with both C+P at the
    # closest strike to underlying.
    front_month = None
    if by_expiry and underlying_price:
        # Sort expiries by date (DDMONYY)
        def _exp_key(s: str) -> int:
            months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                      "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
            try:
                day = int(s[:-5])
                mon = months.get(s[-5:-2], 1)
                yr = int(s[-2:]) + 2000
                return yr * 10000 + mon * 100 + day
            except (ValueError, IndexError, KeyError):
                return 99999999
        sorted_exps = sorted(by_expiry.keys(), key=_exp_key)
        front_month = sorted_exps[0] if sorted_exps else None

    atm_iv_call = atm_iv_put = None
    skew_25d = None
    if front_month and underlying_price:
        front_options = [p for p in parsed if p["expiry"] == front_month]
        calls = sorted([p for p in front_options if p["type"] == "C" and p.get("mark_iv_pct") is not None],
                        key=lambda p: abs(p["strike"] - underlying_price))
        puts = sorted([p for p in front_options if p["type"] == "P" and p.get("mark_iv_pct") is not None],
                       key=lambda p: abs(p["strike"] - underlying_price))
        if calls:
            atm_iv_call = calls[0]["mark_iv_pct"]
        if puts:
            atm_iv_put = puts[0]["mark_iv_pct"]
        # 25-delta skew proxy: nearest OTM put IV - nearest OTM call IV
        # (OTM put = strike below spot, OTM call = strike above spot,
        # ~10-15% OTM is a reasonable proxy when we lack delta data)
        otm_puts = [p for p in puts if p["strike"] < underlying_price * 0.90]
        otm_calls = [p for p in calls if p["strike"] > underlying_price * 1.10]
        if otm_puts and otm_calls:
            put_iv = otm_puts[0]["mark_iv_pct"]
            call_iv = otm_calls[0]["mark_iv_pct"]
            if put_iv is not None and call_iv is not None:
                skew_25d = put_iv - call_iv

    pc_ratio_oi = (put_oi_contracts / call_oi_contracts) if call_oi_contracts else None
    pc_ratio_vol = (put_vol_usd / call_vol_usd) if call_vol_usd else None

    top_oi = sorted(parsed, key=lambda p: p["open_interest"], reverse=True)[:10]

    out = {
        "source": "Deribit /public/get_book_summary_by_currency",
        "currency": cur,
        "underlying_price": underlying_price,
        "total_oi_notional_usd": round(total_oi_usd, 0),
        "call_oi_contracts": round(call_oi_contracts, 1),
        "put_oi_contracts": round(put_oi_contracts, 1),
        "put_call_ratio_oi": round(pc_ratio_oi, 3) if pc_ratio_oi is not None else None,
        "put_call_ratio_volume": round(pc_ratio_vol, 3) if pc_ratio_vol is not None else None,
        "call_volume_usd_24h": round(call_vol_usd, 0),
        "put_volume_usd_24h": round(put_vol_usd, 0),
        "front_month_expiry": front_month,
        "atm_iv_call_pct": atm_iv_call,
        "atm_iv_put_pct": atm_iv_put,
        "skew_25d_pct": skew_25d,
        "expiry_count": len(by_expiry),
        "instrument_count": len(parsed),
        "top_oi": top_oi,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(market_overview("BTC"), indent=2)[:2500])
