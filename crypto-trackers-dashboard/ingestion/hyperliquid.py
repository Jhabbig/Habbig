"""Hyperliquid perp liquidations + funding.

Hyperliquid is an on-chain perps venue. Their public API is a single
POST endpoint that accepts a `type` discriminator:

  POST https://api.hyperliquid.xyz/info
  body: {"type": "metaAndAssetCtxs"}      -> per-asset ctx incl. funding, OI

For liquidations specifically Hyperliquid does NOT expose a public
liquidation REST endpoint - liquidations come over the websocket
(channel: "trades" with the user-trade subtype). For a REST-only client
we use their public `metaAndAssetCtxs` to get funding + open interest +
mark price across every asset, which complements the Binance/OKX
liquidation aggregator we already have.

This module's responsibilities therefore:
  - per-asset funding + OI + mark price across Hyperliquid universe
  - 24h volume snapshot for cross-venue comparison
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from . import _cache, _health

log = logging.getLogger("ct.hyperliquid")

BASE = "https://api.hyperliquid.xyz"
SOURCE = "api.hyperliquid.xyz"


def _post_info(body: dict, timeout: int = 12) -> Optional[dict]:
    started = time.time()
    try:
        r = requests.post(f"{BASE}/info", json=body, timeout=timeout,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "narve-crypto-trackers/1.0"})
    except requests.RequestException as e:
        log.warning("Hyperliquid POST %s failed: %s", body.get("type"), e)
        _health.record_call(SOURCE, ok=False, latency_s=time.time() - started)
        return None
    latency = time.time() - started
    if r.status_code != 200:
        _health.record_call(SOURCE, ok=False, latency_s=latency,
                            http_status=r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        _health.record_call(SOURCE, ok=False, latency_s=latency, http_status=200)
        return None
    _health.record_call(SOURCE, ok=True, latency_s=latency, http_status=200)
    return data


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def market_state() -> dict:
    """Per-asset Hyperliquid ctx: funding, OI, mark price, 24h vol."""
    hit = _cache.get("hl_market_state", ttl_s=30)
    if hit is not None:
        return hit
    res = _post_info({"type": "metaAndAssetCtxs"})
    if not res or not isinstance(res, list) or len(res) < 2:
        return {"error": "Hyperliquid metaAndAssetCtxs failed", "rows": []}
    meta, ctxs = res[0], res[1]
    universe = (meta or {}).get("universe") or []
    rows = []
    for i, ctx in enumerate(ctxs or []):
        if not isinstance(ctx, dict):
            continue
        coin_meta = universe[i] if i < len(universe) else {}
        name = coin_meta.get("name") if isinstance(coin_meta, dict) else None
        rows.append({
            "coin": name,
            "funding_rate": _f(ctx.get("funding")),
            "mark_price": _f(ctx.get("markPx")),
            "open_interest": _f(ctx.get("openInterest")),
            "day_notional_vol_usd": _f(ctx.get("dayNtlVlm")),
            "premium": _f(ctx.get("premium")),
            "oracle_price": _f(ctx.get("oraclePx")),
            "prev_day_px": _f(ctx.get("prevDayPx")),
        })
    rows.sort(key=lambda r: r.get("day_notional_vol_usd") or 0, reverse=True)
    out = {
        "source": "Hyperliquid /info metaAndAssetCtxs",
        "count": len(rows),
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("hl_market_state", out)
    return out


def funding_rates_only() -> dict:
    """Shape match with the other venue funding feeds (for aggregator)."""
    m = market_state()
    if m.get("error"):
        return m
    return {
        "source": m["source"],
        "tickers": [{
            "symbol": (r["coin"] or "") + "USD",  # synthetic shape match
            "funding_rate": r["funding_rate"],
            "mark_price": r["mark_price"],
            "next_funding_time_ms": None,
        } for r in m["rows"] if r.get("coin") and r.get("funding_rate") is not None],
        "fetched_at": m["fetched_at"],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(market_state(), indent=2)[:2000])
