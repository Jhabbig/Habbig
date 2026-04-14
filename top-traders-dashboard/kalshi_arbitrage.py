#!/usr/bin/env python3
"""
Cross-venue arbitrage finder: Polymarket vs Kalshi.

Strategy:
  1. Fetch top open Kalshi markets (by 24h volume).
  2. Fetch top open Polymarket markets (by 24h volume).
  3. For each Kalshi market, find best title match on Polymarket via Jaccard
     similarity over normalized token sets.
  4. Compute YES-side spread between the two venues.
  5. Sort by abs spread × min(volume) so we surface high-confidence arbs only.

Caveats:
  - Title matching is fuzzy; false positives are possible. We require a Jaccard
    score ≥ 0.55 AND ≥ 4 shared tokens before pairing.
  - Spreads include the Kalshi yes_ask vs Polymarket best yes ask, no fee model.

Default thresholds: Jaccard >= 0.55, shared tokens >= 4, abs spread >= 2¢.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("top-traders.kalshi-arb")

POLY_GAMMA_API = "https://gamma-api.polymarket.com"

_STOP_TOKENS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "by",
    "with", "vs", "vs.", "v", "v.", "will", "be", "is", "are", "as", "this",
    "that", "it", "its", "from", "but", "what", "when", "any", "all", "more",
    "than", "before", "after", "during", "no", "yes", "do", "does", "did",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    raw = _TOKEN_RE.findall(text.lower())
    return {t for t in raw if t not in _STOP_TOKENS and len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ─── Polymarket markets (public gamma-api) ─────────────────────────────

def fetch_polymarket_markets(limit: int = 200) -> list[dict]:
    """Fetch active Polymarket markets sorted by 24h volume."""
    url = f"{POLY_GAMMA_API}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": min(limit, 500),
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        with httpx.Client(timeout=15) as c:
            resp = c.get(url, params=params, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            log.warning("Polymarket gamma /markets returned %d", resp.status_code)
            return []
        data = resp.json()
    except httpx.HTTPError as e:
        log.warning("Polymarket gamma /markets fetch failed: %s", e)
        return []

    if not isinstance(data, list):
        return []

    import json as _json
    out: list[dict] = []
    for m in data:
        try:
            outcomes_raw = m.get("outcomes") or "[]"
            prices_raw = m.get("outcomePrices") or "[]"
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw

            yes_idx = None
            for i, o in enumerate(outcomes):
                if str(o).strip().lower() in ("yes", "true"):
                    yes_idx = i
                    break
            if yes_idx is None:
                yes_idx = 0
            yes_price = float(prices[yes_idx]) if yes_idx < len(prices) else 0.0

            out.append({
                "id": m.get("id") or m.get("conditionId") or "",
                "title": m.get("question") or m.get("title") or "",
                "yes_price": round(yes_price, 4),
                "no_price": round(1.0 - yes_price, 4),
                "volume_24h": float(m.get("volume24hr") or 0),
                "volume": float(m.get("volume") or 0),
                "category": m.get("category") or "",
                "slug": m.get("slug") or "",
            })
        except Exception:
            continue

    out.sort(key=lambda x: x["volume_24h"] or x["volume"], reverse=True)
    return out[:limit]


# ─── Cross-venue matching ───────────────────────────────────────────────

def find_cross_venue_opportunities(
    kalshi_markets: list[dict],
    poly_markets: list[dict],
    min_jaccard: float = 0.55,
    min_shared_tokens: int = 4,
    min_abs_spread: float = 0.02,
    limit: int = 30,
) -> list[dict]:
    """Pair Kalshi markets with Polymarket markets via fuzzy title matching.

    Returns the top opportunities sorted by abs(spread) * sqrt(min(vol)).
    """
    poly_token_index = [(p, _tokens(p["title"])) for p in poly_markets]

    opportunities: list[dict] = []
    for k in kalshi_markets:
        ktoks = _tokens(k.get("title") or "")
        if len(ktoks) < min_shared_tokens:
            continue

        best_match: Optional[dict] = None
        best_score = 0.0
        best_shared = 0
        for p, ptoks in poly_token_index:
            shared = len(ktoks & ptoks)
            if shared < min_shared_tokens:
                continue
            score = _jaccard(ktoks, ptoks)
            if score >= min_jaccard and score > best_score:
                best_score = score
                best_match = p
                best_shared = shared

        if not best_match:
            continue

        kalshi_yes = k.get("yes_price") or 0.0
        poly_yes = best_match.get("yes_price") or 0.0
        spread = round(kalshi_yes - poly_yes, 4)
        abs_spread = abs(spread)
        if abs_spread < min_abs_spread:
            continue

        kalshi_vol = k.get("volume_24h") or k.get("volume") or 0
        poly_vol = best_match.get("volume_24h") or best_match.get("volume") or 0
        # Confidence proxy: smaller side's volume
        confidence_vol = min(kalshi_vol, poly_vol)

        opportunities.append({
            "kalshi_ticker": k.get("ticker"),
            "kalshi_title": k.get("title"),
            "kalshi_yes_price": kalshi_yes,
            "kalshi_no_price": k.get("no_price") or 0.0,
            "kalshi_volume_24h": kalshi_vol,
            "poly_id": best_match.get("id"),
            "poly_title": best_match.get("title"),
            "poly_yes_price": poly_yes,
            "poly_no_price": best_match.get("no_price") or 0.0,
            "poly_volume_24h": poly_vol,
            "poly_slug": best_match.get("slug"),
            "spread": spread,
            "abs_spread": abs_spread,
            "match_score": round(best_score, 3),
            "shared_tokens": best_shared,
            "favored_venue": "kalshi" if spread > 0 else "polymarket",
            "confidence_vol": confidence_vol,
        })

    opportunities.sort(
        key=lambda o: (o["abs_spread"] * (o["confidence_vol"] ** 0.5)),
        reverse=True,
    )
    return opportunities[:limit]


# ─── Top-level entry point used by the dashboard ───────────────────────

def run_cross_venue_scan(kalshi_top_n: int = 80, poly_top_n: int = 200) -> dict:
    """Fetch both venues and return scored arbitrage opportunities."""
    from kalshi_client import fetch_top_markets as _fetch_kalshi

    kalshi_markets = _fetch_kalshi(limit=kalshi_top_n)
    poly_markets = fetch_polymarket_markets(limit=poly_top_n)

    opps = find_cross_venue_opportunities(kalshi_markets, poly_markets)

    return {
        "kalshi_markets_scanned": len(kalshi_markets),
        "polymarket_markets_scanned": len(poly_markets),
        "opportunities": opps,
        "total_opportunities": len(opps),
    }


if __name__ == "__main__":
    import json
    result = run_cross_venue_scan(kalshi_top_n=40, poly_top_n=80)
    print(json.dumps({
        "kalshi_scanned": result["kalshi_markets_scanned"],
        "poly_scanned": result["polymarket_markets_scanned"],
        "opps_found": result["total_opportunities"],
        "top_5": result["opportunities"][:5],
    }, indent=2, default=str))
