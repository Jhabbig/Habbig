"""Per-position narve.ai signal overlay for the portfolio view.

The aggregator returns raw positions from Polymarket/Kalshi. This module
annotates each position with the matching market's credibility-weighted
consensus so the portfolio UI can show the ✓/✗/↔ agreement column and the
detail panel can surface edge and recommendation.

A position whose market has no predictions yields agreement="no_signal"
with no edge — the UI treats that as neutral.
"""

from __future__ import annotations

from typing import Optional

from .unified_markets import UnifiedMarket


def _narve_yes_probability(market: UnifiedMarket) -> Optional[float]:
    """Reconstruct narve.ai's credibility-weighted YES probability from
    `betyc_ev_score` = betyc_yes_probability - market.yes_price."""
    if market.betyc_ev_score is None:
        return None
    return max(0.0, min(1.0, market.yes_price + market.betyc_ev_score))


def signal_for_position(
    position: dict, market: Optional[UnifiedMarket],
) -> dict:
    """Build a narve.ai signal summary for one position.

    Agreement is decided off the credibility consensus direction, not
    off the raw probability, so markets at ~50% with no strong side are
    correctly reported as 'neutral' even if narve's number happens to
    sit a hair above or below the price.
    """
    side = (position.get("side") or "").lower()
    base = {
        "narve_yes_probability": None,
        "market_yes_price": None,
        "edge_pp": None,            # signed: positive = aligned with user's side
        "agreement": "no_signal",   # agree | disagree | neutral | no_signal
        "consensus": None,
        "prediction_count": 0,
        "avg_credibility": None,
        "recommendation": "no_signal",
    }

    if market is None:
        return base

    base["market_yes_price"] = market.yes_price
    base["prediction_count"] = market.betyc_prediction_count
    base["avg_credibility"] = market.betyc_avg_credibility
    base["consensus"] = market.betyc_consensus

    narve_yes = _narve_yes_probability(market)
    if narve_yes is None:
        return base

    base["narve_yes_probability"] = round(narve_yes, 4)

    user_side = "YES" if side == "yes" else "NO" if side == "no" else None
    if user_side == "YES":
        edge = narve_yes - market.yes_price
    elif user_side == "NO":
        edge = (1 - narve_yes) - (1 - market.yes_price)
    else:
        edge = 0.0
    base["edge_pp"] = round(edge, 4)

    consensus = market.betyc_consensus
    if consensus in ("YES", "NO"):
        if user_side is None:
            base["agreement"] = "neutral"
        elif consensus == user_side:
            base["agreement"] = "agree"
        else:
            base["agreement"] = "disagree"
    elif consensus == "SPLIT":
        base["agreement"] = "neutral"

    # Recommendation string the UI can render verbatim in the detail panel.
    if base["agreement"] == "agree":
        base["recommendation"] = "aligned"
    elif base["agreement"] == "disagree":
        base["recommendation"] = "contrary"
    elif base["agreement"] == "neutral":
        base["recommendation"] = "neutral"
    else:
        base["recommendation"] = "no_signal"

    return base


def enrich_positions(
    positions: list[dict], market_map: dict[str, UnifiedMarket],
) -> list[dict]:
    """Return a new list of positions, each with a `narve_signal` key."""
    out: list[dict] = []
    for p in positions:
        market = market_map.get(p.get("market_id") or "")
        out.append({**p, "narve_signal": signal_for_position(p, market)})
    return out
