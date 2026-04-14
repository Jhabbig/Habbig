#!/usr/bin/env python3
"""
Smart Money Flow — what are the proven traders positioned in RIGHT NOW?

For each wallet in the trader-quality top list, fetch their currently open
positions on Polymarket and aggregate by (market, outcome). Markets where a
lot of high-quality wallets are positioned the same way are the actionable
copy-trade signals.

Data source:
  https://data-api.polymarket.com/positions?user=<wallet>&sizeThreshold=...
  Returns per-position rows with market metadata + size/value.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

import httpx

DATA_API = "https://data-api.polymarket.com"
HTTP_TIMEOUT = 12.0
RATE_PAUSE = 0.06

logger = logging.getLogger(__name__)


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_positions_for_wallet(wallet: str, limit: int = 200) -> list[dict]:
    """Open positions for one wallet."""
    if not wallet:
        return []
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            r = client.get(
                f"{DATA_API}/positions",
                params={"user": wallet, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("position fetch failed for %s: %s", wallet[:10], e)
        return []


def aggregate_smart_money(
    top_traders: list[dict],
    max_wallets: int = 30,
) -> dict:
    """
    Walk top quality traders, fetch their open positions, and aggregate by
    (market, outcome) so we can show consensus positioning.

    `top_traders` is the list returned by trader_quality.top_quality_traders().
    """
    if not top_traders:
        return {"flows": [], "wallets_scanned": 0, "total_positions": 0}

    flows: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "market_id": "",
        "outcome": "",
        "title": "",
        "slug": "",
        "end_date": "",
        "wallets": [],
        "total_position_usd": 0.0,
        "total_size": 0.0,
        "weighted_quality": 0.0,
        "min_avg_price": 1.0,
        "max_avg_price": 0.0,
    })

    scanned = 0
    total_positions = 0
    for trader in top_traders[:max_wallets]:
        addr = trader.get("address") or ""
        if not addr:
            continue
        positions = fetch_positions_for_wallet(addr)
        scanned += 1
        if not positions:
            time.sleep(RATE_PAUSE)
            continue
        for p in positions:
            cid = p.get("conditionId") or p.get("market") or ""
            outcome = p.get("outcome") or ""
            if not cid or not outcome:
                continue
            cur_value = _safe_float(p.get("currentValue"))
            size = _safe_float(p.get("size"))
            if cur_value < 50 and size < 50:
                continue  # ignore dust
            avg_price = _safe_float(p.get("avgPrice"))
            key = (cid, outcome)
            f = flows[key]
            f["market_id"] = cid
            f["outcome"] = outcome
            f["title"] = f["title"] or p.get("title") or p.get("eventSlug") or ""
            f["slug"] = f["slug"] or p.get("slug") or ""
            f["end_date"] = f["end_date"] or p.get("endDate") or ""
            f["total_position_usd"] += cur_value
            f["total_size"] += size
            quality = trader.get("quality_score") or 0
            f["weighted_quality"] += quality
            if avg_price > 0:
                f["min_avg_price"] = min(f["min_avg_price"], avg_price)
                f["max_avg_price"] = max(f["max_avg_price"], avg_price)
            f["wallets"].append({
                "address": addr,
                "pseudonym": trader.get("pseudonym") or "",
                "quality_score": quality,
                "position_usd": round(cur_value, 2),
                "avg_price": round(avg_price, 4),
            })
            total_positions += 1
        time.sleep(RATE_PAUSE)

    out_flows: list[dict] = []
    for (cid, outcome), f in flows.items():
        if len(f["wallets"]) < 2:
            continue  # at least 2 smart wallets agreeing
        f["smart_wallet_count"] = len(f["wallets"])
        f["avg_quality"] = round(f["weighted_quality"] / len(f["wallets"]), 1)
        f["total_position_usd"] = round(f["total_position_usd"], 2)
        f["total_size"] = round(f["total_size"], 2)
        f["min_avg_price"] = round(f["min_avg_price"], 4) if f["min_avg_price"] < 1 else None
        f["max_avg_price"] = round(f["max_avg_price"], 4) if f["max_avg_price"] > 0 else None
        f["wallets"].sort(key=lambda w: w["position_usd"], reverse=True)
        out_flows.append(f)

    out_flows.sort(
        key=lambda f: (f["smart_wallet_count"], f["total_position_usd"]),
        reverse=True,
    )

    return {
        "flows": out_flows[:30],
        "wallets_scanned": scanned,
        "total_positions": total_positions,
        "consensus_markets": len(out_flows),
    }


if __name__ == "__main__":
    fake_traders = [
        {"address": "0xabc", "pseudonym": "Test", "quality_score": 80},
    ]
    print(aggregate_smart_money(fake_traders))
