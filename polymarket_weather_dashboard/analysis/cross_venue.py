"""Cross-venue arbitrage between Polymarket and Kalshi disaster markets.

Joins matched markets across the two venues on (topic, threshold) and
computes the arb spread (Poly YES - Kalshi YES). Spreads > 3 pp flag a
potential venue-arbitrage opportunity; spreads > 6 pp are highlighted as
strong signals.

Topic + threshold matching:

  * topic comes from ``ingestion.kalshi_disasters._topic_hint`` for Kalshi
    rows and from a similar set of keyword tests applied to the Polymarket
    event_title + question text.
  * threshold is an integer extracted via the matcher's existing
    ``_at_least`` regex.

When a Polymarket market has no matching Kalshi counterpart we still emit
the row so the table can show one-venue markets too (``kalshi_yes`` is
None in that case). Same for Kalshi-only markets.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("disasters.crossvenue")

_RE_AT_LEAST_INT = re.compile(
    r"(?:at\s+least|>=|over|more\s+than|exceed[a-z]*)\s*(\d{1,4})\b", re.I)
_RE_FEWER_INT = re.compile(
    r"(?:fewer\s+than|less\s+than|under|below|no\s+more\s+than)\s*(\d{1,4})\b", re.I)


def _topic_of_polymarket(market: dict) -> str:
    title = ((market.get("_event_title") or "") + " " + (market.get("question") or "")).lower()
    if any(k in title for k in ("hurricane", "named storm", "tropical")):
        return "hurricane"
    if "tornado" in title:
        return "tornado"
    if any(k in title for k in ("earthquake", "magnitude")):
        return "earthquake"
    if any(k in title for k in ("wildfire", "wild fire", "forest fire", "bushfire", "acres")):
        return "wildfire"
    if any(k in title for k in ("volcano", "volcanic", "eruption")):
        return "volcano"
    if "tsunami" in title:
        return "tsunami"
    if "flood" in title:
        return "flood"
    if "fema" in title or "disaster declaration" in title:
        return "fema"
    return "other"


def _threshold_of(text: str) -> Optional[int]:
    if not text:
        return None
    m = _RE_AT_LEAST_INT.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    m = _RE_FEWER_INT.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _polymarket_implied(market: dict) -> Optional[float]:
    for key in ("lastTradePrice", "bestBid", "bestAsk"):
        v = market.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return f
    return None


def join_markets(poly_markets: list[dict], kalshi_rows: list[dict]) -> dict:
    """Join Polymarket + Kalshi disaster markets on (topic, threshold).

    Returns a dict with:
      - matched: list of {topic, threshold, poly_yes, kalshi_yes,
                arb_spread_pp, poly_url, kalshi_url, ...}
      - by_topic: counts per topic
      - poly_only_count: how many Poly markets had no Kalshi match
      - kalshi_only_count: how many Kalshi markets had no Poly match
    """
    poly_index: dict[tuple[str, Optional[int]], list[dict]] = {}
    for m in poly_markets:
        topic = _topic_of_polymarket(m)
        text = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        thr = _threshold_of(text)
        key = (topic, thr)
        poly_index.setdefault(key, []).append(m)

    kalshi_index: dict[tuple[str, Optional[int]], list[dict]] = {}
    for k in kalshi_rows:
        key = (k.get("topic", "other"), k.get("threshold"))
        kalshi_index.setdefault(key, []).append(k)

    matched: list[dict] = []
    matched_keys: set = set()
    for key, polys in poly_index.items():
        topic, thr = key
        if thr is None or topic == "other":
            continue
        kalshis = kalshi_index.get(key)
        if not kalshis:
            continue
        matched_keys.add(key)
        # Pick the most-liquid Kalshi row for the bucket
        kalshi = max(kalshis, key=lambda k: k.get("volume_24h") or 0)
        # Pick the highest-quality Poly row (any with a price)
        poly = next((p for p in polys if _polymarket_implied(p) is not None), polys[0])
        poly_yes = _polymarket_implied(poly)
        kalshi_yes = kalshi.get("yes_price")
        arb_pp: Optional[float] = None
        if poly_yes is not None and kalshi_yes is not None:
            arb_pp = round((poly_yes - kalshi_yes) * 100, 1)
        matched.append({
            "topic": topic,
            "threshold": thr,
            "poly_question": poly.get("question"),
            "poly_yes": poly_yes,
            "poly_url": (
                f"https://polymarket.com/event/{poly.get('slug') or poly.get('_event_slug')}"
                if (poly.get("slug") or poly.get("_event_slug")) else None
            ),
            "kalshi_question": kalshi.get("title"),
            "kalshi_yes": kalshi_yes,
            "kalshi_url": kalshi.get("url"),
            "kalshi_volume_24h": kalshi.get("volume_24h"),
            "arb_spread_pp": arb_pp,
        })

    matched.sort(key=lambda r: -abs(r.get("arb_spread_pp") or 0))

    by_topic: dict[str, int] = {}
    for r in matched:
        by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1

    poly_only = sum(1 for k in poly_index if k not in matched_keys
                    and k[0] != "other" and k[1] is not None)
    kalshi_only = sum(1 for k in kalshi_index if k not in matched_keys
                      and k[0] != "other" and k[1] is not None)

    return {
        "matched": matched,
        "matched_count": len(matched),
        "by_topic": by_topic,
        "poly_only_count": poly_only,
        "kalshi_only_count": kalshi_only,
    }


if __name__ == "__main__":
    # Synthetic smoke test
    poly = [
        {"_event_title": "Atlantic 2026", "question": "At least 14 named storms",
         "lastTradePrice": "0.65", "slug": "atlantic-2026"},
        {"_event_title": "Earthquakes 2026", "question": "At least 18 M7+ earthquakes",
         "lastTradePrice": "0.40", "slug": "quakes-2026"},
    ]
    kalshi = [
        {"topic": "hurricane", "threshold": 14, "title": "Atlantic named storms 2026 - at least 14",
         "yes_price": 0.71, "volume_24h": 18500, "url": "https://kalshi.com/events/atl2026"},
        {"topic": "earthquake", "threshold": 18, "title": "M7+ earthquakes 2026 - at least 18",
         "yes_price": 0.32, "volume_24h": 3200, "url": "https://kalshi.com/events/m7-2026"},
    ]
    import json
    print(json.dumps(join_markets(poly, kalshi), indent=2))
